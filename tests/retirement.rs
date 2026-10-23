//! Retirement is exact-owner operator authority and never replayed after uncertainty.
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

#[derive(Default)]
struct Guest {
    error: Mutex<Option<BackendError>>,
    computers: Mutex<BTreeMap<String, BackendComputer>>,
    ensures: AtomicUsize,
    starts: AtomicUsize,
    grants: AtomicUsize,
    destroys: AtomicUsize,
    fail_commit: Mutex<Option<std::path::PathBuf>>,
    stall_destroy: std::sync::atomic::AtomicBool,
    entered: tokio::sync::Notify,
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
    async fn destroy(&self, handle: &BackendComputer) -> Result<(), BackendError> {
        self.destroys.fetch_add(1, Ordering::SeqCst);
        if let Some(path) = self.fail_commit.lock().await.take() {
            std::fs::rename(&path, path.with_extension("saved")).unwrap();
            std::fs::create_dir(&path).unwrap();
        }
        if self.stall_destroy.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        if let Some(error) = self.error.lock().await.take() {
            return Err(error);
        }
        self.computers.lock().await.remove(&handle.computer_id);
        Ok(())
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

fn retire_request() -> RetirementRequest {
    let req = request("ephemeral");
    RetirementRequest {
        owner: req.owner,
        slot: req.slot,
        profile: req.profile,
    }
}

#[tokio::test]
async fn retirement_requires_stopped_exact_assignment_and_preserves_terminal_tombstone() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    let broker = open(&dir, &guest).await;
    let binding = broker.attach(request("ephemeral")).await.unwrap();
    let id = &binding.lease.computer_id;
    assert!(broker.retire(id, retire_request()).await.is_err());
    broker.release(&binding.token).await.unwrap();
    assert!(broker.retire(id, retire_request()).await.is_err());
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 0);
    broker.stop(id).await.unwrap();
    for wrong in [
        RetirementRequest {
            owner: request("other").owner,
            ..retire_request()
        },
        RetirementRequest {
            slot: "other".into(),
            ..retire_request()
        },
        RetirementRequest {
            profile: "other".into(),
            ..retire_request()
        },
    ] {
        assert!(broker.retire(id, wrong).await.is_err());
    }
    assert!(broker.retire("wrong-id", retire_request()).await.is_err());
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 0);
    broker.retire(id, retire_request()).await.unwrap();
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
    assert_eq!(broker.list_computers().await[0].phase, "retired");
    assert!(broker.attach(request("ephemeral")).await.is_err());
    assert!(broker.stop(id).await.is_err());
    assert!(broker.reap().await.unwrap().is_empty());
    broker.retire(id, retire_request()).await.unwrap();
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
    assert!(state(&dir)["computers"][id]["grant"].is_null());
    drop(broker);
    let broker = open(&dir, &guest).await;
    assert_eq!(broker.list_computers().await[0].phase, "retired");
    broker.retire(id, retire_request()).await.unwrap();
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn retirement_http_is_operator_only_and_accepts_no_extra_authority_fields() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    let broker = open(&dir, &guest).await;
    let binding = broker.attach(request("ephemeral")).await.unwrap();
    let path = format!("/v1/computers/{}/retire", binding.lease.computer_id);
    let body = serde_json::to_value(retire_request()).unwrap();
    for token in [&binding.token, "wrong-token"] {
        let (status, _) = http(&broker, "POST", &path, token, body.clone()).await;
        assert_eq!(status, 401);
    }
    broker.stop(&binding.lease.computer_id).await.unwrap();
    let mut wrong = body.clone();
    wrong["force"] = json!(true);
    assert_eq!(http(&broker, "POST", &path, OPERATOR, wrong).await.0, 422);
    let (status, value) = http(&broker, "POST", &path, OPERATOR, body).await;
    assert_eq!(status, 200);
    assert_eq!(value, json!({"retired":binding.lease.computer_id}));
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn uncertain_destroy_is_persisted_and_never_retried_or_reattached() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    let broker = open(&dir, &guest).await;
    let binding = broker.attach(request("ephemeral")).await.unwrap();
    let id = binding.lease.computer_id;
    broker.stop(&id).await.unwrap();
    *guest.error.lock().await = Some(BackendError::CommandTimeout {
        operation: "destroy".into(),
    });
    let error = broker.retire(&id, retire_request()).await.unwrap_err();
    assert_eq!(serde_json::to_value(error).unwrap()["effect"], "unknown");
    assert_eq!(broker.list_computers().await[0].phase, "retirement_unknown");
    assert!(broker.retire(&id, retire_request()).await.is_err());
    assert!(broker.attach(request("ephemeral")).await.is_err());
    assert!(broker.stop(&id).await.is_err());
    assert!(broker.reap().await.unwrap().is_empty());
    drop(broker);
    let broker = open(&dir, &guest).await;
    assert_eq!(broker.list_computers().await[0].phase, "retirement_unknown");
    assert!(broker.retire(&id, retire_request()).await.is_err());
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn interrupted_destroy_recovers_to_unknown_without_replaying_effect() {
    let dir = TempDir::new().unwrap();
    let guest = Arc::new(Guest::default());
    let broker = open(&dir, &guest).await;
    let binding = broker.attach(request("ephemeral")).await.unwrap();
    let id = binding.lease.computer_id;
    broker.stop(&id).await.unwrap();
    guest.stall_destroy.store(true, Ordering::SeqCst);
    let copy = broker.clone();
    let target = id.clone();
    let task = tokio::spawn(async move { copy.retire(&target, retire_request()).await });
    guest.entered.notified().await;
    assert_eq!(
        state(&dir)["computers"][&id]["lifecycle"]["phase"],
        "retiring"
    );
    task.abort();
    let _ = task.await;
    drop(broker);
    let broker = open(&dir, &guest).await;
    assert_eq!(broker.list_computers().await[0].phase, "retirement_unknown");
    assert!(broker.retire(&id, retire_request()).await.is_err());
    assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn retirement_persistence_failure_after_dispatch_is_unknown_and_not_replayed() {
    for backend_failed in [false, true] {
        let dir = TempDir::new().unwrap();
        let guest = Arc::new(Guest::default());
        let broker = open(&dir, &guest).await;
        let binding = broker.attach(request("ephemeral")).await.unwrap();
        let id = binding.lease.computer_id;
        broker.stop(&id).await.unwrap();
        let path = config(&dir).server.state_dir.join("state.json");
        *guest.fail_commit.lock().await = Some(path.clone());
        if backend_failed {
            *guest.error.lock().await = Some(BackendError::CommandTimeout {
                operation: "destroy".into(),
            });
        }
        let error = broker.retire(&id, retire_request()).await.unwrap_err();
        assert_eq!(error.effect(), "unknown");
        assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
        assert_eq!(broker.list_computers().await[0].phase, "retiring");
        std::fs::remove_dir(&path).unwrap();
        std::fs::rename(path.with_extension("saved"), &path).unwrap();
        assert!(broker.retire(&id, retire_request()).await.is_err());
        drop(broker);
        let broker = open(&dir, &guest).await;
        assert_eq!(broker.list_computers().await[0].phase, "retirement_unknown");
        assert!(broker.retire(&id, retire_request()).await.is_err());
        assert_eq!(guest.destroys.load(Ordering::SeqCst), 1);
    }
}
