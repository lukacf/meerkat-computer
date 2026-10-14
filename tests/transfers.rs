use async_trait::async_trait;
use meerkat_computer::backend::{Backend, BackendComputer, BackendError, ComputerSpec};
use meerkat_computer::{
    AttachmentRequest, Broker, BrokerError, CallRequest, Clock, ComputerOwner, Config,
    FileDirection, WorkerCallResponse, WorkerClient, WorkerLease,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
};
use tempfile::TempDir;
use tokio::sync::Mutex;
use tower::ServiceExt;
#[path = "../src/server.rs"]
mod server;

#[derive(Default)]
struct BackendStub;
#[async_trait]
impl Backend for BackendStub {
    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        Ok(BackendComputer {
            instance_name: format!("mc-{}", spec.computer_id),
            incus_project: None,
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: "instance".into(),
            worker_url: "http://127.0.0.1:18765".into(),
        })
    }
    async fn start(&self, c: &BackendComputer) -> Result<BackendComputer, BackendError> {
        Ok(c.clone())
    }
    async fn stop(&self, _: &BackendComputer) -> Result<(), BackendError> {
        Ok(())
    }
    async fn destroy(&self, _: &BackendComputer) -> Result<(), BackendError> {
        Ok(())
    }
}
struct TestClock(AtomicU64);
impl Clock for TestClock {
    fn now_ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
}
#[derive(Default)]
struct WorkerStub {
    calls: Mutex<Vec<CallRequest>>,
    blobs: Mutex<BTreeMap<String, Vec<u8>>>,
    export: Mutex<Vec<u8>>,
    mode: AtomicUsize,
    staged: AtomicUsize,
    removed: AtomicUsize,
    expire_on_fetch: Mutex<Option<Arc<TestClock>>>,
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
#[async_trait]
impl WorkerClient for WorkerStub {
    async fn install_lease(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
    ) -> Result<(), BrokerError> {
        Ok(())
    }
    async fn stage_file(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        id: &str,
        bytes: &[u8],
    ) -> Result<Value, BrokerError> {
        self.staged.fetch_add(1, Ordering::SeqCst);
        self.blobs.lock().await.insert(id.into(), bytes.into());
        Ok(
            json!({"file_id":id,"size_bytes":bytes.len(),"sha256":if self.mode.load(Ordering::SeqCst)==1 { "wrong".into() } else {hash(bytes)}}),
        )
    }
    async fn fetch_file(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        id: &str,
        _: u64,
    ) -> Result<Vec<u8>, BrokerError> {
        if let Some(clock) = self.expire_on_fetch.lock().await.as_ref() {
            clock.0.store(100_000, Ordering::SeqCst);
        }
        if self.mode.load(Ordering::SeqCst) == 4 {
            return Ok(vec![0; 100]);
        }
        Ok(self.blobs.lock().await.get(id).unwrap().clone())
    }
    async fn remove_file(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        id: &str,
    ) -> Result<(), BrokerError> {
        self.removed.fetch_add(1, Ordering::SeqCst);
        self.blobs.lock().await.remove(id);
        Ok(())
    }
    async fn call(
        &self,
        _: &BackendComputer,
        _: &str,
        _: &WorkerLease,
        req: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        self.calls.lock().await.push(req.clone());
        if self.mode.load(Ordering::SeqCst) == 5 {
            return Err(BrokerError::unknown("guest secret untrusted error"));
        }
        if self.mode.load(Ordering::SeqCst) == 6 {
            return Err(BrokerError::invalid("destination is not valid"));
        }
        let id = req.arguments["file_id"].as_str().unwrap();
        let bytes = if req.name == "transfer_export" {
            let bytes = self.export.lock().await.clone();
            self.blobs.lock().await.insert(id.into(), bytes.clone());
            bytes
        } else {
            self.blobs.lock().await.get(id).unwrap().clone()
        };
        let mut metadata = json!({"file_id":id,"path":req.arguments["path"],"size_bytes":bytes.len(),"sha256":hash(&bytes)});
        if self.mode.load(Ordering::SeqCst) == 2 {
            metadata["host_path"] = json!("/private/guest-controlled");
        }
        if self.mode.load(Ordering::SeqCst) == 3 {
            metadata["sha256"] = json!("0".repeat(64));
        }
        Ok(WorkerCallResponse {
            content: vec![json!({"type":"text","text":metadata.to_string()})],
            metrics: json!({"secret":"untrusted"}),
        })
    }
}
fn config(dir: &TempDir) -> Config {
    let mut c: Config = toml::from_str(include_str!("../configs/local.toml")).unwrap();
    c.server.state_dir = dir.path().join("state");
    let p = c.profiles.get_mut("browser").unwrap();
    p.capabilities
        .extend(["file_import".into(), "file_export".into()]);
    p.transfers.max_file_bytes = 32;
    p.transfers.max_stored_bytes = 64;
    c
}
fn attachment(identity: &str) -> AttachmentRequest {
    AttachmentRequest {
        owner: ComputerOwner::Identity {
            realm: "test".into(),
            namespace: "test".into(),
            identity: identity.into(),
        },
        slot: "main".into(),
        profile: "browser".into(),
        session_id: identity.into(),
        runtime_generation: 1,
        ttl_seconds: 60,
        takeover: false,
    }
}
async fn broker(c: Config, w: Arc<WorkerStub>, clock: Arc<TestClock>) -> Broker {
    Broker::open_with_worker_and_clock(c, Arc::new(BackendStub), w, clock)
        .await
        .unwrap()
}
fn request(id: &str, name: &str, args: Value) -> CallRequest {
    CallRequest {
        action_id: id.into(),
        name: name.into(),
        arguments: args,
    }
}
fn text(response: WorkerCallResponse) -> Value {
    serde_json::from_str(response.content[0]["text"].as_str().unwrap()).unwrap()
}

#[tokio::test]
async fn staged_import_is_private_scoped_fenced_and_idempotent() {
    let d = TempDir::new().unwrap();
    let w = Arc::new(WorkerStub::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1000)));
    let b = broker(config(&d), w.clone(), clock.clone()).await;
    let a = b.attach(attachment("a")).await.unwrap();
    let other = b.attach(attachment("b")).await.unwrap();
    let file = b
        .stage_file(&a.lease.computer_id, "report.txt", b"payload")
        .await
        .unwrap();
    assert_eq!(file.direction, FileDirection::Import);
    assert!(
        b.download_file(&a.lease.computer_id, &file.file_id)
            .await
            .is_err()
    );
    assert!(
        b.call(
            &other.token,
            request(
                "other",
                "computer_upload",
                json!({"file_id":file.file_id,"path":"/tmp/report.txt"})
            )
        )
        .await
        .is_err()
    );
    let call = request(
        "one",
        "computer_upload",
        json!({"file_id":file.file_id,"path":"/tmp/report.txt"}),
    );
    let receipt = b.call(&a.token, call.clone()).await.unwrap();
    assert_eq!(
        receipt.metrics,
        json!({"delta":{"file_imports":1,"file_imports_bytes":7}})
    );
    assert!(
        !serde_json::to_string(&receipt)
            .unwrap()
            .contains("untrusted")
    );
    assert_eq!(text(receipt)["path"], "/tmp/report.txt");
    b.call(&a.token, call).await.unwrap();
    assert_eq!(w.calls.lock().await.len(), 1);
    assert_eq!(w.staged.load(Ordering::SeqCst), 1);
    assert_eq!(w.calls.lock().await[0].name, "transfer_import");
    assert!(
        b.call(
            &a.token,
            request(
                "one",
                "computer_upload",
                json!({"file_id":file.file_id,"path":"different"})
            )
        )
        .await
        .is_err()
    );
    clock.0.store(100_000, Ordering::SeqCst);
    assert!(
        b.call(&a.token, request("expired", "computer_files", json!({})))
            .await
            .is_err()
    );
    assert!(
        b.delete_file(&other.lease.computer_id, &file.file_id)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn export_verifies_bytes_persists_across_restart_and_can_be_deleted() {
    let d = TempDir::new().unwrap();
    let c = config(&d);
    let w = Arc::new(WorkerStub::default());
    *w.export.lock().await = b"result".to_vec();
    let clock = Arc::new(TestClock(AtomicU64::new(1000)));
    let b = broker(c.clone(), w.clone(), clock.clone()).await;
    let a = b.attach(attachment("a")).await.unwrap();
    let value = text(
        b.call(
            &a.token,
            request(
                "export",
                "computer_download",
                json!({"path":"/tmp/result.bin","filename":"result.bin"}),
            ),
        )
        .await
        .unwrap(),
    );
    let id = value["file_id"].as_str().unwrap().to_owned();
    assert_eq!(w.removed.load(Ordering::SeqCst), 1);
    let (metadata, bytes) = b.download_file(&a.lease.computer_id, &id).await.unwrap();
    assert_eq!(bytes, b"result");
    assert_eq!(metadata.direction, FileDirection::Export);
    drop(b);
    let b = broker(c, w, clock).await;
    assert_eq!(b.list_files(&a.lease.computer_id).await.unwrap().len(), 1);
    assert_eq!(
        b.download_file(&a.lease.computer_id, &id).await.unwrap().1,
        b"result"
    );
    b.delete_file(&a.lease.computer_id, &id).await.unwrap();
    assert!(b.list_files(&a.lease.computer_id).await.unwrap().is_empty());
    assert!(b.download_file(&a.lease.computer_id, &id).await.is_err());
}

#[tokio::test]
async fn quota_paths_capabilities_and_unknown_worker_outcomes_are_enforced() {
    let d = TempDir::new().unwrap();
    let w = Arc::new(WorkerStub::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1000)));
    let b = broker(config(&d), w.clone(), clock).await;
    let a = b.attach(attachment("a")).await.unwrap();
    for name in ["../secret", "/tmp/file", "a\\b", ".", "a\n"] {
        assert!(
            b.stage_file(&a.lease.computer_id, name, b"x")
                .await
                .is_err()
        );
    }
    assert!(
        b.stage_file(&a.lease.computer_id, "big", &[0; 33])
            .await
            .is_err()
    );
    let f = b
        .stage_file(&a.lease.computer_id, "one", &[0; 32])
        .await
        .unwrap();
    b.stage_file(&a.lease.computer_id, "two", &[0; 32])
        .await
        .unwrap();
    assert!(
        b.stage_file(&a.lease.computer_id, "three", b"x")
            .await
            .is_err()
    );
    assert!(
        b.call(
            &a.token,
            request(
                "quota",
                "computer_download",
                json!({"path":"x","filename":"x"})
            )
        )
        .await
        .is_err()
    );
    assert_eq!(w.calls.lock().await.len(), 0);
    for (i, path) in ["../outside", "/tmp/../etc/passwd", "a//b", "a/./b"]
        .iter()
        .enumerate()
    {
        assert!(
            b.call(
                &a.token,
                request(
                    &format!("invalid-{i}"),
                    "computer_upload",
                    json!({"file_id":f.file_id,"path":path})
                )
            )
            .await
            .is_err()
        );
    }
    w.mode.store(5, Ordering::SeqCst);
    let call = request(
        "uncertain",
        "computer_upload",
        json!({"file_id":f.file_id,"path":"x"}),
    );
    let error = b.call(&a.token, call.clone()).await.unwrap_err();
    assert_eq!(error.effect(), "unknown");
    assert!(!error.message.contains("secret"));
    b.call(&a.token, call).await.unwrap_err();
    assert_eq!(w.calls.lock().await.len(), 1);
    let d2 = TempDir::new().unwrap();
    let mut c = config(&d2);
    c.profiles
        .get_mut("browser")
        .unwrap()
        .capabilities
        .retain(|cap| !cap.starts_with("file_"));
    let b2 = broker(c, w, Arc::new(TestClock(AtomicU64::new(1000)))).await;
    let a2 = b2.attach(attachment("b")).await.unwrap();
    assert!(
        !b2.tools(&a2.token)
            .await
            .unwrap()
            .iter()
            .any(|t| t["name"] == "computer_upload")
    );
    assert_eq!(
        b2.call(&a2.token, request("cap", "computer_files", json!({})))
            .await
            .unwrap_err()
            .code(),
        "capability_denied"
    );
}

