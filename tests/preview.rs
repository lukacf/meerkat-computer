//! Cached preview tests use real broker routes with counted, synthetic guest effects.
#[path = "../src/server.rs"]
mod server;

use async_trait::async_trait;
use axum::{
    body::{Body, to_bytes},
    http::Request,
};
use meerkat_computer::{
    backend::{Backend, BackendComputer, BackendError, ComputerSpec},
    *,
};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicU64, AtomicUsize, Ordering},
};
use tempfile::TempDir;
use tower::ServiceExt;

const OPERATOR: &str = "synthetic-preview-operator-token-long-enough";
const PNG: &str =
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j7ioAAAAASUVORK5CYII=";

#[derive(Default)]
struct Guest {
    effects: AtomicUsize,
    mode: AtomicUsize,
    entered: tokio::sync::Notify,
}
#[async_trait]
impl Backend for Guest {
    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        Ok(BackendComputer {
            instance_name: "fixture".into(),
            incus_project: None,
            worker_url: "http://127.0.0.1:1".into(),
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: "fixture-incarnation".into(),
        })
    }
    async fn start(&self, computer: &BackendComputer) -> Result<BackendComputer, BackendError> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        Ok(computer.clone())
    }
    async fn stop(&self, _: &BackendComputer) -> Result<(), BackendError> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    async fn destroy(&self, _: &BackendComputer) -> Result<(), BackendError> {
        unreachable!()
    }
    async fn deliver_secret(
        &self,
        _: &BackendComputer,
        _: &SecureInputDelivery,
    ) -> Result<WorkerCallResponse, BrokerError> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        self.entered.notify_one();
        std::future::pending().await
    }
}
#[async_trait]
impl WorkerClient for Guest {
    async fn install_lease(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
    ) -> Result<(), BrokerError> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    async fn call(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        request: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        self.effects.fetch_add(1, Ordering::SeqCst);
        if self.mode.load(Ordering::SeqCst) == 3 {
            return Err(BrokerError::unknown("synthetic effect unknown"));
        }
        if self.mode.load(Ordering::SeqCst) == 4 {
            self.entered.notify_one();
            return std::future::pending().await;
        }
        if request.name == "computer_act" {
            assert_eq!(request.arguments["observation_id"], "a".repeat(32));
        }
        let mut content = vec![
            json!({"type":"text","text":json!({"observation_id":"a".repeat(32),"surface":"browser","coordinate_space":"viewport","mode":"hybrid","width":1,"height":1}).to_string()}),
        ];
        if self.mode.load(Ordering::SeqCst) == 5 {
            content
                .push(json!({"type":"text","text":json!({"privacy":{"active":true}}).to_string()}));
        }
        match self.mode.load(Ordering::SeqCst) {
            1 => content.push(json!({"type":"image","mimeType":"image/svg+xml","data":"https://untrusted.example/credential"})),
            2 => {},
            _ => content.push(json!({"type":"image","mimeType":"image/png","data":PNG})),
        }
        Ok(WorkerCallResponse {
            content,
            metrics: json!({}),
        })
    }
}
struct TestClock(AtomicU64);
impl Clock for TestClock {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}
struct Fixture {
    directory: TempDir,
    config: Config,
    guest: Arc<Guest>,
    clock: Arc<TestClock>,
    broker: Arc<Broker>,
    attachment: Attachment,
}
async fn fixture() -> Fixture {
    let directory = tempfile::tempdir().unwrap();
    let mut config = Config::load("configs/local.toml").unwrap();
    config.server.state_dir = directory.path().join("state");
    let key = directory.path().join("vault.key");
    meerkat_computer::credentials::CredentialVault::init_key(&key).unwrap();
    config.credentials = Some(CredentialConfig { key_file: key });
    let guest = Arc::new(Guest::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1_000_000)));
    let broker = Arc::new(
        Broker::open_with_worker_and_clock(
            config.clone(),
            guest.clone(),
            guest.clone(),
            clock.clone(),
        )
        .await
        .unwrap(),
    );
    let attachment = broker
        .attach(AttachmentRequest {
            owner: ComputerOwner::Identity {
                realm: "realm".into(),
                namespace: "team".into(),
                identity: "artist".into(),
            },
            slot: "main".into(),
            profile: "browser".into(),
            session_id: "preview-fixture".into(),
            runtime_generation: 1,
            ttl_seconds: 3600,
            takeover: false,
        })
        .await
        .unwrap();
    Fixture {
        directory,
        config,
        guest,
        clock,
        broker,
        attachment,
    }
}
async fn preview(f: &Fixture, token: Option<&str>, suffix: &str) -> (u16, Value) {
    request_preview(
        f,
        token,
        &format!(
            "/v1/computers/{}/preview{suffix}",
            f.attachment.lease.computer_id
        ),
    )
    .await
}
async fn grant_preview(f: &Fixture, token: Option<&str>, suffix: &str) -> (u16, Value) {
    request_preview(f, token, &format!("/v1/preview{suffix}")).await
}
async fn request_preview(f: &Fixture, token: Option<&str>, uri: &str) -> (u16, Value) {
    let mut request = Request::builder().uri(uri);
    if let Some(token) = token {
        request = request.header("authorization", format!("Bearer {token}"));
    }
    let response = server::router(f.broker.clone(), OPERATOR)
        .oneshot(request.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status().as_u16();
    if status == 200 {
        assert_eq!(response.headers()["cache-control"], "no-store");
        assert_eq!(response.headers()["referrer-policy"], "no-referrer");
    }
    let bytes = to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

async fn attach_artist(f: &Fixture, identity: &str, takeover: bool) -> Attachment {
    f.broker
        .attach(AttachmentRequest {
            owner: ComputerOwner::Identity {
                realm: "realm".into(),
                namespace: "team".into(),
                identity: identity.into(),
            },
            slot: "main".into(),
            profile: "browser".into(),
            session_id: "inline-preview".into(),
            runtime_generation: 2,
            ttl_seconds: 3600,
            takeover,
        })
        .await
        .unwrap()
}

#[tokio::test]
async fn grant_preview_requires_current_grant_and_has_no_computer_selector() {
    let f = fixture().await;
    let other = attach_artist(&f, "other-artist", false).await;
    observe(&f, "saved-frame").await.unwrap();
    for token in [None, Some(OPERATOR), Some("not-a-grant")] {
        assert_eq!(grant_preview(&f, token, "").await.0, 401);
    }
    let (status, mine) = grant_preview(&f, Some(&f.attachment.token), "").await;
    assert_eq!(status, 200);
    assert_eq!(mine["computer_id"], f.attachment.lease.computer_id);
    assert_eq!(mine["status"], "available");
    assert_eq!(mine["frame"]["data"], PNG);
    assert_eq!(mine.as_object().unwrap().len(), 4);
    assert!(!mine.to_string().contains(&f.attachment.token));
    assert!(!mine.to_string().contains(&f.attachment.lease.lease_id));
    let (status, theirs) = grant_preview(&f, Some(&other.token), "").await;
    assert_eq!(status, 200);
    assert_eq!(theirs["computer_id"], other.lease.computer_id);
    assert_eq!(theirs["status"], "unavailable");
    assert!(theirs["frame"].is_null());
    for selector in ["computer_id", "owner", "identity", "lease_id"] {
        assert_eq!(
            grant_preview(
                &f,
                Some(&f.attachment.token),
                &format!("?{selector}={}", other.lease.computer_id)
            )
            .await
            .0,
            400
        );
    }
    assert_eq!(
        request_preview(
            &f,
            Some(&f.attachment.token),
            &format!("/v1/computers/{}/preview", other.lease.computer_id)
        )
        .await
        .0,
        401
    );
    assert_eq!(
        grant_preview(
            &f,
            Some(&f.attachment.token),
            &format!("/{}", other.lease.computer_id)
        )
        .await
        .0,
        404
    );
}

#[tokio::test]
async fn grant_preview_reads_saved_receipt_without_guest_state_or_cursor_effects() {
    let f = fixture().await;
    observe(&f, "saved-frame").await.unwrap();
    let path = f.config.server.state_dir.join("state.json");
    let state = std::fs::read(&path).unwrap();
    let effects = f.guest.effects.load(Ordering::SeqCst);
    f.clock.0.store(1_002_500, Ordering::SeqCst);
    for after in ["", "?after=not-the-saved-observation", ""] {
        let (status, view) = grant_preview(&f, Some(&f.attachment.token), after).await;
        assert_eq!(status, 200);
        assert_eq!(view["frame"]["data"], PNG);
        assert_eq!(view["frame"]["received_at_ms"], 1_000_000);
        assert_eq!(view["frame"]["age_ms"], 2500);
    }
    let (_, unchanged) = grant_preview(
        &f,
        Some(&f.attachment.token),
        &format!("?after={}", "a".repeat(32)),
    )
    .await;
    assert_eq!(unchanged["status"], "available");
    assert!(unchanged["frame"]["data"].is_null());
    assert_eq!(
        grant_preview(
            &f,
            Some(&f.attachment.token),
            &format!("?after={}", "x".repeat(129))
        )
        .await
        .0,
        422
    );
    assert_eq!(std::fs::read(path).unwrap(), state);
    assert_eq!(f.guest.effects.load(Ordering::SeqCst), effects);
    f.broker.call(&f.attachment.token, CallRequest { action_id: "act-after-grant-polls".into(), name: "computer_act".into(), arguments: json!({"observation_id":"a".repeat(32),"action":{"type":"click","x":0,"y":0}}) }).await.unwrap();
}

#[tokio::test]
async fn grant_preview_denies_expired_released_and_replaced_grants() {
    let f = fixture().await;
    observe(&f, "saved-frame").await.unwrap();
    f.clock
        .0
        .store(f.attachment.lease.expires_at_ms, Ordering::SeqCst);
    let state = std::fs::read(f.config.server.state_dir.join("state.json")).unwrap();
    let effects = f.guest.effects.load(Ordering::SeqCst);
    let (status, expired) = grant_preview(&f, Some(&f.attachment.token), "").await;
    assert_eq!(status, 401);
    assert_eq!(expired["error"]["code"], "lease_expired");
    assert_eq!(f.guest.effects.load(Ordering::SeqCst), effects);
    assert_eq!(
        std::fs::read(f.config.server.state_dir.join("state.json")).unwrap(),
        state
    );

    let f = fixture().await;
    observe(&f, "saved-frame").await.unwrap();
    let replacement = attach_artist(&f, "artist", true).await;
    assert_eq!(
        replacement.lease.computer_id,
        f.attachment.lease.computer_id
    );
    assert_eq!(
        grant_preview(&f, Some(&f.attachment.token), "").await.0,
        401
    );
    assert_eq!(grant_preview(&f, Some(&replacement.token), "").await.0, 200);
    f.broker.release(&replacement.token).await.unwrap();
    let effects = f.guest.effects.load(Ordering::SeqCst);
    assert_eq!(grant_preview(&f, Some(&replacement.token), "").await.0, 401);
    assert_eq!(f.guest.effects.load(Ordering::SeqCst), effects);
    assert_eq!(preview(&f, Some(OPERATOR), "").await.0, 200);
}

#[tokio::test]
async fn grant_preview_preserves_privacy_and_uncertainty_withholding() {
    let f = fixture().await;
    observe(&f, "safe").await.unwrap();
    for (mode, reason) in [
        (1, "no_safe_capture"),
        (2, "no_safe_capture"),
        (3, "outcome_unknown"),
        (5, "no_safe_capture"),
    ] {
        f.guest.mode.store(mode, Ordering::SeqCst);
        let _ = observe(&f, &format!("withheld-{mode}")).await;
        let (status, view) = grant_preview(&f, Some(&f.attachment.token), "").await;
        assert_eq!(status, 200);
        assert_eq!(view["status"], "withheld");
        assert_eq!(view["reason"], reason);
        assert!(view["frame"].is_null());
        assert!(!view.to_string().contains(PNG));
        observe(&f, "safe").await.unwrap();
        assert_eq!(
            grant_preview(&f, Some(&f.attachment.token), "").await.1["status"],
            "withheld"
        );
    }
}
async fn observe(f: &Fixture, id: &str) -> Result<WorkerCallResponse, BrokerError> {
    f.broker
        .call(
            &f.attachment.token,
            CallRequest {
                action_id: id.into(),
                name: "computer_observe".into(),
                arguments: json!({}),
            },
        )
        .await
}

#[tokio::test]
async fn cached_preview_requires_operator_and_polling_has_no_effects() {
    let f = fixture().await;
    assert_eq!(preview(&f, None, "").await.0, 401);
    assert_eq!(preview(&f, Some(&f.attachment.token), "").await.0, 401);
    assert_eq!(
        preview(&f, Some(OPERATOR), "").await.1["status"],
        "unavailable"
    );
    observe(&f, "first-observation").await.unwrap();
    let state = std::fs::read(f.config.server.state_dir.join("state.json")).unwrap();
    let effects = f.guest.effects.load(Ordering::SeqCst);
    f.clock.0.store(1_004_000, Ordering::SeqCst);
    for _ in 0..3 {
        let (status, view) = preview(&f, Some(OPERATOR), "").await;
        assert_eq!(status, 200);
        assert_eq!(view["status"], "available");
        assert_eq!(view["frame"]["data"], PNG);
        assert_eq!(view["frame"]["received_at_ms"], 1_000_000);
        assert_eq!(view["frame"]["age_ms"], 4000);
        assert_eq!(view["computer"]["phase"], "leased");
        assert!(!view.to_string().contains(&f.attachment.token));
    }
    assert_eq!(f.guest.effects.load(Ordering::SeqCst), effects);
    assert_eq!(
        std::fs::read(f.config.server.state_dir.join("state.json")).unwrap(),
        state
    );
    assert!(!String::from_utf8(state).unwrap().contains(PNG));
    let (_, same) = preview(&f, Some(OPERATOR), &format!("?after={}", "a".repeat(32))).await;
    assert_eq!(same["status"], "available");
    assert!(same["frame"]["data"].is_null());
    f.broker.call(&f.attachment.token, CallRequest { action_id: "act-after-polls".into(), name: "computer_act".into(), arguments: json!({"observation_id":"a".repeat(32),"action":{"type":"click","x":0,"y":0}}) }).await.unwrap();
}

#[tokio::test]
async fn malformed_no_image_and_unknown_receipts_never_restore_older_frames() {
    let f = fixture().await;
    observe(&f, "safe").await.unwrap();
    for mode in [1, 2, 3] {
        f.guest.mode.store(mode, Ordering::SeqCst);
        let _ = observe(&f, &format!("bad-{mode}")).await;
        let (_, view) = preview(&f, Some(OPERATOR), "").await;
        assert_eq!(view["status"], "withheld");
        assert!(view["frame"].is_null());
        assert!(!view.to_string().contains("untrusted.example"));
        let _ = observe(&f, "safe").await.unwrap(); // Receipt replay is not a new capture.
        assert_eq!(
            preview(&f, Some(OPERATOR), "").await.1["status"],
            "withheld"
        );
    }
    f.guest.mode.store(0, Ordering::SeqCst);
    observe(&f, "fresh-safe").await.unwrap();
    assert_eq!(
        preview(&f, Some(OPERATOR), "").await.1["status"],
        "available"
    );
}

#[tokio::test]
async fn pending_action_blanks_preview_and_restart_keeps_unknown_withheld() {
    let mut f = fixture().await;
    observe(&f, "safe").await.unwrap();
    f.guest.mode.store(4, Ordering::SeqCst);
    let broker = f.broker.clone();
    let token = f.attachment.token.clone();
    let pending = tokio::spawn(async move {
        broker
            .call(
                &token,
                CallRequest {
                    action_id: "pending".into(),
                    name: "computer_observe".into(),
                    arguments: json!({}),
                },
            )
            .await
    });
    f.guest.entered.notified().await;
    assert_eq!(
        preview(&f, Some(OPERATOR), "").await.1["status"],
        "withheld"
    );
    let (status, view) = grant_preview(&f, Some(&f.attachment.token), "").await;
    assert_eq!(status, 200);
    assert_eq!(view["reason"], "operation_in_flight");
    assert!(view["frame"].is_null());
    pending.abort();
    let _ = pending.await;
    drop(f.broker);
    f.broker = Arc::new(
        Broker::open_with_worker_and_clock(
            f.config.clone(),
            f.guest.clone(),
            f.guest.clone(),
            f.clock.clone(),
        )
        .await
        .unwrap(),
    );
    let (_, view) = preview(&f, Some(OPERATOR), "").await;
    assert_eq!(view["status"], "withheld");
    assert!(view["frame"].is_null());
    assert_eq!(
        grant_preview(&f, Some(&f.attachment.token), "").await.0,
        401
    );
}

#[tokio::test]
async fn private_input_admission_blanks_frame_before_delivery_and_survives_restart() {
    let mut f = fixture().await;
    observe(&f, "safe").await.unwrap();
    let response = f.broker.call(&f.attachment.token, CallRequest { action_id: "request".into(), name: "credentials_request".into(), arguments: json!({"target":{"origin":"https://example.com","observation_id":"a".repeat(32),"secret_ref":"password","username_ref":null}}) }).await.unwrap();
    let request: Value =
        serde_json::from_str(response.content[0]["text"].as_str().unwrap()).unwrap();
    let request_id = request["id"].as_str().unwrap().to_owned();
    let broker = f.broker.clone();
    let pending = tokio::spawn(async move {
        broker.fulfill_credential_request(&request_id, serde_json::from_value(json!({"values":{"username":null,"secret":"synthetic-input-not-a-real-password"},"save":null})).unwrap()).await
    });
    f.guest.entered.notified().await;
    let (_, view) = preview(&f, Some(OPERATOR), "").await;
    assert_eq!(view["status"], "withheld");
    assert_eq!(view["reason"], "private_input");
    assert!(view["frame"].is_null());
    let (status, view) = grant_preview(&f, Some(&f.attachment.token), "").await;
    assert_eq!(status, 200);
    assert_eq!(view["reason"], "private_input");
    assert!(view["frame"].is_null());
    assert!(
        !view
            .to_string()
            .contains("synthetic-input-not-a-real-password")
    );
    pending.abort();
    let _ = pending.await;
    drop(f.broker);
    f.broker = Arc::new(
        Broker::open_with_worker_and_clock(
            f.config.clone(),
            f.guest.clone(),
            f.guest.clone(),
            f.clock.clone(),
        )
        .await
        .unwrap(),
    );
    assert_eq!(
        preview(&f, Some(OPERATOR), "").await.1["reason"],
        "private_input"
    );
}

#[tokio::test]
async fn persisted_preview_survives_restart_and_legacy_state_has_no_implicit_frame() {
    let mut f = fixture().await;
    observe(&f, "safe").await.unwrap();
    drop(f.broker);
    f.broker = Arc::new(
        Broker::open_with_worker_and_clock(
            f.config.clone(),
            f.guest.clone(),
            f.guest.clone(),
            f.clock.clone(),
        )
        .await
        .unwrap(),
    );
    let (_, view) = preview(&f, Some(OPERATOR), "").await;
    assert_eq!(view["status"], "available");
    assert!(view["computer"]["lease"].is_null());
    assert_eq!(
        grant_preview(&f, Some(&f.attachment.token), "").await.0,
        401
    );
    drop(f.broker);
    let path = f.config.server.state_dir.join("state.json");
    let mut state: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    state["computers"][&f.attachment.lease.computer_id]
        .as_object_mut()
        .unwrap()
        .remove("preview");
    std::fs::write(&path, state.to_string()).unwrap();
    f.broker = Arc::new(
        Broker::open_with_worker_and_clock(
            f.config.clone(),
            f.guest.clone(),
            f.guest.clone(),
            f.clock.clone(),
        )
        .await
        .unwrap(),
    );
    assert_eq!(
        preview(&f, Some(OPERATOR), "").await.1["status"],
        "unavailable"
    );
    assert!(f.directory.path().exists());
}
