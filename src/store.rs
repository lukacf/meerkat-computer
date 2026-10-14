use crate::{
    backend::BackendComputer,
    config::ComputerProfile,
    domain::{
        BrokerError, ComputerOwner, FileRecord, ForkRecord, LastBackendFailure, PreviewSource,
        SecureRequestRecord, WorkerCallResponse,
    },
    machine::{ActionSnapshot, LifecycleSnapshot},
};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};
mod transfers;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct GrantRecord {
    pub lease_id: String,
    pub token_hash: String,
    pub session_id: String,
    pub runtime_generation: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionRecord {
    pub digest: String,
    pub lifecycle: ActionSnapshot,
    pub receipt_path: Option<String>,
    pub error: Option<BrokerError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ComputerRecord {
    pub computer_id: String,
    pub ownership_id: String,
    pub owner: ComputerOwner,
    pub slot: String,
    pub profile_name: String,
    pub profile: ComputerProfile,
    pub lifecycle: LifecycleSnapshot,
    pub backend: Option<BackendComputer>,
    pub grant: Option<GrantRecord>,
    pub actions: BTreeMap<String, ActionRecord>,
    #[serde(default)]
    pub preview: Option<PreviewSource>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_backend_failure: Option<LastBackendFailure>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fork_request_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct State {
    pub schema_version: u32,
    pub computers: BTreeMap<String, ComputerRecord>,
    #[serde(default)]
    pub credential_requests: BTreeMap<String, SecureRequestRecord>,
    #[serde(default)]
    pub forks: BTreeMap<String, ForkRecord>,
    #[serde(default)]
    pub files: BTreeMap<String, FileRecord>,
}
impl Default for State {
    fn default() -> Self {
        Self {
            schema_version: 1,
            computers: BTreeMap::new(),
            credential_requests: BTreeMap::new(),
            forks: BTreeMap::new(),
            files: BTreeMap::new(),
        }
    }
}

pub(crate) struct Store {
    path: PathBuf,
    _lock: File,
    pub state: State,
}

impl Store {
    pub fn open(path: &Path) -> Result<Self, BrokerError> {
        private_dir(path)?;
        private_dir(&path.join("workers"))?;
        private_dir(&path.join("receipts"))?;
        private_dir(&path.join("transfers"))?;
        let lock = private_options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path.join("broker.lock"))
            .map_err(BrokerError::storage)?;
        lock.try_lock_exclusive()
            .map_err(|_| BrokerError::conflict("another broker holds this state directory"))?;
        let state_path = path.join("state.json");
        let state = match std::fs::read(&state_path) {
            Ok(bytes) => {
                let state: State = serde_json::from_slice(&bytes)
                    .map_err(|_| BrokerError::storage("invalid broker state"))?;
                if state.schema_version != 1 {
                    return Err(BrokerError::storage("unsupported broker state schema"));
                }
                for (id, record) in &state.computers {
                    if id != &record.computer_id
                        || uuid::Uuid::parse_str(id).is_err()
                        || uuid::Uuid::parse_str(&record.ownership_id).is_err()
                    {
                        return Err(BrokerError::storage("invalid persisted computer identity"));
                    }
                    record.owner.validate()?;
                    if let Some(fork_id) = &record.fork_request_id
                        && !state
                            .forks
                            .get(fork_id)
                            .is_some_and(|fork| fork.children.contains(id))
                    {
                        return Err(BrokerError::storage(
                            "fork computer has no matching provenance",
                        ));
                    }
                }
                for (id, fork) in &state.forks {
                    fork.validate(id, &state)?;
                }
                state
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => State::default(),
            Err(error) => return Err(BrokerError::storage(error)),
        };
        let store = Self {
            path: path.into(),
            _lock: lock,
            state,
        };
        store.validate_files()?;
        store.recover_file_storage()?;
        Ok(store)
    }

    pub fn commit(&mut self, state: State) -> Result<(), BrokerError> {
        atomic_write(
            &self.path.join("state.json"),
            &serde_json::to_vec(&state).map_err(BrokerError::storage)?,
        )?;
        self.state = state;
        Ok(())
    }

    pub fn create_worker_secret(&self, id: &str, secret: &str) -> Result<(), BrokerError> {
        let path = self.path.join("workers").join(format!("{id}.secret"));
        let mut file = private_options()
            .write(true)
            .create_new(true)
            .open(path)
            .map_err(BrokerError::storage)?;
        file.write_all(secret.as_bytes())
            .map_err(BrokerError::storage)?;
        file.sync_all().map_err(BrokerError::storage)?;
        sync_directory(&self.path.join("workers"))
    }

    pub fn worker_secret(&self, id: &str) -> Result<String, BrokerError> {
        std::fs::read_to_string(self.path.join("workers").join(format!("{id}.secret")))
            .map_err(BrokerError::storage)
    }

    pub fn save_receipt(
        &self,
        id: &str,
        action_id: &str,
        response: &WorkerCallResponse,
    ) -> Result<String, BrokerError> {
        let name = format!(
            "{}-{}.json",
            id,
            crate::domain::digest(action_id.as_bytes())
        );
        atomic_write(
            &self.path.join("receipts").join(&name),
            &serde_json::to_vec(response).map_err(BrokerError::storage)?,
        )?;
        Ok(name)
    }

    pub fn read_receipt(&self, name: &str) -> Result<WorkerCallResponse, BrokerError> {
        if name.contains('/') || name.contains('\\') || name.starts_with('.') {
            return Err(BrokerError::storage("invalid receipt path"));
        }
        serde_json::from_slice(
            &std::fs::read(self.path.join("receipts").join(name)).map_err(BrokerError::storage)?,
        )
        .map_err(|_| BrokerError::storage("invalid saved action receipt"))
    }
}

pub(crate) fn private_options() -> OpenOptions {
    let mut options = OpenOptions::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
}

pub(crate) fn private_dir(path: &Path) -> Result<(), BrokerError> {
    if std::fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_symlink()) {
        return Err(BrokerError::storage(
            "state directories must not be symlinks",
        ));
    }
    std::fs::create_dir_all(path).map_err(BrokerError::storage)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(BrokerError::storage)?;
    }
    Ok(())
}

fn sync_directory(path: &Path) -> Result<(), BrokerError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(BrokerError::storage)
}

pub(crate) fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), BrokerError> {
    let parent = path
        .parent()
        .ok_or_else(|| BrokerError::storage("state file has no parent"))?;
    let temporary = parent.join(format!(".pending-{}", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = private_options()
            .create_new(true)
            .write(true)
            .open(&temporary)
            .map_err(BrokerError::storage)?;
        file.write_all(bytes).map_err(BrokerError::storage)?;
        file.sync_all().map_err(BrokerError::storage)?;
        std::fs::rename(&temporary, path).map_err(BrokerError::storage)?;
        sync_directory(parent)
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temporary);
    }
    result
}