#[tokio::test]
async fn malicious_metadata_digest_and_overrun_never_publish_exports() {
    for mode in [2, 3, 4] {
        let d = TempDir::new().unwrap();
        let w = Arc::new(WorkerStub::default());
        w.mode.store(mode, Ordering::SeqCst);
        *w.export.lock().await = b"data".to_vec();
        let b = broker(
            config(&d),
            w.clone(),
            Arc::new(TestClock(AtomicU64::new(1000))),
        )
        .await;
        let a = b.attach(attachment("a")).await.unwrap();
        let call = request(
            "bad",
            "computer_download",
            json!({"path":"result","filename":"result"}),
        );
        assert_eq!(
            b.call(&a.token, call.clone()).await.unwrap_err().effect(),
            "unknown"
        );
        assert!(b.list_files(&a.lease.computer_id).await.unwrap().is_empty());
        b.call(&a.token, call).await.unwrap_err();
        assert_eq!(w.calls.lock().await.len(), 1);
    }
}

#[tokio::test]
async fn staging_ack_mismatch_does_not_dispatch_guest_import() {
    let d = TempDir::new().unwrap();
    let w = Arc::new(WorkerStub::default());
    w.mode.store(1, Ordering::SeqCst);
    let b = broker(
        config(&d),
        w.clone(),
        Arc::new(TestClock(AtomicU64::new(1000))),
    )
    .await;
    let a = b.attach(attachment("a")).await.unwrap();
    let f = b
        .stage_file(&a.lease.computer_id, "file", b"data")
        .await
        .unwrap();
    assert!(
        b.call(
            &a.token,
            request(
                "ack",
                "computer_upload",
                json!({"file_id":f.file_id,"path":"file"})
            )
        )
        .await
        .is_err()
    );
    assert!(w.calls.lock().await.is_empty());
}

