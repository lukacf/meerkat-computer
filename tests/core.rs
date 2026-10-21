use async_trait::async_trait;
use meerkat_computer::backend::{
    Backend, BackendCheckpoint, BackendComputer, BackendError, ComputerSpec,
};
use meerkat_computer::{
    AttachmentRequest, Broker, BrokerError, CallRequest, Clock, ComputerKind, ComputerOwner,
    ComputerProfile, Config, ObservationConfig, ObservationMode, ObservationSurface, PoolConfig,
    ServerConfig, WorkerCallResponse, WorkerClient, WorkerLease,
};
use meerkat_computer::{ForkRequest, ForkTarget};
use serde_json::json;
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
};
use tempfile::TempDir;
use tokio::sync::{Mutex, Notify};
#[path = "../src/server.rs"]
mod server;

#[derive(Default)]
struct TestBackend {
    checkpoints: AtomicUsize,
    restores: AtomicUsize,
    releases: AtomicUsize,
    resumes: AtomicUsize,
    fork_unsupported: AtomicBool,
    fork_failure: AtomicBool,
    fork_stall: AtomicBool,
    capability_stall: AtomicBool,
    capability_release: Notify,
    ensures: AtomicUsize,
    starts: AtomicUsize,
    stops: AtomicUsize,
    stall_ensure: AtomicBool,
    stall_stop: AtomicBool,
    stop_error: AtomicBool,
    entered: Notify,
    computers: Mutex<BTreeMap<String, BackendComputer>>,
}
#[async_trait]
impl Backend for TestBackend {
    async fn resume_fork_source(&self, _: &BackendComputer, _: &str) -> Result<(), BackendError> {
        self.resumes.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    async fn supports_full_state_fork(
        &self,
        _: &ComputerSpec,
        _: &BackendComputer,
    ) -> Result<(), BackendError> {
        if self.capability_stall.load(Ordering::SeqCst) {
            self.entered.notify_one();
            self.capability_release.notified().await;
        }
        if self.fork_unsupported.load(Ordering::SeqCst) {
            return Err(BackendError::Unsupported {
                capability: "full_state_fork".into(),
            });
        }
        Ok(())
    }
    async fn checkpoint_create(
        &self,
        source: &BackendComputer,
        id: &str,
    ) -> Result<BackendCheckpoint, BackendError> {
        self.checkpoints.fetch_add(1, Ordering::SeqCst);
        Ok(BackendCheckpoint {
            source: source.clone(),
            checkpoint_id: id.into(),
            snapshot_name: format!("fork-{id}"),
        })
    }
    async fn fork_restore(
        &self,
        checkpoint: &BackendCheckpoint,
        spec: &ComputerSpec,
    ) -> Result<BackendComputer, BackendError> {
        self.restores.fetch_add(1, Ordering::SeqCst);
        let child = BackendComputer {
            instance_name: checkpoint.source.instance_name.clone(),
            incus_project: Some(format!("mcf-{}", spec.computer_id)),
            worker_url: "http://127.0.0.1:18765".into(),
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: format!("incarnation-{}", spec.computer_id),
        };
        self.computers
            .lock()
            .await
            .insert(spec.computer_id.clone(), child.clone());
        if self.fork_stall.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        if self.fork_failure.load(Ordering::SeqCst) {
            return Err(BackendError::Io("lost fork acknowledgement".into()));
        }
        Ok(child)
    }
    async fn inspect_fork(
        &self,
        _: &BackendCheckpoint,
        spec: &ComputerSpec,
    ) -> Result<Option<BackendComputer>, BackendError> {
        self.inspect(spec).await
    }
    async fn checkpoint_release(&self, _: &BackendCheckpoint) -> Result<(), BackendError> {
        self.releases.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        self.ensures.fetch_add(1, Ordering::SeqCst);
        let computer = BackendComputer {
            instance_name: format!("mc-{}", spec.computer_id),
            incus_project: None,
            worker_url: "http://127.0.0.1:18765".into(),
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: "fake-instance".into(),
        };
        self.computers
            .lock()
            .await
            .insert(spec.computer_id.clone(), computer.clone());
        if self.stall_ensure.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        Ok(computer)
    }
    async fn inspect(&self, spec: &ComputerSpec) -> Result<Option<BackendComputer>, BackendError> {
        Ok(self.computers.lock().await.get(&spec.computer_id).cloned())
    }
    async fn start(&self, computer: &BackendComputer) -> Result<BackendComputer, BackendError> {
        self.starts.fetch_add(1, Ordering::SeqCst);
        Ok(computer.clone())
    }
    async fn stop(&self, _: &BackendComputer) -> Result<(), BackendError> {
        self.stops.fetch_add(1, Ordering::SeqCst);
        if self.stall_stop.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        if self.stop_error.load(Ordering::SeqCst) {
            return Err(BackendError::Io("test stop failed".into()));
        }
        Ok(())
    }
    async fn destroy(&self, _: &BackendComputer) -> Result<(), BackendError> {
        panic!("ordinary lifecycle must not destroy computers")
    }
}

#[derive(Default)]
struct TestWorker {
    calls: AtomicUsize,
    unknown: AtomicBool,
    stall: AtomicBool,
    entered: Notify,
    lease_error: AtomicBool,
    lease_stall: AtomicBool,
    leases: Mutex<BTreeMap<String, WorkerLease>>,
}
#[async_trait]
impl WorkerClient for TestWorker {
    async fn install_lease(
        &self,
        computer: &BackendComputer,
        _: &str,
        lease: &WorkerLease,
    ) -> Result<(), BrokerError> {
        if self.lease_error.load(Ordering::SeqCst) {
            return Err(BrokerError::unknown("lease acknowledgement lost"));
        }
        if self.lease_stall.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        let mut leases = self.leases.lock().await;
        if let Some(old) = leases.get(&computer.computer_id) {
            assert!(lease.epoch >= old.epoch);
        }
        leases.insert(computer.computer_id.clone(), lease.clone());
        Ok(())
    }
    async fn call(
        &self,
        computer: &BackendComputer,
        _: &str,
        lease: &WorkerLease,
        _: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        assert_eq!(
            lease.epoch,
            self.leases.lock().await[&computer.computer_id].epoch
        );
        self.calls.fetch_add(1, Ordering::SeqCst);
        if self.stall.load(Ordering::SeqCst) {
            self.entered.notify_one();
            std::future::pending::<()>().await;
        }
        if self.unknown.load(Ordering::SeqCst) {
            let mut error = BrokerError::unknown("lost observation after click");
            error.metrics = Some(json!({"clicks":1,"counts_are_lower_bounds":true}));
            Err(error)
        } else {
            Ok(WorkerCallResponse {
                content: vec![json!({"type":"text","text":"done"})],
                metrics: json!({"commands":1}),
            })
        }
    }
}

struct TestClock(AtomicU64);
impl Clock for TestClock {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}

fn configuration(dir: &TempDir) -> Config {
    Config {
        credentials: None,
        server: ServerConfig {
            listen: "127.0.0.1:0".into(),
            state_dir: dir.path().join("state"),
            operator_token_env: "COMPUTER_OPERATOR_TOKEN".into(),
        },
        pool: PoolConfig {
            backend: "incus".into(),
            incus_binary: "incus".into(),
            incus_project: "meerkat-computer".into(),
            max_running: 8,
            min_available_memory_mib: 256,
            worker_port: 18765,
            ready_timeout_seconds: 10,
        },
        profiles: BTreeMap::from([(
            "browser".into(),
            ComputerProfile {
                image: "lab-image".into(),
                kind: ComputerKind::Container,
                cpus: 1,
                memory_mib: 1024,
                disk_gib: 10,
                guest_admin: false,
                transfers: Default::default(),
                capabilities: vec![
                    "browser".into(),
                    "screenshots".into(),
                    "browser_semantics".into(),
                ],
                observation: ObservationConfig {
                    surface: Default::default(),
                    mode: ObservationMode::Hybrid,
                    width: 640,
                    height: 480,
                    detail: Default::default(),
                    max_elements: 300,
                    max_text_chars: 24_000,
                },
                idle_stop_seconds: 10,
            },
        )]),
    }
}
fn request(namespace: &str, session: &str, generation: u64) -> AttachmentRequest {
    AttachmentRequest {
        owner: ComputerOwner::Identity {
            realm: "home".into(),
            namespace: namespace.into(),
            identity: "researcher".into(),
        },
        slot: "main".into(),
        profile: "browser".into(),
        session_id: session.into(),
        runtime_generation: generation,
        ttl_seconds: 30,
        takeover: false,
    }
}
fn call(id: &str) -> CallRequest {
    CallRequest {
        action_id: id.into(),
        name: "computer_observe".into(),
        arguments: json!({}),
    }
}

fn fork_request(source: &meerkat_computer::Attachment) -> ForkRequest {
    ForkRequest {
        request_id: uuid::Uuid::new_v4().to_string(),
        source_computer_id: source.lease.computer_id.clone(),
        source_lease_id: source.lease.lease_id.clone(),
        source_epoch: source.lease.epoch,
        targets: ["fork-one", "fork-two"]
            .into_iter()
            .map(|name| ForkTarget {
                owner: request(name, name, 0).owner,
                slot: "main".into(),
            })
            .collect(),
    }
}

#[tokio::test]
async fn full_state_fork_uses_one_checkpoint_and_independent_bindings() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, _, _) = broker(&dir).await;
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    let file = broker
        .stage_file(
            &source.lease.computer_id,
            "source-only.txt",
            b"private staging",
        )
        .await
        .unwrap();
    let fork = fork_request(&source);
    let result = broker.fork(fork.clone()).await.unwrap();
    assert_eq!(result.phase, "completed");
    assert_eq!(
        broker.list_files(&source.lease.computer_id).await.unwrap(),
        vec![file]
    );
    for child in &result.children {
        assert!(broker.list_files(child).await.unwrap().is_empty());
    }
    assert_eq!(result.children.len(), 2);
    assert_eq!(backend.checkpoints.load(Ordering::SeqCst), 1);
    assert_eq!(backend.restores.load(Ordering::SeqCst), 2);
    assert_eq!(backend.releases.load(Ordering::SeqCst), 1);
    assert_eq!(
        broker.fork(fork.clone()).await.unwrap().children,
        result.children
    );
    assert_eq!(backend.restores.load(Ordering::SeqCst), 2);
    let child = broker
        .attach(request("fork-one", "child", 0))
        .await
        .unwrap();
    assert_eq!(child.lease.computer_id, result.children[0]);
    assert_ne!(child.token, source.token);
    assert_eq!(
        backend.ensures.load(Ordering::SeqCst),
        1,
        "fork must never provision from disk image"
    );
    broker
        .call(&source.token, call("parent-alive"))
        .await
        .unwrap();
    broker
        .call(&child.token, call("child-alive"))
        .await
        .unwrap();
    let access = broker
        .operator_access(&child.lease.computer_id)
        .await
        .unwrap();
    assert_eq!(
        access.shell_argv[3],
        format!("mcf-{}", child.lease.computer_id)
    );
    let mut changed = fork;
    changed.targets.pop();
    assert_eq!(broker.fork(changed).await.unwrap_err().code(), "conflict");
}

