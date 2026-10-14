//! Operator diagnostics are historical evidence, never lifecycle or retry authority.
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
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
};
use tempfile::TempDir;
use tokio::sync::Mutex;
use tower::ServiceExt;

const OPERATOR: &str = "synthetic-diagnostic-operator-token-long-enough";
const PRIVATE: &str = "SYNTHETIC_PRIVATE_BACKEND_TEXT";

#[derive(Default)]
struct Guest {
    error: Mutex<Option<BackendError>>,
    computers: Mutex<BTreeMap<String, BackendComputer>>,
    ensures: AtomicUsize,
    starts: AtomicUsize,
    grants: AtomicUsize,
}
#[async_trait]
impl Backend for Guest {
    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        self.ensures.fetch_add(1, Ordering::SeqCst);
        let handle = BackendComputer {
            instance_name: "synthetic-instance".into(),
            incus_project: None,
            worker_url: "http://127.0.0.1:1".into(),
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: "synthetic-incarnation".into(),
        };
        self.computers
            .lock()
            .await
            .insert(spec.computer_id.clone(), handle.clone());
        if let Some(error) = self.error.lock().await.take() {
            return Err(error);
        }
        Ok(handle)
    }
    async fn inspect(&self, spec: &ComputerSpec) -> Result<Option<BackendComputer>, BackendError> {
        Ok(self.computers.lock().await.get(&spec.computer_id).cloned())
    }
    async fn start(&self, handle: &BackendComputer) -> Result<BackendComputer, BackendError> {
        self.starts.fetch_add(1, Ordering::SeqCst);
        if let Some(error) = self.error.lock().await.take() {
            return Err(error);
        }
        Ok(handle.clone())
    }
    async fn stop(&self, _: &BackendComputer) -> Result<(), BackendError> {
        Ok(())
    }
    async fn destroy(&self, _: &BackendComputer) -> Result<(), BackendError> {
        panic!("diagnostics must not destroy computers")
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
        self.grants.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    async fn call(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        _: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        Ok(WorkerCallResponse {
            content: vec![],
            metrics: json!({}),
        })
    }
}
struct TestClock;
impl Clock for TestClock {
    fn now_ms(&self) -> u64 {
        1_000_000
    }
}
fn request(identity: &str) -> AttachmentRequest {
    AttachmentRequest {
        owner: ComputerOwner::Identity {
            realm: "test".into(),
            namespace: "diagnostics".into(),
            identity: identity.into(),
        },
        slot: "main".into(),
        profile: "browser".into(),
        session_id: "synthetic-session".into(),
        runtime_generation: 1,
        ttl_seconds: 300,
        takeover: false,
    }
}
fn config(dir: &TempDir) -> Config {
    let mut config = Config::load("configs/local.toml").unwrap();
    config.server.state_dir = dir.path().join("state");
    config
}
async fn open(dir: &TempDir, guest: &Arc<Guest>) -> Arc<Broker> {
    Arc::new(
        Broker::open_with_worker_and_clock(
            config(dir),
            guest.clone(),
            guest.clone(),
            Arc::new(TestClock),
        )
        .await
        .unwrap(),
    )
}
async fn http(
    broker: &Arc<Broker>,
    method: &str,
    path: &str,
    token: &str,
    body: Value,
) -> (u16, Value) {
    let response = server::router(broker.clone(), OPERATOR)
        .oneshot(
            Request::builder()
                .method(method)
                .uri(path)
                .header("authorization", format!("Bearer {token}"))
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status().as_u16();
    let bytes = to_bytes(response.into_body(), 2 * 1024 * 1024)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}
fn state(dir: &TempDir) -> Value {
    serde_json::from_slice(&std::fs::read(config(dir).server.state_dir.join("state.json")).unwrap())
        .unwrap()
}

#[tokio::test]
async fn backend_diagnostic_survives_stop_restart_and_explicit_reattach_without_replaying() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    *guest.error.lock().await = Some(BackendError::CommandFailed {
        operation: "worker service start".into(),
        message: PRIVATE.into(),
    });
    let broker = open(&dir, &guest).await;
    let (status, failure) = http(
        &broker,
        "POST",
        "/v1/attachments",
        OPERATOR,
        serde_json::to_value(request("failed")).unwrap(),
    )
    .await;
    assert_eq!(status, 502);
    assert_eq!(failure["error"]["effect"], "unknown");
    assert!(!failure.to_string().contains(PRIVATE));
    let expected = json!({"category":"command_failed","operation":"worker_service_start"});
    assert_eq!(failure["error"]["backend_failure"], expected);
    let (status, listed) = http(&broker, "GET", "/v1/computers", OPERATOR, json!(null)).await;
    assert_eq!(status, 200);
    let computer = &listed["computers"][0];
    let id = computer["computer_id"].as_str().unwrap().to_owned();
    assert_eq!(computer["phase"], "faulted");
    assert!(computer["lease"].is_null());
    let historical =
        json!({"epoch":computer["epoch"],"occurred_at_ms":1_000_000,"diagnostic":expected});
    assert_eq!(computer["last_backend_failure"], historical);
    let persisted = state(&dir);
    assert_eq!(
        persisted["computers"][&id]["last_backend_failure"],
        historical
    );
    assert!(persisted["computers"][&id]["grant"].is_null());
    assert!(!persisted.to_string().contains(PRIVATE));
    assert_eq!(guest.ensures.load(Ordering::SeqCst), 1);
    assert_eq!(guest.grants.load(Ordering::SeqCst), 0);

    broker.stop(&id).await.unwrap();
    drop(broker);
    let broker = open(&dir, &guest).await;
    let restored = serde_json::to_value(broker.list_computers().await).unwrap();
    assert_eq!(restored[0]["phase"], "stopped");
    assert_eq!(restored[0]["last_backend_failure"], historical);
    assert_eq!(guest.ensures.load(Ordering::SeqCst), 1);
    assert_eq!(guest.starts.load(Ordering::SeqCst), 0);
    let explicit = broker.attach(request("failed")).await.unwrap();
    assert_eq!(explicit.lease.computer_id, id);
    assert_eq!(guest.starts.load(Ordering::SeqCst), 1);
    assert_eq!(
        serde_json::to_value(broker.list_computers().await).unwrap()[0]["last_backend_failure"],
        historical
    );
    assert!(explicit.lease.epoch > historical["epoch"].as_u64().unwrap());
}

