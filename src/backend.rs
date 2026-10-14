//! Infrastructure effects only. The broker owns computer and grant lifecycle decisions.
use crate::config::{ComputerKind, ComputerProfile, PoolConfig};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::HashMap, fmt, net::Ipv4Addr, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::Mutex,
    time::{sleep, timeout},
};

pub mod fork;
pub use fork::BackendCheckpoint;

const MANAGED_KEY: &str = "user.meerkat-computer.managed";
const COMPUTER_KEY: &str = "user.meerkat-computer.id";
const OWNER_KEY: &str = "user.meerkat-computer.owner";
const GUEST_ADMIN_KEY: &str = "user.meerkat-computer.guest-admin";
const IMAGE_PROTOCOL_KEY: &str = "user.meerkat-computer.worker-protocol";
const IMAGE_DESKTOP_KEY: &str = "user.meerkat-computer.desktop";
const HOST_OWNER_KEY: &str = "user.meerkat-computer.host-owner";
const DENIED_EGRESS: &str = "0.0.0.0/8,10.0.0.0/8,100.64.0.0/10,127.0.0.0/8,169.254.0.0/16,172.16.0.0/12,192.168.0.0/16,224.0.0.0/3";
const MAX_COMMAND_STDOUT_BYTES: usize = 16 * 1024 * 1024;

/// A cancelled command must not leave a local CLI descendant holding its pipes open.
#[cfg(unix)]
struct CommandProcessGroup(Option<u32>);

#[cfg(unix)]
impl CommandProcessGroup {
    fn terminate(&mut self) {
        if let Some(id) = self.0.take() {
            // The child starts a fresh group whose ID is its own process ID.
            // Killing that group never targets the broker's process group.
            unsafe { libc::kill(-(id as i32), libc::SIGKILL) };
        }
    }
}

#[cfg(unix)]
impl Drop for CommandProcessGroup {
    fn drop(&mut self) {
        self.terminate();
    }
}

#[derive(Clone)]
pub struct ComputerSpec {
    pub computer_id: String,
    pub ownership_id: String,
    pub profile: ComputerProfile,
    pub worker_token: String,
}

impl fmt::Debug for ComputerSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ComputerSpec")
            .field("computer_id", &self.computer_id)
            .field("ownership_id", &self.ownership_id)
            .field("profile", &self.profile)
            .field("worker_token", &"[REDACTED]")
            .finish()
    }
}

/// An exact backend incarnation. Names alone never authorize an effect.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BackendComputer {
    pub instance_name: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub incus_project: Option<String>,
    pub worker_url: String,
    pub computer_id: String,
    pub ownership_id: String,
    pub instance_id: String,
}

#[derive(Debug, thiserror::Error)]
pub enum BackendError {
    #[error("backend does not support {capability}")]
    Unsupported { capability: String },
    #[error("invalid backend configuration: {0}")]
    InvalidConfiguration(String),
    #[error("backend operation {operation} failed: {message}")]
    CommandFailed { operation: String, message: String },
    #[error("backend operation {operation} timed out; its effect may be unknown")]
    CommandTimeout { operation: String },
    #[error("backend ownership or incarnation mismatch for {instance}")]
    OwnershipMismatch { instance: String },
    #[error("computer admission denied: {reason}")]
    AdmissionDenied { reason: String },
    #[error("worker did not become ready for {instance}")]
    WorkerNotReady { instance: String },
    #[error("invalid backend state: {0}")]
    InvalidBackendState(String),
    #[error("backend I/O failed: {0}")]
    Io(String),
}

/// Safe operator evidence. Free-form backend details never enter this projection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackendFailureDiagnostic {
    pub category: BackendFailureCategory,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub operation: Option<BackendOperation>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendFailureCategory {
    Unsupported,
    InvalidConfiguration,
    CommandFailed,
    CommandTimeout,
    OwnershipMismatch,
    AdmissionDenied,
    WorkerNotReady,
    InvalidBackendState,
    Io,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendOperation {
    Inventory,
    ForkCapabilityInspection,
    ForkPrepare,
    CheckpointCreate,
    CheckpointInspection,
    CheckpointRelease,
    ForkCopy,
    ForkNetwork,
    ForkResume,
    ForkRebind,
    ImageValidation,
    NetworkBoundaryInspection,
    NetworkReadiness,
    Create,
    Start,
    GuestAgentReadiness,
    SystemdManagerReadiness,
    WorkerCredentialInjection,
    WorkerServiceStart,
    PrivateInputDelivery,
    Stop,
    Destroy,
}
impl BackendOperation {
    fn from_command(operation: &str) -> Option<Self> {
        Some(match operation {
            "inventory" => Self::Inventory,
            "fork capability inspection" => Self::ForkCapabilityInspection,
            "fork prepare" => Self::ForkPrepare,
            "checkpoint create" => Self::CheckpointCreate,
            "checkpoint inspection" => Self::CheckpointInspection,
            "checkpoint release" => Self::CheckpointRelease,
            "fork copy" => Self::ForkCopy,
            "fork network" => Self::ForkNetwork,
            "fork resume" => Self::ForkResume,
            "fork rebind" => Self::ForkRebind,
            "image validation" => Self::ImageValidation,
            "network boundary inspection" => Self::NetworkBoundaryInspection,
            "network readiness" => Self::NetworkReadiness,
            "create" => Self::Create,
            "start" => Self::Start,
            "guest agent readiness" => Self::GuestAgentReadiness,
            "systemd manager readiness" => Self::SystemdManagerReadiness,
            "worker credential injection" => Self::WorkerCredentialInjection,
            "worker service start" => Self::WorkerServiceStart,
            "private input delivery" => Self::PrivateInputDelivery,
            "stop" => Self::Stop,
            "destroy" => Self::Destroy,
            _ => return None,
        })
    }
}
impl BackendError {
    pub fn diagnostic(&self) -> BackendFailureDiagnostic {
        use BackendFailureCategory as Category;
        let category = match self {
            Self::Unsupported { .. } => Category::Unsupported,
            Self::InvalidConfiguration(_) => Category::InvalidConfiguration,
            Self::CommandFailed { .. } => Category::CommandFailed,
            Self::CommandTimeout { .. } => Category::CommandTimeout,
            Self::OwnershipMismatch { .. } => Category::OwnershipMismatch,
            Self::AdmissionDenied { .. } => Category::AdmissionDenied,
            Self::WorkerNotReady { .. } => Category::WorkerNotReady,
            Self::InvalidBackendState(_) => Category::InvalidBackendState,
            Self::Io(_) => Category::Io,
        };
        // Overall startup deadlines can cover queue/create/start/readiness. They
        // do not identify a failed suboperation; only command errors carry one.
        let operation = match self {
            Self::CommandFailed { operation, .. } | Self::CommandTimeout { operation } => {
                BackendOperation::from_command(operation)
            }
            _ => None,
        };
        BackendFailureDiagnostic {
            category,
            operation,
        }
    }
}

#[async_trait]
pub trait Backend: Send + Sync {
    async fn supports_full_state_fork(
        &self,
        _spec: &ComputerSpec,
        _computer: &BackendComputer,
    ) -> Result<(), BackendError> {
        Err(fork::unsupported())
    }
    async fn resume_fork_source(
        &self,
        _computer: &BackendComputer,
        _checkpoint_id: &str,
    ) -> Result<(), BackendError> {
        Err(fork::unsupported())
    }
    async fn checkpoint_create(
        &self,
        _computer: &BackendComputer,
        _checkpoint_id: &str,
    ) -> Result<BackendCheckpoint, BackendError> {
        Err(fork::unsupported())
    }
    async fn fork_restore(
        &self,
        _checkpoint: &BackendCheckpoint,
        _spec: &ComputerSpec,
    ) -> Result<BackendComputer, BackendError> {
        Err(fork::unsupported())
    }
    async fn inspect_fork(
        &self,
        _checkpoint: &BackendCheckpoint,
        _spec: &ComputerSpec,
    ) -> Result<Option<BackendComputer>, BackendError> {
        Err(fork::unsupported())
    }
    async fn checkpoint_release(
        &self,
        _checkpoint: &BackendCheckpoint,
    ) -> Result<(), BackendError> {
        Err(fork::unsupported())
    }
    /// Resolve a durable creation intent without creating or starting a computer.
    async fn inspect(&self, _spec: &ComputerSpec) -> Result<Option<BackendComputer>, BackendError> {
        Err(BackendError::InvalidBackendState(
            "backend does not support read-only creation recovery".into(),
        ))
    }
    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError>;
    async fn start(&self, computer: &BackendComputer) -> Result<BackendComputer, BackendError>;
    async fn stop(&self, computer: &BackendComputer) -> Result<(), BackendError>;
    async fn destroy(&self, computer: &BackendComputer) -> Result<(), BackendError>;
    /// Guest-local private pipe. Never send this payload through the worker HTTP transport.
    async fn deliver_secret(
        &self,
        _computer: &BackendComputer,
        _delivery: &crate::SecureInputDelivery,
    ) -> Result<crate::WorkerCallResponse, crate::BrokerError> {
        Err(crate::BrokerError::invalid(
            "backend does not support private input delivery",
        ))
    }
}

/// Runs beside Incus on the Linux host. It never opens the Incus daemon remotely.
pub struct IncusBackend {
    config: PoolConfig,
    effects: Mutex<()>,
    client: reqwest::Client,
}

#[derive(Debug, Deserialize, Clone)]
struct Instance {
    name: String,
    #[serde(default)]
    project: Option<String>,
    status: String,
    #[serde(default)]
    stateful: bool,
    #[serde(rename = "type")]
    kind: String,
    #[serde(default)]
    config: HashMap<String, String>,
    #[serde(default)]
    expanded_config: HashMap<String, String>,
    #[serde(default)]
    expanded_devices: HashMap<String, HashMap<String, String>>,
}

impl Instance {
    fn effective_config(&self) -> &HashMap<String, String> {
        if self.expanded_config.is_empty() {
            &self.config
        } else {
            &self.expanded_config
        }
    }

    fn assert_owner(
        &self,
        computer_id: &str,
        ownership_id: &str,
        incarnation: Option<&str>,
    ) -> Result<(), BackendError> {
        self.assert_owner_named(
            computer_id,
            ownership_id,
            incarnation,
            &instance_name(computer_id)?,
        )
    }

    fn assert_owner_named(
        &self,
        computer_id: &str,
        ownership_id: &str,
        incarnation: Option<&str>,
        expected_name: &str,
    ) -> Result<(), BackendError> {
        let good = self.name == expected_name
            && self.config.get(MANAGED_KEY).map(String::as_str) == Some("1")
            && self.config.get(COMPUTER_KEY).map(String::as_str) == Some(computer_id)
            && self.config.get(OWNER_KEY).map(String::as_str) == Some(ownership_id)
            && self
                .config
                .get("volatile.uuid")
                .is_some_and(|value| !value.is_empty())
            && incarnation.is_none_or(|expected| {
                self.config.get("volatile.uuid").map(String::as_str) == Some(expected)
            });
        if good {
            Ok(())
        } else {
            Err(BackendError::OwnershipMismatch {
                instance: self.name.clone(),
            })
        }
    }

    fn assert_profile(&self, profile: &ComputerProfile) -> Result<(), BackendError> {
        let config = self.effective_config();
        let kind = match profile.kind {
            ComputerKind::Container => "container",
            ComputerKind::VirtualMachine => "virtual-machine",
        };
        let good = self.kind == kind
            && config.get("volatile.base_image").map(String::as_str)
                == Some(profile.image.as_str())
            && config.get("limits.cpu") == Some(&profile.cpus.to_string())
            && config.get("limits.memory") == Some(&format!("{}MiB", profile.memory_mib))
            // This marker lives in host-owned Incus metadata, never in guest state.
            && self.config.get(GUEST_ADMIN_KEY).map(String::as_str).unwrap_or("0")
                == if profile.guest_admin { "1" } else { "0" }
            && (!profile
                .capabilities
                .iter()
                .any(|capability| capability == "desktop")
                || config
                    .get("image.user.meerkat-computer.desktop")
                    .map(String::as_str)
                    == Some("1"))
            && config
                .get("security.privileged")
                .map(String::as_str)
                .unwrap_or("false")
                == "false"
            && (profile.kind != ComputerKind::Container
                || config.get("security.idmap.isolated").map(String::as_str) == Some("true"))
            && self.expanded_devices.len() == 2
            && self
                .expanded_devices
                .get("root")
                .and_then(|root| root.get("size"))
                == Some(&format!("{}GiB", profile.disk_gib))
            && self.expanded_devices.get("eth0").is_some_and(|nic| {
                nic.get("type").map(String::as_str) == Some("nic")
                    && nic.get("security.port_isolation").map(String::as_str) == Some("true")
                    && nic.get("security.ipv4_filtering").map(String::as_str) == Some("true")
                    && nic.get("security.ipv6_filtering").map(String::as_str) == Some("true")
                    && nic.get("security.mac_filtering").map(String::as_str) == Some("true")
            });
        if good {
            Ok(())
        } else {
            Err(BackendError::InvalidBackendState(format!(
                "{} does not match its immutable computer profile",
                self.name
            )))
        }
    }

    fn handle(&self, computer_id: &str, ownership_id: &str) -> BackendComputer {
        BackendComputer {
            instance_name: self.name.clone(),
            incus_project: None,
            worker_url: String::new(),
            computer_id: computer_id.to_owned(),
            ownership_id: ownership_id.to_owned(),
            instance_id: self.config["volatile.uuid"].clone(),
        }
    }
}

fn instance_name(computer_id: &str) -> Result<String, BackendError> {
    if computer_id.len() < 16
        || computer_id.len() > 48
        || !computer_id
            .bytes()
            .all(|b| b.is_ascii_hexdigit() || b == b'-')
    {
        return Err(BackendError::InvalidConfiguration(
            "computer ID must be a canonical hexadecimal identifier".into(),
        ));
    }
    Ok(format!("mc-{computer_id}"))
}

fn init_args(spec: &ComputerSpec) -> Result<Vec<String>, BackendError> {
    // A local image fingerprint prevents alias drift, remote pulls and template mutation.
    if spec.profile.image.len() != 64 || !spec.profile.image.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err(BackendError::InvalidConfiguration(
            "Incus profiles require a full local image fingerprint from infra/build-image.py"
                .into(),
        ));
    }
    if spec.ownership_id.is_empty() || spec.worker_token.len() < 32 {
        return Err(BackendError::InvalidConfiguration(
            "creation ownership and worker credentials are required".into(),
        ));
    }
    let mut args = vec![
        "init".into(),
        spec.profile.image.clone(),
        instance_name(&spec.computer_id)?,
        "--profile".into(),
        "default".into(),
        "--config".into(),
        format!("{MANAGED_KEY}=1"),
        "--config".into(),
        format!("{COMPUTER_KEY}={}", spec.computer_id),
        "--config".into(),
        format!("{OWNER_KEY}={}", spec.ownership_id),
        "--config".into(),
        format!(
            "{GUEST_ADMIN_KEY}={}",
            if spec.profile.guest_admin { "1" } else { "0" }
        ),
        "--config".into(),
        format!("limits.cpu={}", spec.profile.cpus),
        "--config".into(),
        format!("limits.memory={}MiB", spec.profile.memory_mib),
        "--device".into(),
        format!("root,size={}GiB", spec.profile.disk_gib),
    ];
    match spec.profile.kind {
        ComputerKind::Container => args.extend([
            "--config".into(),
            "security.privileged=false".into(),
            "--config".into(),
            "security.idmap.isolated=true".into(),
        ]),
        ComputerKind::VirtualMachine => args.extend([
            "--vm".into(),
            "--config".into(),
            "migration.stateful=true".into(),
            "--device".into(),
            format!(
                "root,size.state={}MiB",
                spec.profile.memory_mib.checked_add(512).ok_or_else(|| {
                    BackendError::InvalidConfiguration("VM state reserve overflow".into())
                })?
            ),
        ]),
    }
    Ok(args)
}

