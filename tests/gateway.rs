use async_trait::async_trait;
use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode},
    response::IntoResponse,
    routing::{get, post},
};
use meerkat_computer::{
    Clock,
    gateway::{Gateway, GatewayConfig, HostPower},
};
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::sync::Notify;
use tower::ServiceExt;

struct Time(AtomicU64);
impl Clock for Time {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}
#[derive(Default)]
struct Power {
    wakes: AtomicUsize,
    sleeps: AtomicUsize,
    fail: AtomicBool,
    unavailable: AtomicBool,
    events: Mutex<Vec<&'static str>>,
}
#[async_trait]
impl HostPower for Power {
    async fn ready(&self) -> bool {
        self.events.lock().unwrap().push("ready");
        !self.unavailable.load(Ordering::SeqCst)
    }
    async fn wake(&self) -> Result<(), String> {
        self.events.lock().unwrap().push("wake");
        self.wakes.fetch_add(1, Ordering::SeqCst);
        if self.fail.load(Ordering::SeqCst) {
            Err("private diagnostic must not escape".into())
        } else {
            self.unavailable.store(false, Ordering::SeqCst);
            Ok(())
        }
    }
    async fn suspend(&self) -> Result<(), String> {
        self.sleeps.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
const TOKEN: &str = "trusted-host-test-token-at-least-32-bytes";
const WAIT_BOUND: Duration = Duration::from_secs(5);

#[tokio::test]
async fn head_health_and_credential_fulfillment_do_not_create_false_uncertainty() {
    let app = Router::new()
        .route("/health", get(|| async { axum::Json(serde_json::json!({"service":"meerkat-computer","protocol_version":1})) }))
        .route("/v1/credential-requests/r1/fulfill", post(|| async { axum::Json(serde_json::json!({"content":[{"type":"text","text":"Input delivered"}],"metrics":{}})) }));
    let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
    assert_eq!(
        g.router()
            .oneshot(request("/host/wake", true))
            .await
            .unwrap()
            .status(),
        200
    );
    let mut head = request("/health", true);
    *head.method_mut() = axum::http::Method::HEAD;
    assert_eq!(g.router().oneshot(head).await.unwrap().status(), 200);
    assert_eq!(host_status(&g).await["inhibited"], false);
    assert_eq!(
        g.router()
            .oneshot(request("/v1/credential-requests/r1/fulfill", true))
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(host_status(&g).await["inhibited"], false);
    clock.0.store(11001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    server.abort();
}

fn request(path: &str, authenticated: bool) -> Request<Body> {
    let mut r = Request::builder()
        .method("POST")
        .uri(path)
        .header("authorization", "Bearer guest-control-token")
        .header("content-type", "application/json");
    if authenticated {
        r = r.header("x-meerkat-computer-host-token", TOKEN);
    }
    r.body(Body::from(
        r#"{"action_id":"a1","name":"computer_observe","arguments":{}}"#,
    ))
    .unwrap()
}
async fn fixture() -> (
    Arc<Gateway>,
    Arc<Power>,
    Arc<Time>,
    Arc<AtomicUsize>,
    tempfile::TempDir,
    tokio::task::JoinHandle<()>,
) {
    let calls = Arc::new(AtomicUsize::new(0));
    let seen = calls.clone();
    let app = Router::new()
        .route(
            "/v1/tools/call",
            post(move |headers: axum::http::HeaderMap| {
                seen.fetch_add(1, Ordering::SeqCst);
                assert!(!headers.contains_key("x-meerkat-computer-host-token"));
                assert_eq!(headers["authorization"], "Bearer guest-control-token");
                async { axum::Json(serde_json::json!({"content":[], "metrics":{}})) }
            }),
        )
        .route(
            "/v1/lease/renew",
            post(|| async { axum::Json(serde_json::json!({"lease":{"computer_id":"c1","lease_id":"l1","epoch":1,"expires_at_ms":100000,"session_id":"s1","runtime_generation":1,"capabilities":[]}})) }),
        )
        .route(
            "/v1/preview",
            get(|| async { axum::Json(serde_json::json!({"preview":null})) }),
        );
    let (gateway, power, clock, dir, server, _) = fixture_with_app(app).await;
    (gateway, power, clock, calls, dir, server)
}

type AppFixture = (
    Arc<Gateway>,
    Arc<Power>,
    Arc<Time>,
    tempfile::TempDir,
    tokio::task::JoinHandle<()>,
    GatewayConfig,
);

async fn fixture_with_app(app: Router) -> AppFixture {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    let dir = tempfile::tempdir().unwrap();
    let config = GatewayConfig::for_test(format!("http://{address}"), dir.path().to_path_buf(), 10);
    let power = Arc::new(Power::default());
    let clock = Arc::new(Time(AtomicU64::new(1000)));
    let gateway = Gateway::open(config.clone(), TOKEN, power.clone(), clock.clone()).unwrap();
    (gateway, power, clock, dir, server, config)
}

#[tokio::test]
async fn authenticates_host_before_any_wake_and_keeps_grants_at_the_broker() {
    let (g, p, _, calls, _dir, server) = fixture().await;
    let response = g
        .router()
        .oneshot(request("/v1/tools/call", false))
        .await
        .unwrap();
    assert_eq!(response.status(), 401);
    assert_eq!(p.wakes.load(Ordering::SeqCst), 0);
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(
        g.router()
            .oneshot(request("/v1/tools/call", true))
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn use_wakes_once_then_idle_suspend_and_use_wake_again() {
    let (g, p, clock, calls, _dir, server) = fixture().await;
    for _ in 0..2 {
        assert_eq!(
            g.router()
                .oneshot(request("/v1/tools/call", true))
                .await
                .unwrap()
                .status(),
            200
        );
    }
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
    clock.0.store(11001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    assert_eq!(
        g.router()
            .oneshot(request("/v1/tools/call", true))
            .await
            .unwrap()
            .status(),
        200
    );
    assert_eq!(p.wakes.load(Ordering::SeqCst), 2);
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    server.abort();
}

#[tokio::test]
async fn renewal_does_not_wake_or_extend_the_idle_clock() {
    let (g, p, clock, _, _dir, server) = fixture().await;
    assert_eq!(
        g.router()
            .oneshot(request("/v1/lease/renew", true))
            .await
            .unwrap()
            .status(),
        503
    );
    assert_eq!(p.wakes.load(Ordering::SeqCst), 0);
    g.router()
        .oneshot(request("/v1/tools/call", true))
        .await
        .unwrap();
    clock.0.store(9000, Ordering::SeqCst);
    assert_eq!(
        g.router()
            .oneshot(request("/v1/lease/renew", true))
            .await
            .unwrap()
            .status(),
        200
    );
    clock.0.store(11001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    server.abort();
}

#[tokio::test]
async fn failed_wake_has_no_tool_effect_and_is_never_dispatched() {
    let (g, p, _, calls, _dir, server) = fixture().await;
    p.fail.store(true, Ordering::SeqCst);
    let response = g
        .router()
        .oneshot(request("/v1/tools/call", true))
        .await
        .unwrap();
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["error"]["effect"], "none");
    assert_eq!(body["error"]["code"], "host_unavailable");
    assert!(!String::from_utf8_lossy(&bytes).contains("private diagnostic"));
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    server.abort();
}

#[tokio::test]
async fn lost_forwarding_result_inhibits_sleep_without_replay() {
    let (g, p, clock, _, _dir, server) = fixture().await;
    server.abort();
    let _ = server.await;
    let response = g
        .router()
        .oneshot(request("/v1/tools/call", true))
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["error"]["effect"], "unknown");
    clock.0.store(20000, Ordering::SeqCst);
    assert!(!g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
}

fn read_request(path: &str) -> Request<Body> {
    Request::builder()
        .method("GET")
        .uri(path)
        .header("authorization", "Bearer guest-control-token")
        .header("x-meerkat-computer-host-token", TOKEN)
        .body(Body::empty())
        .unwrap()
}

fn action_request(action_id: &str) -> Request<Body> {
    let mut request = request("/v1/tools/call", true);
    *request.body_mut() = Body::from(
        serde_json::json!({
            "action_id": action_id, "name": "computer_observe", "arguments": {}
        })
        .to_string(),
    );
    request
}

async fn host_status(gateway: &Arc<Gateway>) -> serde_json::Value {
    let response = gateway
        .router()
        .oneshot(read_request("/host/status"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    serde_json::from_slice::<serde_json::Value>(&bytes).unwrap()["host"].clone()
}

fn response_app(status: StatusCode, body: serde_json::Value, calls: Arc<AtomicUsize>) -> Router {
    Router::new().route(
        "/v1/tools/call",
        post(move || {
            let body = body.clone();
            calls.fetch_add(1, Ordering::SeqCst);
            async move { (status, axum::Json(body)).into_response() }
        }),
    )
}

#[derive(Default)]
struct CallGate {
    started: Notify,
    release: Notify,
    returned: Notify,
}

async fn notified(signal: &Notify, description: &str) {
    tokio::time::timeout(WAIT_BOUND, signal.notified())
        .await
        .expect(description);
}

fn held_app(first: Arc<CallGate>, second: Arc<CallGate>, calls: Arc<AtomicUsize>) -> Router {
    Router::new().route(
        "/v1/tools/call",
        post(move |axum::Json(body): axum::Json<serde_json::Value>| {
            let gate = match body["action_id"].as_str() {
                Some("first") => first.clone(),
                Some("second") => second.clone(),
                other => panic!("unexpected test action ID: {other:?}"),
            };
            calls.fetch_add(1, Ordering::SeqCst);
            async move {
                gate.started.notify_one();
                gate.release.notified().await;
                gate.returned.notify_one();
                axum::Json(serde_json::json!({"content":[], "metrics":{}}))
            }
        }),
    )
}

async fn assert_completed(task: tokio::task::JoinHandle<axum::response::Response>) {
    let response = tokio::time::timeout(WAIT_BOUND, task)
        .await
        .expect("gateway did not finish after the broker replied")
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn malformed_success_receipt_inhibits_idle_without_replaying_the_action() {
    for (case, body) in [
        ("empty object", serde_json::json!({})),
        (
            "null error",
            serde_json::json!({"content": [], "error": null}),
        ),
        ("null content block", serde_json::json!({"content": [null]})),
        (
            "missing text",
            serde_json::json!({"content": [{"type": "text"}]}),
        ),
        (
            "missing image MIME type",
            serde_json::json!({"content": [{"type": "image", "data": "AAAA"}]}),
        ),
        (
            "unsupported audio block",
            serde_json::json!({"content": [{"type": "audio", "data": "AAAA", "mimeType": "audio/wav"}]}),
        ),
    ] {
        let calls = Arc::new(AtomicUsize::new(0));
        let app = response_app(StatusCode::OK, body, calls.clone());
        let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
        let _response = g
            .router()
            .oneshot(action_request("malformed"))
            .await
            .unwrap();
        assert_eq!(
            host_status(&g).await["inhibited"],
            true,
            "{case} cannot establish a valid SDK tool receipt"
        );
        clock.0.store(20000, Ordering::SeqCst);
        assert!(!g.reap().await.unwrap());
        assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        server.abort();
    }
}

#[tokio::test]
async fn non_success_without_explicit_no_effect_inhibits_idle() {
    for (status, body) in [
        (StatusCode::BAD_REQUEST, serde_json::json!({})),
        (
            StatusCode::INTERNAL_SERVER_ERROR,
            serde_json::json!({"message":"internal failure"}),
        ),
        (
            StatusCode::SERVICE_UNAVAILABLE,
            serde_json::json!({"content":[], "metrics":{}}),
        ),
    ] {
        let calls = Arc::new(AtomicUsize::new(0));
        let app = response_app(status, body, calls.clone());
        let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
        let _response = g
            .router()
            .oneshot(action_request("unconfirmed"))
            .await
            .unwrap();
        assert_eq!(
            host_status(&g).await["inhibited"],
            true,
            "HTTP {status} without effect:none cannot prove quiescence"
        );
        clock.0.store(20000, Ordering::SeqCst);
        assert!(!g.reap().await.unwrap());
        assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        server.abort();
    }
}

#[tokio::test]
async fn confirmed_no_effect_rejection_allows_idle_suspend() {
    let calls = Arc::new(AtomicUsize::new(0));
    let app = response_app(
        StatusCode::UNPROCESSABLE_ENTITY,
        serde_json::json!({
            "error":{"code":"invalid_input", "effect":"none"}, "metrics":{}
        }),
        calls.clone(),
    );
    let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
    let response = g
        .router()
        .oneshot(action_request("rejected"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(host_status(&g).await["inhibited"], false);
    clock.0.store(20000, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn concurrent_calls_share_one_wake_and_each_hold_prevents_suspend() {
    let first = Arc::new(CallGate::default());
    let second = Arc::new(CallGate::default());
    let calls = Arc::new(AtomicUsize::new(0));
    let app = held_app(first.clone(), second.clone(), calls.clone());
    let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
    let a = g.clone();
    let one =
        tokio::spawn(async move { a.router().oneshot(action_request("first")).await.unwrap() });
    let b = g.clone();
    let two =
        tokio::spawn(async move { b.router().oneshot(action_request("second")).await.unwrap() });
    notified(&first.started, "first action was not dispatched").await;
    notified(&second.started, "second action was not dispatched").await;
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    assert_eq!(host_status(&g).await["active"], 2);
    clock.0.store(20000, Ordering::SeqCst);
    assert!(!g.reap().await.unwrap());
    first.release.notify_one();
    assert_completed(one).await;
    assert_eq!(host_status(&g).await["active"], 1);
    assert!(!g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
    second.release.notify_one();
    assert_completed(two).await;
    let status = host_status(&g).await;
    assert_eq!(status["active"], 0);
    assert_eq!(status["last_activity_ms"], 20000);
    assert!(
        !g.reap().await.unwrap(),
        "completion starts a fresh idle interval"
    );
    clock.0.store(30001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn cancelling_the_incoming_request_does_not_cancel_dispatch_or_release_its_hold() {
    let first = Arc::new(CallGate::default());
    let calls = Arc::new(AtomicUsize::new(0));
    let app = held_app(first.clone(), Arc::new(CallGate::default()), calls.clone());
    let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
    let incoming = g.clone();
    let task = tokio::spawn(async move {
        incoming
            .router()
            .oneshot(action_request("first"))
            .await
            .unwrap()
    });
    notified(&first.started, "broker did not receive the action").await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    clock.0.store(20000, Ordering::SeqCst);
    assert_eq!(host_status(&g).await["active"], 1);
    assert!(!g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
    first.release.notify_one();
    notified(
        &first.returned,
        "cancellation prevented the broker from finishing",
    )
    .await;
    // The detached forward task has no caller to join. Observe its public completion
    // after the broker reply, without estimating network or persistence timing.
    let status = tokio::time::timeout(WAIT_BOUND, async {
        loop {
            let status = host_status(&g).await;
            if status["active"] == 0 {
                break status;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("detached forwarding did not finish its hold");
    assert_eq!(status["inhibited"], false);
    assert_eq!(status["last_activity_ms"], 20000);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    clock.0.store(30001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn restart_preserves_uncertainty_even_after_a_later_confirmed_action() {
    let uncertain = Arc::new(AtomicBool::new(true));
    let mode = uncertain.clone();
    let calls = Arc::new(AtomicUsize::new(0));
    let seen = calls.clone();
    let app = Router::new().route(
        "/v1/tools/call",
        post(move || {
            seen.fetch_add(1, Ordering::SeqCst);
            let body = if mode.load(Ordering::SeqCst) {
                serde_json::json!({"error":{"code":"unknown_effect", "effect":"unknown"}})
            } else {
                serde_json::json!({"content":[], "metrics":{}})
            };
            async move { axum::Json(body) }
        }),
    );
    let (g, p, clock, _dir, server, config) = fixture_with_app(app).await;
    g.router()
        .oneshot(action_request("uncertain"))
        .await
        .unwrap();
    assert_eq!(host_status(&g).await["inhibited"], true);
    drop(g);
    let reopened = Gateway::open(config, TOKEN, p.clone(), clock.clone()).unwrap();
    assert_eq!(host_status(&reopened).await["inhibited"], true);
    assert_eq!(
        calls.load(Ordering::SeqCst),
        1,
        "opening state must not replay an action"
    );
    uncertain.store(false, Ordering::SeqCst);
    assert_eq!(
        reopened
            .router()
            .oneshot(action_request("fresh-action"))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(host_status(&reopened).await["inhibited"], true);
    clock.0.store(20000, Ordering::SeqCst);
    assert!(!reopened.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    server.abort();
}

#[tokio::test]
async fn renewal_and_preview_polling_preserve_the_original_idle_deadline() {
    let (g, p, clock, calls, _dir, server) = fixture().await;
    assert_eq!(
        g.router()
            .oneshot(read_request("/v1/preview"))
            .await
            .unwrap()
            .status(),
        StatusCode::SERVICE_UNAVAILABLE
    );
    assert_eq!(p.wakes.load(Ordering::SeqCst), 0);
    g.router()
        .oneshot(action_request("activity"))
        .await
        .unwrap();
    clock.0.store(9000, Ordering::SeqCst);
    assert_eq!(
        g.router()
            .oneshot(request("/v1/lease/renew", true))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    for now in [10000, 11001] {
        clock.0.store(now, Ordering::SeqCst);
        assert_eq!(
            g.router()
                .oneshot(read_request("/v1/preview"))
                .await
                .unwrap()
                .status(),
            StatusCode::OK
        );
    }
    assert_eq!(host_status(&g).await["last_activity_ms"], 1000);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn a_dead_tunnel_is_checked_and_recovered_once_before_new_dispatch() {
    let (g, p, _clock, calls, _dir, server) = fixture().await;
    g.router()
        .oneshot(action_request("before-tunnel-loss"))
        .await
        .unwrap();
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
    p.unavailable.store(true, Ordering::SeqCst);
    assert_eq!(
        g.router()
            .oneshot(action_request("recover-tunnel"))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(*p.events.lock().unwrap(), vec!["wake", "ready", "wake"]);
    assert_eq!(p.wakes.load(Ordering::SeqCst), 2);
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    assert_eq!(
        g.router()
            .oneshot(action_request("after-recovery"))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(p.wakes.load(Ordering::SeqCst), 2);
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
    server.abort();
}

#[tokio::test]
async fn failed_wake_keeps_suspend_inhibited_until_explicit_operator_reconciliation() {
    let (g, p, clock, calls, _dir, server) = fixture().await;
    p.fail.store(true, Ordering::SeqCst);
    let failed = g
        .router()
        .oneshot(action_request("wake-failed"))
        .await
        .unwrap();
    assert_eq!(failed.status(), StatusCode::SERVICE_UNAVAILABLE);
    let bytes = axum::body::to_bytes(failed.into_body(), 4096)
        .await
        .unwrap();
    let receipt: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        receipt["error"]["effect"], "none",
        "guest action was not forwarded"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(
        host_status(&g).await["inhibited"],
        true,
        "failed wake cannot prove that the host stayed stopped"
    );

    p.fail.store(false, Ordering::SeqCst);
    assert_eq!(
        g.router()
            .oneshot(action_request("new-request"))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert_eq!(host_status(&g).await["inhibited"], true);
    clock.0.store(20000, Ordering::SeqCst);
    assert!(!g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);

    let mut reconciliation = request("/host/clear-inhibit", true);
    *reconciliation.body_mut() = Body::from(r#"{"confirmed_no_inflight_effects":true}"#);
    assert_eq!(
        g.router().oneshot(reconciliation).await.unwrap().status(),
        StatusCode::OK
    );
    assert_eq!(host_status(&g).await["inhibited"], false);
    assert_eq!(
        g.router()
            .oneshot(action_request("after-reconciliation"))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    clock.0.store(30001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    assert_eq!(
        calls.load(Ordering::SeqCst),
        2,
        "failed guest action must not be replayed"
    );
    server.abort();
}

#[tokio::test]
async fn host_wake_query_is_rejected_before_any_power_effect() {
    let (g, p, _, calls, _dir, server) = fixture().await;
    let response = g
        .router()
        .oneshot(request("/host/wake?x", true))
        .await
        .unwrap();
    assert!(response.status().is_client_error());
    assert_eq!(p.wakes.load(Ordering::SeqCst), 0);
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 0);
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert_eq!(host_status(&g).await["active"], 0);
    server.abort();
}

#[test]
fn gateway_configuration_refuses_to_forward_back_to_its_own_port() {
    let dir = tempfile::tempdir().unwrap();
    for broker_url in ["http://127.0.0.1:18761", "http://localhost:18761"] {
        let config = GatewayConfig::for_test(broker_url.into(), dir.path().to_path_buf(), 10);
        assert!(
            config.validate().is_err(),
            "self-forwarding loopback configuration was accepted"
        );
    }
}

#[tokio::test]
async fn clean_restart_reaps_idle_host_without_new_demand_or_another_wake() {
    let calls = Arc::new(AtomicUsize::new(0));
    let app = response_app(
        StatusCode::OK,
        serde_json::json!({"content": [], "metrics": {}}),
        calls.clone(),
    );
    let (g, p, clock, _dir, server, config) = fixture_with_app(app).await;
    assert_eq!(
        g.router()
            .oneshot(action_request("before-restart"))
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    let status = host_status(&g).await;
    assert_eq!(status["active"], 0);
    assert_eq!(status["inhibited"], false);
    assert_eq!(status["last_activity_ms"], 1000);
    drop(g);

    let reopened = Gateway::open(config, TOKEN, p.clone(), clock.clone()).unwrap();
    clock.0.store(20000, Ordering::SeqCst);
    // No HTTP request after recovery: the periodic reaper must use saved activity.
    assert!(reopened.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    assert!(!reopened.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn valid_text_and_image_receipt_preserves_content_and_allows_idle_suspend() {
    let calls = Arc::new(AtomicUsize::new(0));
    let body = serde_json::json!({
        "content": [
            {"type": "text", "text": "Observed desktop"},
            {"type": "image", "mimeType": "image/png", "data": "iVBORw0KGgo="}
        ],
        "metrics": {"delta": {"observations": 1}}
    });
    let app = response_app(StatusCode::OK, body.clone(), calls.clone());
    let (g, p, clock, _dir, server, _) = fixture_with_app(app).await;
    let response = g
        .router()
        .oneshot(action_request("valid-observation"))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&bytes).unwrap(),
        body
    );
    assert_eq!(host_status(&g).await["inhibited"], false);
    clock.0.store(11001, Ordering::SeqCst);
    assert!(g.reap().await.unwrap());
    assert_eq!(p.sleeps.load(Ordering::SeqCst), 1);
    assert_eq!(p.wakes.load(Ordering::SeqCst), 1);
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    server.abort();
}

#[tokio::test]
async fn only_file_staging_accepts_large_raw_bodies_and_exports_stay_binary() {
    use axum::{
        body::{Bytes, to_bytes},
        extract::DefaultBodyLimit,
        routing::delete,
    };
    use serde_json::json;
    let computer = uuid::Uuid::new_v4().to_string();
    let file = uuid::Uuid::new_v4().to_string();
    let base = format!("/v1/computers/{computer}/files");
    let item = format!("{base}/{file}");
    let payload = vec![0xff; 3 * 1024 * 1024];
    let expected = payload.clone();
    let metadata = json!({"file_id":file,"filename":"input.bin","direction":"import","size_bytes":payload.len(),"sha256":"a".repeat(64),"created_at_ms":1});
    let deleted = file.clone();
    let app = Router::new()
        .route(
            &base,
            post(move |body: Bytes| {
                let metadata = metadata.clone();
                async move {
                    assert_eq!(body.len(), 3 * 1024 * 1024);
                    axum::Json(metadata)
                }
            })
            .layer(DefaultBodyLimit::max(16 * 1024 * 1024)),
        )
        .route(
            &item,
            get(move || {
                let expected = expected.clone();
                async move {
                    (
                        [
                            ("content-type", "application/octet-stream"),
                            ("content-disposition", "attachment; filename=input.bin"),
                            ("x-content-sha256", "test-digest"),
                        ],
                        expected,
                    )
                }
            })
            .merge(delete(move || {
                let deleted = deleted.clone();
                async move { axum::Json(json!({"deleted":deleted})) }
            })),
        );
    let (g, _, _, _dir, server, _) = fixture_with_app(app).await;
    let upload = Request::builder()
        .method("POST")
        .uri(format!("{base}?filename=input.bin"))
        .header("x-meerkat-computer-host-token", TOKEN)
        .header("authorization", "Bearer operator")
        .header("content-type", "application/octet-stream")
        .body(Body::from(payload.clone()))
        .unwrap();
    assert_eq!(
        g.router().oneshot(upload).await.unwrap().status(),
        StatusCode::OK
    );
    assert_eq!(host_status(&g).await["inhibited"], false);
    let large_other = Request::builder()
        .method("POST")
        .uri("/v1/tools/call")
        .header("x-meerkat-computer-host-token", TOKEN)
        .body(Body::from(payload.clone()))
        .unwrap();
    assert_eq!(
        g.router().oneshot(large_other).await.unwrap().status(),
        StatusCode::PAYLOAD_TOO_LARGE
    );
    let download = Request::builder()
        .uri(&item)
        .header("x-meerkat-computer-host-token", TOKEN)
        .body(Body::empty())
        .unwrap();
    let response = g.router().oneshot(download).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers()["content-type"],
        "application/octet-stream"
    );
    assert_eq!(response.headers()["cache-control"], "no-store");
    assert_eq!(response.headers()["x-content-type-options"], "nosniff");
    assert_eq!(
        to_bytes(response.into_body(), 16 * 1024 * 1024)
            .await
            .unwrap(),
        payload
    );
    let remove = Request::builder()
        .method("DELETE")
        .uri(&item)
        .header("x-meerkat-computer-host-token", TOKEN)
        .body(Body::empty())
        .unwrap();
    assert_eq!(
        g.router().oneshot(remove).await.unwrap().status(),
        StatusCode::OK
    );
    assert_eq!(host_status(&g).await["inhibited"], false);
    server.abort();
}