#[tokio::test]
async fn fork_rejects_stale_grants_existing_slots_and_unsupported_backend_before_effects() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, _, _) = broker(&dir).await;
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    let mut stale = fork_request(&source);
    stale.source_epoch += 1;
    assert!(broker.fork(stale).await.is_err());
    let mut duplicate = fork_request(&source);
    duplicate.targets[1] = duplicate.targets[0].clone();
    assert!(broker.fork(duplicate).await.is_err());
    let mut occupied = fork_request(&source);
    occupied.targets[0].owner = request("source", "parent", 0).owner;
    assert!(broker.fork(occupied).await.is_err());
    backend.fork_unsupported.store(true, Ordering::SeqCst);
    let error = broker.fork(fork_request(&source)).await.unwrap_err();
    assert_eq!(error.effect(), "none");
    assert_eq!(backend.checkpoints.load(Ordering::SeqCst), 0);
    assert_eq!(broker.list_computers().await.len(), 1);
}

#[tokio::test]
async fn uncertain_fork_never_replays_or_attaches_a_fresh_computer() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, _, _) = broker(&dir).await;
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    backend.fork_failure.store(true, Ordering::SeqCst);
    let fork = fork_request(&source);
    let result = broker.fork(fork.clone()).await.unwrap();
    assert_eq!(result.phase, "unknown");
    assert_eq!(broker.fork(fork).await.unwrap().phase, "unknown");
    assert_eq!(backend.restores.load(Ordering::SeqCst), 1);
    assert!(
        broker
            .attach(request("fork-one", "child", 0))
            .await
            .is_err()
    );
    broker.stop(&result.children[0]).await.unwrap();
    assert!(
        broker
            .attach(request("fork-one", "child", 0))
            .await
            .is_err()
    );
    assert_eq!(backend.ensures.load(Ordering::SeqCst), 1);
    broker.resume_fork_source(&result.request_id).await.unwrap();
    assert_eq!(backend.resumes.load(Ordering::SeqCst), 1);
    assert_eq!(
        broker.fork_status(&result.request_id).await.unwrap().phase,
        "unknown"
    );
}