/// Pure policy guard: counts actual active guests and reserves the requested memory.
pub fn check_admission(
    running: usize,
    available_mib: u64,
    requested_mib: u64,
    config: &PoolConfig,
) -> Result<(), BackendError> {
    if running >= config.max_running {
        return Err(BackendError::AdmissionDenied {
            reason: format!(
                "pool has {running} active computers; maximum is {}",
                config.max_running
            ),
        });
    }
    if available_mib.saturating_sub(requested_mib) < config.min_available_memory_mib
        || requested_mib > available_mib
    {
        return Err(BackendError::AdmissionDenied {
            reason: format!(
                "{available_mib} MiB available cannot satisfy {requested_mib} MiB plus {} MiB host reserve",
                config.min_available_memory_mib
            ),
        });
    }
    Ok(())
}

fn available_memory_mib(meminfo: &str) -> Result<u64, BackendError> {
    meminfo
        .lines()
        .find_map(|line| {
            line.strip_prefix("MemAvailable:")
                .and_then(|value| value.split_whitespace().next())
                .and_then(|value| value.parse::<u64>().ok())
                .map(|kb| kb / 1024)
        })
        .ok_or_else(|| BackendError::InvalidBackendState("Linux MemAvailable is missing".into()))
}

fn private_worker_url(state: &Value, port: u16) -> Option<String> {
    let state = state.get("metadata").unwrap_or(state);
    state
        .get("network")?
        .as_object()?
        .iter()
        .filter(|(name, _)| name.as_str() != "lo")
        .flat_map(|(_, network)| {
            network
                .get("addresses")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
        })
        .filter(|address| address.get("family").and_then(Value::as_str) == Some("inet"))
        .filter_map(|address| address.get("address").and_then(Value::as_str))
        .filter_map(|address| address.parse::<Ipv4Addr>().ok())
        .find(|address| address.is_private())
        .map(|address| format!("http://{address}:{port}"))
}

/// Fork branches have a host-owned static address. Other guest interfaces must
/// not redirect readiness to a Docker bridge, VPN or guest-created endpoint.
fn bound_worker_url(state: &Value, port: u16, expected: &str) -> Option<String> {
    let address = expected.parse::<Ipv4Addr>().ok()?;
    if !address.is_private() {
        return None;
    }
    let state = state.get("metadata").unwrap_or(state);
    let present = state
        .get("network")?
        .as_object()?
        .iter()
        .filter(|(name, _)| name.as_str() != "lo")
        .any(|(_, network)| {
            network
                .get("addresses")
                .and_then(Value::as_array)
                .is_some_and(|addresses| {
                    addresses.iter().any(|candidate| {
                        candidate.get("family").and_then(Value::as_str) == Some("inet")
                            && candidate.get("address").and_then(Value::as_str) == Some(expected)
                    })
                })
        });
    present.then(|| format!("http://{address}:{port}"))
}

fn assert_network_boundary(network: &Value, acl: &Value) -> Result<(), BackendError> {
    let config = &network["config"];
    let name = network["name"].as_str().unwrap_or_default();
    let address = config["ipv4.address"].as_str().unwrap_or_default();
    let host_ip = address.split('/').next().unwrap_or_default();
    let owner = config[HOST_OWNER_KEY].as_str().unwrap_or_default();
    let expected_ingress = serde_json::json!({
        "action": "allow", "state": "enabled", "source": format!("{host_ip}/32"),
    });
    let expected_egress = serde_json::json!({
        "action": "reject", "state": "enabled", "destination": DENIED_EGRESS,
    });
    // Incus serializes unused rule fields as empty strings. No non-empty
    // criteria may silently widen the operator's network boundary.
    let rule_matches = |rules: &Value, expected: &Value| {
        let Some(rules) = rules.as_array() else {
            return false;
        };
        if rules.len() != 1 {
            return false;
        }
        let Some(rule) = rules[0].as_object() else {
            return false;
        };
        let normalized: serde_json::Map<String, Value> = rule
            .iter()
            .filter(|(_, value)| value.as_str() != Some(""))
            .map(|(key, value)| (key.clone(), value.clone()))
            .collect();
        Value::Object(normalized) == *expected
    };
    let valid = network["type"] == "bridge"
        && network["managed"] == true
        && !owner.is_empty()
        && host_ip.parse::<Ipv4Addr>().is_ok_and(|ip| ip.is_private())
        && config["ipv4.nat"] == "true"
        && config["ipv6.address"] == "none"
        && config["ipv4.firewall"].as_str().unwrap_or("true") == "true"
        && config["security.acls"] == format!("{name}-isolation")
        && config["security.acls.default.ingress.action"] == "reject"
        && config["security.acls.default.egress.action"] == "allow"
        && acl["name"] == format!("{name}-isolation")
        && acl["config"][HOST_OWNER_KEY] == owner
        && rule_matches(&acl["ingress"], &expected_ingress)
        && rule_matches(&acl["egress"], &expected_egress);
    if valid {
        Ok(())
    } else {
        Err(BackendError::InvalidBackendState(
            "private bridge isolation differs; run infra/host-setup.py with a fresh owned pool"
                .into(),
        ))
    }
}