#[tokio::test]
async fn operator_file_routes_require_operator_auth_and_never_serve_imports() {
    use axum::{
        body::{Body, to_bytes},
        http::{Request, StatusCode},
    };
    let d = TempDir::new().unwrap();
    let b = Arc::new(
        broker(
            config(&d),
            Arc::new(WorkerStub::default()),
            Arc::new(TestClock(AtomicU64::new(1000))),
        )
        .await,
    );
    let a = b.attach(attachment("a")).await.unwrap();
    let base = format!("/v1/computers/{}/files", a.lease.computer_id);
    let app = server::router(b, "operator-secret");
    let unauthorized = app
        .clone()
        .oneshot(
            Request::builder()
                .uri(&base)
                .header("authorization", format!("Bearer {}", a.token))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!("{base}?filename=hello.txt"))
                .header("authorization", "Bearer operator-secret")
                .header("content-type", "application/octet-stream")
                .body(Body::from("hello"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["cache-control"], "no-store");
    let metadata: Value =
        serde_json::from_slice(&to_bytes(response.into_body(), 4096).await.unwrap()).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .uri(format!("{base}/{}", metadata["file_id"].as_str().unwrap()))
                .header("authorization", "Bearer operator-secret")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn definite_failed_import_cleans_up_staged_worker_blob_without_retry() {
    let d = TempDir::new().unwrap();
    let w = Arc::new(WorkerStub::default());
    w.mode.store(6, Ordering::SeqCst);
    let b = broker(
        config(&d),
        w.clone(),
        Arc::new(TestClock(AtomicU64::new(1000))),
    )
    .await;
    let a = b.attach(attachment("a")).await.unwrap();
    let f = b
        .stage_file(&a.lease.computer_id, "file", b"data")
        .await
        .unwrap();
    let request = request(
        "invalid-destination",
        "computer_upload",
        json!({"file_id":f.file_id,"path":"missing/directory/file"}),
    );
    assert_eq!(
        b.call(&a.token, request.clone())
            .await
            .unwrap_err()
            .effect(),
        "none"
    );
    assert_eq!(w.removed.load(Ordering::SeqCst), 1);
    assert!(w.blobs.lock().await.is_empty());
    b.call(&a.token, request).await.unwrap_err();
    assert_eq!(w.staged.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn expired_grant_cannot_publish_an_export_after_fetching_its_bytes() {
    let d = TempDir::new().unwrap();
    let w = Arc::new(WorkerStub::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1000)));
    *w.export.lock().await = b"result".to_vec();
    *w.expire_on_fetch.lock().await = Some(clock.clone());
    let b = broker(config(&d), w.clone(), clock).await;
    let a = b.attach(attachment("a")).await.unwrap();
    let error = b
        .call(
            &a.token,
            request(
                "export-expired",
                "computer_download",
                json!({"path":"result","filename":"result"}),
            ),
        )
        .await
        .unwrap_err();
    assert_eq!(error.effect(), "unknown");
    assert!(b.list_files(&a.lease.computer_id).await.unwrap().is_empty());
    assert_eq!(w.calls.lock().await.len(), 1);
}

#[tokio::test]
async fn import_and_export_capabilities_are_independent_and_limit_metadata_visibility() {
    for (capability, allowed, denied) in [
        ("file_import", "computer_upload", "computer_download"),
        ("file_export", "computer_download", "computer_upload"),
    ] {
        let d = TempDir::new().unwrap();
        let mut c = config(&d);
        c.profiles
            .get_mut("browser")
            .unwrap()
            .capabilities
            .retain(|cap| !cap.starts_with("file_") || cap == capability);
        let b = broker(
            c,
            Arc::new(WorkerStub::default()),
            Arc::new(TestClock(AtomicU64::new(1000))),
        )
        .await;
        let a = b.attach(attachment("a")).await.unwrap();
        b.stage_file(&a.lease.computer_id, "input", b"x")
            .await
            .unwrap();
        let tools = b.tools(&a.token).await.unwrap();
        assert!(tools.iter().any(|t| t["name"] == allowed));
        assert!(!tools.iter().any(|t| t["name"] == denied));
        let listed = text(
            b.call(&a.token, request("list", "computer_files", json!({})))
                .await
                .unwrap(),
        );
        assert_eq!(
            listed["files"].as_array().unwrap().len(),
            if capability == "file_import" { 1 } else { 0 }
        );
        assert_eq!(
            b.call(&a.token, request("denied", denied, json!({})))
                .await
                .unwrap_err()
                .code(),
            "capability_denied"
        );
    }
}

#[tokio::test]
async fn stored_file_integrity_and_private_filesystem_boundaries_are_enforced() {
    let d = TempDir::new().unwrap();
    let c = config(&d);
    let root = c.server.state_dir.clone();
    let b = broker(
        c,
        Arc::new(WorkerStub::default()),
        Arc::new(TestClock(AtomicU64::new(1000))),
    )
    .await;
    let a = b.attach(attachment("a")).await.unwrap();
    let f = b
        .stage_file(&a.lease.computer_id, "friendly-name", b"original")
        .await
        .unwrap();
    let directory = root.join("transfers").join(&a.lease.computer_id);
    let path = directory.join(format!("{}.blob", f.file_id));
    assert!(path.is_file());
    assert!(!directory.join("friendly-name").exists());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&directory).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    std::fs::write(&path, b"tampered").unwrap();
    assert_eq!(
        b.call(
            &a.token,
            request(
                "tampered",
                "computer_upload",
                json!({"file_id":f.file_id,"path":"guest"})
            )
        )
        .await
        .unwrap_err()
        .code(),
        "storage_failure"
    );
    #[cfg(unix)]
    {
        let external = d.path().join("external");
        std::fs::write(&external, b"original").unwrap();
        std::fs::remove_file(&path).unwrap();
        std::os::unix::fs::symlink(&external, &path).unwrap();
        assert!(
            b.call(
                &a.token,
                request(
                    "symlink",
                    "computer_upload",
                    json!({"file_id":f.file_id,"path":"guest"})
                )
            )
            .await
            .is_err()
        );
        assert_eq!(std::fs::read(&external).unwrap(), b"original");
    }
}

#[tokio::test]
async fn transfer_limits_default_and_are_immutable_after_restart() {
    let d = TempDir::new().unwrap();
    let mut c = config(&d);
    let w = Arc::new(WorkerStub::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1000)));
    let defaults: Config = toml::from_str(include_str!("../configs/local.toml")).unwrap();
    assert_eq!(
        defaults.profiles["browser"].transfers.max_file_bytes,
        16 * 1024 * 1024
    );
    assert_eq!(
        defaults.profiles["browser"].transfers.max_stored_bytes,
        128 * 1024 * 1024
    );
    c.profiles
        .get_mut("browser")
        .unwrap()
        .transfers
        .max_file_bytes = 16 * 1024 * 1024 + 1;
    assert!(c.validate().is_err());
    c = config(&d);
    let b = broker(c.clone(), w.clone(), clock.clone()).await;
    b.attach(attachment("a")).await.unwrap();
    drop(b);
    c.profiles
        .get_mut("browser")
        .unwrap()
        .transfers
        .max_stored_bytes = 128;
    let b = broker(c, w, clock).await;
    assert_eq!(
        b.attach(attachment("a")).await.unwrap_err().code(),
        "conflict"
    );
}