#[tokio::test]
async fn fork_restart_marks_pending_unknown_and_does_not_repeat_effects() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let broker = Arc::new(broker);
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    backend.fork_stall.store(true, Ordering::SeqCst);
    let fork = fork_request(&source);
    let task = tokio::spawn({
        let broker = broker.clone();
        let fork = fork.clone();
        async move { broker.fork(fork).await }
    });
    backend.entered.notified().await;
    task.abort();
    let _ = task.await;
    drop(broker);
    let broker =
        Broker::open_with_worker_and_clock(configuration(&dir), backend.clone(), worker, clock)
            .await
            .unwrap();
    assert_eq!(broker.fork(fork.clone()).await.unwrap().phase, "unknown");
    assert_eq!(
        broker.fork_status(&fork.request_id).await.unwrap().phase,
        "unknown"
    );
    assert_eq!(backend.restores.load(Ordering::SeqCst), 1);
    assert_eq!(backend.releases.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn fork_http_is_operator_only_and_returns_no_control_secrets() {
    use axum::{
        body::{Body, to_bytes},
        http::Request,
    };
    use tower::ServiceExt;
    let dir = TempDir::new().unwrap();
    let (broker, _, _, _) = broker(&dir).await;
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    let fork = fork_request(&source);
    let router = server::router(Arc::new(broker), "operator");
    let body = serde_json::to_vec(&fork).unwrap();
    let denied = router
        .clone()
        .oneshot(
            Request::post("/v1/forks")
                .header("content-type", "application/json")
                .header("authorization", format!("Bearer {}", source.token))
                .body(Body::from(body.clone()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(denied.status(), 401);
    let response = router
        .clone()
        .oneshot(
            Request::post("/v1/forks")
                .header("content-type", "application/json")
                .header("authorization", "Bearer operator")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    let value: serde_json::Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 1_000_000).await.unwrap()).unwrap();
    assert_eq!(value["phase"], "completed");
    assert!(!value.to_string().contains(&source.token));
    assert!(value.get("token").is_none());
    let status = router
        .oneshot(
            Request::get(format!("/v1/forks/{}", fork.request_id))
                .header("authorization", "Bearer operator")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(status.status(), 200);
}

#[tokio::test]
async fn fork_rechecks_lease_after_slow_preflight() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, _, clock) = broker(&dir).await;
    let broker = Arc::new(broker);
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    backend.capability_stall.store(true, Ordering::SeqCst);
    let task = tokio::spawn({
        let broker = broker.clone();
        let fork = fork_request(&source);
        async move { broker.fork(fork).await }
    });
    backend.entered.notified().await;
    clock.0.store(source.lease.expires_at_ms, Ordering::SeqCst);
    backend.capability_release.notify_one();
    assert_eq!(task.await.unwrap().unwrap_err().code(), "unauthorized");
    assert_eq!(backend.checkpoints.load(Ordering::SeqCst), 0);
    assert_eq!(broker.list_computers().await.len(), 1);
}

#[tokio::test]
async fn cancelled_direct_fork_can_recover_source_without_broker_restart() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, _, _) = broker(&dir).await;
    let broker = Arc::new(broker);
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    backend.fork_stall.store(true, Ordering::SeqCst);
    let fork = fork_request(&source);
    let task = tokio::spawn({
        let broker = broker.clone();
        let fork = fork.clone();
        async move { broker.fork(fork).await }
    });
    backend.entered.notified().await;
    task.abort();
    let _ = task.await;
    broker.resume_fork_source(&fork.request_id).await.unwrap();
    assert_eq!(broker.fork(fork).await.unwrap().phase, "unknown");
    assert_eq!(backend.restores.load(Ordering::SeqCst), 1);
    assert_eq!(backend.resumes.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn fork_holds_source_barrier_while_other_computers_can_act() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, _) = broker(&dir).await;
    let broker = Arc::new(broker);
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    let other = broker
        .attach(request("unrelated", "unrelated", 0))
        .await
        .unwrap();
    backend.fork_stall.store(true, Ordering::SeqCst);
    let task = tokio::spawn({
        let broker = broker.clone();
        let fork = fork_request(&source);
        async move { broker.fork(fork).await }
    });
    backend.entered.notified().await;
    broker
        .call(&other.token, call("unrelated-action"))
        .await
        .unwrap();
    assert!(
        tokio::time::timeout(
            std::time::Duration::from_millis(30),
            broker.call(&source.token, call("parent-waits"))
        )
        .await
        .is_err()
    );
    assert_eq!(worker.calls.load(Ordering::SeqCst), 1);
    task.abort();
    let _ = task.await;
}

