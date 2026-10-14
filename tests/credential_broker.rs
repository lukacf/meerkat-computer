//! Credential boundary tests use fake infrastructure; real browser delivery is in stack.rs.
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
use std::{
    os::unix::fs::PermissionsExt,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
};
use tempfile::TempDir;
use tower::ServiceExt;

#[derive(Default)]
struct FakeBackend {
    deliveries: AtomicUsize,
    unknown: AtomicBool,
    stall: AtomicBool,
    entered: tokio::sync::Notify,
}
#[async_trait]
impl Backend for FakeBackend {
    async fn ensure(&self, s: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        Ok(BackendComputer {
            instance_name: format!("mc-{}", s.computer_id),
            incus_project: None,
            worker_url: "http://127.0.0.1:1".into(),
            computer_id: s.computer_id.clone(),
            ownership_id: s.ownership_id.clone(),
            instance_id: "test-incarnation".into(),
        })
    }
    async fn start(&self, c: &BackendComputer) -> Result<BackendComputer, BackendError> {
        Ok(c.clone())
    }
    async fn stop(&self, _: &BackendComputer) -> Result<(), BackendError> {
        Ok(())
    }
    async fn destroy(&self, _: &BackendComputer) -> Result<(), BackendError> {
        unreachable!()
    }
    async fn deliver_secret(
        &self,
        _: &BackendComputer,
        delivery: &SecureInputDelivery,
    ) -> Result<WorkerCallResponse, BrokerError> {
        self.deliveries.fetch_add(1, Ordering::SeqCst);
        assert!(!format!("{delivery:?}").contains("private-pass"));
        if self.stall.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        if self.unknown.load(Ordering::SeqCst) {
            return Err(BrokerError::unknown("synthetic lost acknowledgement"));
        }
        Ok(WorkerCallResponse {
            content: vec![
                json!({"type":"text","text":json!({"filled":true,"observation_id":"safe-continuation","action_refs":["submit"]}).to_string()}),
            ],
            metrics: json!({"secure_fields_filled":2}),
        })
    }
}
struct FakeWorker;
#[async_trait]
impl WorkerClient for FakeWorker {
    async fn install_lease(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
    ) -> Result<(), BrokerError> {
        Ok(())
    }
    async fn call(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        _: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        panic!("credentials must not use worker HTTP")
    }
}
struct FakeClock(AtomicU64);
impl Clock for FakeClock {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}
struct Fixture {
    dir: TempDir,
    broker: Arc<Broker>,
    backend: Arc<FakeBackend>,
    clock: Arc<FakeClock>,
    attachment: Attachment,
}
async fn fixture() -> Fixture {
    let dir = TempDir::new().unwrap();
    let key = dir.path().join("vault.key");
    std::fs::write(&key, [42u8; 32]).unwrap();
    std::fs::set_permissions(&key, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut config = Config::load("configs/local.toml").unwrap();
    config.server.state_dir = dir.path().join("state");
    config.credentials = Some(CredentialConfig { key_file: key });
    let backend = Arc::new(FakeBackend::default());
    let clock = Arc::new(FakeClock(AtomicU64::new(1_000_000)));
    let broker = Arc::new(
        Broker::open_with_worker_and_clock(
            config,
            backend.clone(),
            Arc::new(FakeWorker),
            clock.clone(),
        )
        .await
        .unwrap(),
    );
    let attachment = broker
        .attach(attach_request("agents", "alice", false))
        .await
        .unwrap();
    Fixture {
        dir,
        broker,
        backend,
        clock,
        attachment,
    }
}
fn attach_request(namespace: &str, identity: &str, takeover: bool) -> AttachmentRequest {
    AttachmentRequest {
        owner: ComputerOwner::Identity {
            realm: "home".into(),
            namespace: namespace.into(),
            identity: identity.into(),
        },
        slot: "main".into(),
        profile: "browser".into(),
        session_id: uuid::Uuid::new_v4().to_string(),
        runtime_generation: 1,
        ttl_seconds: 3600,
        takeover,
    }
}
fn target() -> Value {
    json!({"origin":"https://example.com","observation_id":"obs1","secret_ref":"password1","username_ref":"username1"})
}
fn tool(action_id: &str, name: &str, args: Value) -> CallRequest {
    CallRequest {
        action_id: action_id.into(),
        name: name.into(),
        arguments: args,
    }
}
fn result(value: WorkerCallResponse) -> Value {
    serde_json::from_str(value.content[0]["text"].as_str().unwrap()).unwrap()
}
const OPERATOR: &str = "test-operator-token-at-least-32-characters";
async fn api(
    f: &Fixture,
    method: &str,
    path: &str,
    body: Value,
    token: Option<&str>,
) -> (u16, Value) {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header("content-type", "application/json")
        .header("host", "127.0.0.1:18760");
    if let Some(token) = token {
        request = request.header("authorization", format!("Bearer {token}"));
    }
    let response = server::router(f.broker.clone(), OPERATOR)
        .oneshot(request.body(Body::from(body.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status().as_u16();
    let bytes = to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}
#[tokio::test]
async fn request_is_bound_to_current_grant_and_idempotent() {
    let f = fixture().await;
    let call = tool(
        "request-1",
        "credentials_request",
        json!({"target":target()}),
    );
    let first = result(
        f.broker
            .call(&f.attachment.token, call.clone())
            .await
            .unwrap(),
    );
    let second = result(f.broker.call(&f.attachment.token, call).await.unwrap());
    assert_eq!(first["id"], second["id"]);
    assert_eq!(first["phase"], "pending");
    assert_eq!(first["owner"]["identity"], "alice");
    assert!(
        first["operator_url"]
            .as_str()
            .unwrap()
            .contains("/credentials#")
    );
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 0);
    let other = f
        .broker
        .attach(attach_request("other", "alice", false))
        .await
        .unwrap();
    let error = f
        .broker
        .call(
            &other.token,
            tool(
                "status",
                "credentials_status",
                json!({"request_id":first["id"]}),
            ),
        )
        .await
        .unwrap_err();
    assert_eq!(error.code(), "unauthorized");
}
#[tokio::test]
async fn credentials_management_requires_operator_and_has_no_read_secret_endpoint() {
    let f = fixture().await;
    assert_eq!(
        api(&f, "GET", "/v1/credentials", json!({}), None).await.0,
        401
    );
    assert_eq!(
        api(
            &f,
            "GET",
            "/v1/credentials",
            json!({}),
            Some(&f.attachment.token)
        )
        .await
        .0,
        401
    );
    let (status,saved)=api(&f,"POST","/v1/credentials",json!({"realm":"home","label":"Example","origin":"https://example.com","username":"private-user","secret":"private-password"}),Some(OPERATOR)).await;
    assert_eq!(status, 200);
    assert!(!saved.to_string().contains("private-"));
    let (status, list) = api(&f, "GET", "/v1/credentials", json!({}), Some(OPERATOR)).await;
    assert_eq!(status, 200);
    assert_eq!(list["credentials"].as_array().unwrap().len(), 1);
    assert!(!list.to_string().contains("private-"));
}

async fn saved(f: &Fixture, secret: &str) -> String {
    let (status,value)=api(f,"POST","/v1/credentials",json!({"realm":"home","label":"Login","origin":"https://example.com","username":"private-user","secret":secret}),Some(OPERATOR)).await;
    assert_eq!(status, 200);
    value["id"].as_str().unwrap().into()
}
async fn assignment(f: &Fixture, id: &str, remove: bool) -> u16 {
    api(f,"POST",if remove{"/v1/credentials/unassign"}else{"/v1/credentials/assign"},json!({"credential_ids":[id],"owners":[{"kind":"identity","realm":"home","namespace":"agents","identity":"alice"}]}),Some(OPERATOR)).await.0
}
#[tokio::test]
async fn assigned_login_never_uses_http_and_revoke_prevents_future_delivery() {
    let f = fixture().await;
    let id = saved(&f, "fixture-only-private-password").await;
    let call = tool(
        "use-1",
        "credentials_use",
        json!({"credential_id":id,"target":target()}),
    );
    assert!(
        f.broker
            .call(&f.attachment.token, call.clone())
            .await
            .is_err()
    );
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 0);
    assert_eq!(assignment(&f, &id, false).await, 200);
    let first = f
        .broker
        .call(&f.attachment.token, call.clone())
        .await
        .unwrap();
    let again = f.broker.call(&f.attachment.token, call).await.unwrap();
    assert_eq!(first.content, again.content);
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 1);
    assert_eq!(assignment(&f, &id, true).await, 200);
    assert!(
        f.broker
            .call(
                &f.attachment.token,
                tool(
                    "use-2",
                    "credentials_use",
                    json!({"credential_id":id,"target":target()})
                )
            )
            .await
            .is_err()
    );
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 1);
    let disk = std::fs::read_to_string(f.dir.path().join("state/state.json")).unwrap();
    assert!(!disk.contains("fixture-only-private-password"));
    assert!(!disk.contains("private-user"));
}
#[tokio::test]
async fn manual_input_expiry_and_takeover_deny_delivery() {
    let f = fixture().await;
    let req = result(
        f.broker
            .call(
                &f.attachment.token,
                tool(
                    "request-expiry",
                    "credentials_request",
                    json!({"target":target()}),
                ),
            )
            .await
            .unwrap(),
    );
    f.clock.0.fetch_add(601_000, Ordering::SeqCst);
    let input = json!({"values":{"username":"private-user","secret":"private-pass"}});
    let path = format!(
        "/v1/credential-requests/{}/fulfill",
        req["id"].as_str().unwrap()
    );
    assert_ne!(
        api(&f, "POST", &path, input.clone(), Some(OPERATOR))
            .await
            .0,
        200
    );
    let (_, requests) = api(
        &f,
        "GET",
        "/v1/credential-requests",
        json!({}),
        Some(OPERATOR),
    )
    .await;
    assert_eq!(requests["requests"][0]["phase"], "expired");
    let req = result(
        f.broker
            .call(
                &f.attachment.token,
                tool(
                    "request-takeover",
                    "credentials_request",
                    json!({"target":target()}),
                ),
            )
            .await
            .unwrap(),
    );
    f.broker
        .attach(attach_request("agents", "alice", true))
        .await
        .unwrap();
    let path = format!(
        "/v1/credential-requests/{}/fulfill",
        req["id"].as_str().unwrap()
    );
    assert_ne!(api(&f, "POST", &path, input, Some(OPERATOR)).await.0, 200);
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 0);
}
#[tokio::test]
async fn manual_delivery_is_one_time_and_status_carries_safe_continuation() {
    let f = fixture().await;
    let req = result(
        f.broker
            .call(
                &f.attachment.token,
                tool(
                    "request-once",
                    "credentials_request",
                    json!({"target":target()}),
                ),
            )
            .await
            .unwrap(),
    );
    let path = format!(
        "/v1/credential-requests/{}/fulfill",
        req["id"].as_str().unwrap()
    );
    let input = json!({"values":{"username":"private-user","secret":"private-pass"},"save":{"label":"Saved explicitly"}});
    assert_eq!(
        api(&f, "POST", &path, input.clone(), Some(OPERATOR))
            .await
            .0,
        200
    );
    assert_ne!(api(&f, "POST", &path, input, Some(OPERATOR)).await.0, 200);
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 1);
    let status = result(
        f.broker
            .call(
                &f.attachment.token,
                tool(
                    "status",
                    "credentials_status",
                    json!({"request_id":req["id"]}),
                ),
            )
            .await
            .unwrap(),
    );
    assert_eq!(status["phase"], "delivered");
    assert!(status["receipt"]["content"].is_array());
    let entries = result(
        f.broker
            .call(
                &f.attachment.token,
                tool("list", "credentials_list", json!({})),
            )
            .await
            .unwrap(),
    );
    assert_eq!(entries["credentials"].as_array().unwrap().len(), 1);
    assert!(entries["credentials"][0].get("assignments").is_none());
    assert!(!entries.to_string().contains("private-"));
}
#[tokio::test]
async fn operator_api_does_not_echo_secret_validation_errors_and_rejects_cross_origin() {
    let f = fixture().await;
    let sentinel = "private-must-not-echo";
    let (status,error)=api(&f,"POST","/v1/credentials",json!({"realm":"home","label":"x","origin":"https://example.com","secret":{"bad":sentinel}}),Some(OPERATOR)).await;
    assert_eq!(status, 422);
    assert!(!error.to_string().contains(sentinel));
    let request = Request::builder()
        .method("POST")
        .uri("/v1/credentials")
        .header("host", "127.0.0.1:18760")
        .header("origin", "https://evil.example")
        .header("authorization", format!("Bearer {OPERATOR}"))
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"realm":"home","label":"x","origin":"https://example.com","secret":sentinel})
                .to_string(),
        ))
        .unwrap();
    let response = server::router(f.broker.clone(), OPERATOR)
        .oneshot(request)
        .await
        .unwrap();
    assert_eq!(response.status(), 422);
    assert_eq!(response.headers()["cache-control"], "no-store");
    assert!(
        f.broker
            .call(
                &f.attachment.token,
                tool(
                    "no-raw",
                    "credentials_request",
                    json!({"target":target(),"secret":sentinel})
                )
            )
            .await
            .is_err()
    );
    assert!(
        !std::fs::read_to_string(f.dir.path().join("state/state.json"))
            .unwrap()
            .contains(sentinel)
    );
}