#[tokio::test]
async fn backend_diagnostic_categories_do_not_copy_free_form_details_or_guess_timeout_stage() {
    let cases = [
        (
            BackendError::InvalidConfiguration(PRIVATE.into()),
            "invalid_configuration",
            None,
        ),
        (
            BackendError::CommandFailed {
                operation: PRIVATE.into(),
                message: PRIVATE.into(),
            },
            "command_failed",
            None,
        ),
        (
            BackendError::CommandTimeout {
                operation: "worker credential injection".into(),
            },
            "command_timeout",
            Some("worker_credential_injection"),
        ),
        (
            BackendError::CommandTimeout {
                operation: "systemd manager readiness".into(),
            },
            "command_timeout",
            Some("systemd_manager_readiness"),
        ),
        (
            BackendError::CommandTimeout {
                operation: PRIVATE.into(),
            },
            "command_timeout",
            None,
        ),
        (
            BackendError::OwnershipMismatch {
                instance: PRIVATE.into(),
            },
            "ownership_mismatch",
            None,
        ),
        (
            BackendError::AdmissionDenied {
                reason: PRIVATE.into(),
            },
            "admission_denied",
            None,
        ),
        (
            BackendError::WorkerNotReady {
                instance: PRIVATE.into(),
            },
            "worker_not_ready",
            None,
        ),
        (
            BackendError::InvalidBackendState(PRIVATE.into()),
            "invalid_backend_state",
            None,
        ),
        (BackendError::Io(PRIVATE.into()), "io", None),
    ];
    for (error, category, operation) in cases {
        let dir = TempDir::new().unwrap();
        let guest = Arc::new(Guest::default());
        *guest.error.lock().await = Some(error);
        let broker = open(&dir, &guest).await;
        let error = broker.attach(request("failed")).await.unwrap_err();
        assert_eq!(error.effect(), "unknown");
        let encoded = serde_json::to_value(error).unwrap();
        assert!(!encoded.to_string().contains(PRIVATE));
        let mut expected = json!({"category":category});
        if let Some(operation) = operation {
            expected["operation"] = json!(operation);
        }
        assert_eq!(encoded["backend_failure"], expected);
        assert_eq!(guest.ensures.load(Ordering::SeqCst), 1);
    }
}

#[tokio::test]
async fn backend_diagnostics_require_operator_and_failed_takeover_invalidates_old_grant() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    let broker = open(&dir, &guest).await;
    let attached = broker.attach(request("existing")).await.unwrap();
    *guest.error.lock().await = Some(BackendError::CommandTimeout {
        operation: "start".into(),
    });
    let mut takeover = request("existing");
    takeover.takeover = true;
    let error = broker.attach(takeover).await.unwrap_err();
    assert_eq!(error.effect(), "unknown");
    let persisted = state(&dir);
    let record = &persisted["computers"][&attached.lease.computer_id];
    assert!(record["grant"].is_null());
    assert_eq!(
        record["last_backend_failure"]["diagnostic"]["category"],
        "command_timeout"
    );
    for token in ["invalid", attached.token.as_str()] {
        let (status, body) = http(&broker, "GET", "/v1/computers", token, json!(null)).await;
        assert_eq!(status, 401);
        assert!(!body.to_string().contains("backend_failure"));
    }
    let other = broker.attach(request("other")).await.unwrap();
    assert_eq!(
        http(&broker, "GET", "/v1/computers", &other.token, json!(null))
            .await
            .0,
        401
    );
    let (status, body) = http(&broker, "GET", "/v1/computers", OPERATOR, json!(null)).await;
    assert_eq!(status, 200);
    assert!(body.to_string().contains("command_timeout"));
    assert_eq!(
        broker.tools(&attached.token).await.unwrap_err().code(),
        "unauthorized"
    );
    assert_eq!(guest.starts.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn backend_diagnostics_accept_state_written_before_the_optional_history_field() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    let broker = open(&dir, &guest).await;
    broker.attach(request("legacy")).await.unwrap();
    drop(broker);
    let mut legacy = state(&dir);
    for record in legacy["computers"].as_object_mut().unwrap().values_mut() {
        record
            .as_object_mut()
            .unwrap()
            .remove("last_backend_failure");
    }
    std::fs::write(
        config(&dir).server.state_dir.join("state.json"),
        legacy.to_string(),
    )
    .unwrap();
    let broker = open(&dir, &guest).await;
    let restored = serde_json::to_value(broker.list_computers().await).unwrap();
    assert_eq!(restored[0]["phase"], "faulted");
    assert!(restored[0].get("last_backend_failure").is_none());
    assert_eq!(guest.ensures.load(Ordering::SeqCst), 1);
}