#[tokio::test]
async fn persisted_fork_cannot_point_at_an_unrelated_computer() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    let fork = fork_request(&source);
    broker.fork(fork.clone()).await.unwrap();
    drop(broker);
    let path = dir.path().join("state/state.json");
    let mut state: serde_json::Value =
        serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    state["forks"][&fork.request_id]["children"][0] = source.lease.computer_id.into();
    std::fs::write(&path, serde_json::to_vec(&state).unwrap()).unwrap();
    assert!(
        Broker::open_with_worker_and_clock(configuration(&dir), backend, worker, clock)
            .await
            .is_err()
    );
}
async fn broker(dir: &TempDir) -> (Broker, Arc<TestBackend>, Arc<TestWorker>, Arc<TestClock>) {
    let backend = Arc::new(TestBackend::default());
    let worker = Arc::new(TestWorker::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1_000_000)));
    let broker = Broker::open_with_worker_and_clock(
        configuration(dir),
        backend.clone(),
        worker.clone(),
        clock.clone(),
    )
    .await
    .unwrap();
    (broker, backend, worker, clock)
}

#[tokio::test]
async fn identity_keeps_computer_across_sessions_generations_and_restarts() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let first = broker
        .attach(request("mob-one", "session-one", 0))
        .await
        .unwrap();
    broker.release(&first.token).await.unwrap();
    let second = broker
        .attach(request("mob-one", "session-two", 7))
        .await
        .unwrap();
    assert_eq!(first.lease.computer_id, second.lease.computer_id);
    assert!(second.lease.epoch > first.lease.epoch);
    assert_eq!(backend.ensures.load(Ordering::SeqCst), 1);
    drop(broker);
    let restored =
        Broker::open_with_worker_and_clock(configuration(&dir), backend.clone(), worker, clock)
            .await
            .unwrap();
    assert_eq!(
        restored
            .call(&second.token, call("stale"))
            .await
            .unwrap_err()
            .code(),
        "unauthorized"
    );
    let third = restored
        .attach(request("mob-one", "session-three", 0))
        .await
        .unwrap();
    assert_eq!(third.lease.computer_id, first.lease.computer_id);
    assert!(third.lease.epoch > second.lease.epoch);
    assert_eq!(backend.ensures.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn namespace_and_session_owners_do_not_collide() {
    let dir = TempDir::new().unwrap();
    let (broker, _, _, _) = broker(&dir).await;
    let first = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let second = broker.attach(request("b", "session-one", 0)).await.unwrap();
    let mut session = request("a", "session-one", 0);
    session.owner = ComputerOwner::Session {
        realm: "home".into(),
        session_id: "session-one".into(),
    };
    let third = broker.attach(session.clone()).await.unwrap();
    assert_ne!(first.lease.computer_id, second.lease.computer_id);
    assert_ne!(first.lease.computer_id, third.lease.computer_id);
    session.session_id = "another-session".into();
    assert_eq!(
        broker.attach(session).await.unwrap_err().code(),
        "invalid_input"
    );
}

#[tokio::test]
async fn concurrent_attach_has_one_winner_and_takeover_revokes_the_old_token() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, _) = broker(&dir).await;
    let (one, two) = tokio::join!(
        broker.attach(request("a", "session-one", 0)),
        broker.attach(request("a", "session-two", 0))
    );
    assert_eq!(usize::from(one.is_ok()) + usize::from(two.is_ok()), 1);
    let first = one.or(two).unwrap();
    let mut takeover = request("a", "session-three", 1);
    takeover.takeover = true;
    let next = broker.attach(takeover).await.unwrap();
    assert_eq!(first.lease.computer_id, next.lease.computer_id);
    assert!(next.lease.epoch > first.lease.epoch);
    assert_eq!(
        broker
            .call(&first.token, call("old"))
            .await
            .unwrap_err()
            .code(),
        "unauthorized"
    );
    broker.call(&next.token, call("new")).await.unwrap();
    assert_eq!(worker.calls.load(Ordering::SeqCst), 1);
    assert_eq!(backend.ensures.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn completed_action_is_deduplicated_and_payload_conflicts_are_rejected() {
    let dir = TempDir::new().unwrap();
    let (broker, _, worker, _) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let first = broker
        .call(&attachment.token, call("same-action"))
        .await
        .unwrap();
    let second = broker
        .call(&attachment.token, call("same-action"))
        .await
        .unwrap();
    assert_eq!(first.content, second.content);
    assert_eq!(worker.calls.load(Ordering::SeqCst), 1);
    let changed = CallRequest {
        arguments: json!({"extra":true}),
        ..call("same-action")
    };
    assert_eq!(
        broker
            .call(&attachment.token, changed)
            .await
            .unwrap_err()
            .code(),
        "conflict"
    );
}

#[tokio::test]
async fn unknown_action_cannot_be_replayed_after_rebind_or_restart() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    worker.unknown.store(true, Ordering::SeqCst);
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let first = broker
        .call(&attachment.token, call("uncertain"))
        .await
        .unwrap_err();
    assert_eq!(first.effect(), "unknown");
    let duplicate = broker
        .call(&attachment.token, call("uncertain"))
        .await
        .unwrap_err();
    assert_eq!(first.metrics, duplicate.metrics);
    drop(broker);
    worker.unknown.store(false, Ordering::SeqCst);
    let restored =
        Broker::open_with_worker_and_clock(configuration(&dir), backend, worker.clone(), clock)
            .await
            .unwrap();
    let current = restored
        .attach(request("a", "session-two", 5))
        .await
        .unwrap();
    assert_eq!(
        restored
            .call(&current.token, call("uncertain"))
            .await
            .unwrap_err()
            .effect(),
        "unknown"
    );
    assert_eq!(worker.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn cancellation_after_admission_is_unknown_after_restart() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    worker.stall.store(true, Ordering::SeqCst);
    let broker = Arc::new(broker);
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let task_broker = broker.clone();
    let token = attachment.token.clone();
    let task = tokio::spawn(async move { task_broker.call(&token, call("interrupted")).await });
    worker.entered.notified().await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    drop(broker);
    worker.stall.store(false, Ordering::SeqCst);
    let restored =
        Broker::open_with_worker_and_clock(configuration(&dir), backend, worker.clone(), clock)
            .await
            .unwrap();
    let attachment = restored
        .attach(request("a", "session-two", 1))
        .await
        .unwrap();
    assert_eq!(
        restored
            .call(&attachment.token, call("interrupted"))
            .await
            .unwrap_err()
            .effect(),
        "unknown"
    );
    assert_eq!(worker.calls.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn failed_renewal_revokes_control_instead_of_claiming_success() {
    let dir = TempDir::new().unwrap();
    let (broker, _, worker, _) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    worker.lease_error.store(true, Ordering::SeqCst);
    assert_eq!(
        broker
            .renew(&attachment.token, 60)
            .await
            .unwrap_err()
            .effect(),
        "unknown"
    );
    assert_eq!(
        broker.tools(&attachment.token).await.unwrap_err().code(),
        "unauthorized"
    );
}

#[tokio::test]
async fn operator_stop_recovers_cancelled_provision_release_and_stop() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, _) = broker(&dir).await;
    let broker = Arc::new(broker);
    backend.stall_ensure.store(true, Ordering::SeqCst);
    let task_broker = broker.clone();
    let task =
        tokio::spawn(async move { task_broker.attach(request("a", "session-one", 0)).await });
    backend.entered.notified().await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    let interrupted = broker.list_computers().await.remove(0);
    assert_eq!(interrupted.phase, "preparing");
    broker.stop(&interrupted.computer_id).await.unwrap();
    assert_eq!(backend.stops.load(Ordering::SeqCst), 1);
    assert_eq!(broker.list_computers().await[0].phase, "stopped");

    backend.stall_ensure.store(false, Ordering::SeqCst);
    let attachment = broker.attach(request("a", "session-two", 1)).await.unwrap();
    worker.lease_stall.store(true, Ordering::SeqCst);
    let task_broker = broker.clone();
    let token = attachment.token.clone();
    let task = tokio::spawn(async move { task_broker.release(&token).await });
    worker.entered.notified().await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_eq!(broker.list_computers().await[0].phase, "revoking");
    broker.stop(&interrupted.computer_id).await.unwrap();
    assert_eq!(broker.list_computers().await[0].phase, "stopped");

    worker.lease_stall.store(false, Ordering::SeqCst);
    broker
        .attach(request("a", "session-three", 2))
        .await
        .unwrap();
    backend.stall_stop.store(true, Ordering::SeqCst);
    let task_broker = broker.clone();
    let id = interrupted.computer_id.clone();
    let task = tokio::spawn(async move { task_broker.stop(&id).await });
    backend.entered.notified().await;
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
    assert_eq!(broker.list_computers().await[0].phase, "stopping");
    backend.stall_stop.store(false, Ordering::SeqCst);
    broker.stop(&interrupted.computer_id).await.unwrap();
    assert_eq!(broker.list_computers().await[0].phase, "stopped");
    assert!(broker.list_computers().await[0].epoch > attachment.lease.epoch);
}

#[tokio::test]
async fn operator_access_preserves_control_and_rejects_a_replacement_instance() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, _) = broker(&dir).await;
    let request = request("a", "session-one", 0);
    let owner = request.owner.clone();
    let attachment = broker.attach(request).await.unwrap();
    let id = &attachment.lease.computer_id;
    let before = serde_json::to_value(broker.list_computers().await).unwrap();
    let worker_leases = serde_json::to_value(&*worker.leases.lock().await).unwrap();

    let access = broker.operator_access(id).await.unwrap();
    assert_eq!(access.computer_id, *id);
    assert_eq!(access.owner, owner);
    assert_eq!(access.slot, "main");
    assert_eq!(access.profile, "browser");
    assert!(!access.guest_admin);
    assert_eq!(access.instance_name, format!("mc-{id}"));
    assert_eq!(access.instance_id, "fake-instance");
    assert!(access.control_active);
    assert_eq!(
        access.shell_argv,
        vec![
            "incus".to_owned(),
            "--force-local".to_owned(),
            "--project".to_owned(),
            "meerkat-computer".to_owned(),
            "exec".to_owned(),
            format!("mc-{id}"),
            "--".to_owned(),
            "/bin/bash".to_owned(),
            "-l".to_owned(),
        ]
    );
    assert_eq!(
        serde_json::to_value(broker.list_computers().await).unwrap(),
        before
    );
    assert_eq!(
        serde_json::to_value(&*worker.leases.lock().await).unwrap(),
        worker_leases
    );
    assert_eq!(worker.calls.load(Ordering::SeqCst), 0);
    broker
        .call(&attachment.token, call("still-controlled"))
        .await
        .unwrap();

    backend
        .computers
        .lock()
        .await
        .get_mut(id)
        .unwrap()
        .instance_id = "replacement-instance".into();
    let error = broker.operator_access(id).await.unwrap_err();
    assert_eq!(error.code(), "conflict");
    assert!(error.to_string().contains("replaced"));
    assert_eq!(
        serde_json::to_value(&*worker.leases.lock().await).unwrap(),
        worker_leases
    );
    assert_eq!(backend.ensures.load(Ordering::SeqCst), 1);
    assert_eq!(backend.starts.load(Ordering::SeqCst), 0);
    assert_eq!(backend.stops.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn operator_stop_revokes_broker_control_without_worker_consent() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, _) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let worker_leases = serde_json::to_value(&*worker.leases.lock().await).unwrap();
    worker.lease_error.store(true, Ordering::SeqCst);

    broker.stop(&attachment.lease.computer_id).await.unwrap();
    assert_eq!(backend.stops.load(Ordering::SeqCst), 1);
    let stopped = broker.list_computers().await.remove(0);
    assert_eq!(stopped.phase, "stopped");
    assert!(stopped.epoch > attachment.lease.epoch);
    assert!(stopped.lease.is_none());
    assert_eq!(
        serde_json::to_value(&*worker.leases.lock().await).unwrap(),
        worker_leases
    );
    assert_eq!(
        broker
            .call(&attachment.token, call("denied-after-stop"))
            .await
            .unwrap_err()
            .code(),
        "unauthorized"
    );
    assert_eq!(worker.calls.load(Ordering::SeqCst), 0);
}

