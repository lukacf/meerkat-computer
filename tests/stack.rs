//! Real broker HTTP + real Python worker + Chromium + official stdio MCP client.
//! LocalWorker is a test process adapter, not VM or container isolation evidence.
#[path = "../src/server.rs"]
mod server;

use axum::{response::Html, routing::get};
use meerkat_computer::{Attachment, AttachmentRequest, Broker, CallRequest, ComputerOwner, Config};
use serde_json::{Value, json};
use std::{collections::HashMap, path::PathBuf, sync::Arc, time::Duration};
use tokio::{process::Command, sync::Mutex};

#[path = "support/local_worker.rs"]
mod local_worker;
use local_worker::LocalWorker;

#[tokio::test]
#[ignore = "requires uv sync and playwright install chromium; run make smoke-stack"]
async fn identity_handoff_over_real_browser_and_mcp() -> Result<(), Box<dyn std::error::Error>> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    // AF_UNIX limits include the complete computer-id/state/socket suffix.
    let state = tempfile::Builder::new()
        .prefix("mc-stack-")
        .tempdir_in("/tmp")?;
    let python = root.join(".venv/bin/python");
    assert!(python.exists(), "run uv sync before this smoke");
    let mut config = Config::load(root.join("configs/local.toml"))?;
    config.server.state_dir = state.path().join("broker");
    let desktop = std::env::var("MEERKAT_COMPUTER_DESKTOP_TEST").as_deref() == Ok("1");
    let backend = Arc::new(LocalWorker {
        root: state.path().join("guests"),
        python: python.clone(),
        desktop,
        children: Mutex::new(HashMap::new()),
    });
    let broker = Arc::new(Broker::open(config.clone(), backend.clone()).await?);
    let operator = "test-operator-secret-with-at-least-32-chars";
    let fixture = "<!doctype html><button onclick=\"document.getElementById('count').textContent='Count: '+(++window.count)\">Increment</button><p id='count'>Count: 0</p><script>window.count=0</script>";
    let app = server::router(broker.clone(), operator)
        .route("/fixture", get(move || async move { Html(fixture) }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}", listener.local_addr()?);
    let serving = tokio::spawn(async move { axum::serve(listener, app).await });
    let client = reqwest::Client::builder().no_proxy().build()?;
    let request = AttachmentRequest {
        owner: ComputerOwner::Identity {
            realm: "test".into(),
            namespace: "mob:smoke".into(),
            identity: "researcher".into(),
        },
        slot: "main".into(),
        profile: if desktop { "desktop" } else { "coder" }.into(),
        session_id: "session-one".into(),
        runtime_generation: 1,
        ttl_seconds: 300,
        takeover: false,
    };
    let response = client
        .post(format!("{url}/v1/attachments"))
        .bearer_auth(operator)
        .json(&request)
        .send()
        .await?;
    assert!(
        response.status().is_success(),
        "attach failed: {}",
        response.text().await?
    );
    let first: Attachment = response.json().await?;
    let access_url = format!("{url}/v1/computers/{}/access", first.lease.computer_id);
    assert_eq!(
        client
            .get(&access_url)
            .bearer_auth(&first.token)
            .send()
            .await?
            .status(),
        reqwest::StatusCode::UNAUTHORIZED
    );
    let access = client.get(&access_url).bearer_auth(operator).send().await?;
    assert!(access.status().is_success());
    let access: Value = access.json().await?;
    assert_eq!(access["computer_id"], first.lease.computer_id);
    assert!(access.get("token").is_none() && access.get("worker_url").is_none());
    let binding = state.path().join("binding.json");
    std::fs::write(&binding, serde_json::to_vec(&first)?)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&binding, std::fs::Permissions::from_mode(0o600))?;
    }
    assert_eq!(
        client
            .get(format!("{url}/v1/computers"))
            .bearer_auth(&first.token)
            .send()
            .await?
            .status(),
        reqwest::StatusCode::UNAUTHORIZED
    );
    let output = tokio::time::timeout(
        Duration::from_secs(90),
        Command::new(&python)
            .arg(root.join("scripts/mcp_smoke.py"))
            .arg("--broker-url")
            .arg(&url)
            .arg("--binding-file")
            .arg(&binding)
            .arg("--fixture-url")
            .arg(format!("{url}/fixture"))
            .kill_on_drop(true)
            .output(),
    )
    .await??;
    assert!(
        output.status.success(),
        "MCP smoke failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    println!("{}", String::from_utf8_lossy(&output.stdout));
    // The preview must reuse an actual regular agent capture, with no separate capture path.
    let captured = broker
        .call(
            &first.token,
            CallRequest {
                action_id: "preview-source-observation".into(),
                name: "computer_observe".into(),
                arguments: json!({"surface":if desktop {"desktop"} else {"browser"}}),
            },
        )
        .await?;
    let expected_png = captured
        .content
        .iter()
        .find(|block| block["type"] == "image")
        .unwrap()["data"]
        .clone();
    let preview_url = format!("{url}/v1/computers/{}/preview", first.lease.computer_id);
    let preview: Value = client
        .get(&preview_url)
        .bearer_auth(operator)
        .send()
        .await?
        .json()
        .await?;
    assert_eq!(preview["status"], "available");
    assert_eq!(preview["frame"]["data"], expected_png);
    let state_before = std::fs::read(config.server.state_dir.join("state.json"))?;
    let preview_output = tokio::time::timeout(
        Duration::from_secs(30),
        Command::new(&python)
            .arg(root.join("scripts/preview_smoke.py"))
            .arg("--broker-url")
            .arg(&url)
            .env("MEERKAT_PREVIEW_SMOKE_OPERATOR", operator)
            .kill_on_drop(true)
            .output(),
    )
    .await??;
    assert!(
        preview_output.status.success(),
        "Preview Chromium smoke failed: {}",
        String::from_utf8_lossy(&preview_output.stderr)
    );
    assert_eq!(
        std::fs::read(config.server.state_dir.join("state.json"))?,
        state_before
    );
    println!("{}", String::from_utf8_lossy(&preview_output.stdout));
    let mut second_request = request.clone();
    second_request.session_id = "session-two".into();
    second_request.runtime_generation = 2;
    assert!(broker.attach(second_request.clone()).await.is_err());
    second_request.takeover = true;
    let second = broker.attach(second_request.clone()).await?;
    assert_eq!(first.lease.computer_id, second.lease.computer_id);
    assert!(second.lease.epoch > first.lease.epoch);
    assert!(broker.tools(&first.token).await.is_err());
    let result = broker
        .call(
            &second.token,
            CallRequest {
                action_id: "read-durable".into(),
                name: "workspace_read".into(),
                arguments: json!({"path":"identity.txt"}),
            },
        )
        .await?;
    let body: Value = serde_json::from_str(result.content[0]["text"].as_str().unwrap())?;
    assert_eq!(body["text"], "persistent identity workspace\n");
    broker.release(&second.token).await?;
    serving.abort();
    let _ = serving.await;
    drop(broker);
    let recovered = Broker::open(config, backend.clone()).await?;
    assert!(recovered.tools(&second.token).await.is_err());
    let third = recovered.attach(second_request).await?;
    assert_eq!(third.lease.computer_id, first.lease.computer_id);
    recovered.stop(&third.lease.computer_id).await?;
    Ok(())
}