#[tokio::test]
async fn operator_publication_and_deletion_commit_failures_are_unknown() {
    for deleting in [false, true] {
        let d = TempDir::new().unwrap();
        let c = config(&d);
        let state = c.server.state_dir.clone();
        let b = broker(
            c,
            Arc::new(WorkerStub::default()),
            Arc::new(TestClock(AtomicU64::new(1000))),
        )
        .await;
        let a = b.attach(attachment("a")).await.unwrap();
        let prior = if deleting {
            Some(
                b.stage_file(&a.lease.computer_id, "old", b"content")
                    .await
                    .unwrap(),
            )
        } else {
            None
        };
        // Make the atomic state replacement fail after blob publication/unlink.
        let state_path = state.join("state.json");
        let saved = std::fs::read(&state_path).unwrap();
        std::fs::remove_file(&state_path).unwrap();
        std::fs::create_dir(&state_path).unwrap();
        let error = if let Some(file) = &prior {
            b.delete_file(&a.lease.computer_id, &file.file_id)
                .await
                .unwrap_err()
        } else {
            b.stage_file(&a.lease.computer_id, "new", b"content")
                .await
                .unwrap_err()
        };
        assert_eq!(error.effect(), "unknown");
        let blobs = std::fs::read_dir(state.join("transfers").join(&a.lease.computer_id))
            .unwrap()
            .count();
        assert_eq!(blobs, if deleting { 0 } else { 1 });
        std::fs::remove_dir(&state_path).unwrap();
        std::fs::write(&state_path, saved).unwrap();
    }
}

