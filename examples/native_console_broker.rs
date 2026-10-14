//! Local integration harness: real broker and installed Python worker.
//! It is not a VM backend or an operator deployment entry point.
#[path = "../tests/support/local_worker.rs"]
mod local_worker;
#[path = "../src/server.rs"]
mod server;

use axum::{response::Html, routing::get};
use local_worker::LocalWorker;
use meerkat_computer::{AttachmentRequest, Broker, ComputerOwner, Config};
use serde_json::json;
use std::{collections::HashMap, os::unix::fs::OpenOptionsExt, path::PathBuf, sync::Arc};
use tokio::sync::Mutex;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(std::env::var("COMPUTER_FIXTURE_STATE")?);
    std::fs::create_dir_all(&root)?;
    let python = PathBuf::from(std::env::var("COMPUTER_FIXTURE_PYTHON")?);
    let mut config =
        Config::load(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("configs/local.toml"))?;
    config.server.state_dir = root.join("broker");
    let backend = Arc::new(LocalWorker {
        root: root.join("guests"),
        python,
        desktop: false,
        children: Mutex::new(HashMap::new()),
    });
    let broker = Arc::new(Broker::open(config, backend).await?);
    let operator = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let fixture = "<!doctype html><title>Computer package acceptance</title><style>body{font:24px system-ui;background:#123;color:white;padding:48px}button{font:inherit;padding:12px}</style><h1>Real Computer worker</h1><button onclick=\"document.getElementById('count').textContent='Count: '+(++window.count)\">Increment</button><p id='count'>Count: 0</p><script>window.count=0</script>";
    let app = server::router(broker.clone(), &operator)
        .route("/fixture", get(move || async move { Html(fixture) }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}", listener.local_addr()?);
    let attachment = broker
        .attach(AttachmentRequest {
            owner: ComputerOwner::Identity {
                realm: "native-console-acceptance".into(),
                namespace: "mob:computer-acceptance".into(),
                identity: "router:main".into(),
            },
            slot: "main".into(),
            profile: "coder".into(),
            session_id: "native-console-acceptance".into(),
            runtime_generation: 1,
            ttl_seconds: 3600,
            takeover: false,
        })
        .await?;
    for (name, value) in [
        ("binding.json", serde_json::to_value(&attachment)?),
        ("operator.json", json!({"token": operator})),
    ] {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(root.join(name))?;
        serde_json::to_writer(file, &value)?;
    }
    println!(
        "COMPUTER_FIXTURE_READY {}",
        json!({
            "url": url, "binding_file": root.join("binding.json"),
            "computer_id": attachment.lease.computer_id,
        })
    );
    axum::serve(listener, app).await?;
    Ok(())
}