#[tokio::test]
async fn expiry_fences_then_idle_reap_stops_without_destroying_storage() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    clock.0.fetch_add(30_001, Ordering::SeqCst);
    assert_eq!(
        broker
            .call(&attachment.token, call("expired"))
            .await
            .unwrap_err()
            .code(),
        "lease_expired"
    );
    assert!(broker.reap().await.unwrap().is_empty());
    let lease = worker.leases.lock().await[&attachment.lease.computer_id].clone();
    assert!(lease.epoch > attachment.lease.epoch);
    assert_eq!(lease.expires_at_ms, 0);
    clock.0.fetch_add(10_001, Ordering::SeqCst);
    assert_eq!(
        broker.reap().await.unwrap(),
        vec![attachment.lease.computer_id.clone()]
    );
    assert_eq!(backend.stops.load(Ordering::SeqCst), 1);
    let resumed = broker
        .attach(request("a", "next-session", 1))
        .await
        .unwrap();
    assert_eq!(resumed.lease.computer_id, attachment.lease.computer_id);
    assert_eq!(backend.ensures.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn idle_reaping_skips_a_computer_with_an_active_command() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let broker = Arc::new(broker);
    let active = broker
        .attach(request("active", "session-one", 0))
        .await
        .unwrap();
    let idle = broker
        .attach(request("idle", "session-two", 0))
        .await
        .unwrap();
    broker.release(&idle.token).await.unwrap();
    worker.stall.store(true, Ordering::SeqCst);
    let task_broker = broker.clone();
    let task =
        tokio::spawn(async move { task_broker.call(&active.token, call("long-command")).await });
    worker.entered.notified().await;
    clock.0.fetch_add(10_001, Ordering::SeqCst);
    let stopped = tokio::time::timeout(std::time::Duration::from_secs(2), broker.reap())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(stopped, vec![idle.lease.computer_id]);
    assert_eq!(backend.stops.load(Ordering::SeqCst), 1);
    task.abort();
    assert!(task.await.unwrap_err().is_cancelled());
}

