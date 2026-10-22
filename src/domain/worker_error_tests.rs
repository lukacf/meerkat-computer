use super::*;
use axum::{Json, Router, http::StatusCode, routing::post};
use std::sync::atomic::{AtomicUsize, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};

#[derive(Clone, Copy)]
enum Disconnect {
    OnReusedConnection,
    AfterRequest,
    DuringResponseBody,
}

struct ClosingWorker {
    url: String,
    connections: Arc<AtomicUsize>,
    actions: Arc<std::sync::Mutex<Vec<String>>>,
    server: tokio::task::JoinHandle<()>,
}

impl Drop for ClosingWorker {
    fn drop(&mut self) {
        self.server.abort();
    }
}

impl ClosingWorker {
    async fn start(disconnect: Disconnect) -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let connections = Arc::new(AtomicUsize::new(0));
        let actions = Arc::new(std::sync::Mutex::new(Vec::new()));
        let observed_connections = Arc::clone(&connections);
        let observed_actions = Arc::clone(&actions);
        let server = tokio::spawn(async move {
            let mut handlers = tokio::task::JoinSet::new();
            loop {
                let (stream, _) = listener.accept().await.unwrap();
                observed_connections.fetch_add(1, Ordering::SeqCst);
                let actions = Arc::clone(&observed_actions);
                handlers.spawn(async move {
                    let mut stream = BufReader::new(stream);
                    let mut requests = 0;
                    loop {
                        let mut length = 0;
                        loop {
                            let mut line = String::new();
                            if stream.read_line(&mut line).await.unwrap() == 0 {
                                return;
                            }
                            if line == "\r\n" {
                                break;
                            }
                            if let Some(value) = line.to_ascii_lowercase().strip_prefix("content-length:") {
                                length = value.trim().parse().unwrap();
                            }
                        }
                        let mut body = vec![0; length];
                        stream.read_exact(&mut body).await.unwrap();
                        let request: Value = serde_json::from_slice(&body).unwrap();
                        actions.lock().unwrap().push(request["action_id"].as_str().unwrap().to_owned());
                        requests += 1;
                        // An idle-close race can consume the next POST before the
                        // client sees EOF. Do not make this test wait for a timer.
                        if matches!(disconnect, Disconnect::AfterRequest)
                            || (requests > 1 && matches!(disconnect, Disconnect::OnReusedConnection))
                        {
                            return;
                        }
                        let body = br#"{"content":[],"metrics":{"delta":{"exec_commands":1}}}"#;
                        let header = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nContent-Type: application/json\r\n\r\n", body.len());
                        stream.get_mut().write_all(header.as_bytes()).await.unwrap();
                        if matches!(disconnect, Disconnect::DuringResponseBody) {
                            stream.get_mut().write_all(&body[..5]).await.unwrap();
                            return;
                        }
                        stream.get_mut().write_all(body).await.unwrap();
                    }
                });
            }
        });
        Self {
            url,
            connections,
            actions,
            server,
        }
    }

    async fn call(
        &self,
        client: &HttpWorkerClient,
        action_id: &str,
    ) -> Result<WorkerCallResponse, BrokerError> {
        let config: Config = toml::from_str(include_str!("../../configs/local.toml")).unwrap();
        let computer = BackendComputer {
            instance_name: "idle-close-test".into(),
            incus_project: None,
            worker_url: self.url.clone(),
            computer_id: "computer".into(),
            ownership_id: "owner".into(),
            instance_id: "instance".into(),
        };
        let lease = WorkerLease {
            lease_id: "lease".into(),
            epoch: 1,
            expires_at_ms: u64::MAX,
            capabilities: vec!["workspace_exec".into()],
            observation: config.profiles["desktop"].observation.clone(),
        };
        let request = CallRequest {
            action_id: action_id.into(),
            name: "workspace_exec".into(),
            arguments: json!({"command": "true", "timeout_seconds": 30}),
        };
        tokio::time::timeout(
            Duration::from_secs(2),
            client.call(&computer, "synthetic-worker-token", &lease, &request),
        )
        .await
        .expect("loopback transport must finish promptly")
    }
}

#[tokio::test]
async fn worker_calls_use_fresh_connections_instead_of_reusing_idle_sockets() {
    let worker = ClosingWorker::start(Disconnect::OnReusedConnection).await;
    let client = HttpWorkerClient::new().unwrap();
    for action_id in ["first", "second"] {
        let result = worker.call(&client, action_id).await;
        assert!(
            result.is_ok(),
            "fresh connection must avoid idle-close race: {result:?}"
        );
    }
    assert_eq!(worker.connections.load(Ordering::SeqCst), 2);
    assert_eq!(*worker.actions.lock().unwrap(), ["first", "second"]);
}

#[tokio::test]
async fn lost_worker_response_never_replays_an_uncertain_post() {
    let worker = ClosingWorker::start(Disconnect::AfterRequest).await;
    let error = worker
        .call(&HttpWorkerClient::new().unwrap(), "effectful")
        .await
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::UnknownEffect);
    assert_eq!(error.effect, Effect::Unknown);
    assert!(error.message.contains("do not replay"));
    assert_eq!(worker.connections.load(Ordering::SeqCst), 1);
    assert_eq!(*worker.actions.lock().unwrap(), ["effectful"]);
}

#[tokio::test]
async fn truncated_worker_response_never_replays_an_uncertain_post() {
    let worker = ClosingWorker::start(Disconnect::DuringResponseBody).await;
    let error = worker
        .call(&HttpWorkerClient::new().unwrap(), "effectful")
        .await
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::UnknownEffect);
    assert_eq!(error.effect, Effect::Unknown);
    assert!(error.message.contains("do not replay"));
    assert_eq!(worker.connections.load(Ordering::SeqCst), 1);
    assert_eq!(*worker.actions.lock().unwrap(), ["effectful"]);
}

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
