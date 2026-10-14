#![cfg(unix)]

use meerkat_computer::gateway::GatewayConfig;
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpListener},
    path::Path,
    process::Stdio,
    time::Duration,
};
use tokio::{
    process::{Child, Command},
    time::{Instant, sleep, timeout},
};

const FIXTURE_TEST: &str = "shutdown_process_fixture";
const HOST_TOKEN: &str = "gateway-shutdown-test-host-token-at-least-32-bytes";

fn fixture_argv(dir: &Path, address: SocketAddr, role: &str) -> Vec<String> {
    vec![
        "/usr/bin/env".into(),
        format!("MEERKAT_GATEWAY_SHUTDOWN_DIR={}", dir.display()),
        format!("MEERKAT_GATEWAY_SHUTDOWN_ADDRESS={address}"),
        format!("MEERKAT_GATEWAY_SHUTDOWN_ROLE={role}"),
        std::env::current_exe()
            .unwrap()
            .to_string_lossy()
            .into_owned(),
        "--exact".into(),
        FIXTURE_TEST.into(),
        "--ignored".into(),
    ]
}

struct GatewayProcess {
    child: Child,
    dir: tempfile::TempDir,
    address: SocketAddr,
    broker_address: SocketAddr,
}

impl GatewayProcess {
    fn start() -> Self {
        let dir = tempfile::tempdir().unwrap();
        // Keep both reservations until their distinct addresses are selected.
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let broker = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let broker_address = broker.local_addr().unwrap();
        let config = GatewayConfig {
            listen: address.to_string(),
            broker_url: format!("http://{broker_address}"),
            state_dir: dir.path().join("gateway-state"),
            host_token_env: "MEERKAT_GATEWAY_SHUTDOWN_TOKEN".into(),
            idle_suspend_seconds: 0,
            request_timeout_seconds: 5,
            wake_timeout_seconds: 5,
            wake_command: fixture_argv(dir.path(), broker_address, "power"),
            suspend_command: fixture_argv(dir.path(), broker_address, "power"),
            tunnel_command: fixture_argv(dir.path(), broker_address, "wrapper"),
        };
        let config_path = dir.path().join("gateway.toml");
        std::fs::write(&config_path, toml::to_string(&config).unwrap()).unwrap();
        drop(listener);
        drop(broker);
        let child = Command::new(env!("CARGO_BIN_EXE_rkat-computer"))
            .arg("gateway")
            .arg("--config")
            .arg(config_path)
            .env("MEERKAT_GATEWAY_SHUTDOWN_TOKEN", HOST_TOKEN)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        Self {
            child,
            dir,
            address,
            broker_address,
        }
    }

    async fn wait_for_server(&mut self, client: &reqwest::Client) {
        let deadline = Instant::now() + Duration::from_secs(5);
        loop {
            assert!(
                self.child.try_wait().unwrap().is_none(),
                "gateway exited before startup"
            );
            if client
                .get(format!("http://{}/host/status", self.address))
                .header("X-Meerkat-Computer-Host-Token", HOST_TOKEN)
                .send()
                .await
                .is_ok_and(|response| response.status().is_success())
            {
                return;
            }
            assert!(
                Instant::now() < deadline,
                "gateway did not become available"
            );
            sleep(Duration::from_millis(20)).await;
        }
    }

    async fn listener_released(&self) -> bool {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if TcpListener::bind(self.broker_address).is_ok() {
                return true;
            }
            if Instant::now() >= deadline {
                return false;
            }
            sleep(Duration::from_millis(10)).await;
        }
    }
}

impl Drop for GatewayProcess {
    fn drop(&mut self) {
        let _ = self.child.start_kill();
        // Red runs must not leave their orphaned SSH-like descendants behind.
        // The regression checks the listener before reaching this cleanup.
        if let Ok(text) = std::fs::read_to_string(self.dir.path().join("wrapper.pid"))
            && let Ok(pid) = text.parse::<i32>()
        {
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
        }
        if let Ok(text) = std::fs::read_to_string(self.dir.path().join("listener.pid"))
            && let Ok(pid) = text.parse::<i32>()
        {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
        }
    }
}

#[tokio::test]
async fn sigterm_drains_gateway_and_releases_owned_descendant_listener() {
    let mut gateway = GatewayProcess::start();
    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(8))
        .build()
        .unwrap();
    gateway.wait_for_server(&client).await;
    let wake = client
        .post(format!("http://{}/host/wake", gateway.address))
        .header("X-Meerkat-Computer-Host-Token", HOST_TOKEN)
        .send()
        .await
        .unwrap();
    assert!(wake.status().is_success());
    assert_eq!(
        wake.json::<serde_json::Value>().await.unwrap()["ready"],
        true
    );
    assert!(gateway.dir.path().join("listener.pid").exists());
    assert!(
        TcpListener::bind(gateway.broker_address).is_err(),
        "fixture listener was not owned"
    );

    let pid = gateway.child.id().expect("running gateway process");
    assert_eq!(unsafe { libc::kill(pid as i32, libc::SIGTERM) }, 0);
    let status = timeout(Duration::from_secs(5), gateway.child.wait())
        .await
        .expect("gateway did not finish SIGTERM shutdown")
        .unwrap();
    assert!(
        gateway.listener_released().await,
        "SIGTERM left the gateway's descendant tunnel listening",
    );
    assert!(
        status.success(),
        "SIGTERM did not complete graceful gateway shutdown"
    );
}

// Execute this ignored test only through fixture_argv. It creates a real child
// listener in the wrapper's inherited process group without external runtimes.
#[test]
#[ignore = "subprocess fixture invoked by the gateway shutdown regression"]
fn shutdown_process_fixture() {
    let Ok(role) = std::env::var("MEERKAT_GATEWAY_SHUTDOWN_ROLE") else {
        return;
    };
    if role == "power" {
        return;
    }
    let dir = std::path::PathBuf::from(std::env::var("MEERKAT_GATEWAY_SHUTDOWN_DIR").unwrap());
    if role == "wrapper" {
        std::fs::write(dir.join("wrapper.pid"), std::process::id().to_string()).unwrap();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", FIXTURE_TEST, "--ignored"])
            .env("MEERKAT_GATEWAY_SHUTDOWN_ROLE", "listener")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        child.wait().unwrap();
        return;
    }
    assert_eq!(role, "listener");
    let listener =
        TcpListener::bind(std::env::var("MEERKAT_GATEWAY_SHUTDOWN_ADDRESS").unwrap()).unwrap();
    std::fs::write(dir.join("listener.pid"), std::process::id().to_string()).unwrap();
    for stream in listener.incoming() {
        let mut stream = stream.unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(1)))
            .unwrap();
        let mut request = [0; 2048];
        if stream.read(&mut request).is_err() {
            continue;
        }
        let body = r#"{"protocol_version":1,"service":"meerkat-computer"}"#;
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len(),
        );
        let _ = stream.write_all(response.as_bytes());
    }
}