#[tokio::test]
async fn idle_reaping_attempts_every_available_computer_after_a_failure() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, _, clock) = broker(&dir).await;
    let first = broker
        .attach(request("first", "session-one", 0))
        .await
        .unwrap();
    let second = broker
        .attach(request("second", "session-two", 0))
        .await
        .unwrap();
    broker.release(&first.token).await.unwrap();
    broker.release(&second.token).await.unwrap();
    clock.0.fetch_add(10_001, Ordering::SeqCst);
    backend.stop_error.store(true, Ordering::SeqCst);
    let failure = broker.reap().await.unwrap_err();
    assert_eq!(backend.stops.load(Ordering::SeqCst), 2);
    assert!(failure.message.contains("2 computer(s)"));
    assert_eq!(failure.effect(), "unknown");
    assert_eq!(
        serde_json::to_value(failure).unwrap()["backend_failure"],
        json!({"category":"io"})
    );
    for computer in broker.list_computers().await {
        assert_eq!(computer.phase, "faulted");
        assert!(computer.lease.is_none());
        let diagnostic = serde_json::to_value(computer).unwrap();
        assert_eq!(
            diagnostic["last_backend_failure"]["diagnostic"],
            json!({"category":"io"})
        );
    }
}

#[tokio::test]
async fn renew_preserves_epoch_and_stop_revokes_current_grant() {
    let dir = TempDir::new().unwrap();
    let (broker, _, _, clock) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    clock.0.fetch_add(10_000, Ordering::SeqCst);
    let renewed = broker.renew(&attachment.token, 60).await.unwrap();
    assert_eq!(renewed.epoch, attachment.lease.epoch);
    assert!(renewed.expires_at_ms > attachment.lease.expires_at_ms);
    broker.stop(&attachment.lease.computer_id).await.unwrap();
    assert_eq!(
        broker
            .call(&attachment.token, call("stopped"))
            .await
            .unwrap_err()
            .code(),
        "unauthorized"
    );
}