impl IncusBackend {
    pub fn new(config: PoolConfig) -> Result<Self, BackendError> {
        if config.backend != "incus"
            || config.incus_binary.is_empty()
            || config.incus_project.is_empty()
            || !config
                .incus_project
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            || config.incus_project.starts_with('-')
            || config.max_running == 0
            || config.worker_port == 0
            || config.ready_timeout_seconds == 0
        {
            return Err(BackendError::InvalidConfiguration(
                "Incus pool parameters are invalid".into(),
            ));
        }
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(3))
            .redirect(reqwest::redirect::Policy::none())
            .no_proxy()
            .build()
            .map_err(|error| BackendError::Io(error.to_string()))?;
        Ok(Self {
            config,
            effects: Mutex::new(()),
            client,
        })
    }

    fn project_for<'a>(&'a self, handle: &'a BackendComputer) -> Result<&'a str, BackendError> {
        if handle
            .instance_name
            .strip_prefix("mc-")
            .is_none_or(|id| instance_name(id).is_err())
        {
            return Err(BackendError::OwnershipMismatch {
                instance: handle.instance_name.clone(),
            });
        }
        match handle.incus_project.as_deref() {
            None => Ok(&self.config.incus_project),
            Some(project)
                if fork::matches_branch_project(
                    &self.config.incus_project,
                    project,
                    &handle.computer_id,
                )? =>
            {
                Ok(project)
            }
            Some(_) => Err(BackendError::OwnershipMismatch {
                instance: handle.instance_name.clone(),
            }),
        }
    }

    async fn command(
        &self,
        operation: &str,
        args: &[String],
        input: Option<Vec<u8>>,
    ) -> Result<Vec<u8>, BackendError> {
        self.command_in(&self.config.incus_project, operation, args, input)
            .await
    }

    async fn command_in(
        &self,
        project: &str,
        operation: &str,
        args: &[String],
        input: Option<Vec<u8>>,
    ) -> Result<Vec<u8>, BackendError> {
        let mut command = Command::new(&self.config.incus_binary);
        command.arg("--force-local");
        // Incus 6 query scopes projects in its URL. Cross-project inventory
        // rejects the global flag because --all-projects is mutually exclusive.
        let global_scope = args.first().map(String::as_str) == Some("query")
            || (args.first().map(String::as_str) == Some("list")
                && args.iter().any(|arg| arg == "--all-projects"));
        if !global_scope {
            command.args(["--project", project]);
        }
        command
            .args(args)
            .kill_on_drop(true)
            .stdin(if input.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        #[cfg(unix)]
        command.process_group(0);
        let mut child = command
            .spawn()
            .map_err(|error| BackendError::Io(error.to_string()))?;
        #[cfg(unix)]
        let mut group = CommandProcessGroup(child.id());
        let mut stdin = child.stdin.take();
        let mut stdout = child
            .stdout
            .take()
            .ok_or_else(|| BackendError::Io("missing output pipe".into()))?;
        let deadline = Duration::from_secs(self.config.ready_timeout_seconds);
        let execute = async {
            let write_input = async {
                if let Some(input) = input {
                    let mut stdin = stdin
                        .take()
                        .ok_or_else(|| BackendError::Io("missing input pipe".into()))?;
                    stdin
                        .write_all(&input)
                        .await
                        .map_err(|error| BackendError::Io(error.to_string()))?;
                    stdin
                        .shutdown()
                        .await
                        .map_err(|error| BackendError::Io(error.to_string()))?;
                }
                Ok::<_, BackendError>(())
            };
            let read_output = async {
                let mut output = Vec::new();
                let mut chunk = [0_u8; 16 * 1024];
                loop {
                    let count = stdout
                        .read(&mut chunk)
                        .await
                        .map_err(|error| BackendError::Io(error.to_string()))?;
                    if count == 0 {
                        return Ok::<_, BackendError>(output);
                    }
                    if output.len().saturating_add(count) > MAX_COMMAND_STDOUT_BYTES {
                        return Err(BackendError::InvalidBackendState(
                            "Incus response exceeded 16 MiB".into(),
                        ));
                    }
                    output.extend_from_slice(&chunk[..count]);
                }
            };
            let wait = async {
                child
                    .wait()
                    .await
                    .map_err(|error| BackendError::Io(error.to_string()))
            };
            // Drain output while sending stdin so a guest cannot deadlock the two pipes.
            let ((), output, status) = tokio::try_join!(write_input, read_output, wait)?;
            if !status.success() {
                // Guest provisioning receives a secret on stdin. Never echo command output.
                return Err(BackendError::CommandFailed {
                    operation: operation.into(),
                    message: format!("Incus exited with {status}"),
                });
            }
            Ok(output)
        };
        let result = timeout(deadline, execute).await.unwrap_or_else(|_| {
            Err(BackendError::CommandTimeout {
                operation: operation.into(),
            })
        });
        #[cfg(unix)]
        group.terminate();
        if result.is_err() {
            // Do not return with a live command or zombie after overflow, timeout or I/O failure.
            let _ = child.start_kill();
            let _ = child.wait().await;
        }
        result
    }

    async fn simple(&self, operation: &str, args: &[&str]) -> Result<Vec<u8>, BackendError> {
        self.command(
            operation,
            &args.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>(),
            None,
        )
        .await
    }

    async fn simple_in(
        &self,
        project: &str,
        operation: &str,
        args: &[&str],
    ) -> Result<Vec<u8>, BackendError> {
        self.command_in(
            project,
            operation,
            &args.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>(),
            None,
        )
        .await
    }

    async fn inventory_in(&self, project: &str) -> Result<Vec<Instance>, BackendError> {
        let output = self
            .simple_in(project, "inventory", &["list", "--format=json"])
            .await?;
        serde_json::from_slice(&output).map_err(|_| {
            BackendError::InvalidBackendState("invalid Incus inventory response".into())
        })
    }

    async fn inventory(&self) -> Result<Vec<Instance>, BackendError> {
        self.inventory_in(&self.config.incus_project).await
    }

    async fn inspect_handle(&self, handle: &BackendComputer) -> Result<Instance, BackendError> {
        let project = self.project_for(handle)?;
        if handle.incus_project.is_some() {
            self.inspect_branch_project(handle).await?;
        }
        let instance = self
            .inventory_in(project)
            .await?
            .into_iter()
            .find(|item| item.name == handle.instance_name)
            .ok_or_else(|| BackendError::OwnershipMismatch {
                instance: handle.instance_name.clone(),
            })?;
        instance.assert_owner_named(
            &handle.computer_id,
            &handle.ownership_id,
            Some(&handle.instance_id),
            &handle.instance_name,
        )?;
        if handle.incus_project.is_none()
            && handle.instance_name != instance_name(&handle.computer_id)?
        {
            return Err(BackendError::OwnershipMismatch {
                instance: handle.instance_name.clone(),
            });
        }
        Ok(instance)
    }

    async fn inspect_network_boundary(&self, instance: &Instance) -> Result<(), BackendError> {
        let config = instance.effective_config();
        if config
            .get("security.privileged")
            .map(String::as_str)
            .unwrap_or("false")
            != "false"
            || (instance.kind == "container"
                && config.get("security.idmap.isolated").map(String::as_str) != Some("true"))
        {
            return Err(BackendError::InvalidBackendState(
                "computer user namespace isolation differs".into(),
            ));
        }
        let nic = instance.expanded_devices.get("eth0").ok_or_else(|| {
            BackendError::InvalidBackendState("computer has no private network device".into())
        })?;
        let name = nic
            .get("network")
            .filter(|name| {
                !name.is_empty()
                    && name.bytes().all(|byte| {
                        byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'
                    })
            })
            .ok_or_else(|| {
                BackendError::InvalidBackendState("computer has no managed private bridge".into())
            })?;
        for setting in [
            "security.port_isolation",
            "security.ipv4_filtering",
            "security.ipv6_filtering",
            "security.mac_filtering",
        ] {
            if nic.get(setting).map(String::as_str) != Some("true") {
                return Err(BackendError::InvalidBackendState(
                    "computer network filtering differs".into(),
                ));
            }
        }
        let mut objects = Vec::with_capacity(2);
        for path in [
            format!("/1.0/networks/{name}?project=default"),
            format!("/1.0/network-acls/{name}-isolation?project=default"),
        ] {
            let bytes = self
                .simple("network boundary inspection", &["query", &path])
                .await?;
            let value: Value = serde_json::from_slice(&bytes).map_err(|_| {
                BackendError::InvalidBackendState("invalid network boundary response".into())
            })?;
            objects.push(value.get("metadata").cloned().unwrap_or(value));
        }
        assert_network_boundary(&objects[0], &objects[1])
    }

    fn active_pool_instances(&self, inventory: &[Instance]) -> usize {
        let prefix = format!("{}-fork-", self.config.incus_project);
        inventory
            .iter()
            .filter(|instance| {
                let project = instance
                    .project
                    .as_deref()
                    .unwrap_or(&self.config.incus_project);
                instance.status != "Stopped"
                    && (project == self.config.incus_project
                        || project.starts_with(&prefix)
                        || instance.config.get(fork::FORK_POOL_KEY)
                            == Some(&self.config.incus_project))
            })
            .count()
    }

    async fn admit(&self, requested_mib: u64) -> Result<(), BackendError> {
        // Branches retain the source name in separate projects. A base-project
        // inventory alone would silently evade the pool's resource admission.
        let output = self
            .simple("inventory", &["list", "--all-projects", "--format=json"])
            .await?;
        let inventory: Vec<Instance> = serde_json::from_slice(&output).map_err(|_| {
            BackendError::InvalidBackendState("invalid cross-project inventory response".into())
        })?;
        let active = self.active_pool_instances(&inventory);
        let meminfo = tokio::fs::read_to_string("/proc/meminfo")
            .await
            .map_err(|error| {
                BackendError::Io(format!("host memory admission requires Linux: {error}"))
            })?;
        check_admission(
            active,
            available_memory_mib(&meminfo)?,
            requested_mib,
            &self.config,
        )
    }

    async fn worker_ready(&self, url: &str) -> bool {
        let Ok(mut response) = self.client.get(format!("{url}/health")).send().await else {
            return false;
        };
        if !response.status().is_success() {
            return false;
        }
        let mut bytes = Vec::with_capacity(512);
        loop {
            match response.chunk().await {
                Ok(Some(chunk)) if bytes.len() + chunk.len() <= 4096 => {
                    bytes.extend_from_slice(&chunk)
                }
                Ok(None) => break,
                _ => return false,
            }
        }
        let Ok(health) = serde_json::from_slice::<Value>(&bytes) else {
            return false;
        };
        health.get("protocol_version").and_then(Value::as_u64) == Some(1)
            && health.get("ready").and_then(Value::as_bool) == Some(true)
    }

    async fn ready(&self, mut computer: BackendComputer) -> Result<BackendComputer, BackendError> {
        loop {
            let instance = self.inspect_handle(&computer).await?;
            if instance.status != "Running" {
                return Err(BackendError::InvalidBackendState(format!(
                    "{} is {} while waiting for its worker",
                    computer.instance_name, instance.status
                )));
            }
            let state = self
                .simple(
                    "network readiness",
                    &[
                        "query",
                        &format!(
                            "/1.0/instances/{}/state?project={}",
                            computer.instance_name,
                            self.project_for(&computer)?
                        ),
                    ],
                )
                .await?;
            let state: Value = serde_json::from_slice(&state).map_err(|_| {
                BackendError::InvalidBackendState("invalid Incus state response".into())
            })?;
            let worker_url = if computer.incus_project.is_some() {
                let expected = instance
                    .expanded_devices
                    .get("eth0")
                    .and_then(|nic| nic.get("ipv4.address"))
                    .ok_or_else(|| {
                        BackendError::InvalidBackendState(
                            "fork has no reserved management address".into(),
                        )
                    })?;
                bound_worker_url(&state, self.config.worker_port, expected)
            } else {
                private_worker_url(&state, self.config.worker_port)
            };
            if let Some(url) = worker_url
                && self.worker_ready(&url).await
            {
                computer.worker_url = url;
                return Ok(computer);
            }
            sleep(Duration::from_millis(250)).await;
        }
    }

    async fn ensure_inner(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        let guard = self.effects.lock().await;
        let args = init_args(spec)?;
        let name = instance_name(&spec.computer_id)?;
        let inventory = self.inventory().await?;
        let mut instance = if let Some(instance) =
            inventory.into_iter().find(|item| item.name == name)
        {
            // A prior creation intent is recoverable only with its exact durable owner token.
            instance.assert_owner(&spec.computer_id, &spec.ownership_id, None)?;
            instance
        } else {
            let image = self
                .simple(
                    "image validation",
                    &[
                        "query",
                        &format!(
                            "/1.0/images/{}?project={}",
                            spec.profile.image, self.config.incus_project
                        ),
                    ],
                )
                .await?;
            let image: Value = serde_json::from_slice(&image)
                .map_err(|_| BackendError::InvalidBackendState("invalid image response".into()))?;
            let image = image.get("metadata").unwrap_or(&image);
            if image
                .get("properties")
                .and_then(|value| value.get(IMAGE_PROTOCOL_KEY))
                .and_then(Value::as_str)
                != Some("1")
            {
                return Err(BackendError::InvalidConfiguration(
                    "image is not a version 1 Meerkat Computer worker image".into(),
                ));
            }
            if spec
                .profile
                .capabilities
                .iter()
                .any(|capability| capability == "desktop")
                && image
                    .get("properties")
                    .and_then(|value| value.get(IMAGE_DESKTOP_KEY))
                    .and_then(Value::as_str)
                    != Some("1")
            {
                return Err(BackendError::InvalidConfiguration(
                    "desktop capability requires an image built with --desktop".into(),
                ));
            }
            self.admit(spec.profile.memory_mib).await?;
            self.command("create", &args, None).await?;
            self.inventory()
                .await?
                .into_iter()
                .find(|item| item.name == name)
                .ok_or_else(|| {
                    BackendError::InvalidBackendState(
                        "created instance is missing from inventory".into(),
                    )
                })?
        };
        instance.assert_owner(&spec.computer_id, &spec.ownership_id, None)?;
        instance.assert_profile(&spec.profile)?;
        self.inspect_network_boundary(&instance).await?;
        if instance.status == "Stopped" {
            self.admit(spec.profile.memory_mib).await?;
            self.simple("start", &["start", &name]).await?;
            instance.status = "Running".into();
        } else if instance.status != "Running" {
            return Err(BackendError::InvalidBackendState(format!(
                "{name} is {}; explicit operator recovery required",
                instance.status
            )));
        }
        let handle = instance.handle(&spec.computer_id, &spec.ownership_id);
        // Admission/create/start stay serialized. Guest readiness must not stall the pool.
        // The broker retains the same-computer operation authority throughout this call.
        drop(guard);
        // Incus VM RUNNING is not guest-agent readiness. Poll an inert command first.
        loop {
            self.inspect_handle(&handle).await?;
            match self
                .simple("guest agent readiness", &["exec", &name, "--", "true"])
                .await
            {
                Ok(_) => break,
                Err(BackendError::CommandFailed { .. }) => sleep(Duration::from_millis(250)).await,
                Err(error) => return Err(error),
            }
        }
        // Exec can work before the guest's systemd manager accepts requests. This
        // read-only probe shares ensure's deadline; no bootstrap/start is replayed.
        loop {
            self.inspect_handle(&handle).await?;
            match self
                .simple(
                    "systemd manager readiness",
                    &[
                        "exec",
                        &name,
                        "--",
                        "systemctl",
                        "show",
                        "--property=Version",
                        "--value",
                    ],
                )
                .await
            {
                Ok(value) if value.iter().any(|byte| !byte.is_ascii_whitespace()) => break,
                Ok(_) | Err(BackendError::CommandFailed { .. }) => {
                    sleep(Duration::from_millis(250)).await
                }
                Err(error) => return Err(error),
            }
        }
        self.inspect_handle(&handle).await?;
        let input = serde_json::to_vec(&serde_json::json!({
            "token": spec.worker_token,
            "port": self.config.worker_port,
            "width": spec.profile.observation.width,
            "height": spec.profile.observation.height,
            "guest_admin": spec.profile.guest_admin,
        }))
        .map_err(|_| BackendError::InvalidConfiguration("cannot encode worker bootstrap".into()))?;
        self.command(
            "worker credential injection",
            &[
                "exec".into(),
                name.clone(),
                "--".into(),
                "python3".into(),
                "-c".into(),
                INSTALL_CREDENTIALS.into(),
            ],
            Some(input),
        )
        .await?;
        self.simple(
            "worker service start",
            &[
                "exec",
                &name,
                "--",
                "systemctl",
                "start",
                "meerkat-computer-worker.service",
            ],
        )
        .await?;
        self.ready(handle).await
    }
}

