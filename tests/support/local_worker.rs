//! Real local process backend for integration harnesses, never isolation evidence.
use async_trait::async_trait;
use meerkat_computer::{
    BrokerError, SecureInputDelivery, WorkerCallResponse,
    backend::{Backend, BackendComputer, BackendError, ComputerSpec, private_input_receipt},
};
use serde_json::Value;
use std::{
    collections::HashMap, os::unix::process::ExitStatusExt, path::PathBuf, process::Stdio,
    time::Duration,
};
use tokio::{
    io::AsyncWriteExt,
    process::{Child, Command},
    sync::Mutex,
};

pub struct LocalWorker {
    pub root: PathBuf,
    pub python: PathBuf,
    pub desktop: bool,
    pub children: Mutex<HashMap<String, (BackendComputer, Child)>>,
}

#[async_trait]
impl Backend for LocalWorker {
    async fn inspect(&self, spec: &ComputerSpec) -> Result<Option<BackendComputer>, BackendError> {
        Ok(self
            .children
            .lock()
            .await
            .get(&spec.computer_id)
            .map(|(handle, _)| handle.clone()))
    }
    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        let mut children = self.children.lock().await;
        if let Some((handle, _)) = children.get(&spec.computer_id) {
            return Ok(handle.clone());
        }
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        drop(listener);
        let directory = self.root.join(&spec.computer_id);
        let mut command = Command::new(&self.python);
        command
            .args([
                "-m",
                "meerkat_computer.worker",
                "--host",
                "127.0.0.1",
                "--port",
                &port.to_string(),
                "--state-dir",
            ])
            .arg(directory.join("worker"))
            .arg("--workspace")
            .arg(directory.join("workspace"))
            .env("MEERKAT_COMPUTER_WORKER_TOKEN", &spec.worker_token);
        if self.desktop {
            command.arg("--desktop");
        }
        let child = command
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| BackendError::Io("cannot launch local smoke worker".into()))?;
        let handle = BackendComputer {
            instance_name: format!("test-{}", spec.computer_id),
            incus_project: None,
            worker_url: format!("http://127.0.0.1:{port}"),
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: spec.computer_id.clone(),
        };
        children.insert(spec.computer_id.clone(), (handle.clone(), child));
        drop(children);
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        for _ in 0..150 {
            if let Ok(response) = client
                .get(format!("{}/health", handle.worker_url))
                .send()
                .await
                && response.status().is_success()
                && let Ok(body) = response.json::<Value>().await
                && body["ready"] == true
                && body["protocol_version"] == 1
            {
                return Ok(handle);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        Err(BackendError::WorkerNotReady {
            instance: handle.instance_name,
        })
    }
    async fn start(&self, computer: &BackendComputer) -> Result<BackendComputer, BackendError> {
        Ok(computer.clone())
    }
    async fn stop(&self, computer: &BackendComputer) -> Result<(), BackendError> {
        if let Some((_, mut child)) = self.children.lock().await.remove(&computer.computer_id) {
            let shutdown = async {
                if let Some(status) = child
                    .try_wait()
                    .map_err(|_| BackendError::Io("cannot inspect test worker exit".into()))?
                {
                    return if status.success() {
                        Ok(())
                    } else {
                        Err(BackendError::Io(format!(
                            "test worker exited before graceful shutdown: {status}"
                        )))
                    };
                }
                let pid = child
                    .id()
                    .ok_or_else(|| BackendError::Io("test worker has no process ID".into()))?;
                // Signal only this backend's actual Child. Uvicorn's lifespan
                // must finish Browser.close before the runtime state is removed.
                let sent = Command::new("kill")
                    .args(["-TERM", &pid.to_string()])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .kill_on_drop(true)
                    .status()
                    .await
                    .map_err(|_| BackendError::Io("cannot send TERM to test worker".into()))?;
                if !sent.success() {
                    return Err(BackendError::Io(
                        "TERM was not delivered to test worker".into(),
                    ));
                }
                let status = child
                    .wait()
                    .await
                    .map_err(|_| BackendError::Io("cannot await test worker shutdown".into()))?;
                // Uvicorn re-raises its captured signal after graceful cleanup.
                if status.success() || status.signal() == Some(15) {
                    Ok(())
                } else {
                    Err(BackendError::Io(format!(
                        "test worker failed during graceful shutdown: {status}"
                    )))
                }
            };
            let failure = match tokio::time::timeout(Duration::from_secs(8), shutdown).await {
                Ok(Ok(())) => return Ok(()),
                Ok(Err(error)) => error,
                Err(_) => {
                    BackendError::Io("test worker graceful shutdown exceeded 8 seconds".into())
                }
            };
            child
                .start_kill()
                .map_err(|_| BackendError::Io(format!("{failure}; forced termination failed")))?;
            match tokio::time::timeout(Duration::from_secs(2), child.wait()).await {
                Ok(Ok(_)) => return Err(failure),
                Ok(Err(_)) => {
                    return Err(BackendError::Io(format!(
                        "{failure}; worker could not be reaped"
                    )));
                }
                Err(_) => {
                    return Err(BackendError::Io(format!(
                        "{failure}; worker did not exit after KILL"
                    )));
                }
            }
        }
        Ok(())
    }
    async fn destroy(&self, computer: &BackendComputer) -> Result<(), BackendError> {
        self.stop(computer).await
    }
    async fn deliver_secret(
        &self,
        computer: &BackendComputer,
        delivery: &SecureInputDelivery,
    ) -> Result<WorkerCallResponse, BrokerError> {
        if !self
            .children
            .lock()
            .await
            .get(&computer.computer_id)
            .is_some_and(|(registered, _)| registered == computer)
        {
            return Err(BrokerError::invalid(
                "local worker incarnation does not match",
            ));
        }
        let mut child = Command::new(&self.python)
            .arg(
                PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .join("python/meerkat_computer/private_client.py"),
            )
            .arg(
                self.root
                    .join(&computer.computer_id)
                    .join("worker/secure-input.sock"),
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| BrokerError::unknown("local private input helper could not start"))?;
        let mut input = child
            .stdin
            .take()
            .ok_or_else(|| BrokerError::unknown("local private input pipe unavailable"))?;
        let output = tokio::time::timeout(Duration::from_secs(35), async {
            input
                .write_all(&delivery.private_wire_bytes()?)
                .await
                .map_err(|_| BrokerError::unknown("local private input pipe failed"))?;
            drop(input);
            child
                .wait_with_output()
                .await
                .map_err(|_| BrokerError::unknown("local private input result unavailable"))
        })
        .await
        .map_err(|_| BrokerError::unknown("local private input outcome is unknown"))??;
        if !output.status.success() {
            return Err(BrokerError::unknown(
                "local private input outcome is unknown",
            ));
        }
        private_input_receipt(&output.stdout)
    }
}