#[tokio::test]
async fn immutable_profile_and_capability_boundary_are_enforced() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let error = broker
        .call(
            &attachment.token,
            CallRequest {
                name: "workspace_exec".into(),
                arguments: json!({"command":"echo hello"}),
                ..call("forbidden")
            },
        )
        .await
        .unwrap_err();
    assert_eq!(error.code(), "capability_denied");
    assert_eq!(worker.calls.load(Ordering::SeqCst), 0);
    drop(broker);
    let mut config = configuration(&dir);
    config.profiles.get_mut("browser").unwrap().cpus = 2;
    let changed = Broker::open_with_worker_and_clock(config, backend, worker, clock)
        .await
        .unwrap();
    assert_eq!(
        changed
            .attach(request("a", "session-two", 0))
            .await
            .unwrap_err()
            .code(),
        "conflict"
    );
}

#[tokio::test]
async fn guest_admin_defaults_false_and_cannot_elevate_an_existing_computer() {
    let dir = TempDir::new().unwrap();
    let mut config = configuration(&dir);
    let mut legacy_profile = serde_json::to_value(&config.profiles["browser"]).unwrap();
    legacy_profile
        .as_object_mut()
        .unwrap()
        .remove("guest_admin");
    let legacy: ComputerProfile = serde_json::from_value(legacy_profile).unwrap();
    assert!(!legacy.guest_admin);

    let (broker, backend, worker, clock) = broker(&dir).await;
    broker.attach(request("a", "session-one", 0)).await.unwrap();
    drop(broker);
    config.profiles.get_mut("browser").unwrap().guest_admin = true;
    let changed = Broker::open_with_worker_and_clock(config, backend, worker, clock)
        .await
        .unwrap();
    assert_eq!(
        changed
            .attach(request("a", "session-two", 0))
            .await
            .unwrap_err()
            .code(),
        "conflict"
    );
}

#[tokio::test]
async fn desktop_tools_follow_surface_capabilities_without_browser_authority() {
    let dir = TempDir::new().unwrap();
    let mut config = configuration(&dir);
    let profile = config.profiles.get_mut("browser").unwrap();
    profile.capabilities = vec![
        "desktop".into(),
        "screenshots".into(),
        "desktop_semantics".into(),
    ];
    profile.observation.surface = ObservationSurface::Desktop;
    let worker = Arc::new(TestWorker::default());
    let broker = Broker::open_with_worker_and_clock(
        config,
        Arc::new(TestBackend::default()),
        worker.clone(),
        Arc::new(TestClock(AtomicU64::new(1_000))),
    )
    .await
    .unwrap();
    let attachment = broker
        .attach(request("desktop", "session-one", 0))
        .await
        .unwrap();
    let tools = broker.tools(&attachment.token).await.unwrap();
    let names: Vec<_> = tools
        .iter()
        .filter_map(|tool| tool["name"].as_str())
        .collect();
    assert!(names.contains(&"computer_observe"));
    assert!(names.contains(&"computer_act"));
    assert!(names.contains(&"computer_apps"));
    assert!(!names.contains(&"browser_navigate"));
    assert!(!names.contains(&"workspace_exec"));
    let error = broker
        .call(
            &attachment.token,
            CallRequest {
                name: "browser_navigate".into(),
                arguments: json!({"url":"https://example.com"}),
                ..call("not-a-browser")
            },
        )
        .await
        .unwrap_err();
    assert_eq!(error.code(), "capability_denied");
    assert_eq!(worker.calls.load(Ordering::SeqCst), 0);
}

