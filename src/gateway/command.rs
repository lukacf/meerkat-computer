//! Host-provided argv effects. No shell, model arguments, or credentials in logs.
use super::{GatewayConfig, HostPower};
use async_trait::async_trait;
use std::{process::Stdio, time::Duration};
use tokio::{
    process::{Child, Command},
    sync::Mutex,
    time::{Instant, sleep, timeout, timeout_at},
};

const PROCESS_REAP_TIMEOUT: Duration = Duration::from_millis(500);

struct Tunnel {
    child: Child,
    // Child::id() becomes None once wait/try_wait reaps the group leader.
    // Descendants can still hold the forwarding port after that point.
    #[cfg(unix)]
    process_group: Option<libc::pid_t>,
}
impl Tunnel {
    fn spawn(argv: &[String]) -> Result<Self, String> {
        let (program, args) = argv.split_first().ok_or("host command not configured")?;
        let mut command = Command::new(program);
        command
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        #[cfg(unix)]
        command.process_group(0);
        let child = command
            .spawn()
            .map_err(|_| "host process could not start")?;
        #[cfg(unix)]
        let process_group = child.id().map(|id| id as libc::pid_t);
        Ok(Self {
            child,
            #[cfg(unix)]
            process_group,
        })
    }

    fn running(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    fn terminate(&mut self) {
        #[cfg(unix)]
        if let Some(group) = self.process_group.take() {
            // Every owned process is spawned into a fresh group. Terminate
            // descendants before reaping the leader; do not reuse this ID.
            unsafe {
                libc::kill(-group, libc::SIGKILL);
            }
        }
        let _ = self.child.start_kill();
    }

    async fn stop(mut self) {
        self.terminate();
        let _ = timeout(PROCESS_REAP_TIMEOUT, self.child.wait()).await;
    }
}
impl Drop for Tunnel {
    fn drop(&mut self) {
        // Cancellation cannot await cleanup. Kill the whole group here and
        // let Tokio's kill_on_drop child handling finish reaping the leader.
        self.terminate();
    }
}

pub struct CommandHostPower {
    config: GatewayConfig,
    http: reqwest::Client,
    tunnel: Mutex<Option<Tunnel>>,
}
impl CommandHostPower {
    pub fn new(config: GatewayConfig) -> Result<Self, String> {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(3))
            .build()
            .map_err(|_| "readiness client unavailable")?;
        Ok(Self {
            config,
            http,
            tunnel: Mutex::new(None),
        })
    }
    async fn command(&self, argv: &[String], deadline: Instant) -> Result<(), String> {
        let mut child = Tunnel::spawn(argv)?;
        let result = timeout_at(deadline, child.child.wait()).await;
        child.stop().await;
        match result {
            Ok(Ok(status)) if status.success() => Ok(()),
            _ => Err("host command did not confirm completion; inspect its saved operation".into()),
        }
    }
    async fn healthy(&self) -> bool {
        if !self.config.tunnel_command.is_empty() {
            let mut tunnel = self.tunnel.lock().await;
            if !tunnel.as_mut().is_some_and(Tunnel::running) {
                if let Some(stopped) = tunnel.take() {
                    stopped.stop().await;
                }
                return false;
            }
        }
        self.broker_healthy().await
    }
    async fn broker_healthy(&self) -> bool {
        let url = format!("{}/health", self.config.broker_url.trim_end_matches('/'));
        match self.http.get(url).send().await {
            Ok(response) if response.status().is_success() => response
                .json::<serde_json::Value>()
                .await
                .is_ok_and(|v| v["protocol_version"] == 1 && v["service"] == "meerkat-computer"),
            _ => false,
        }
    }
}
#[async_trait]
impl HostPower for CommandHostPower {
    async fn ready(&self) -> bool {
        self.healthy().await
    }
    async fn wake(&self) -> Result<(), String> {
        let deadline = Instant::now() + Duration::from_secs(self.config.wake_timeout_seconds);
        // The candidate belongs to this attempt until readiness is confirmed.
        // Cancellation drops it here instead of leaving it in shared state.
        let mut candidate = None;
        let result = timeout_at(deadline, async {
            let prior = self.tunnel.lock().await.take();
            if let Some(prior) = prior {
                prior.stop().await;
            }
            // The helper reconciles cloud status and validates incarnation.
            self.command(&self.config.wake_command, deadline).await?;
            if !self.config.tunnel_command.is_empty() {
                let url = reqwest::Url::parse(&self.config.broker_url)
                    .map_err(|_| "invalid broker URL")?;
                let host = url
                    .host_str()
                    .unwrap_or("127.0.0.1")
                    .trim_matches(['[', ']']);
                // Refuse to forward credentials through an unrelated listener.
                let probe = tokio::net::TcpListener::bind((
                    host,
                    url.port_or_known_default().unwrap_or(80),
                ))
                .await
                .map_err(|_| "broker tunnel port is already occupied")?;
                drop(probe);
                candidate = Some(Tunnel::spawn(&self.config.tunnel_command)?);
            }
            loop {
                if candidate.as_mut().is_some_and(|tunnel| !tunnel.running()) {
                    return Err("tunnel exited before broker readiness".into());
                }
                if self.broker_healthy().await {
                    *self.tunnel.lock().await = candidate.take();
                    return Ok(());
                }
                sleep(Duration::from_millis(500)).await;
            }
        })
        .await
        .unwrap_or_else(|_| {
            Err("host wake did not confirm readiness; inspect its saved operation".into())
        });
        if let Some(candidate) = candidate {
            // This is bounded cleanup after the single wake deadline expires,
            // not another command or readiness budget.
            candidate.stop().await;
        }
        result
    }
    async fn suspend(&self) -> Result<(), String> {
        let deadline = Instant::now() + Duration::from_secs(self.config.wake_timeout_seconds);
        let mut owned = None;
        let result = timeout_at(deadline, async {
            owned = self.tunnel.lock().await.take();
            self.command(&self.config.suspend_command, deadline).await
        })
        .await
        .unwrap_or_else(|_| {
            Err("host suspend did not confirm completion; inspect its saved operation".into())
        });
        if let Some(owned) = owned {
            owned.stop().await;
        }
        result
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{
        io::{Read, Write},
        net::{SocketAddr, TcpListener},
        path::Path,
    };

    const FIXTURE_TEST: &str = "gateway::command::tests::command_process_fixture";

    struct Fixture {
        dir: tempfile::TempDir,
        address: SocketAddr,
        config: GatewayConfig,
    }

    impl Fixture {
        fn new(health: &str, command_delay_ms: u64) -> Self {
            let dir = tempfile::tempdir().unwrap();
            let reservation = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = reservation.local_addr().unwrap();
            drop(reservation);
            let mut config =
                GatewayConfig::for_test(format!("http://{address}"), dir.path().to_path_buf(), 10);
            config.wake_timeout_seconds = 1;
            config.wake_command =
                Self::argv(dir.path(), address, "power", health, command_delay_ms);
            config.suspend_command = Self::argv(dir.path(), address, "power", health, 0);
            config.tunnel_command = Self::argv(dir.path(), address, "wrapper", health, 0);
            Self {
                dir,
                address,
                config,
            }
        }

        fn argv(
            dir: &Path,
            address: SocketAddr,
            role: &str,
            health: &str,
            delay_ms: u64,
        ) -> Vec<String> {
            vec![
                "/usr/bin/env".into(),
                format!("MEERKAT_GATEWAY_TEST_DIR={}", dir.display()),
                format!("MEERKAT_GATEWAY_TEST_ADDRESS={address}"),
                format!("MEERKAT_GATEWAY_TEST_ROLE={role}"),
                format!("MEERKAT_GATEWAY_TEST_HEALTH={health}"),
                format!("MEERKAT_GATEWAY_TEST_DELAY_MS={delay_ms}"),
                std::env::current_exe()
                    .unwrap()
                    .to_string_lossy()
                    .into_owned(),
                "--exact".into(),
                FIXTURE_TEST.into(),
                "--ignored".into(),
            ]
        }

        async fn listener_started(&self) {
            let deadline = Instant::now() + Duration::from_secs(5);
            while !self.dir.path().join("listener.ready").exists() {
                assert!(Instant::now() < deadline, "fixture listener did not start");
                sleep(Duration::from_millis(10)).await;
            }
        }

        async fn port_released(&self) -> bool {
            let deadline = Instant::now() + Duration::from_secs(1);
            loop {
                if TcpListener::bind(self.address).is_ok() {
                    return true;
                }
                if Instant::now() >= deadline {
                    return false;
                }
                sleep(Duration::from_millis(10)).await;
            }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            // Keep the red regressions from leaking their deliberately orphaned
            // listeners. Assertions above always run before this test cleanup.
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

    // The test executable supplies a portable Unix process fixture without a
    // Python, netcat, or cloud dependency. Its listener deliberately inherits
    // the wrapper's process group, just like gcloud's SSH descendant.
    #[test]
    #[ignore = "subprocess fixture invoked by the command lifecycle tests"]
    fn command_process_fixture() {
        let Ok(role) = std::env::var("MEERKAT_GATEWAY_TEST_ROLE") else {
            return;
        };
        let dir = std::path::PathBuf::from(std::env::var("MEERKAT_GATEWAY_TEST_DIR").unwrap());
        if role == "power" {
            let delay = std::env::var("MEERKAT_GATEWAY_TEST_DELAY_MS")
                .unwrap()
                .parse()
                .unwrap();
            std::thread::sleep(Duration::from_millis(delay));
            return;
        }
        if role == "wrapper" {
            std::fs::write(dir.join("wrapper.pid"), std::process::id().to_string()).unwrap();
            let mut child = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", FIXTURE_TEST, "--ignored"])
                .env("MEERKAT_GATEWAY_TEST_ROLE", "listener")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            child.wait().unwrap();
            return;
        }
        assert_eq!(role, "listener");
        let address = std::env::var("MEERKAT_GATEWAY_TEST_ADDRESS").unwrap();
        let listener = TcpListener::bind(address).unwrap();
        std::fs::write(dir.join("listener.pid"), std::process::id().to_string()).unwrap();
        std::fs::write(dir.join("listener.ready"), b"ready").unwrap();
        let health = std::env::var("MEERKAT_GATEWAY_TEST_HEALTH").unwrap();
        for stream in listener.incoming() {
            let mut stream = stream.unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let mut buffer = [0; 2048];
            if stream.read(&mut buffer).is_err() {
                continue;
            }
            if health == "hang" {
                std::thread::sleep(Duration::from_secs(10));
                continue;
            }
            let body = if health == "valid" {
                r#"{"protocol_version":1,"service":"meerkat-computer"}"#
            } else {
                r#"{"protocol_version":0,"service":"unrelated"}"#
            };
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len(),
            );
            let _ = stream.write_all(response.as_bytes());
        }
    }

    #[tokio::test]
    async fn suspend_releases_the_descendant_listener() {
        let fixture = Fixture::new("valid", 0);
        let power = CommandHostPower::new(fixture.config.clone()).unwrap();
        power.wake().await.unwrap();
        fixture.listener_started().await;
        power.suspend().await.unwrap();
        assert!(
            fixture.port_released().await,
            "suspend left a tunnel descendant listening"
        );
    }

    #[tokio::test]
    async fn replacement_wake_releases_the_old_descendant_listener() {
        let fixture = Fixture::new("valid", 0);
        let power = CommandHostPower::new(fixture.config.clone()).unwrap();
        power.wake().await.unwrap();
        fixture.listener_started().await;
        power
            .wake()
            .await
            .expect("replacement must free the old tunnel port");
        assert!(power.ready().await);
        power.suspend().await.unwrap();
        assert!(fixture.port_released().await);
    }

    #[tokio::test]
    async fn wake_uses_one_deadline_for_command_and_readiness() {
        let fixture = Fixture::new("wrong", 700);
        let power = CommandHostPower::new(fixture.config.clone()).unwrap();
        let start = Instant::now();
        assert!(power.wake().await.is_err());
        assert!(
            start.elapsed() < Duration::from_millis(1500),
            "wake incorrectly gave readiness a fresh timeout after the power command",
        );
    }

    #[tokio::test]
    async fn failed_health_releases_the_owned_tunnel() {
        let fixture = Fixture::new("wrong", 0);
        let power = CommandHostPower::new(fixture.config.clone()).unwrap();
        assert!(power.wake().await.is_err());
        fixture.listener_started().await;
        assert!(
            power.tunnel.lock().await.is_none(),
            "failed wake retained its tunnel handle"
        );
        assert!(
            fixture.port_released().await,
            "failed wake retained its descendant listener"
        );
    }

    #[tokio::test]
    async fn pending_health_is_bounded_by_the_same_wake_deadline() {
        let fixture = Fixture::new("hang", 0);
        let power = CommandHostPower::new(fixture.config.clone()).unwrap();
        let start = Instant::now();
        assert!(power.wake().await.is_err());
        assert!(
            start.elapsed() < Duration::from_millis(1500),
            "health exceeded the wake deadline"
        );
        assert!(fixture.port_released().await);
    }

    #[tokio::test]
    async fn cancelling_readiness_drops_the_owned_tunnel() {
        let fixture = Fixture::new("hang", 0);
        let power = std::sync::Arc::new(CommandHostPower::new(fixture.config.clone()).unwrap());
        let task_power = power.clone();
        let wake = tokio::spawn(async move { task_power.wake().await });
        fixture.listener_started().await;
        wake.abort();
        assert!(wake.await.unwrap_err().is_cancelled());
        assert!(
            power.tunnel.lock().await.is_none(),
            "cancelled wake retained its tunnel handle"
        );
        assert!(
            fixture.port_released().await,
            "cancelled wake retained its descendant listener"
        );
    }
}