#[tokio::test]
async fn session_owners_can_request_once_but_cannot_use_identity_credentials() {
    let f = fixture().await;
    let mut request = attach_request("unused", "unused", false);
    request.owner = ComputerOwner::Session {
        realm: "home".into(),
        session_id: request.session_id.clone(),
    };
    let attached = f.broker.attach(request).await.unwrap();
    let names = f.broker.tools(&attached.token).await.unwrap();
    assert!(names.iter().any(|t| t["name"] == "credentials_request"));
    assert!(!names.iter().any(|t| t["name"] == "credentials_use"));
    let req = result(
        f.broker
            .call(
                &attached.token,
                tool(
                    "session-request",
                    "credentials_request",
                    json!({"target":target()}),
                ),
            )
            .await
            .unwrap(),
    );
    let path = format!(
        "/v1/credential-requests/{}/fulfill",
        req["id"].as_str().unwrap()
    );
    assert_ne!(api(&f,"POST",&path,json!({"values":{"username":"private-user","secret":"private-pass"},"save":{"label":"No implicit identity"}}),Some(OPERATOR)).await.0,200);
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 0);
    assert_eq!(
        api(
            &f,
            "POST",
            &path,
            json!({"values":{"username":"private-user","secret":"private-pass"}}),
            Some(OPERATOR)
        )
        .await
        .0,
        200
    );
    let (_, saved) = api(&f, "GET", "/v1/credentials", json!({}), Some(OPERATOR)).await;
    assert_eq!(saved["credentials"], json!([]));
}