#[async_trait]
impl Backend for IncusBackend {
    async fn supports_full_state_fork(
        &self,
        spec: &ComputerSpec,
        computer: &BackendComputer,
    ) -> Result<(), BackendError> {
        self.fork_capability(spec, computer).await
    }
    async fn resume_fork_source(
        &self,
        computer: &BackendComputer,
        checkpoint_id: &str,
    ) -> Result<(), BackendError> {
        self.resume_source(computer, checkpoint_id).await
    }
    async fn checkpoint_create(
        &self,
        computer: &BackendComputer,
        checkpoint_id: &str,
    ) -> Result<BackendCheckpoint, BackendError> {
        self.create_checkpoint(computer, checkpoint_id).await
    }
    async fn fork_restore(
        &self,
        checkpoint: &BackendCheckpoint,
        spec: &ComputerSpec,
    ) -> Result<BackendComputer, BackendError> {
        self.restore_fork(checkpoint, spec).await
    }
    async fn inspect_fork(
        &self,
        checkpoint: &BackendCheckpoint,
        spec: &ComputerSpec,
    ) -> Result<Option<BackendComputer>, BackendError> {
        self.inspect_fork_child(checkpoint, spec).await
    }
    async fn checkpoint_release(&self, checkpoint: &BackendCheckpoint) -> Result<(), BackendError> {
        self.release_checkpoint(checkpoint).await
    }
    async fn deliver_secret(
        &self,
        computer: &BackendComputer,
        delivery: &crate::SecureInputDelivery,
    ) -> Result<crate::WorkerCallResponse, crate::BrokerError> {
        self.inspect_handle(computer).await.map_err(|_| {
            crate::BrokerError::invalid("private input target incarnation could not be verified")
        })?;
        let bytes = self
            .command_in(
                self.project_for(computer).map_err(|_| {
                    crate::BrokerError::invalid(
                        "private input target project could not be verified",
                    )
                })?,
                "private input delivery",
                &[
                    "exec".into(),
                    computer.instance_name.clone(),
                    "--".into(),
                    "python3".into(),
                    "-c".into(),
                    include_str!("../python/meerkat_computer/private_client.py").into(),
                    "/var/lib/meerkat-computer/state/secure-input.sock".into(),
                ],
                Some(delivery.private_wire_bytes()?),
            )
            .await
            .map_err(|_| {
                crate::BrokerError::unknown("private input acknowledgement was lost; do not replay")
            })?;
        private_input_receipt(&bytes)
    }
    async fn inspect(&self, spec: &ComputerSpec) -> Result<Option<BackendComputer>, BackendError> {
        let name = instance_name(&spec.computer_id)?;
        let Some(instance) = self
            .inventory()
            .await?
            .into_iter()
            .find(|item| item.name == name)
        else {
            return Ok(None);
        };
        instance.assert_owner(&spec.computer_id, &spec.ownership_id, None)?;
        instance.assert_profile(&spec.profile)?;
        Ok(Some(instance.handle(&spec.computer_id, &spec.ownership_id)))
    }

    async fn ensure(&self, spec: &ComputerSpec) -> Result<BackendComputer, BackendError> {
        timeout(
            Duration::from_secs(self.config.ready_timeout_seconds),
            self.ensure_inner(spec),
        )
        .await
        .map_err(|_| BackendError::WorkerNotReady {
            instance: instance_name(&spec.computer_id).unwrap_or_default(),
        })?
    }

    async fn start(&self, computer: &BackendComputer) -> Result<BackendComputer, BackendError> {
        let work = async {
            let guard = self.effects.lock().await;
            let actual = self.inspect_handle(computer).await?;
            self.assert_branch_ready(computer, &actual)?;
            self.inspect_network_boundary(&actual).await?;
            if actual.status == "Stopped" {
                let memory = actual
                    .effective_config()
                    .get("limits.memory")
                    .and_then(|value| value.strip_suffix("MiB"))
                    .and_then(|value| value.parse::<u64>().ok())
                    .ok_or_else(|| {
                        BackendError::InvalidBackendState(
                            "owned instance has no MiB memory limit".into(),
                        )
                    })?;
                self.admit(memory).await?;
                self.simple_in(
                    self.project_for(computer)?,
                    "start",
                    &["start", &computer.instance_name],
                )
                .await?;
            } else if actual.status != "Running" {
                return Err(BackendError::InvalidBackendState(format!(
                    "{} is {}",
                    computer.instance_name, actual.status
                )));
            }
            drop(guard);
            self.ready(computer.clone()).await
        };
        timeout(Duration::from_secs(self.config.ready_timeout_seconds), work)
            .await
            .map_err(|_| BackendError::WorkerNotReady {
                instance: computer.instance_name.clone(),
            })?
    }

    async fn stop(&self, computer: &BackendComputer) -> Result<(), BackendError> {
        let _guard = self.effects.lock().await;
        let instance = self.inspect_handle(computer).await?;
        if instance.status == "Stopped" {
            return Ok(());
        }
        if instance.status != "Running" {
            return Err(BackendError::InvalidBackendState(format!(
                "{} is {}",
                instance.name, instance.status
            )));
        }
        if computer.incus_project.is_some() && instance.kind == "virtual-machine" {
            // Restored QEMU guests can ignore the ACPI power-button event used
            // by Incus stop. Ask this exact guest once, then trust host state.
            let mut dispatch_error = None;
            let work = async {
                dispatch_error = self
                    .simple_in(
                        self.project_for(computer)?,
                        "stop",
                        &[
                            "exec",
                            &computer.instance_name,
                            "--",
                            "systemctl",
                            "poweroff",
                            "--no-block",
                        ],
                    )
                    .await
                    .err();
                // Shutdown may close the agent channel before its reply. A
                // lost reply is successful only after exact stopped evidence.
                loop {
                    let observed = self.inspect_handle(computer).await?;
                    if observed.status == "Stopped" {
                        return Ok(());
                    }
                    if observed.status != "Running" && observed.status != "Stopping" {
                        return Err(BackendError::InvalidBackendState(format!(
                            "{} is {} while waiting for shutdown",
                            observed.name, observed.status
                        )));
                    }
                    sleep(Duration::from_millis(250)).await;
                }
            };
            return timeout(Duration::from_secs(self.config.ready_timeout_seconds), work)
                .await
                .unwrap_or_else(|_| {
                    Err(
                        dispatch_error.unwrap_or_else(|| BackendError::CommandTimeout {
                            operation: "stop".into(),
                        }),
                    )
                });
        }
        self.simple_in(
            self.project_for(computer)?,
            "stop",
            &[
                "stop",
                &computer.instance_name,
                "--timeout",
                &self.config.ready_timeout_seconds.to_string(),
            ],
        )
        .await?;
        if self.inspect_handle(computer).await?.status != "Stopped" {
            return Err(BackendError::InvalidBackendState(
                "Incus did not confirm the stopped state".into(),
            ));
        }
        Ok(())
    }

    async fn destroy(&self, computer: &BackendComputer) -> Result<(), BackendError> {
        let _guard = self.effects.lock().await;
        let instance = self.inspect_handle(computer).await?;
        if instance.status != "Stopped" {
            return Err(BackendError::InvalidBackendState(
                "destroy requires an explicitly stopped computer".into(),
            ));
        }
        self.simple_in(
            self.project_for(computer)?,
            "destroy",
            &["delete", &computer.instance_name],
        )
        .await?;
        if self
            .inventory_in(self.project_for(computer)?)
            .await?
            .iter()
            .any(|item| item.name == computer.instance_name)
        {
            return Err(BackendError::InvalidBackendState(
                "Incus still reports the destroyed instance".into(),
            ));
        }
        self.delete_branch_resources(computer).await?;
        Ok(())
    }
}

/// Decode a private channel response without exposing guest diagnostics.
pub fn private_input_receipt(
    bytes: &[u8],
) -> Result<crate::WorkerCallResponse, crate::BrokerError> {
    const MAX_BYTES: usize = 64 * 1024;
    const MAX_REFS: usize = 200;
    const MAX_COUNTER: u64 = (1_u64 << 53) - 1;
    const COUNTERS: &[&str] = &[
        "tool_calls_admitted",
        "tool_calls_completed",
        "tool_calls_failed",
        "uncertain_effects",
        "secure_input_attempts",
        "secure_input_completed",
        "ui_command_attempts",
        "ui_commands",
        "secure_fields_filled",
        "observation_attempts",
        "observations",
        "navigation_attempts",
        "navigations",
        "clicks",
        "tab_command_attempts",
        "tab_commands",
        "native_focus_commands",
        "native_invocations",
        "native_text_commands",
        "application_launch_attempts",
        "application_launches",
        "file_reads",
        "file_bytes_read",
        "file_writes",
        "file_bytes_written",
        "exec_commands",
        "exec_timeouts",
    ];
    fn unknown() -> crate::BrokerError {
        crate::BrokerError::unknown("private input receipt was invalid; do not replay")
    }
    fn hex_id(value: &str, bytes: usize) -> bool {
        value.len() == bytes * 2
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    }
    fn number(value: &Value) -> Option<u64> {
        value.as_u64().filter(|value| *value <= MAX_COUNTER)
    }
    fn safe_metrics(value: &Value) -> Result<serde_json::Map<String, Value>, crate::BrokerError> {
        let mut metrics = serde_json::Map::new();
        if let Some(source) = value.get("metrics") {
            let source = source.as_object().ok_or_else(unknown)?;
            if let Some(action) = source.get("action_id") {
                let action = action
                    .as_str()
                    .filter(|action| hex_id(action, 16))
                    .ok_or_else(unknown)?;
                metrics.insert("action_id".into(), Value::String(action.into()));
            }
            if let Some(duration) = source.get("duration_ms").and_then(number) {
                metrics.insert("duration_ms".into(), duration.into());
            }
            for category in ["delta", "totals"] {
                if let Some(counters) = source.get(category).and_then(Value::as_object) {
                    let safe = COUNTERS
                        .iter()
                        .filter_map(|key| {
                            counters
                                .get(*key)
                                .and_then(number)
                                .map(|value| ((*key).into(), value.into()))
                        })
                        .collect();
                    metrics.insert(category.into(), Value::Object(safe));
                }
            }
            if let Some(lower_bounds) = source
                .get("counts_are_lower_bounds")
                .and_then(Value::as_bool)
            {
                metrics.insert("counts_are_lower_bounds".into(), lower_bounds.into());
            }
        }
        Ok(metrics)
    }
    if bytes.len() > MAX_BYTES {
        return Err(unknown());
    }
    let value: Value = serde_json::from_slice(bytes).map_err(|_| unknown())?;
    if let Some(error) = value.get("error") {
        let no_effect = error.get("effect").and_then(Value::as_str) == Some("none");
        let mut metrics = safe_metrics(&value)?;
        let mut error = if no_effect {
            crate::BrokerError::invalid(
                "private input target or grant was rejected; obtain a fresh browser observation",
            )
        } else {
            metrics.insert("counts_are_lower_bounds".into(), true.into());
            crate::BrokerError::unknown("private input outcome is uncertain; do not replay")
        };
        if !metrics.is_empty() {
            error.metrics = Some(Value::Object(metrics));
        }
        return Err(error);
    }
    let content = value
        .get("content")
        .and_then(Value::as_array)
        .ok_or_else(unknown)?;
    if content.len() != 1 || content[0].get("type").and_then(Value::as_str) != Some("text") {
        return Err(unknown());
    }
    let text = content[0]
        .get("text")
        .and_then(Value::as_str)
        .ok_or_else(unknown)?;
    let view: Value = serde_json::from_str(text).map_err(|_| unknown())?;
    if view.get("filled") != Some(&Value::Bool(true))
        || view.get("submitted") != Some(&Value::Bool(false))
        || view.get("surface").and_then(Value::as_str) != Some("browser")
        || view
            .get("privacy")
            .and_then(|privacy| privacy.get("active"))
            != Some(&Value::Bool(true))
    {
        return Err(unknown());
    }
    let observation = view
        .get("observation_id")
        .and_then(Value::as_str)
        .ok_or_else(unknown)?;
    if !hex_id(observation, 16) {
        return Err(unknown());
    }
    let refs = view
        .get("action_refs")
        .and_then(Value::as_array)
        .ok_or_else(unknown)?;
    if refs.len() > MAX_REFS {
        return Err(unknown());
    }
    let mut unique_refs = std::collections::HashSet::new();
    for reference in refs {
        let reference = reference.as_str().ok_or_else(unknown)?;
        if !reference
            .strip_prefix("ref_")
            .is_some_and(|suffix| hex_id(suffix, 8))
            || !unique_refs.insert(reference)
        {
            return Err(unknown());
        }
    }
    // Reconstruct instead of forwarding: even a structurally valid receipt may
    // contain guest prose, screenshots or strings under unknown metric names.
    let safe_view = serde_json::json!({
        "filled": true,
        "submitted": false,
        "surface": "browser",
        "observation_id": observation,
        "action_refs": refs,
        "privacy": {
            "active": true,
            "message": "Screenshots and semantic content are withheld. You may explicitly click an already observed action ref, or navigate away. A new document releases the privacy guard."
        }
    });
    let metrics = safe_metrics(&value)?;
    Ok(crate::WorkerCallResponse {
        content: vec![serde_json::json!({"type": "text", "text": safe_view.to_string()})],
        metrics: Value::Object(metrics),
    })
}

