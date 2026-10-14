//! Real broker, Python worker, Chromium and stdio MCP file transfer qualification.
//! The local process backend is not evidence of VM or container isolation.
#[path = "../src/server.rs"]
mod server;

#[path = "support/local_worker.rs"]
mod local_worker;

use axum::{
    Json,
    body::Bytes,
    extract::DefaultBodyLimit,
    response::Html,
    routing::{get, post},
};
use local_worker::LocalWorker;
use meerkat_computer::{AttachmentRequest, Broker, ComputerOwner, Config};
use serde_json::{Value, json};
use std::{collections::HashMap, path::PathBuf, sync::Arc, time::Duration};
use tokio::{process::Command, sync::Mutex};

#[tokio::test]
#[ignore = "requires uv sync and playwright install chromium; run make smoke-stack"]
async fn binary_files_cross_operator_guest_website_and_mcp_boundaries()
-> Result<(), Box<dyn std::error::Error>> {
    use sha2::{Digest, Sha256};

    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let temporary = tempfile::Builder::new()
        .prefix("mc-xfer-")
        .tempdir_in("/tmp")?;
    let state = temporary.path().canonicalize()?;
    let python = root.join(".venv/bin/python");
    assert!(python.exists(), "run uv sync before this smoke");
    let mut config = Config::load(root.join("configs/local.toml"))?;
    config.server.state_dir = state.join("broker");
    config
        .profiles
        .get_mut("coder")
        .unwrap()
        .capabilities
        .extend(["file_import".into(), "file_export".into()]);
    let backend = Arc::new(LocalWorker {
        root: state.join("guests"),
        python: python.clone(),
        desktop: false,
        children: Mutex::new(HashMap::new()),
    });
    let broker = Arc::new(Broker::open(config, backend).await?);
    let operator = "synthetic-transfer-operator-token-at-least-32-characters";
    let received = Arc::new(Mutex::new(Vec::<Value>::new()));
    let uploads = received.clone();
    let fixture = r#"<!doctype html><html><body>
      <label>Website document<input id="document" type="file"></label>
      <p id="status">Waiting for upload</p>
      <a href="/fixture/download">Download website copy</a>
      <script>document.getElementById('document').onchange = async event => {
        const response = await fetch('/fixture/upload', {
          method:'POST', body:await event.target.files[0].arrayBuffer()
        });
        const result = await response.json();
        document.getElementById('status').textContent =
          'Website received ' + result.size_bytes + ' bytes';
      };</script></body></html>"#;
    let downloaded_bytes: Vec<u8> = (0..=255).cycle().take(3 * 1024 * 1024).collect();
    let app = server::router(broker.clone(), operator)
        .route("/fixture", get(move || async move { Html(fixture) }))
        .route(
            "/fixture/download",
            get(move || async move {
                (
                    [
                        ("Content-Type", "application/octet-stream"),
                        (
                            "Content-Disposition",
                            "attachment; filename=website-copy.bin",
                        ),
                    ],
                    downloaded_bytes,
                )
            }),
        )
        .route(
            "/fixture/upload",
            post(move |bytes: Bytes| {
                let uploads = uploads.clone();
                async move {
                    let metadata = json!({
                        "size_bytes":bytes.len(),
                        "sha256":format!("{:x}", Sha256::digest(&bytes))
                    });
                    uploads.lock().await.push(metadata.clone());
                    Json(metadata)
                }
            })
            // This applies only to the synthetic website route. The broker's
            // operator staging endpoint must carry its own large-body limit.
            .layer(DefaultBodyLimit::max(4 * 1024 * 1024)),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}", listener.local_addr()?);
    let serving = tokio::spawn(async move { axum::serve(listener, app).await });
    let attachment = broker
        .attach(AttachmentRequest {
            owner: ComputerOwner::Identity {
                realm: "file-transfer-test".into(),
                namespace: "mob:transfer-smoke".into(),
                identity: "assigned-agent".into(),
            },
            slot: "main".into(),
            profile: "coder".into(),
            session_id: "file-transfer-session".into(),
            runtime_generation: 1,
            ttl_seconds: 300,
            takeover: false,
        })
        .await?;
    let binding = state.join("binding.json");
    std::fs::write(&binding, serde_json::to_vec(&attachment)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&binding, std::fs::Permissions::from_mode(0o600))?;
    }
    let result = tokio::time::timeout(
        Duration::from_secs(120),
        Command::new(&python)
            .arg(root.join("scripts/transfer_smoke.py"))
            .arg("--broker-url")
            .arg(&url)
            .arg("--binding-file")
            .arg(&binding)
            .arg("--fixture-url")
            .arg(format!("{url}/fixture"))
            .env("MEERKAT_TRANSFER_SMOKE_OPERATOR", operator)
            .kill_on_drop(true)
            .output(),
    )
    .await;
    // Shut down the real Chromium owner even when the smoke process failed.
    let stopped = broker.stop(&attachment.lease.computer_id).await;
    serving.abort();
    let _ = serving.await;
    stopped?;
    let output = result??;
    assert!(
        output.status.success(),
        "Transfer MCP smoke failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let summary: Value = serde_json::from_slice(&output.stdout)?;
    let receipts = received.lock().await;
    assert_eq!(receipts.len(), 1, "website must receive exactly one upload");
    assert_eq!(receipts[0]["sha256"], summary["sha256"]);
    assert_eq!(receipts[0]["size_bytes"], summary["size_bytes"]);
    assert!(summary["size_bytes"].as_u64().unwrap() > 2 * 1024 * 1024);
    println!("{summary}");
    Ok(())
}