#[tokio::test]
async fn unknown_delivery_and_recovery_never_replay() {
    let f = fixture().await;
    let id = saved(&f, "private-pass").await;
    assert_eq!(assignment(&f, &id, false).await, 200);
    f.backend.unknown.store(true, Ordering::SeqCst);
    let call = tool(
        "uncertain",
        "credentials_use",
        json!({"credential_id":id,"target":target()}),
    );
    assert_eq!(
        f.broker
            .call(&f.attachment.token, call.clone())
            .await
            .unwrap_err()
            .effect(),
        "unknown"
    );
    assert_eq!(
        f.broker
            .call(&f.attachment.token, call)
            .await
            .unwrap_err()
            .effect(),
        "unknown"
    );
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 1);
    let (_, requests) = api(
        &f,
        "GET",
        "/v1/credential-requests",
        json!({}),
        Some(OPERATOR),
    )
    .await;
    assert_eq!(requests["requests"][0]["phase"], "unknown");
    let fork = meerkat_computer::ForkRequest {
        request_id: uuid::Uuid::new_v4().to_string(),
        source_computer_id: f.attachment.lease.computer_id.clone(),
        source_lease_id: f.attachment.lease.lease_id.clone(),
        source_epoch: f.attachment.lease.epoch,
        targets: vec![meerkat_computer::ForkTarget {
            owner: attach_request("agents", "child", false).owner,
            slot: "main".into(),
        }],
    };
    assert_eq!(f.broker.fork(fork).await.unwrap_err().code(), "conflict");
}