#[tokio::test]
#[ignore = "requires uv sync and playwright install chromium; run make smoke-stack"]
async fn credential_login_over_real_browser_and_mcp() -> Result<(), Box<dyn std::error::Error>> {
    use axum::{
        Form,
        http::{HeaderMap, StatusCode},
        response::IntoResponse,
        routing::post,
    };
    use meerkat_computer::{CredentialConfig, credentials::CredentialVault};
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let state = tempfile::Builder::new()
        .prefix("mc-login-")
        .tempdir_in("/tmp")?;
    let python = root.join(".venv/bin/python");
    assert!(python.exists(), "run uv sync before this smoke");
    let mut config = Config::load(root.join("configs/local.toml"))?;
    config.server.state_dir = state.path().join("broker");
    let key = state.path().join("vault-key");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(state.path(), std::fs::Permissions::from_mode(0o700))?;
    }
    CredentialVault::init_key(&key)?;
    config.credentials = Some(CredentialConfig { key_file: key });
    let backend = Arc::new(LocalWorker {
        root: state.path().join("guests"),
        python: python.clone(),
        desktop: false,
        children: Mutex::new(HashMap::new()),
    });
    let broker = Arc::new(Broker::open(config, backend.clone()).await?);
    let operator = "fixture-operator-credential-at-least-32-characters";
    let fixture = r#"<!doctype html><html><body><h1>Private login fixture</h1>
      <form method="post" action="/fixture/login">
        <label>Username<input name="username" autocomplete="off"></label>
        <label>Password<input name="password" type="password" autocomplete="off"></label>
        <button type="submit">Sign in</button>
      </form></body></html>"#;
    let app = server::router(broker.clone(), operator)
        .route("/fixture", get(move || async move { Html(fixture) }))
        .route("/fixture/login", post(|Form(fields): Form<HashMap<String,String>>| async move {
            let variant = match (fields.get("username").map(String::as_str), fields.get("password").map(String::as_str)) {
                (Some("fixture-account"), Some("fixture-password-first-A7!")) => "first",
                (Some("fixture-account"), Some("fixture-password-rotated-B8!")) => "rotated",
                (Some("fixture-account"), Some("fixture-password-once-C9!")) => "once",
                _ => return (StatusCode::UNAUTHORIZED, "Fixture login rejected").into_response(),
            };
            (StatusCode::SEE_OTHER, [("location", "/fixture/session".to_string()),
                ("set-cookie", format!("fixture_session={variant}; HttpOnly; SameSite=Strict; Path=/fixture"))]).into_response()
        }))
        .route("/fixture/session", get(|headers: HeaderMap| async move {
            let cookie = headers.get("cookie").and_then(|value|value.to_str().ok()).unwrap_or("");
            let variant = ["first","rotated","once"].into_iter().find(|variant|cookie.split(';').any(|part|part.trim()==format!("fixture_session={variant}")));
            Html(match variant {Some(variant)=>format!("<html><body><h1>Signed in: {variant}</h1></body></html>"),None=>"<html><body><h1>Not signed in</h1></body></html>".into()})
        }));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let url = format!("http://{}", listener.local_addr()?);
    let serving = tokio::spawn(async move { axum::serve(listener, app).await });
    let mut bindings = Vec::new();
    for identity in ["assigned-agent", "unassigned-agent"] {
        let attachment = broker
            .attach(AttachmentRequest {
                owner: ComputerOwner::Identity {
                    realm: "secure-test".into(),
                    namespace: "mob:credential-smoke".into(),
                    identity: identity.into(),
                },
                slot: "main".into(),
                profile: "browser".into(),
                session_id: format!("session-{identity}"),
                runtime_generation: 1,
                ttl_seconds: 300,
                takeover: false,
            })
            .await?;
        let path = state.path().join(format!("{identity}.json"));
        std::fs::write(&path, serde_json::to_vec(&attachment)?)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        }
        bindings.push((attachment, path));
    }
    let output = tokio::time::timeout(
        Duration::from_secs(120),
        Command::new(&python)
            .arg(root.join("scripts/credential_smoke.py"))
            .arg("--broker-url")
            .arg(&url)
            .arg("--binding-file")
            .arg(&bindings[0].1)
            .arg("--other-binding-file")
            .arg(&bindings[1].1)
            .arg("--fixture-url")
            .arg(format!("{url}/fixture"))
            .env("MEERKAT_CREDENTIAL_SMOKE_OPERATOR", operator)
            .kill_on_drop(true)
            .output(),
    )
    .await??;
    for (binding, _) in &bindings {
        broker.stop(&binding.lease.computer_id).await?;
    }
    serving.abort();
    let _ = serving.await;
    assert!(
        output.status.success(),
        "Credential MCP smoke failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    println!("{}", String::from_utf8_lossy(&output.stdout));
    fn scan(path: &std::path::Path) -> Result<(), Box<dyn std::error::Error>> {
        if path
            .file_name()
            .is_some_and(|name| name == "browser-profile")
        {
            return Ok(());
        }
        if path.is_dir() {
            for entry in std::fs::read_dir(path)? {
                scan(&entry?.path())?;
            }
        } else if path.is_file() {
            let bytes = std::fs::read(path)?;
            for secret in [
                "fixture-password-first-A7!",
                "fixture-password-rotated-B8!",
                "fixture-password-once-C9!",
            ] {
                assert!(
                    !bytes
                        .windows(secret.len())
                        .any(|part| part == secret.as_bytes()),
                    "fixture secret leaked outside browser profile"
                );
            }
        }
        Ok(())
    }
    scan(state.path())?;
    Ok(())
}