// Static program text, never assembled from model inputs. The credential is read
// on stdin, atomically stored, and never printed or put in the process arguments.
const INSTALL_CREDENTIALS: &str = r#"
import json, os, pathlib, pwd, subprocess, sys, tempfile
data = json.load(sys.stdin)
sudoers = pathlib.Path('/etc/sudoers.d/meerkat-computer')
sudo_rule = '# Managed by Meerkat Computer guest bootstrap\nmeerkat-computer ALL=(ALL:ALL) NOPASSWD: ALL\n'
if sudoers.is_symlink() or (sudoers.exists() and (not sudoers.is_file() or sudoers.read_text() != sudo_rule)):
    raise RuntimeError('Refusing to replace an unrecognized sudoers entry')
base = pathlib.Path('/var/lib/meerkat-computer')
user = pwd.getpwnam('meerkat-computer')
legacy_env = 'MEERKAT_COMPUTER_WORKER_PORT=' + str(data['port']) + '\n'
worker_env = legacy_env + 'MEERKAT_COMPUTER_WIDTH=' + str(data['width']) + '\n' + 'MEERKAT_COMPUTER_HEIGHT=' + str(data['height']) + '\n'
# Validate every established value before any filesystem mutation, including
# sudoers. A token mismatch must not partially upgrade the environment.
values = {'worker.env': worker_env, 'worker.token': data['token']}
unchanged = set()
for name, value in values.items():
    path = base / name
    if path.exists():
        existing = path.read_text()
        if existing == value:
            unchanged.add(name)
        elif name != 'worker.env' or existing != legacy_env:
            raise RuntimeError('Existing worker bootstrap differs; refusing credential replacement')
def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
if data['guest_admin']:
    sudoers.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.meerkat-computer-', dir=sudoers.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(sudo_rule)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o440)
        os.chown(temporary, 0, 0)
        subprocess.run(['/usr/sbin/visudo', '-cf', temporary], check=True, capture_output=True, timeout=5)
        os.replace(temporary, sudoers)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
elif sudoers.exists():
    sudoers.unlink()