#[tokio::test]
async fn restart_cancels_pending_and_marks_in_flight_unknown() {
    let f = fixture().await;
    let pending = result(
        f.broker
            .call(
                &f.attachment.token,
                tool("pending", "credentials_request", json!({"target":target()})),
            )
            .await
            .unwrap(),
    );
    let active = result(
        f.broker
            .call(
                &f.attachment.token,
                tool("active", "credentials_request", json!({"target":target()})),
            )
            .await
            .unwrap(),
    );
    f.backend.stall.store(true, Ordering::SeqCst);
    let active_id = active["id"].as_str().unwrap().to_owned();
    let b = f.broker.clone();
    let task = tokio::spawn(async move {
        b.fulfill_credential_request(
            &active_id,
            serde_json::from_value(
                json!({"values":{"username":"private-user","secret":"private-pass"}}),
            )
            .unwrap(),
        )
        .await
    });
    f.backend.entered.notified().await;
    task.abort();
    let _ = task.await;
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 1);
    let mut config = Config::load("configs/local.toml").unwrap();
    config.server.state_dir = f.dir.path().join("state");
    config.credentials = Some(CredentialConfig {
        key_file: f.dir.path().join("vault.key"),
    });
    drop(f.broker);
    let reopened = Broker::open_with_worker_and_clock(
        config,
        f.backend.clone(),
        Arc::new(FakeWorker),
        f.clock.clone(),
    )
    .await
    .unwrap();
    let records = reopened.credential_requests().await.unwrap();
    assert_eq!(
        records.iter().find(|r| r["id"] == pending["id"]).unwrap()["phase"],
        "cancelled"
    );
    assert_eq!(
        records.iter().find(|r| r["id"] == active["id"]).unwrap()["phase"],
        "unknown"
    );
    assert_eq!(f.backend.deliveries.load(Ordering::SeqCst), 1);
}