#[test]
fn observation_projection_defaults_and_profile_ceilings_are_explicit() {
    let legacy: ObservationConfig = serde_json::from_value(json!({
        "mode":"hybrid", "width":640, "height":480
    }))
    .unwrap();
    let serialized = serde_json::to_value(legacy).unwrap();
    assert_eq!(serialized["detail"], "compact");
    assert_eq!(serialized["max_elements"], 300);
    assert_eq!(serialized["max_text_chars"], 24000);
    let dir = TempDir::new().unwrap();
    for (elements, text, valid) in [
        (1, 256, true),
        (300, 24000, true),
        (0, 6000, false),
        (301, 6000, false),
        (60, 255, false),
        (60, 24001, false),
    ] {
        let mut config = configuration(&dir);
        config.profiles.get_mut("browser").unwrap().observation = serde_json::from_value(json!({
            "mode":"hybrid", "width":640, "height":480, "detail":"full",
            "max_elements":elements, "max_text_chars":text
        }))
        .unwrap();
        assert_eq!(config.validate().is_ok(), valid);
    }
}

#[tokio::test]
async fn observation_projection_tool_exposes_bounded_native_expansion() {
    let dir = TempDir::new().unwrap();
    let (broker, _, _, _) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let tools = broker.tools(&attachment.token).await.unwrap();
    let tool = tools
        .iter()
        .find(|tool| tool["name"] == "computer_observe")
        .unwrap();
    let properties = &tool["inputSchema"]["properties"];
    assert_eq!(properties["detail"]["enum"], json!(["compact", "full"]));
    assert_eq!(properties["max_elements"]["maximum"], 300);
    assert_eq!(properties["max_text_chars"]["maximum"], 24000);
    assert_eq!(properties["scope_ref"]["type"], "string");
    assert_eq!(properties["observation_id"]["type"], "string");
}

#[test]
fn desktop_profile_requires_matching_semantics_and_preserves_browser_defaults() {
    let legacy: ObservationConfig = serde_json::from_value(json!({
        "mode":"hybrid", "width":640, "height":480
    }))
    .unwrap();
    assert_eq!(legacy.surface, ObservationSurface::Browser);
    let dir = TempDir::new().unwrap();
    let mut config = configuration(&dir);
    let profile = config.profiles.get_mut("browser").unwrap();
    profile.capabilities.push("desktop".into());
    profile.observation.surface = ObservationSurface::Desktop;
    assert!(config.validate().is_err());
    config
        .profiles
        .get_mut("browser")
        .unwrap()
        .capabilities
        .push("desktop_semantics".into());
    assert!(config.validate().is_ok());
}

#[tokio::test]
async fn only_hashes_of_control_tokens_are_persisted_and_debug_redacts_them() {
    let dir = TempDir::new().unwrap();
    let (broker, _, _, _) = broker(&dir).await;
    let attachment = broker.attach(request("a", "session-one", 0)).await.unwrap();
    let state = std::fs::read_to_string(dir.path().join("state/state.json")).unwrap();
    assert!(!state.contains(&attachment.token));
    assert!(state.contains("token_hash"));
    assert!(!format!("{attachment:?}").contains(&attachment.token));
    let listings = serde_json::to_string(&broker.list_computers().await).unwrap();
    assert!(!listings.contains("token"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(dir.path().join("state/state.json"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

#[tokio::test]
async fn state_directory_is_exclusive_and_corruption_fails_closed() {
    let dir = TempDir::new().unwrap();
    let (broker, backend, worker, clock) = broker(&dir).await;
    let other = Broker::open_with_worker_and_clock(
        configuration(&dir),
        backend.clone(),
        worker.clone(),
        clock.clone(),
    )
    .await;
    assert!(other.is_err());
    drop(broker);
    std::fs::write(dir.path().join("state/state.json"), "{ broken state").unwrap();
    let corrupt =
        Broker::open_with_worker_and_clock(configuration(&dir), backend, worker, clock).await;
    assert!(corrupt.is_err());
}

#[tokio::test]
async fn retirement_rejects_fork_sources_and_children_before_effects() {
    let dir = TempDir::new().unwrap();
    let (broker, _, _, _) = broker(&dir).await;
    let source = broker.attach(request("source", "parent", 0)).await.unwrap();
    let fork = broker.fork(fork_request(&source)).await.unwrap();
    for id in std::iter::once(&source.lease.computer_id).chain(fork.children.iter()) {
        broker.stop(id).await.unwrap();
        let row = broker
            .list_computers()
            .await
            .into_iter()
            .find(|row| &row.computer_id == id)
            .unwrap();
        let error = broker
            .retire(
                id,
                meerkat_computer::RetirementRequest {
                    owner: row.owner,
                    slot: row.slot,
                    profile: row.profile,
                },
            )
            .await
            .unwrap_err();
        assert!(error.to_string().contains("fork-related"));
        assert_eq!(
            broker
                .list_computers()
                .await
                .into_iter()
                .find(|row| &row.computer_id == id)
                .unwrap()
                .phase,
            "stopped"
        );
    }
}