base.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(base, user.pw_uid, user.pw_gid)
# worker.token opens the enabled service's boot condition. Make the required
# environment durable first, including when resuming a partial initial install.
for name, value in values.items():
    path = base / name
    if name in unchanged:
        with path.open('rb') as stream:
            os.fsync(stream.fileno())
        sync_directory(base)
        continue
    fd, temporary = tempfile.mkstemp(dir=base)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.chmod(temporary, 0o600)
            os.chown(temporary, user.pw_uid, user.pw_gid)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(base)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{ObservationConfig, ObservationMode};

    #[cfg(unix)]
    fn bootstrap_publication_fixture(scenario: &str) -> Value {
        // Execute the production program with filesystem/identity adapters. Observe
        // each real atomic rename, including the instant the boot condition opens.
        const HARNESS: &str = r#"
import io, json, os, pathlib, pwd, stat, subprocess, sys, types
program, directory, scenario = sys.argv[1:]
real_path = type(pathlib.Path())
root = real_path(directory)
base = root / 'state'
base.mkdir()
expected_env = 'MEERKAT_COMPUTER_WORKER_PORT=18765\nMEERKAT_COMPUTER_WIDTH=1280\nMEERKAT_COMPUTER_HEIGHT=720\n'
legacy_env = 'MEERKAT_COMPUTER_WORKER_PORT=18765\n'
data = {'token': 'synthetic-new-worker-token-' + 'a' * 32, 'port': 18765, 'width': 1280, 'height': 720, 'guest_admin': False}
if scenario == 'token_mismatch':
    (base / 'worker.token').write_text('synthetic-established-token-' + 'b' * 32)
    (base / 'worker.env').write_text(legacy_env)
    (root / 'sudoers').write_text('# Managed by Meerkat Computer guest bootstrap\nmeerkat-computer ALL=(ALL:ALL) NOPASSWD: ALL\n')
if scenario == 'env_mismatch':
    (base / 'worker.env').write_text('MEERKAT_COMPUTER_WORKER_PORT=12345\n')
if scenario == 'complete_env':
    (base / 'worker.env').write_text(expected_env)
if scenario == 'legacy_upgrade':
    (base / 'worker.token').write_text(data['token'])
    (base / 'worker.env').write_text(legacy_env)
original_env = (base / 'worker.env').read_bytes() if (base / 'worker.env').exists() else None
original_sudoers = (root / 'sudoers').read_bytes() if (root / 'sudoers').exists() else None
def mapped_path(value):
    if value == '/var/lib/meerkat-computer':
        return base
    if value == '/etc/sudoers.d/meerkat-computer':
        return root / 'sudoers'
    raise AssertionError('unexpected bootstrap path')
pathlib.Path = mapped_path
pwd.getpwnam = lambda name: types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
def owned_chown(path, uid, gid):
    assert real_path(path).is_relative_to(root)
    assert uid == os.getuid() and gid == os.getgid()
os.chown = owned_chown
def forbidden_process(*args, **kwargs):
    raise AssertionError('unexpected bootstrap subprocess')
subprocess.run = forbidden_process
real_replace = os.replace
real_fsync = os.fsync
activation_observations = []
activation_durability = []
publications = []
env_durable = False
token_durable = False
def observed_replace(source, target):
    global env_durable, token_durable
    source, target = real_path(source), real_path(target)
    assert source.is_relative_to(root) and target.is_relative_to(root)
    if scenario == 'interrupt_env' and target.name == 'worker.env':
        raise InterruptedError('synthetic publication interruption')
    real_replace(source, target)
    publications.append(target.name)
    if target.name == 'worker.env':
        env_durable = False
    if target.name == 'worker.token':
        token_durable = False
    if (base / 'worker.token').exists():
        env = base / 'worker.env'
        activation_observations.append(env.exists() and env.read_text() == expected_env)
        activation_durability.append(env_durable)
os.replace = observed_replace
def observed_fsync(fd):
    global env_durable, token_durable
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        assert os.fstat(fd).st_ino == os.stat(base).st_ino
        if scenario == 'interrupt_env_sync' and not (base / 'worker.token').exists():
            raise OSError('synthetic directory sync failure')
        real_fsync(fd)
        env = base / 'worker.env'
        env_durable = env.exists() and env.read_text() == expected_env
        token_durable = (base / 'worker.token').exists()
    else:
        real_fsync(fd)
os.fsync = observed_fsync
sys.stdin = io.StringIO(json.dumps(data))
failed = False
try:
    exec(compile(program, 'INSTALL_CREDENTIALS', 'exec'), {})
except (RuntimeError, OSError):
    failed = True
env = base / 'worker.env'
print(json.dumps({
    'failed': failed,
    'activation_observations': activation_observations,
    'activation_durability': activation_durability,
    'token_durable': token_durable,
    'token_present': (base / 'worker.token').exists(),
    'env_complete': env.exists() and env.read_text() == expected_env,
    'env_unchanged': (env.read_bytes() if env.exists() else None) == original_env,
    'sudoers_unchanged': ((root / 'sudoers').read_bytes() if (root / 'sudoers').exists() else None) == original_sudoers,
    'publications': publications,
}))
"#;
        let directory = tempfile::tempdir().unwrap();
        let output = std::process::Command::new("python3")
            .args(["-c", HARNESS, INSTALL_CREDENTIALS])
            .arg(directory.path())
            .arg(scenario)
            .output()
            .unwrap();
        assert!(output.status.success(), "bootstrap fixture failed");
        serde_json::from_slice(&output.stdout).unwrap()
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_activation_token_never_precedes_complete_environment() {
        let result = bootstrap_publication_fixture("first_install");
        assert_eq!(result["failed"], false);
        assert_eq!(result["token_present"], true);
        assert_eq!(result["env_complete"], true);
        let observations = result["activation_observations"].as_array().unwrap();
        assert!(!observations.is_empty());
        assert!(observations.iter().all(|ready| ready == true), "{result}");
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_interrupted_environment_publication_leaves_no_activation_token() {
        let result = bootstrap_publication_fixture("interrupt_env");
        assert_eq!(result["failed"], true);
        assert_eq!(result["token_present"], false);
        assert_eq!(result["env_complete"], false);
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_environment_directory_is_durable_before_activation_token_publication() {
        let result = bootstrap_publication_fixture("first_install");
        assert_eq!(result["failed"], false);
        let observations = result["activation_durability"].as_array().unwrap();
        assert!(!observations.is_empty());
        assert!(observations.iter().all(|ready| ready == true), "{result}");
        assert_eq!(result["token_durable"], true);
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_failed_environment_directory_sync_leaves_no_activation_token() {
        let result = bootstrap_publication_fixture("interrupt_env_sync");
        assert_eq!(result["failed"], true);
        assert_eq!(result["token_present"], false);
        assert_eq!(result["env_complete"], true);
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_token_mismatch_does_not_partially_upgrade_environment() {
        let result = bootstrap_publication_fixture("token_mismatch");
        assert_eq!(result["failed"], true);
        assert_eq!(result["env_unchanged"], true);
        assert_eq!(result["sudoers_unchanged"], true);
        assert_eq!(result["publications"], serde_json::json!([]));
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_environment_mismatch_does_not_publish_an_activation_token() {
        let result = bootstrap_publication_fixture("env_mismatch");
        assert_eq!(result["failed"], true);
        assert_eq!(result["token_present"], false);
        assert_eq!(result["env_unchanged"], true);
        assert_eq!(result["publications"], serde_json::json!([]));
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_established_token_allows_legacy_environment_upgrade() {
        let result = bootstrap_publication_fixture("legacy_upgrade");
        assert_eq!(result["failed"], false);
        assert_eq!(result["token_present"], true);
        assert_eq!(result["env_complete"], true);
        assert_eq!(result["publications"], serde_json::json!(["worker.env"]));
    }

    #[cfg(unix)]
    #[test]
    fn bootstrap_resumed_install_syncs_existing_environment_before_activation() {
        let result = bootstrap_publication_fixture("complete_env");
        assert_eq!(result["failed"], false);
        assert_eq!(result["activation_durability"], serde_json::json!([true]));
        assert_eq!(result["publications"], serde_json::json!(["worker.token"]));
        assert_eq!(result["token_durable"], true);
    }

    #[cfg(unix)]
    pub(super) fn write_incus_fixture(
        directory: &std::path::Path,
        body: &str,
    ) -> std::path::PathBuf {
        // A concurrent fork can briefly inherit the body's writable descriptor.
        // Execute the stable launcher; Python reads this body as ordinary data.
        std::fs::write(directory.join("incus-fixture"), body).unwrap();
        let executable = directory.join("incus-launcher");
        std::os::unix::fs::symlink(
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/tests/fixtures/incus_launcher.py"
            ),
            &executable,
        )
        .unwrap();
        executable
    }

    #[cfg(unix)]
    fn command_fixture(body: &str) -> (tempfile::TempDir, IncusBackend, std::path::PathBuf) {
        let directory = tempfile::tempdir().unwrap();
        let pid_path = directory.path().join("child.pid");
        let executable = write_incus_fixture(
            directory.path(),
            &format!(
                "#!/usr/bin/env python3\nimport os, sys, time\nwith open({}, 'w') as output:\n    output.write(str(os.getpid()))\n{body}\n",
                serde_json::to_string(&pid_path).unwrap(),
            ),
        );
        let mut config = pool();
        config.incus_binary = executable.to_str().unwrap().to_owned();
        config.ready_timeout_seconds = 2;
        (directory, IncusBackend::new(config).unwrap(), pid_path)
    }

    #[cfg(unix)]
    fn assert_command_reaped(pid_path: &std::path::Path) {
        let pid: i32 = std::fs::read_to_string(pid_path).unwrap().parse().unwrap();
        // A zombie still answers kill(pid, 0), so this also checks that we waited.
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1, "command still exists");
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }

    #[cfg(unix)]
    fn stop_fixture(
        mode: &str,
        branch: bool,
        status: &str,
    ) -> (tempfile::TempDir, IncusBackend, BackendComputer) {
        let (directory, backend, _) = command_fixture(
            r#"
import json, pathlib
root = pathlib.Path(__file__).parent
settings = json.loads((root / 'stop.json').read_text())
handle = settings['handle']
args = sys.argv[1:]
assert args.pop(0) == '--force-local'
project = handle.get('incus_project', 'computers')
if args[:1] == ['--project']:
    assert args[1] == project
    args = args[2:]
operation = args.pop(0)
sent = root / 'stop-sent.json'
config = {'user.meerkat-computer.managed':'1', 'user.meerkat-computer.id':handle['computer_id'], 'user.meerkat-computer.owner':handle['ownership_id'], 'user.meerkat-computer.fork-pool':'computers', 'volatile.uuid':handle['instance_id']}
if settings['mode'] == 'wrong_owner':
    config['user.meerkat-computer.owner'] = 'another-owner'
if operation == 'query':
    assert args == ['/1.0/projects/' + project]
    print(json.dumps({'name':project, 'config':config}))
elif operation == 'list':
    status = settings['status']
    if sent.exists():
        if settings['mode'] == 'replacement':
            config['volatile.uuid'] = 'replacement-incarnation'
        elif settings['mode'] not in ['no_stop', 'lost_ack_no_stop', 'blocked']:
            status = 'Stopped'
    print(json.dumps([{'name':handle['instance_name'], 'status':status, 'type':'virtual-machine', 'config':config}]))
elif operation in ['exec', 'stop']:
    assert not sent.exists(), 'shutdown effect was replayed'
    sent.write_text(json.dumps([operation] + args))
    if settings['mode'] == 'blocked':
        time.sleep(60)
    if settings['mode'] in ['lost_ack', 'lost_ack_no_stop']:
        sys.exit(7)
else:
    raise AssertionError(operation)
"#,
        );
        let mut handle = instance().handle(
            "1234567890abcdef1234567890abcdef",
            "durable-creation-intent",
        );
        if branch {
            handle.incus_project = Some("mcf-1234567890abcdef1234567890abcdef".into());
        }
        std::fs::write(
            directory.path().join("stop.json"),
            serde_json::to_vec(&serde_json::json!({"handle":handle,"mode":mode,"status":status}))
                .unwrap(),
        )
        .unwrap();
        (directory, backend, handle)
    }

    #[cfg(unix)]
    fn stop_effect(directory: &tempfile::TempDir) -> Vec<String> {
        serde_json::from_slice(&std::fs::read(directory.path().join("stop-sent.json")).unwrap())
            .unwrap()
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn restored_vm_stop_uses_one_guest_poweroff_and_requires_observed_stop() {
        let (directory, backend, handle) = stop_fixture("normal", true, "Running");
        backend.stop(&handle).await.unwrap();
        assert_eq!(
            stop_effect(&directory),
            [
                "exec",
                &handle.instance_name,
                "--",
                "systemctl",
                "poweroff",
                "--no-block"
            ]
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn restored_vm_stop_lost_ack_is_resolved_by_exact_stopped_observation() {
        let (directory, backend, handle) = stop_fixture("lost_ack", true, "Running");
        backend.stop(&handle).await.unwrap();
        assert_eq!(stop_effect(&directory)[0], "exec");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn restored_vm_stop_timeout_never_retries_or_forces() {
        let (directory, backend, handle) = stop_fixture("no_stop", true, "Running");
        let error = backend.stop(&handle).await.unwrap_err();
        assert!(matches!(error, BackendError::CommandTimeout { operation } if operation == "stop"));
        assert_eq!(stop_effect(&directory)[0], "exec");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn restored_vm_stop_lost_ack_without_stopped_evidence_remains_failed() {
        let (directory, backend, handle) = stop_fixture("lost_ack_no_stop", true, "Running");
        let error = backend.stop(&handle).await.unwrap_err();
        assert!(
            matches!(error, BackendError::CommandFailed { operation, .. } if operation == "stop")
        );
        assert_eq!(stop_effect(&directory)[0], "exec");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn restored_vm_stop_rejects_changed_ownership_and_replacement_incarnation() {
        let (directory, backend, handle) = stop_fixture("wrong_owner", true, "Running");
        assert!(matches!(
            backend.stop(&handle).await,
            Err(BackendError::OwnershipMismatch { .. })
        ));
        assert!(!directory.path().join("stop-sent.json").exists());
        let (directory, backend, handle) = stop_fixture("replacement", true, "Running");
        assert!(matches!(
            backend.stop(&handle).await,
            Err(BackendError::OwnershipMismatch { .. })
        ));
        assert_eq!(stop_effect(&directory)[0], "exec");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn stop_preserves_normal_guest_and_frozen_instance_behavior() {
        let (directory, backend, handle) = stop_fixture("normal", false, "Running");
        backend.stop(&handle).await.unwrap();
        assert_eq!(
            stop_effect(&directory),
            ["stop", &handle.instance_name, "--timeout", "2"]
        );
        let (directory, backend, handle) = stop_fixture("normal", true, "Frozen");
        assert!(matches!(
            backend.stop(&handle).await,
            Err(BackendError::InvalidBackendState(_))
        ));
        assert!(!directory.path().join("stop-sent.json").exists());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn cancelling_restored_vm_stop_reaps_the_command_without_a_second_effect() {
        let (directory, backend, handle) = stop_fixture("blocked", true, "Running");
        let sent = directory.path().join("stop-sent.json");
        let task = tokio::spawn(async move { backend.stop(&handle).await });
        timeout(Duration::from_secs(2), async {
            while !sent.exists() {
                sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        timeout(Duration::from_secs(2), async {
            let pid: i32 = std::fs::read_to_string(directory.path().join("child.pid"))
                .unwrap()
                .parse()
                .unwrap();
            while unsafe { libc::kill(pid, 0) } == 0 {
                sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(stop_effect(&directory)[0], "exec");
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn command_stops_on_stdout_overflow_before_process_exit() {
        let (_directory, backend, pid) = command_fixture(
            "for _ in range(257):\n    sys.stdout.buffer.write(b'x' * 65536)\n    sys.stdout.buffer.flush()\ntime.sleep(60)",
        );
        let error = backend
            .command("bounded output", &[], None)
            .await
            .unwrap_err();
        assert!(
            matches!(error, BackendError::InvalidBackendState(_)),
            "{error}"
        );
        assert_command_reaped(&pid);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn command_drains_stdout_while_writing_private_stdin() {
        let (_directory, backend, pid) = command_fixture(
            "sys.stdout.buffer.write(b'x' * 1048576)\nsys.stdout.buffer.flush()\npayload = sys.stdin.buffer.read()\nassert payload == b'y' * 1048576\nsys.stdout.buffer.write(b'ok')",
        );
        let response = backend
            .command("duplex input", &[], Some(vec![b'y'; 1024 * 1024]))
            .await
            .unwrap();
        assert_eq!(response.len(), 1024 * 1024 + 2);
        assert!(response.ends_with(b"ok"));
        assert_command_reaped(&pid);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn command_timeout_reaps_child_even_when_private_stdin_is_blocked() {
        let (_directory, backend, pid) = command_fixture("time.sleep(60)");
        let error = backend
            .command("blocked input", &[], Some(vec![b's'; 1024 * 1024]))
            .await
            .unwrap_err();
        assert!(matches!(error, BackendError::CommandTimeout { .. }));
        assert_command_reaped(&pid);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn command_discards_stderr_and_never_returns_guest_diagnostics() {
        let (_directory, backend, pid) = command_fixture(
            "for _ in range(512):\n    sys.stderr.buffer.write(b'SYNTHETIC_PRIVATE_DIAGNOSTIC' * 4096)\nsys.stderr.buffer.flush()\nsys.stdout.write('SYNTHETIC_PRIVATE_RESPONSE')\nsys.exit(7)",
        );
        let error = backend
            .command("guest fixture", &[], None)
            .await
            .unwrap_err();
        assert!(matches!(error, BackendError::CommandFailed { .. }));
        assert!(!error.to_string().contains("SYNTHETIC_PRIVATE"));
        assert_command_reaped(&pid);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn query_uses_url_project_scope_without_unsupported_global_flag() {
        let (_directory, backend, _) =
            command_fixture("import json\nprint(json.dumps(sys.argv[1:]))");
        let path = "/1.0/instances/mc-fixture/state?project=computers";
        let output = backend
            .simple("scoped query", &["query", path])
            .await
            .unwrap();
        let arguments: Vec<String> = serde_json::from_slice(&output).unwrap();
        assert_eq!(arguments, vec!["--force-local", "query", path]);
        let output = backend
            .simple("inventory", &["list", "--format=json"])
            .await
            .unwrap();
        let arguments: Vec<String> = serde_json::from_slice(&output).unwrap();
        assert_eq!(
            arguments,
            vec![
                "--force-local",
                "--project",
                "computers",
                "list",
                "--format=json"
            ]
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn all_project_inventory_omits_mutually_exclusive_project_flag() {
        let (_directory, backend, _) =
            command_fixture("import json\nprint(json.dumps(sys.argv[1:]))");
        let output = backend
            .simple("inventory", &["list", "--all-projects", "--format=json"])
            .await
            .unwrap();
        let arguments: Vec<String> = serde_json::from_slice(&output).unwrap();
        assert_eq!(
            arguments,
            vec!["--force-local", "list", "--all-projects", "--format=json"]
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn command_fixture_body_may_remain_open_for_writing() {
        let (directory, backend, pid) = command_fixture("sys.stdout.write('fixture-ready')");
        // A parallel fork can retain a writer after the creating thread closes it.
        // Fixture bodies are data for the interpreter, not executable inodes.
        let _writer = std::fs::OpenOptions::new()
            .write(true)
            .open(directory.path().join("incus-fixture"))
            .unwrap();
        let output = backend.command("fixture writer", &[], None).await.unwrap();
        assert_eq!(output, b"fixture-ready");
        assert_command_reaped(&pid);
    }

    #[cfg(unix)]
    struct ReadinessFixture {
        directory: tempfile::TempDir,
        backend: IncusBackend,
        specs: [ComputerSpec; 2],
        server: tokio::task::JoinHandle<()>,
        health_requests: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    }

    #[cfg(unix)]
    impl Drop for ReadinessFixture {
        fn drop(&mut self) {
            self.server.abort();
        }
    }

    #[cfg(unix)]
    async fn readiness_fixture(
        bootstrap_barrier: bool,
        stopped: bool,
        max_running: usize,
    ) -> ReadinessFixture {
        use axum::{Json, extract::State, http::HeaderMap, routing::get};
        use std::{collections::HashSet, sync::Arc};
        #[derive(Clone)]
        struct Health {
            seen: Arc<Mutex<HashSet<String>>>,
            notify: Arc<tokio::sync::Notify>,
            needed: usize,
            requests: Arc<std::sync::atomic::AtomicUsize>,
        }
        let health_requests = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let health = Health {
            seen: Arc::new(Mutex::new(HashSet::new())),
            notify: Arc::new(tokio::sync::Notify::new()),
            needed: max_running.min(2),
            requests: health_requests.clone(),
        };
        let app = axum::Router::new()
            .route(
                "/health",
                get(
                    |State(health): State<Health>, headers: HeaderMap| async move {
                        health
                            .requests
                            .fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                        health
                            .seen
                            .lock()
                            .await
                            .insert(headers["host"].to_str().unwrap().to_owned());
                        health.notify.notify_waiters();
                        loop {
                            let notified = health.notify.notified();
                            if health.seen.lock().await.len() >= health.needed {
                                break;
                            }
                            notified.await;
                        }
                        Json(serde_json::json!({"protocol_version":1,"ready":true}))
                    },
                ),
            )
            .with_state(health);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let proxy = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let directory = tempfile::tempdir().unwrap();
        let executable = write_incus_fixture(
            directory.path(),
            include_str!("../tests/fixtures/incus_readiness.py"),
        );
        let first = spec();
        let mut second = first.clone();
        second.computer_id = "abcdef1234567890abcdef1234567890".into();
        second.ownership_id = "second-durable-intent".into();
        let specs = [first, second];
        let mut addresses = serde_json::Map::new();
        let instances: Vec<Value> = specs.iter().enumerate().map(|(index, spec)| {
            let name = instance_name(&spec.computer_id).unwrap();
            addresses.insert(name.clone(), serde_json::json!(format!("10.211.0.{}", index + 2)));
            serde_json::json!({"name":name,"type":"container","status":if stopped {"Stopped"} else {"Running"},
                "config": {MANAGED_KEY:"1",COMPUTER_KEY:spec.computer_id,OWNER_KEY:spec.ownership_id,
                    "volatile.uuid":format!("incarnation-{index}"),"volatile.base_image":spec.profile.image,
                    "limits.cpu":"2","limits.memory":"1024MiB","security.idmap.isolated":"true"},
                "expanded_devices":{"root":{"size":"10GiB"},"eth0":{"type":"nic","network":"mcbr0",
                    "security.port_isolation":"true","security.ipv4_filtering":"true","security.ipv6_filtering":"true","security.mac_filtering":"true"}}
            })
        }).collect();
        std::fs::write(
            directory.path().join("inventory.json"),
            serde_json::to_vec(&instances).unwrap(),
        )
        .unwrap();
        let settings = serde_json::json!({"addresses":addresses,"bootstrap_barrier":bootstrap_barrier,
            "network":{"name":"mcbr0","type":"bridge","managed":true,"config":{
                HOST_OWNER_KEY:"pool-owner","ipv4.address":"10.211.0.1/24","ipv4.nat":"true","ipv6.address":"none",
                "security.acls":"mcbr0-isolation","security.acls.default.ingress.action":"reject","security.acls.default.egress.action":"allow"}},
            "acl":{"name":"mcbr0-isolation","config":{HOST_OWNER_KEY:"pool-owner"},
                "ingress":[{"action":"allow","state":"enabled","source":"10.211.0.1/32"}],
                "egress":[{"action":"reject","state":"enabled","destination":DENIED_EGRESS}]}
        });
        std::fs::write(
            directory.path().join("fixture.json"),
            serde_json::to_vec(&settings).unwrap(),
        )
        .unwrap();
        let mut config = pool();
        config.incus_binary = executable.to_str().unwrap().to_owned();
        config.max_running = max_running;
        config.ready_timeout_seconds = 5;
        let mut backend = IncusBackend::new(config).unwrap();
        // A local HTTP proxy fixture serves the real private-IP health requests without
        // changing production URL validation or requiring host network reconfiguration.
        backend.client = reqwest::Client::builder()
            .proxy(reqwest::Proxy::all(proxy).unwrap())
            .timeout(Duration::from_secs(3))
            .build()
            .unwrap();
        ReadinessFixture {
            directory,
            backend,
            specs,
            server,
            health_requests,
        }
    }

    #[cfg(unix)]
    fn manager_fixture_settings(fixture: &ReadinessFixture, changes: Value) {
        let path = fixture.directory.path().join("fixture.json");
        let mut settings: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        settings
            .as_object_mut()
            .unwrap()
            .extend(changes.as_object().unwrap().clone());
        std::fs::write(path, serde_json::to_vec(&settings).unwrap()).unwrap();
    }

    #[cfg(unix)]
    fn fixture_calls(fixture: &ReadinessFixture, label: &str) -> usize {
        let name = instance_name(&fixture.specs[0].computer_id).unwrap();
        std::fs::read_to_string(fixture.directory.path().join(format!("{label}-{name}")))
            .map(|value| value.parse().unwrap())
            .unwrap_or(0)
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn manager_readiness_gates_bootstrap_and_start_until_nonempty_property_response() {
        let fixture = readiness_fixture(false, false, 1).await;
        manager_fixture_settings(
            &fixture,
            serde_json::json!({"require_manager":true,"manager_failures":2,"manager_empty":1}),
        );
        fixture.backend.ensure(&fixture.specs[0]).await.unwrap();
        assert_eq!(fixture_calls(&fixture, "manager-probes"), 4);
        assert_eq!(fixture_calls(&fixture, "service-starts"), 1);
        assert!(
            !fixture
                .directory
                .path()
                .join("bootstrap-before-manager")
                .exists()
        );
        assert!(
            !fixture
                .directory
                .path()
                .join("service-start-before-manager")
                .exists()
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn manager_readiness_unavailable_expires_without_bootstrap_or_service_start() {
        // Keep the fixture deadline: preparatory subprocesses must reach the
        // manager probe even when the rest of the suite runs in parallel.
        let fixture = readiness_fixture(false, false, 1).await;
        manager_fixture_settings(
            &fixture,
            serde_json::json!({"require_manager":true,"manager_unavailable":true}),
        );
        let result = fixture.backend.ensure(&fixture.specs[0]).await;
        assert!(matches!(result, Err(BackendError::WorkerNotReady { .. })));
        assert!(fixture_calls(&fixture, "manager-probes") > 0);
        assert_eq!(fixture_calls(&fixture, "service-starts"), 0);
        let name = instance_name(&fixture.specs[0].computer_id).unwrap();
        assert!(
            !fixture
                .directory
                .path()
                .join(format!("bootstrapped-{name}"))
                .exists()
        );
        assert_eq!(
            fixture
                .health_requests
                .load(std::sync::atomic::Ordering::SeqCst),
            0
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn manager_readiness_does_not_retry_or_hide_failed_service_start() {
        let fixture = readiness_fixture(false, false, 1).await;
        manager_fixture_settings(&fixture, serde_json::json!({"service_start_fails":true}));
        let error = fixture.backend.ensure(&fixture.specs[0]).await.unwrap_err();
        assert!(matches!(error, BackendError::CommandFailed { .. }));
        assert_eq!(
            error.diagnostic(),
            BackendFailureDiagnostic {
                category: BackendFailureCategory::CommandFailed,
                operation: Some(BackendOperation::WorkerServiceStart),
            }
        );
        assert_eq!(fixture_calls(&fixture, "service-starts"), 1);
        // This fixture's health endpoint would report ready if queried. An uncertain
        // failed start must not be converted to success by that observation.
        assert_eq!(
            fixture
                .health_requests
                .load(std::sync::atomic::Ordering::SeqCst),
            0
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn different_guests_bootstrap_and_worker_readiness_overlap() {
        let fixture = readiness_fixture(true, false, 2).await;
        let (first, second) = tokio::join!(
            fixture.backend.ensure(&fixture.specs[0]),
            fixture.backend.ensure(&fixture.specs[1])
        );
        assert!(
            first.is_ok() && second.is_ok(),
            "independent guest readiness was serialized: {first:?}, {second:?}"
        );
        assert!(
            !fixture
                .directory
                .path()
                .join("admission-overlapped")
                .exists()
        );
        for spec in &fixture.specs {
            assert!(
                fixture
                    .directory
                    .path()
                    .join(format!(
                        "bootstrapped-{}",
                        instance_name(&spec.computer_id).unwrap()
                    ))
                    .exists()
            );
        }
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn different_guests_restart_worker_readiness_overlap() {
        let fixture = readiness_fixture(false, false, 2).await;
        let first = fixture
            .backend
            .inspect(&fixture.specs[0])
            .await
            .unwrap()
            .unwrap();
        let second = fixture
            .backend
            .inspect(&fixture.specs[1])
            .await
            .unwrap()
            .unwrap();
        let (first, second) = tokio::join!(
            fixture.backend.start(&first),
            fixture.backend.start(&second)
        );
        assert!(
            first.is_ok() && second.is_ok(),
            "independent guest health waits were serialized: {first:?}, {second:?}"
        );
        assert!(
            !fixture
                .directory
                .path()
                .join("admission-overlapped")
                .exists()
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn readiness_fixture_body_may_remain_open_for_writing() {
        let fixture = readiness_fixture(false, false, 1).await;
        let _writer = std::fs::OpenOptions::new()
            .write(true)
            .open(fixture.directory.path().join("incus-fixture"))
            .unwrap();
        let handle = fixture.backend.inspect(&fixture.specs[0]).await.unwrap();
        assert!(handle.is_some());
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn concurrent_cold_starts_preserve_single_slot_admission() {
        let fixture = readiness_fixture(false, true, 1).await;
        let first = fixture
            .backend
            .inspect(&fixture.specs[0])
            .await
            .unwrap()
            .unwrap();
        let second = fixture
            .backend
            .inspect(&fixture.specs[1])
            .await
            .unwrap()
            .unwrap();
        let (first, second) = tokio::join!(
            fixture.backend.start(&first),
            fixture.backend.start(&second)
        );
        assert_eq!(usize::from(first.is_ok()) + usize::from(second.is_ok()), 1);
        assert!(
            matches!(first, Err(BackendError::AdmissionDenied { .. }))
                || matches!(second, Err(BackendError::AdmissionDenied { .. }))
        );
        assert!(
            !fixture
                .directory
                .path()
                .join("admission-overlapped")
                .exists()
        );
    }

    fn pool() -> PoolConfig {
        PoolConfig {
            backend: "incus".into(),
            incus_binary: "incus".into(),
            incus_project: "computers".into(),
            max_running: 2,
            min_available_memory_mib: 512,
            worker_port: 8000,
            ready_timeout_seconds: 30,
        }
    }

    fn spec() -> ComputerSpec {
        ComputerSpec {
            computer_id: "1234567890abcdef1234567890abcdef".into(),
            ownership_id: "durable-creation-intent".into(),
            worker_token: "worker-secret-never-in-process-arguments".into(),
            profile: ComputerProfile {
                image: "a".repeat(64),
                kind: ComputerKind::Container,
                cpus: 2,
                memory_mib: 1024,
                disk_gib: 10,
                guest_admin: false,
                transfers: Default::default(),
                capabilities: vec!["browser".into()],
                observation: ObservationConfig {
                    surface: Default::default(),
                    mode: ObservationMode::Visual,
                    width: 1280,
                    height: 800,
                    detail: Default::default(),
                    max_elements: 300,
                    max_text_chars: 24_000,
                },
                idle_stop_seconds: 900,
            },
        }
    }

    fn instance() -> Instance {
        let spec = spec();
        Instance {
            name: instance_name(&spec.computer_id).unwrap(),
            project: None,
            status: "Running".into(),
            stateful: false,
            kind: "container".into(),
            config: HashMap::from([
                (MANAGED_KEY.into(), "1".into()),
                (COMPUTER_KEY.into(), spec.computer_id),
                (OWNER_KEY.into(), spec.ownership_id),
                ("volatile.uuid".into(), "original-incarnation".into()),
            ]),
            expanded_config: HashMap::new(),
            expanded_devices: HashMap::new(),
        }
    }

    #[test]
    fn legacy_backend_handles_have_no_project_override() {
        let value = serde_json::json!({
            "instance_name": "mc-1234567890abcdef1234567890abcdef",
            "worker_url": "http://10.211.0.8:18765",
            "computer_id": "1234567890abcdef1234567890abcdef",
            "ownership_id": "owner",
            "instance_id": "incarnation",
        });
        let handle: BackendComputer = serde_json::from_value(value).unwrap();
        assert_eq!(handle.incus_project, None);
    }

    #[test]
    fn restored_branch_retains_instance_name_but_requires_its_exact_project() {
        let backend = IncusBackend::new(pool()).unwrap();
        let mut handle = instance().handle("1234567890abcdef1234567890abcdef", "owner");
        handle.computer_id = "abcdef1234567890abcdef1234567890".into();
        handle.incus_project = Some("mcf-abcdef1234567890abcdef1234567890".into());
        assert_eq!(
            backend.project_for(&handle).unwrap(),
            handle.incus_project.as_deref().unwrap()
        );
        // Retained legacy handles still authorize inspection and stopping.
        handle.incus_project = Some("computers-fork-abcdef1234567890abcdef1234567890".into());
        assert_eq!(
            backend.project_for(&handle).unwrap(),
            handle.incus_project.as_deref().unwrap()
        );
        handle.incus_project = Some("unrelated-project".into());
        assert!(matches!(
            backend.project_for(&handle),
            Err(BackendError::OwnershipMismatch { .. })
        ));
        handle.incus_project = Some("mcf-abcdef1234567890abcdef1234567891".into());
        assert!(backend.project_for(&handle).is_err());
        handle.incus_project = Some("other-pool-fork-abcdef1234567890abcdef1234567890".into());
        assert!(backend.project_for(&handle).is_err());
        handle.incus_project = Some("--project=default".into());
        assert!(backend.project_for(&handle).is_err());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn branch_commands_cannot_fall_back_to_pool_project() {
        let (_directory, backend, _) =
            command_fixture("import json\nprint(json.dumps(sys.argv[1:]))");
        let output = backend
            .simple_in(
                "mcf-abcdef1234567890abcdef1234567890",
                "stop",
                &["stop", "same-name"],
            )
            .await
            .unwrap();
        let arguments: Vec<String> = serde_json::from_slice(&output).unwrap();
        assert_eq!(
            arguments,
            vec![
                "--force-local",
                "--project",
                "mcf-abcdef1234567890abcdef1234567890",
                "stop",
                "same-name"
            ]
        );
    }

    #[test]
    fn commands_are_typed_and_never_include_worker_credentials() {
        let spec = spec();
        let args = init_args(&spec).unwrap();
        assert_eq!(
            &args[..3],
            &[
                "init",
                &"a".repeat(64),
                "mc-1234567890abcdef1234567890abcdef"
            ]
        );
        assert!(!args.iter().any(|arg| arg.contains(&spec.worker_token)));
        assert!(args.iter().any(|arg| arg == "root,size=10GiB"));
        assert!(args.iter().any(|arg| arg == "security.idmap.isolated=true"));
        assert!(
            args.iter()
                .any(|arg| arg == "user.meerkat-computer.guest-admin=0")
        );
        assert!(!format!("{spec:?}").contains(&spec.worker_token));
    }

    #[test]
    fn new_virtual_machines_reserve_full_state_checkpoint_space() {
        let mut spec = spec();
        spec.profile.kind = ComputerKind::VirtualMachine;
        let args = init_args(&spec).unwrap();
        assert!(args.iter().any(|arg| arg == "migration.stateful=true"));
        assert!(args.iter().any(|arg| arg == "root,size.state=1536MiB"));
    }

    #[test]
    fn remote_images_and_unsafe_names_are_refused() {
        let mut spec = spec();
        spec.profile.image = "images:ubuntu/24.04".into();
        assert!(init_args(&spec).is_err());
        assert!(instance_name("--project=somewhere-else").is_err());
        assert!(instance_name("a/../../unexpected").is_err());
    }

    #[test]
    fn only_the_owned_network_policy_allows_a_computer_to_start() {
        let network = serde_json::json!({
            "name": "mcbr0", "type": "bridge", "managed": true,
            "config": {
                HOST_OWNER_KEY: "pool-owner", "ipv4.address": "10.211.0.1/24",
                "ipv4.nat": "true", "ipv6.address": "none",
                "security.acls": "mcbr0-isolation",
                "security.acls.default.ingress.action": "reject",
                "security.acls.default.egress.action": "allow",
            },
        });
        let acl = serde_json::json!({
            "name": "mcbr0-isolation", "config": {HOST_OWNER_KEY: "pool-owner"},
            "ingress": [{"action": "allow", "state": "enabled", "source": "10.211.0.1/32", "description": ""}],
            "egress": [{"action": "reject", "state": "enabled", "destination": DENIED_EGRESS}],
        });
        assert_network_boundary(&network, &acl).unwrap();
        for field in [
            "security.acls",
            "security.acls.default.ingress.action",
            "ipv6.address",
        ] {
            let mut modified = network.clone();
            modified["config"].as_object_mut().unwrap().remove(field);
            assert!(assert_network_boundary(&modified, &acl).is_err());
        }
        let mut modified = acl.clone();
        modified["egress"][0]["destination"] = "10.0.0.0/8".into();
        assert!(assert_network_boundary(&network, &modified).is_err());
        modified = acl.clone();
        modified["config"][HOST_OWNER_KEY] = "another-pool".into();
        assert!(assert_network_boundary(&network, &modified).is_err());
        modified = acl;
        modified["ingress"][0]["source"] = "0.0.0.0/0".into();
        assert!(assert_network_boundary(&network, &modified).is_err());
    }

    #[test]
    fn a_headless_image_cannot_recover_as_a_desktop_computer() {
        let mut spec = spec();
        let mut actual = instance();
        actual.config.extend([
            ("volatile.base_image".into(), spec.profile.image.clone()),
            ("limits.cpu".into(), spec.profile.cpus.to_string()),
            ("security.idmap.isolated".into(), "true".into()),
            (
                "limits.memory".into(),
                format!("{}MiB", spec.profile.memory_mib),
            ),
        ]);
        actual.expanded_devices = HashMap::from([
            (
                "root".into(),
                HashMap::from([("size".into(), "10GiB".into())]),
            ),
            (
                "eth0".into(),
                HashMap::from([
                    ("type".into(), "nic".into()),
                    ("security.port_isolation".into(), "true".into()),
                    ("security.ipv4_filtering".into(), "true".into()),
                    ("security.ipv6_filtering".into(), "true".into()),
                    ("security.mac_filtering".into(), "true".into()),
                ]),
            ),
        ]);
        actual.assert_profile(&spec.profile).unwrap();
        spec.profile.guest_admin = true;
        assert!(actual.assert_profile(&spec.profile).is_err());
        actual.config.insert(GUEST_ADMIN_KEY.into(), "1".into());
        actual.assert_profile(&spec.profile).unwrap();
        spec.profile.guest_admin = false;
        assert!(actual.assert_profile(&spec.profile).is_err());
        actual.config.remove(GUEST_ADMIN_KEY);
        spec.profile.capabilities.push("desktop".into());
        assert!(actual.assert_profile(&spec.profile).is_err());
        actual
            .config
            .insert("image.user.meerkat-computer.desktop".into(), "1".into());
        actual.assert_profile(&spec.profile).unwrap();
    }

    #[test]
    fn name_alone_and_replaced_incarnations_never_authorize_effects() {
        let spec = spec();
        let mut actual = instance();
        actual
            .assert_owner(
                &spec.computer_id,
                &spec.ownership_id,
                Some("original-incarnation"),
            )
            .unwrap();
        actual
            .config
            .insert("volatile.uuid".into(), "replacement".into());
        assert!(matches!(
            actual.assert_owner(
                &spec.computer_id,
                &spec.ownership_id,
                Some("original-incarnation")
            ),
            Err(BackendError::OwnershipMismatch { .. })
        ));
        actual.config.remove(OWNER_KEY);
        assert!(
            actual
                .assert_owner(&spec.computer_id, &spec.ownership_id, None)
                .is_err()
        );
    }

    #[test]
    fn admission_counts_running_branches_across_projects() {
        let backend = IncusBackend::new(pool()).unwrap();
        let mut parent = instance();
        parent.project = Some("computers".into());
        let mut child = parent.clone();
        child.project = Some("mcf-abcdef1234567890abcdef1234567890".into());
        child
            .config
            .insert(fork::FORK_POOL_KEY.into(), "computers".into());
        let mut legacy_child = parent.clone();
        legacy_child.project = Some("computers-fork-abcdef1234567890abcdef1234567891".into());
        let mut stopped = child.clone();
        stopped.status = "Stopped".into();
        let mut unrelated = parent.clone();
        unrelated.project = Some("unrelated".into());
        let mut other_pool = child.clone();
        other_pool
            .config
            .insert(fork::FORK_POOL_KEY.into(), "other-pool".into());
        let mut unowned_compact = child.clone();
        unowned_compact.config.remove(fork::FORK_POOL_KEY);
        assert_eq!(
            backend.active_pool_instances(&[
                parent,
                child,
                legacy_child,
                stopped,
                unrelated,
                other_pool,
                unowned_compact
            ]),
            3
        );
    }

    #[test]
    fn admission_checks_slots_and_memory_including_request() {
        let pool = pool();
        assert!(check_admission(1, 1536, 1024, &pool).is_ok());
        assert!(check_admission(2, 64000, 1024, &pool).is_err());
        assert!(check_admission(0, 1535, 1024, &pool).is_err());
        assert!(check_admission(0, 0, u64::MAX, &pool).is_err());
        assert_eq!(
            available_memory_mib("MemFree: 1 kB\nMemAvailable: 1572864 kB\n").unwrap(),
            1536
        );
        assert!(available_memory_mib("MemFree: 1000000 kB").is_err());
    }

    #[test]
    fn branch_readiness_uses_its_reserved_address_instead_of_docker_or_vpn() {
        let state = serde_json::json!({"network": {
            "docker0": {"addresses": [{"family":"inet", "address":"172.17.0.1"}]},
            "eth0": {"addresses": [{"family":"inet", "address":"10.211.0.2"}]},
            "tun0": {"addresses": [{"family":"inet", "address":"10.99.0.1"}]}
        }});
        assert_eq!(
            bound_worker_url(&state, 18765, "10.211.0.2").as_deref(),
            Some("http://10.211.0.2:18765")
        );
        assert_eq!(bound_worker_url(&state, 18765, "10.211.0.3"), None);
        assert_eq!(bound_worker_url(&state, 18765, "8.8.8.8"), None);
        assert_eq!(bound_worker_url(&state, 18765, "not-an-address"), None);
    }

    #[test]
    fn worker_endpoint_must_be_private_and_not_loopback() {
        let state = serde_json::json!({"network": {
            "lo": {"addresses": [{"family":"inet", "address":"127.0.0.1"}]},
            "public": {"addresses": [{"family":"inet", "address":"8.8.8.8"}]},
            "eth0": {"addresses": [{"family":"inet6", "address":"::1"}, {"family":"inet", "address":"10.211.0.8"}]}
        }});
        assert_eq!(
            private_worker_url(&state, 8123).as_deref(),
            Some("http://10.211.0.8:8123")
        );
        assert_eq!(
            private_worker_url(
                &serde_json::json!({"network":{"eth0":{"addresses":[{"family":"inet","address":"198.51.100.10"}]}}}),
                8000
            ),
            None
        );
    }
}