#[test]
fn private_receipt_parser_never_echoes_guest_errors() {
    for body in [
        json!({"error":{"effect":"none","message":"private-password"}}),
        json!({"error":{"effect":"unknown","message":"private-password"}}),
    ] {
        let error =
            meerkat_computer::backend::private_input_receipt(&serde_json::to_vec(&body).unwrap())
                .unwrap_err();
        assert!(!error.to_string().contains("private-password"));
    }
}

#[test]
fn enabled_credentials_reject_nonloopback_broker_listeners() {
    let mut config = Config::load("configs/local.toml").unwrap();
    config.credentials = Some(CredentialConfig {
        key_file: "/private/key".into(),
    });
    for listen in ["0.0.0.0:18760", "192.168.1.2:18760", "[::]:18760"] {
        config.server.listen = listen.into();
        assert!(config.validate().is_err());
    }
}

#[tokio::test]
async fn embedding_host_can_supply_a_store_without_local_key_configuration() {
    let dir = TempDir::new().unwrap();
    let key = dir.path().join("injected.key");
    credentials::CredentialVault::init_key(&key).unwrap();
    let vault = Arc::new(
        credentials::CredentialVault::open(&dir.path().join("external/vault"), &key).unwrap(),
    );
    let mut config = Config::load("configs/local.toml").unwrap();
    config.server.state_dir = dir.path().join("broker");
    assert!(config.credentials.is_none());
    let broker =
        Broker::open_with_credential_store(config, Arc::new(FakeBackend::default()), vault.clone())
            .await
            .unwrap();
    broker
        .save_credential(credentials::CredentialDraft {
            realm: "home".into(),
            label: "Injected".into(),
            origin: "https://example.com".into(),
            username: None,
            secret: "private-value".into(),
        })
        .await
        .unwrap();
    assert_eq!(vault.list().unwrap().len(), 1);
    assert!(!dir.path().join("broker/vault").exists());
}

#[tokio::test]
async fn ordinary_tools_cannot_reuse_a_private_request_action_id() {
    let f = fixture().await;
    f.broker
        .call(
            &f.attachment.token,
            tool("same-id", "credentials_request", json!({"target":target()})),
        )
        .await
        .unwrap();
    let error = f
        .broker
        .call(
            &f.attachment.token,
            tool("same-id", "computer_observe", json!({})),
        )
        .await
        .unwrap_err();
    assert_eq!(error.code(), "conflict");
}
