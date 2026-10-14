use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

pub const CAPABILITIES: &[&str] = &[
    "browser",
    "desktop",
    "screenshots",
    "browser_semantics",
    "desktop_semantics",
    "exec",
    "files",
    "file_import",
    "file_export",
];

pub const MAX_TRANSFER_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct TransferLimits {
    pub max_file_bytes: u64,
    pub max_stored_bytes: u64,
}
impl Default for TransferLimits {
    fn default() -> Self {
        Self {
            max_file_bytes: MAX_TRANSFER_BYTES,
            max_stored_bytes: 128 * 1024 * 1024,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub server: ServerConfig,
    pub pool: PoolConfig,
    pub profiles: BTreeMap<String, ComputerProfile>,
    #[serde(default)]
    pub credentials: Option<CredentialConfig>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialConfig {
    pub key_file: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    pub listen: String,
    pub state_dir: PathBuf,
    pub operator_token_env: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PoolConfig {
    pub backend: String,
    #[serde(default = "incus_binary")]
    pub incus_binary: String,
    #[serde(default = "incus_project")]
    pub incus_project: String,
    pub max_running: usize,
    #[serde(default)]
    pub min_available_memory_mib: u64,
    #[serde(default = "worker_port")]
    pub worker_port: u16,
    #[serde(default = "ready_timeout")]
    pub ready_timeout_seconds: u64,
}
fn incus_binary() -> String {
    "incus".into()
}
fn incus_project() -> String {
    "meerkat-computer".into()
}
fn worker_port() -> u16 {
    18765
}
fn ready_timeout() -> u64 {
    120
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComputerProfile {
    pub image: String,
    pub kind: ComputerKind,
    pub cpus: u32,
    pub memory_mib: u64,
    pub disk_gib: u64,
    #[serde(default)]
    pub guest_admin: bool,
    #[serde(default)]
    pub transfers: TransferLimits,
    pub capabilities: Vec<String>,
    pub observation: ObservationConfig,
    pub idle_stop_seconds: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum ComputerKind {
    Container,
    VirtualMachine,
}
impl ComputerKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Container => "container",
            Self::VirtualMachine => "virtual-machine",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservationConfig {
    #[serde(default)]
    pub surface: ObservationSurface,
    pub mode: ObservationMode,
    pub width: u32,
    pub height: u32,
    #[serde(default)]
    pub detail: ObservationDetail,
    #[serde(default = "default_observation_elements")]
    pub max_elements: u32,
    #[serde(default = "default_observation_text")]
    pub max_text_chars: u32,
}

fn default_observation_elements() -> u32 {
    300
}

fn default_observation_text() -> u32 {
    24_000
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationDetail {
    #[default]
    Compact,
    Full,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationSurface {
    #[default]
    Browser,
    Desktop,
}
impl ObservationSurface {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Browser => "browser",
            Self::Desktop => "desktop",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ObservationMode {
    Visual,
    Semantic,
    Hybrid,
}
impl ObservationMode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Visual => "visual",
            Self::Semantic => "semantic",
            Self::Hybrid => "hybrid",
        }
    }
}

impl Config {
    pub fn load(path: impl AsRef<Path>) -> Result<Self, crate::BrokerError> {
        let data = std::fs::read_to_string(path).map_err(crate::BrokerError::storage)?;
        let config: Self = toml::from_str(&data)
            .map_err(|_| crate::BrokerError::invalid("invalid computer configuration"))?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), crate::BrokerError> {
        if self
            .credentials
            .as_ref()
            .is_some_and(|c| c.key_file.as_os_str().is_empty())
        {
            return Err(crate::BrokerError::invalid(
                "credentials.key_file must be explicit",
            ));
        }
        let address = self
            .server
            .listen
            .parse::<std::net::SocketAddr>()
            .map_err(|_| crate::BrokerError::invalid("server.listen must be a socket address"))?;
        if self.credentials.is_some() && !address.ip().is_loopback() {
            return Err(crate::BrokerError::invalid(
                "credential broker must bind loopback; use SSH or IAP forwarding",
            ));
        }
        if self.server.state_dir.as_os_str().is_empty()
            || self.server.operator_token_env.is_empty()
            || self.pool.backend != "incus"
            || self.pool.max_running == 0
            || self.pool.worker_port == 0
            || self.pool.ready_timeout_seconds == 0
            || self.profiles.is_empty()
        {
            return Err(crate::BrokerError::invalid(
                "server, pool and profiles must be explicitly configured",
            ));
        }
        for (name, profile) in &self.profiles {
            if name.is_empty()
                || name.len() > 128
                || profile.image.is_empty()
                || profile.cpus == 0
                || profile.memory_mib < 128
                || profile.disk_gib == 0
                || profile.transfers.max_file_bytes == 0
                || profile.transfers.max_file_bytes > MAX_TRANSFER_BYTES
                || profile.transfers.max_stored_bytes < profile.transfers.max_file_bytes
                || profile.observation.width < 160
                || profile.observation.width > 3840
                || profile.observation.height < 120
                || profile.observation.height > 2160
                || !(1..=300).contains(&profile.observation.max_elements)
                || !(256..=24_000).contains(&profile.observation.max_text_chars)
            {
                return Err(crate::BrokerError::invalid(
                    "invalid computer profile resource or observation limits",
                ));
            }
            let caps: std::collections::BTreeSet<_> =
                profile.capabilities.iter().map(String::as_str).collect();
            if caps.len() != profile.capabilities.len()
                || caps.iter().any(|cap| !CAPABILITIES.contains(cap))
            {
                return Err(crate::BrokerError::invalid(
                    "unsupported or duplicate capability",
                ));
            }
            let has_surface = caps.contains("browser") || caps.contains("desktop");
            if has_surface && !caps.contains(profile.observation.surface.as_str()) {
                return Err(crate::BrokerError::invalid(
                    "default observation surface must be enabled",
                ));
            }
            if caps.contains("screenshots") && !has_surface {
                return Err(crate::BrokerError::invalid(
                    "screenshots require a computer surface",
                ));
            }
            for (surface, semantics) in [
                ("browser", "browser_semantics"),
                ("desktop", "desktop_semantics"),
            ] {
                if !caps.contains(surface) {
                    if caps.contains(semantics) {
                        return Err(crate::BrokerError::invalid(
                            "semantic capability requires its surface",
                        ));
                    }
                    continue;
                }
                let valid = match profile.observation.mode {
                    ObservationMode::Visual => caps.contains("screenshots"),
                    ObservationMode::Semantic => caps.contains(semantics),
                    ObservationMode::Hybrid => {
                        caps.contains("screenshots") && caps.contains(semantics)
                    }
                };
                if !valid {
                    return Err(crate::BrokerError::invalid(
                        "observation mode requires corresponding capabilities",
                    ));
                }
            }
        }
        Ok(())
    }
}
