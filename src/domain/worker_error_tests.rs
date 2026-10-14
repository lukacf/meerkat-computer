use super::*;
use axum::{Json, Router, http::StatusCode, routing::post};
use std::sync::atomic::{AtomicUsize, Ordering};

async fn worker_error(code: &str, effect: &str) -> BrokerError {
    worker_error_with_metrics(code, effect, json!({"delta": {"observations": 1}})).await
}

async fn worker_error_with_metrics(code: &str, effect: &str, metrics: Value) -> BrokerError {
    let calls = Arc::new(AtomicUsize::new(0));
    let observed_calls = Arc::clone(&calls);
    let response = json!({
        "error": {
            "code": code,
            "effect": effect,
            "message": "The desktop changed while collecting this observation."
        },
        "metrics": metrics
    });
    let app = Router::new().route(
        "/v1/call",
        post(move || {
            let response = response.clone();
            observed_calls.fetch_add(1, Ordering::SeqCst);
            async move { (StatusCode::CONFLICT, Json(response)) }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    let config: Config = toml::from_str(include_str!("../../configs/local.toml")).unwrap();
    let computer = BackendComputer {
        instance_name: "error-mapping-test".into(),
        incus_project: None,
        worker_url: format!("http://{address}"),
        computer_id: "computer".into(),
        ownership_id: "owner".into(),
        instance_id: "instance".into(),
    };
    let lease = WorkerLease {
        lease_id: "lease".into(),
        epoch: 1,
        expires_at_ms: u64::MAX,
        capabilities: vec!["desktop".into()],
        observation: config.profiles["desktop"].observation.clone(),
    };
    let request = CallRequest {
        action_id: "action".into(),
        name: "computer_observe".into(),
        arguments: json!({}),
    };
    let result = HttpWorkerClient::new()
        .unwrap()
        .call(&computer, "synthetic-worker-token", &lease, &request)
        .await;
    server.abort();
    assert_eq!(calls.load(Ordering::SeqCst), 1, "never replay an action");
    result.unwrap_err()
}

#[tokio::test]
async fn unstable_observation_is_a_no_effect_conflict() {
    let error = worker_error("observation_unstable", "none").await;
    assert_eq!(error.code, ErrorCode::Conflict);
    assert_eq!(error.effect, Effect::None);
    assert_eq!(
        error.message,
        "The desktop changed while collecting this observation."
    );
    assert_eq!(error.metrics, Some(json!({"delta": {"observations": 1}})));
}

#[tokio::test]
async fn stale_worker_authority_remains_a_no_effect_conflict() {
    for code in ["stale_lease", "stale_observation", "stale_ref"] {
        let error = worker_error(code, "none").await;
        assert_eq!(error.code, ErrorCode::Conflict, "{code}");
        assert_eq!(error.effect, Effect::None);
    }
}

#[tokio::test]
async fn rejection_metrics_survive_both_definitive_and_uncertain_worker_errors() {
    let metrics = json!({
        "action_id": "action",
        "duration_ms": 17,
        "delta": {"ui_input_rejections": 1, "stale_input_focus": 1},
        "totals": {"ui_input_rejections": 3, "stale_input_focus": 2, "clicks": 4}
    });
    for effect in ["none", "unknown"] {
        let error = worker_error_with_metrics("stale_ref", effect, metrics.clone()).await;
        assert_eq!(error.metrics, Some(metrics.clone()));
        assert_eq!(
            error.code,
            if effect == "none" {
                ErrorCode::Conflict
            } else {
                ErrorCode::UnknownEffect
            }
        );
    }
}

#[tokio::test]
async fn unstable_observation_with_uncertain_effect_stays_unknown() {
    let error = worker_error("observation_unstable", "unknown").await;
    assert_eq!(error.code, ErrorCode::UnknownEffect);
    assert_eq!(error.effect, Effect::Unknown);
    assert!(error.message.contains("do not replay"));
}