#[tokio::test]
async fn orphan_blob_usage_counts_until_owned_startup_cleanup() {
    let d = TempDir::new().unwrap();
    let c = config(&d);
    let root = c.server.state_dir.clone();
    let w = Arc::new(WorkerStub::default());
    let clock = Arc::new(TestClock(AtomicU64::new(1000)));
    let b = broker(c.clone(), w.clone(), clock.clone()).await;
    let a = b.attach(attachment("a")).await.unwrap();
    let file = b
        .stage_file(&a.lease.computer_id, "owned", &[0; 32])
        .await
        .unwrap();
    let orphan = root
        .join("transfers")
        .join(&a.lease.computer_id)
        .join(format!("{}.blob", uuid::Uuid::new_v4()));
    std::fs::write(&orphan, [0; 32]).unwrap();
    assert!(
        b.stage_file(&a.lease.computer_id, "over-quota", b"x")
            .await
            .is_err()
    );
    drop(b);
    let b = broker(c, w, clock).await;
    assert!(!orphan.exists());
    assert_eq!(
        b.list_files(&a.lease.computer_id).await.unwrap(),
        vec![file]
    );
    b.stage_file(&a.lease.computer_id, "space-recovered", b"x")
        .await
        .unwrap();
}

#[cfg(unix)]
#[tokio::test]
async fn a_fifo_at_a_private_blob_path_is_rejected_without_waiting_for_a_writer() {
    use std::{
        ffi::CString,
        os::unix::{ffi::OsStrExt, fs::OpenOptionsExt},
        time::{Duration, Instant},
    };
    let d = TempDir::new().unwrap();
    let c = config(&d);
    let root = c.server.state_dir.clone();
    let b = broker(
        c,
        Arc::new(WorkerStub::default()),
        Arc::new(TestClock(AtomicU64::new(1000))),
    )
    .await;
    let a = b.attach(attachment("a")).await.unwrap();
    let f = b
        .stage_file(&a.lease.computer_id, "file", b"data")
        .await
        .unwrap();
    let path = root
        .join("transfers")
        .join(&a.lease.computer_id)
        .join(format!("{}.blob", f.file_id));
    std::fs::remove_file(&path).unwrap();
    let name = CString::new(path.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    // A delayed nonblocking writer unwedges a regression so the test itself
    // cannot hang forever if O_NONBLOCK is accidentally removed from the reader.
    let fallback = std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(750));
        let _ = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path);
    });
    let started = Instant::now();
    let error = b
        .call(
            &a.token,
            request(
                "fifo",
                "computer_upload",
                json!({"file_id":f.file_id,"path":"guest"}),
            ),
        )
        .await
        .unwrap_err();
    assert_eq!(error.code(), "storage_failure");
    assert!(started.elapsed() < Duration::from_millis(500));
    fallback.join().unwrap();
}
