use crate::{
    backend::{Backend, BackendComputer, BackendError, BackendFailureDiagnostic, ComputerSpec},
    config::{Config, ObservationConfig},
    machine::{
        ActionInput, ActionPhase, ActionSnapshot, ComputerEffect, ComputerInput, ComputerPhase,
        LifecycleSnapshot,
    },
    store::{ActionRecord, ComputerRecord, GrantRecord, Store},
};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fmt,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use subtle::ConstantTimeEq;
use tokio::sync::Mutex;
mod fork;
mod preview;
mod retirement;
mod secure;
pub(crate) mod transfers;
pub(crate) use fork::ForkRecord;
pub use fork::{ForkRequest, ForkTarget, ForkView};
pub(crate) use preview::PreviewSource;
pub use retirement::RetirementRequest;
pub(crate) use secure::SecureRequestRecord;
pub use secure::{CredentialFulfillment, SecureInputDelivery, SecureInputTarget};
pub(crate) use transfers::FileRecord;
pub use transfers::{FileDirection, FileMetadata};
#[cfg(test)]
mod worker_error_tests;

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ComputerOwner {
    Identity {
        realm: String,
        namespace: String,
        identity: String,
    },
    Session {
        realm: String,
        session_id: String,
    },
}

impl ComputerOwner {
    pub(crate) fn validate(&self) -> Result<(), BrokerError> {
        let fields = match self {
            Self::Identity {
                realm,
                namespace,
                identity,
            } => vec![realm, namespace, identity],
            Self::Session { realm, session_id } => vec![realm, session_id],
        };
        if fields.iter().any(|part| {
            part.is_empty()
                || part.len() > 512
                || part.trim() != *part
                || part.chars().any(char::is_control)
        }) {
            return Err(BrokerError::invalid(
                "owner references must be nonempty canonical scoped identifiers",
            ));
        }
        Ok(())
    }
}

/// Trusted host input. Never accept an owner from an agent tool invocation.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AttachmentRequest {
    pub owner: ComputerOwner,
    #[serde(default = "default_slot")]
    pub slot: String,
    pub profile: String,
    pub session_id: String,
    pub runtime_generation: u64,
    pub ttl_seconds: u64,
    #[serde(default)]
    pub takeover: bool,
}
fn default_slot() -> String {
    "main".into()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LeaseView {
    pub computer_id: String,
    pub lease_id: String,
    pub epoch: u64,
    pub expires_at_ms: u64,
    pub session_id: String,
    pub runtime_generation: u64,
    pub capabilities: Vec<String>,
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Attachment {
    #[serde(flatten)]
    pub lease: LeaseView,
    pub token: String,
}
impl fmt::Debug for Attachment {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("Attachment")
            .field("lease", &self.lease)
            .field("token", &"[REDACTED]")
            .finish()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ComputerView {
    pub computer_id: String,
    pub owner: ComputerOwner,
    pub slot: String,
    pub profile: String,
    pub phase: String,
    pub epoch: u64,
    pub lease: Option<LeaseView>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_backend_failure: Option<LastBackendFailure>,
}

/// Bounded historical evidence. Its epoch may precede the computer's current epoch.
/// It never grants control, changes lifecycle state, or authorizes a retry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LastBackendFailure {
    pub epoch: u64,
    pub occurred_at_ms: u64,
    pub diagnostic: BackendFailureDiagnostic,
}

/// Read-only host maintenance target. This is not a control grant or reservation.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OperatorAccess {
    pub computer_id: String,
    pub owner: ComputerOwner,
    pub slot: String,
    pub profile: String,
    pub guest_admin: bool,
    pub instance_name: String,
    pub instance_id: String,
    pub control_active: bool,
    pub shell_argv: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallRequest {
    pub action_id: String,
    pub name: String,
    pub arguments: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerCallResponse {
    pub content: Vec<Value>,
    #[serde(default)]
    pub metrics: Value,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkerLease {
    pub lease_id: String,
    pub epoch: u64,
    pub expires_at_ms: u64,
    pub capabilities: Vec<String>,
    pub observation: ObservationConfig,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidInput,
    Unauthorized,
    LeaseExpired,
    Conflict,
    NotFound,
    CapabilityDenied,
    BackendFailure,
    StorageFailure,
    WorkerUnavailable,
    UnknownEffect,
}
impl ErrorCode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::InvalidInput => "invalid_input",
            Self::Unauthorized => "unauthorized",
            Self::LeaseExpired => "lease_expired",
            Self::Conflict => "conflict",
            Self::NotFound => "not_found",
            Self::CapabilityDenied => "capability_denied",
            Self::BackendFailure => "backend_failure",
            Self::StorageFailure => "storage_failure",
            Self::WorkerUnavailable => "worker_unavailable",
            Self::UnknownEffect => "unknown_effect",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Effect {
    None,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, thiserror::Error)]
#[error("{message}")]
pub struct BrokerError {
    pub code: ErrorCode,
    pub message: String,
    pub effect: Effect,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metrics: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend_failure: Option<BackendFailureDiagnostic>,
}
impl BrokerError {
    pub fn code(&self) -> &'static str {
        self.code.as_str()
    }
    pub fn effect(&self) -> &'static str {
        if self.effect == Effect::Unknown {
            "unknown"
        } else {
            "none"
        }
    }
    pub fn invalid(message: impl Into<String>) -> Self {
        Self {
            code: ErrorCode::InvalidInput,
            message: message.into(),
            effect: Effect::None,
            metrics: None,
            backend_failure: None,
        }
    }
    pub(crate) fn storage(error: impl fmt::Display) -> Self {
        Self {
            code: ErrorCode::StorageFailure,
            message: format!("computer state persistence failed: {error}"),
            effect: Effect::None,
            metrics: None,
            backend_failure: None,
        }
    }
    pub(crate) fn conflict(message: impl Into<String>) -> Self {
        Self {
            code: ErrorCode::Conflict,
            message: message.into(),
            effect: Effect::None,
            metrics: None,
            backend_failure: None,
        }
    }
    pub fn unknown(message: impl Into<String>) -> Self {
        Self {
            code: ErrorCode::UnknownEffect,
            message: message.into(),
            effect: Effect::Unknown,
            metrics: None,
            backend_failure: None,
        }
    }
    fn unauthorized() -> Self {
        Self {
            code: ErrorCode::Unauthorized,
            message: "control grant is not current".into(),
            effect: Effect::None,
            metrics: None,
            backend_failure: None,
        }
    }
    fn missing() -> Self {
        Self {
            code: ErrorCode::NotFound,
            message: "computer not found".into(),
            effect: Effect::None,
            metrics: None,
            backend_failure: None,
        }
    }
    fn backend(error: BackendError) -> Self {
        Self {
            code: ErrorCode::BackendFailure,
            message: "backend operation failed; inspect operator diagnostics".into(),
            effect: Effect::Unknown,
            metrics: None,
            backend_failure: Some(error.diagnostic()),
        }
    }
}

pub trait Clock: Send + Sync {
    fn now_ms(&self) -> u64;
}
struct SystemClock;
impl Clock for SystemClock {
    fn now_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock precedes Unix epoch")
            .as_millis()
            .try_into()
            .expect("clock fits u64")
    }
}

#[async_trait]
pub trait WorkerClient: Send + Sync {
    async fn stage_file(
        &self,
        _computer: &BackendComputer,
        _token: &str,
        _lease: &WorkerLease,
        _file_id: &str,
        _bytes: &[u8],
    ) -> Result<Value, BrokerError> {
        Err(BrokerError::invalid(
            "worker transport does not support file transfers",
        ))
    }
    async fn fetch_file(
        &self,
        _computer: &BackendComputer,
        _token: &str,
        _lease: &WorkerLease,
        _file_id: &str,
        _max_bytes: u64,
    ) -> Result<Vec<u8>, BrokerError> {
        Err(BrokerError::invalid(
            "worker transport does not support file transfers",
        ))
    }
    async fn remove_file(
        &self,
        _computer: &BackendComputer,
        _token: &str,
        _lease: &WorkerLease,
        _file_id: &str,
    ) -> Result<(), BrokerError> {
        Ok(())
    }
    async fn install_lease(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
    ) -> Result<(), BrokerError>;
    async fn call(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        request: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError>;
}

struct HttpWorkerClient {
    client: reqwest::Client,
}
impl HttpWorkerClient {
    fn new() -> Result<Self, BrokerError> {
        Ok(Self {
            client: reqwest::Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(10))
                .timeout(Duration::from_secs(135))
                .build()
                .map_err(|_| BrokerError::invalid("cannot construct worker transport"))?,
        })
    }
    async fn response_json(mut response: reqwest::Response) -> Result<Value, BrokerError> {
        let mut bytes = Vec::new();
        while let Some(chunk) = response.chunk().await.map_err(|_| {
            BrokerError::unknown("worker response was lost; do not replay the action")
        })? {
            if bytes.len().saturating_add(chunk.len()) > 32 * 1024 * 1024 {
                return Err(BrokerError::unknown("worker response exceeds size limit"));
            }
            bytes.extend_from_slice(&chunk);
        }
        serde_json::from_slice(&bytes)
            .map_err(|_| BrokerError::unknown("worker returned an invalid response"))
    }
}

#[async_trait]
impl WorkerClient for HttpWorkerClient {
    async fn stage_file(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        file_id: &str,
        bytes: &[u8],
    ) -> Result<Value, BrokerError> {
        self.stage_blob(computer, token, lease, file_id, bytes)
            .await
    }
    async fn fetch_file(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        file_id: &str,
        max_bytes: u64,
    ) -> Result<Vec<u8>, BrokerError> {
        self.fetch_blob(computer, token, lease, file_id, max_bytes)
            .await
    }
    async fn remove_file(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        file_id: &str,
    ) -> Result<(), BrokerError> {
        self.remove_blob(computer, token, lease, file_id).await
    }
    async fn install_lease(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
    ) -> Result<(), BrokerError> {
        let response = self
            .client
            .post(format!(
                "{}/v1/lease",
                computer.worker_url.trim_end_matches('/')
            ))
            .bearer_auth(token)
            .json(lease)
            .timeout(Duration::from_secs(30))
            .send()
            .await
            .map_err(|_| BrokerError::unknown("worker lease acknowledgement was lost"))?;
        if !response.status().is_success() {
            return Err(BrokerError {
                code: ErrorCode::WorkerUnavailable,
                message: "worker refused control grant".into(),
                effect: Effect::Unknown,
                metrics: None,
                backend_failure: None,
            });
        }
        let acknowledgement = Self::response_json(response).await?;
        if acknowledgement
            .get("protocol_version")
            .and_then(Value::as_u64)
            != Some(1)
            || acknowledgement.get("lease_id").and_then(Value::as_str)
                != Some(lease.lease_id.as_str())
            || acknowledgement.get("epoch").and_then(Value::as_u64) != Some(lease.epoch)
        {
            return Err(BrokerError::unknown(
                "worker acknowledged a different control grant",
            ));
        }
        Ok(())
    }

    async fn call(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        request: &CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        let body = json!({"lease_id":lease.lease_id,"epoch":lease.epoch,"action_id":request.action_id,"name":request.name,"arguments":request.arguments});
        let response = self
            .client
            .post(format!(
                "{}/v1/call",
                computer.worker_url.trim_end_matches('/')
            ))
            .bearer_auth(token)
            .json(&body)
            .send()
            .await
            .map_err(|_| {
                BrokerError::unknown("worker response was lost; do not replay the action")
            })?;
        let success = response.status().is_success();
        let value = Self::response_json(response).await?;
        if !success || value.get("error").is_some() {
            let error = value.get("error");
            let no_effect = error
                .and_then(|error| error.get("effect"))
                .and_then(Value::as_str)
                == Some("none");
            if !no_effect {
                let mut failure = BrokerError::unknown(
                    "worker could not prove the action outcome; do not replay it",
                );
                failure.metrics = value.get("metrics").cloned();
                return Err(failure);
            }
            let code = match error
                .and_then(|error| error.get("code"))
                .and_then(Value::as_str)
            {
                Some("capability_denied") => ErrorCode::CapabilityDenied,
                Some(
                    "stale_lease" | "stale_observation" | "stale_ref" | "observation_unstable",
                ) => ErrorCode::Conflict,
                _ => ErrorCode::InvalidInput,
            };
            return Err(BrokerError {
                code,
                message: error
                    .and_then(|e| e.get("message"))
                    .and_then(Value::as_str)
                    .unwrap_or("worker rejected action")
                    .chars()
                    .take(1024)
                    .collect(),
                effect: Effect::None,
                metrics: value.get("metrics").cloned(),
                backend_failure: None,
            });
        }
        serde_json::from_value(value)
            .map_err(|_| BrokerError::unknown("worker response did not contain a receipt"))
    }
}

pub struct Broker {
    config: Config,
    backend: Arc<dyn Backend>,
    worker: Arc<dyn WorkerClient>,
    clock: Arc<dyn Clock>,
    store: Mutex<Store>,
    computers: Mutex<BTreeMap<String, Arc<Mutex<()>>>>,
    vault: Option<Arc<dyn crate::credentials::CredentialStore>>,
    credential_policy: Mutex<()>,
    forks: Mutex<()>,
}

impl Broker {
    pub async fn open(config: Config, backend: Arc<dyn Backend>) -> Result<Self, BrokerError> {
        Self::open_with_worker_and_clock(
            config,
            backend,
            Arc::new(HttpWorkerClient::new()?),
            Arc::new(SystemClock),
        )
        .await
    }

    pub async fn open_with_worker_and_clock(
        config: Config,
        backend: Arc<dyn Backend>,
        worker: Arc<dyn WorkerClient>,
        clock: Arc<dyn Clock>,
    ) -> Result<Self, BrokerError> {
        config.validate()?;
        let vault = config
            .credentials
            .as_ref()
            .map(|credentials| {
                crate::credentials::CredentialVault::open(
                    &config.server.state_dir.join("vault"),
                    &credentials.key_file,
                )
                .map(|v| Arc::new(v) as Arc<dyn crate::credentials::CredentialStore>)
            })
            .transpose()?;
        Self::open_internal(config, backend, worker, clock, vault).await
    }

    /// The trusted embedding host can inject a credential backend without local key configuration.
    pub async fn open_with_credential_store(
        config: Config,
        backend: Arc<dyn Backend>,
        vault: Arc<dyn crate::credentials::CredentialStore>,
    ) -> Result<Self, BrokerError> {
        if config.credentials.is_some() {
            return Err(BrokerError::invalid(
                "choose either local credential configuration or an injected store",
            ));
        }
        Self::open_internal(
            config,
            backend,
            Arc::new(HttpWorkerClient::new()?),
            Arc::new(SystemClock),
            Some(vault),
        )
        .await
    }

    async fn open_internal(
        config: Config,
        backend: Arc<dyn Backend>,
        worker: Arc<dyn WorkerClient>,
        clock: Arc<dyn Clock>,
        vault: Option<Arc<dyn crate::credentials::CredentialStore>>,
    ) -> Result<Self, BrokerError> {
        config.validate()?;
        if vault.is_some()
            && !config
                .server
                .listen
                .parse::<std::net::SocketAddr>()
                .map_err(|_| BrokerError::invalid("invalid listener"))?
                .ip()
                .is_loopback()
        {
            return Err(BrokerError::invalid("credential broker must bind loopback"));
        }
        let mut store = Store::open(&config.server.state_dir)?;
        let mut recovered = store.state.clone();
        for record in recovered.computers.values_mut() {
            record.lifecycle.apply(ComputerInput::Recover {
                now_ms: clock.now_ms(),
            })?;
            record.grant = None;
            for action in record.actions.values_mut() {
                action.lifecycle.apply(ActionInput::Recover)?;
            }
        }
        for request in recovered.credential_requests.values_mut() {
            request.recover()?;
        }
        for fork in recovered.forks.values_mut() {
            fork.recover()?;
        }
        store.commit(recovered)?;
        Ok(Self {
            config,
            backend,
            worker,
            clock,
            store: Mutex::new(store),
            computers: Mutex::new(BTreeMap::new()),
            vault,
            credential_policy: Mutex::new(()),
            forks: Mutex::new(()),
        })
    }

    async fn computer_lock(&self, id: &str) -> Arc<Mutex<()>> {
        self.computers
            .lock()
            .await
            .entry(id.into())
            .or_default()
            .clone()
    }

    async fn update<R>(
        &self,
        id: &str,
        f: impl FnOnce(&mut ComputerRecord) -> Result<R, BrokerError>,
    ) -> Result<R, BrokerError> {
        let mut store = self.store.lock().await;
        let mut next = store.state.clone();
        let value = f(next
            .computers
            .get_mut(id)
            .ok_or_else(BrokerError::missing)?)?;
        store.commit(next)?;
        Ok(value)
    }

    async fn record(&self, id: &str) -> Result<ComputerRecord, BrokerError> {
        self.store
            .lock()
            .await
            .state
            .computers
            .get(id)
            .cloned()
            .ok_or_else(BrokerError::missing)
    }

    async fn authenticate(&self, token: &str) -> Result<String, BrokerError> {
        let store = self.store.lock().await;
        Ok(self.authenticate_record(&store, token)?.computer_id.clone())
    }

    /// Reuse current-grant authority while the caller holds the store lock.
    fn authenticate_record<'a>(
        &self,
        store: &'a Store,
        token: &str,
    ) -> Result<&'a ComputerRecord, BrokerError> {
        if token.len() != 64 {
            return Err(BrokerError::unauthorized());
        }
        let hash = digest(token.as_bytes());
        for record in store.state.computers.values() {
            if let Some(grant) = &record.grant
                && bool::from(grant.token_hash.as_bytes().ct_eq(hash.as_bytes()))
                && record.lifecycle.phase == ComputerPhase::Leased
            {
                if record.lifecycle.expires_at_ms <= self.clock.now_ms() {
                    return Err(BrokerError {
                        code: ErrorCode::LeaseExpired,
                        message: "control grant has expired".into(),
                        effect: Effect::None,
                        metrics: None,
                        backend_failure: None,
                    });
                }
                return Ok(record);
            }
        }
        Err(BrokerError::unauthorized())
    }

    pub async fn attach(&self, request: AttachmentRequest) -> Result<Attachment, BrokerError> {
        request.owner.validate()?;
        validate_identifier(&request.slot, "slot")?;
        validate_identifier(&request.session_id, "session_id")?;
        if let ComputerOwner::Session { session_id, .. } = &request.owner
            && session_id != &request.session_id
        {
            return Err(BrokerError::invalid(
                "session-owned computer must be attached by its owner session",
            ));
        }
        let expires_at_ms = expiry(self.clock.now_ms(), request.ttl_seconds)?;
        let profile = self
            .config
            .profiles
            .get(&request.profile)
            .cloned()
            .ok_or_else(|| BrokerError::invalid("unknown computer profile"))?;
        let id = {
            let mut store = self.store.lock().await;
            if let Some(record) = store
                .state
                .computers
                .values()
                .find(|record| record.owner == request.owner && record.slot == request.slot)
            {
                fork::require_attachable(&store.state, record)?;
                if record.profile != profile || record.profile_name != request.profile {
                    return Err(BrokerError::conflict(
                        "this computer has a different immutable profile; use another slot",
                    ));
                }
                record.computer_id.clone()
            } else {
                let id = uuid::Uuid::new_v4().simple().to_string();
                let secret = random_secret();
                store.create_worker_secret(&id, &secret)?;
                let mut next = store.state.clone();
                next.computers.insert(
                    id.clone(),
                    ComputerRecord {
                        computer_id: id.clone(),
                        ownership_id: uuid::Uuid::new_v4().simple().to_string(),
                        owner: request.owner.clone(),
                        slot: request.slot.clone(),
                        profile_name: request.profile.clone(),
                        profile,
                        lifecycle: LifecycleSnapshot::default(),
                        backend: None,
                        grant: None,
                        actions: BTreeMap::new(),
                        preview: None,
                        last_backend_failure: None,
                        fork_request_id: None,
                    },
                );
                store.commit(next)?;
                id
            }
        };
        let lock = self.computer_lock(&id).await;
        let _guard = lock.lock().await;
        let token = random_secret();
        let effects = self
            .update(&id, |record| {
                let effects = record.lifecycle.apply_with_effects(ComputerInput::Attach {
                    now_ms: self.clock.now_ms(),
                    expires_at_ms,
                    takeover: request.takeover,
                })?;
                record.grant = Some(GrantRecord {
                    lease_id: uuid::Uuid::new_v4().to_string(),
                    token_hash: digest(token.as_bytes()),
                    session_id: request.session_id,
                    runtime_generation: request.runtime_generation,
                });
                Ok(effects)
            })
            .await?;
        expect_effect(effects, ComputerEffect::StartBackend)?;
        let record = self.record(&id).await?;
        let epoch = record.lifecycle.epoch;
        let secret = self.store.lock().await.worker_secret(&id)?;
        let backend = match &record.backend {
            Some(computer) => self.backend.start(computer).await,
            None => {
                self.backend
                    .ensure(&ComputerSpec {
                        computer_id: id.clone(),
                        ownership_id: record.ownership_id.clone(),
                        profile: record.profile.clone(),
                        worker_token: secret.clone(),
                    })
                    .await
            }
        };
        let backend = match backend {
            Ok(backend) => backend,
            Err(error) => {
                self.fail_backend(&id, epoch, &error).await?;
                return Err(BrokerError::backend(error));
            }
        };
        if backend.computer_id != id || backend.ownership_id != record.ownership_id {
            self.fail(&id, epoch).await?;
            return Err(BrokerError::storage(
                "backend returned a different computer owner",
            ));
        }
        let effects = self
            .update(&id, |record| {
                let effects = record
                    .lifecycle
                    .apply_with_effects(ComputerInput::BackendReady { epoch })?;
                record.backend = Some(backend.clone());
                Ok(effects)
            })
            .await?;
        expect_effect(effects, ComputerEffect::InstallWorkerGrant)?;
        let worker_lease = worker_lease(&record)?;
        if let Err(error) = self
            .worker
            .install_lease(&backend, &secret, &worker_lease)
            .await
        {
            self.fail(&id, epoch).await?;
            return Err(error);
        }
        if let Err(error) = self
            .update(&id, |record| {
                record.lifecycle.apply(ComputerInput::Ready {
                    epoch,
                    now_ms: self.clock.now_ms(),
                })
            })
            .await
        {
            self.fail(&id, epoch).await?;
            return Err(error);
        }
        let lease = lease_view(&self.record(&id).await?).ok_or_else(BrokerError::unauthorized)?;
        Ok(Attachment { lease, token })
    }

    async fn fail(&self, id: &str, epoch: u64) -> Result<(), BrokerError> {
        self.fail_with_diagnostic(id, epoch, None).await
    }

    async fn fail_backend(
        &self,
        id: &str,
        epoch: u64,
        error: &BackendError,
    ) -> Result<(), BrokerError> {
        self.fail_with_diagnostic(id, epoch, Some(error.diagnostic()))
            .await
    }

    async fn fail_with_diagnostic(
        &self,
        id: &str,
        epoch: u64,
        diagnostic: Option<BackendFailureDiagnostic>,
    ) -> Result<(), BrokerError> {
        self.update(id, |record| {
            record.lifecycle.apply(ComputerInput::Failed { epoch })?;
            record.grant = None;
            if let Some(diagnostic) = diagnostic {
                record.last_backend_failure = Some(LastBackendFailure {
                    epoch,
                    occurred_at_ms: self.clock.now_ms(),
                    diagnostic,
                });
            }
            Ok(())
        })
        .await
    }

    pub async fn list_computers(&self) -> Vec<ComputerView> {
        self.store
            .lock()
            .await
            .state
            .computers
            .values()
            .map(|record| ComputerView {
                computer_id: record.computer_id.clone(),
                owner: record.owner.clone(),
                slot: record.slot.clone(),
                profile: record.profile_name.clone(),
                phase: record.lifecycle.phase_label(),
                epoch: record.lifecycle.epoch,
                lease: lease_view(record),
                last_backend_failure: record.last_backend_failure,
            })
            .collect()
    }

    /// Trusted operator inspection. Does not attach, renew, or change a controller.
    pub async fn operator_access(&self, id: &str) -> Result<OperatorAccess, BrokerError> {
        let lock = self.computer_lock(id).await;
        let _guard = lock.lock().await;
        let record = self.record(id).await?;
        let expected = record.backend.as_ref().ok_or_else(BrokerError::missing)?;
        let secret = self.store.lock().await.worker_secret(id)?;
        let actual = self
            .inspect_record(
                &record,
                &ComputerSpec {
                    computer_id: id.into(),
                    ownership_id: record.ownership_id.clone(),
                    profile: record.profile.clone(),
                    worker_token: secret,
                },
            )
            .await
            .map_err(BrokerError::backend)?
            .ok_or_else(BrokerError::missing)?;
        if actual.computer_id != record.computer_id
            || actual.ownership_id != record.ownership_id
            || actual.instance_name != expected.instance_name
            || actual.instance_id != expected.instance_id
            || actual.incus_project != expected.incus_project
        {
            return Err(BrokerError::conflict("operator access target was replaced"));
        }
        let control_active =
            lease_view(&record).is_some_and(|lease| lease.expires_at_ms > self.clock.now_ms());
        let shell_argv = vec![
            self.config.pool.incus_binary.clone(),
            "--force-local".into(),
            "--project".into(),
            actual
                .incus_project
                .clone()
                .unwrap_or_else(|| self.config.pool.incus_project.clone()),
            "exec".into(),
            actual.instance_name.clone(),
            "--".into(),
            "/bin/bash".into(),
            "-l".into(),
        ];
        Ok(OperatorAccess {
            computer_id: id.into(),
            owner: record.owner,
            slot: record.slot,
            profile: record.profile_name,
            guest_admin: record.profile.guest_admin,
            instance_name: actual.instance_name,
            instance_id: actual.instance_id,
            control_active,
            shell_argv,
        })
    }

    pub async fn tools(&self, token: &str) -> Result<Vec<Value>, BrokerError> {
        let id = self.authenticate(token).await?;
        let record = self.record(&id).await?;
        let mut definitions: Vec<Value> = tool_definitions()
            .into_iter()
            .filter(|tool| {
                tool_capabilities(tool["name"].as_str().unwrap()).is_some_and(|caps| {
                    record
                        .profile
                        .capabilities
                        .iter()
                        .any(|value| caps.contains(&value.as_str()))
                })
            })
            .collect();
        if self.vault.is_some()
            && record
                .profile
                .capabilities
                .iter()
                .any(|c| c == "browser_semantics")
        {
            definitions.extend(
                secure::credential_tool_definitions()
                    .into_iter()
                    .filter(|tool| {
                        matches!(record.owner, ComputerOwner::Identity { .. })
                            || matches!(
                                tool["name"].as_str(),
                                Some("credentials_request" | "credentials_status")
                            )
                    }),
            );
        }
        Ok(definitions)
    }

    pub async fn call(
        &self,
        token: &str,
        request: CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        validate_identifier(&request.action_id, "action_id")?;
        if request.action_id.len() > 128 {
            return Err(BrokerError::invalid("action_id exceeds 128 bytes"));
        }
        if !request.arguments.is_object() {
            return Err(BrokerError::invalid("tool arguments must be an object"));
        }
        if request.name.starts_with("credentials_") {
            return self.credential_tool(token, request).await;
        }
        let arguments = serde_json::to_vec(&(&request.name, &request.arguments))
            .map_err(BrokerError::storage)?;
        if arguments.len() > 2 * 1024 * 1024 {
            return Err(BrokerError::invalid("tool arguments exceed size limit"));
        }
        let capabilities = tool_capabilities(&request.name)
            .ok_or_else(|| BrokerError::invalid("unknown computer tool"))?;
        let id = self.authenticate(token).await?;
        let lock = self.computer_lock(&id).await;
        let _guard = lock.lock().await;
        self.authenticate(token).await?;
        let record = self.record(&id).await?;
        if !record
            .profile
            .capabilities
            .iter()
            .any(|cap| capabilities.contains(&cap.as_str()))
        {
            return Err(BrokerError {
                code: ErrorCode::CapabilityDenied,
                message: "computer profile does not grant this capability".into(),
                effect: Effect::None,
                metrics: None,
                backend_failure: None,
            });
        }
        let request_digest = digest(&arguments);
        if self
            .store
            .lock()
            .await
            .state
            .credential_requests
            .values()
            .any(|r| r.computer_id == id && r.action_id == request.action_id)
        {
            return Err(BrokerError::conflict(
                "action_id already belongs to a private input operation",
            ));
        }
        if let Some(action) = record.actions.get(&request.action_id) {
            if action.digest != request_digest {
                return Err(BrokerError::conflict(
                    "action_id was already used for different arguments",
                ));
            }
            if action.lifecycle.phase != ActionPhase::Completed {
                return Err(action.error.clone().unwrap_or_else(|| {
                    BrokerError::unknown(
                        "action has no definitive receipt; it will not be replayed",
                    )
                }));
            }
            if let Some(error) = &action.error {
                return Err(error.clone());
            }
            return self.store.lock().await.read_receipt(
                action.receipt_path.as_deref().ok_or_else(|| {
                    BrokerError::storage("completed action is missing its receipt")
                })?,
            );
        }
        let lease = worker_lease(&record)?;
        self.update(&id, |record| {
            record.lifecycle.apply(ComputerInput::Touch {
                epoch: lease.epoch,
                now_ms: self.clock.now_ms(),
            })?;
            record.actions.insert(
                request.action_id.clone(),
                ActionRecord {
                    digest: request_digest,
                    lifecycle: ActionSnapshot::default(),
                    receipt_path: None,
                    error: None,
                },
            );
            record.preview = Some(PreviewSource::Action {
                action_id: request.action_id.clone(),
                received_at_ms: None,
            });
            Ok(())
        })
        .await?;
        let backend = record
            .backend
            .as_ref()
            .ok_or_else(|| BrokerError::storage("leased computer has no backend handle"))?;
        let secret = self.store.lock().await.worker_secret(&id)?;
        let result = if matches!(
            request.name.as_str(),
            "computer_files" | "computer_upload" | "computer_download"
        ) {
            self.transfer_call(token, &record, &lease, &request, backend, &secret)
                .await
        } else {
            self.worker.call(backend, &secret, &lease, &request).await
        };
        let receipt_path = match &result {
            Ok(response) => {
                match self
                    .store
                    .lock()
                    .await
                    .save_receipt(&id, &request.action_id, response)
                {
                    Ok(path) => Some(path),
                    Err(_) => {
                        let mut failure = BrokerError::unknown(
                            "action completed but its receipt could not be persisted; do not replay it",
                        );
                        failure.metrics = Some(response.metrics.clone());
                        return Err(failure);
                    }
                }
            }
            Err(_) => None,
        };
        let commit = self
            .update(&id, |record| {
                let action = record
                    .actions
                    .get_mut(&request.action_id)
                    .ok_or_else(|| BrokerError::storage("admitted action is missing"))?;
                action.lifecycle.apply(
                    if result
                        .as_ref()
                        .err()
                        .is_some_and(|error| error.effect == Effect::Unknown)
                    {
                        ActionInput::Uncertain
                    } else {
                        ActionInput::Complete
                    },
                )?;
                action.receipt_path = receipt_path;
                action.error = result.as_ref().err().cloned();
                if let Some(PreviewSource::Action {
                    action_id,
                    received_at_ms,
                }) = &mut record.preview
                    && action_id == &request.action_id
                {
                    *received_at_ms = Some(self.clock.now_ms());
                }
                Ok(())
            })
            .await;
        if commit.is_err() {
            let mut failure =
                BrokerError::unknown("action outcome could not be committed; do not replay it");
            failure.metrics = match &result {
                Ok(response) => Some(response.metrics.clone()),
                Err(error) => error.metrics.clone(),
            };
            return Err(failure);
        }
        result
    }

    pub async fn renew(&self, token: &str, ttl_seconds: u64) -> Result<LeaseView, BrokerError> {
        let id = self.authenticate(token).await?;
        let lock = self.computer_lock(&id).await;
        let _guard = lock.lock().await;
        self.authenticate(token).await?;
        let expires_at_ms = expiry(self.clock.now_ms(), ttl_seconds)?;
        let record = self.record(&id).await?;
        let epoch = record.lifecycle.epoch;
        let effects = self
            .update(&id, |record| {
                record.lifecycle.apply_with_effects(ComputerInput::Renew {
                    epoch,
                    now_ms: self.clock.now_ms(),
                    expires_at_ms,
                })
            })
            .await?;
        expect_effect(effects, ComputerEffect::InstallWorkerGrant)?;
        let record = self.record(&id).await?;
        let secret = self.store.lock().await.worker_secret(&id)?;
        if let Err(error) = self
            .worker
            .install_lease(
                record.backend.as_ref().ok_or_else(BrokerError::missing)?,
                &secret,
                &worker_lease(&record)?,
            )
            .await
        {
            self.fail(&id, epoch).await?;
            return Err(error);
        }
        lease_view(&record).ok_or_else(BrokerError::unauthorized)
    }

    pub async fn release(&self, token: &str) -> Result<(), BrokerError> {
        let id = self.authenticate(token).await?;
        let lock = self.computer_lock(&id).await;
        let _guard = lock.lock().await;
        self.authenticate(token).await?;
        self.revoke_locked(&id).await
    }

    async fn revoke_locked(&self, id: &str) -> Result<(), BrokerError> {
        let effects = self
            .update(id, |record| {
                let effects = record.lifecycle.apply_with_effects(ComputerInput::Revoke {
                    now_ms: self.clock.now_ms(),
                })?;
                record.grant = None;
                Ok(effects)
            })
            .await?;
        expect_effect(effects, ComputerEffect::FenceWorker)?;
        self.finish_revocation_locked(id).await
    }

    async fn finish_revocation_locked(&self, id: &str) -> Result<(), BrokerError> {
        let record = self.record(id).await?;
        let epoch = record.lifecycle.epoch;
        let secret = self.store.lock().await.worker_secret(id)?;
        let lease = WorkerLease {
            lease_id: uuid::Uuid::new_v4().to_string(),
            epoch,
            expires_at_ms: 0,
            capabilities: vec![],
            observation: record.profile.observation.clone(),
        };
        if let Some(backend) = &record.backend
            && let Err(error) = self.worker.install_lease(backend, &secret, &lease).await
        {
            self.fail(id, epoch).await?;
            return Err(error);
        }
        self.update(id, |record| {
            record.lifecycle.apply(ComputerInput::Fenced { epoch })
        })
        .await
    }

    pub async fn stop(&self, computer_id: &str) -> Result<(), BrokerError> {
        let lock = self.computer_lock(computer_id).await;
        let _guard = lock.lock().await;
        self.stop_locked(computer_id).await
    }

    async fn stop_locked(&self, id: &str) -> Result<(), BrokerError> {
        let effects = self
            .update(id, |record| {
                let effects = record.lifecycle.apply_with_effects(ComputerInput::Stop {
                    now_ms: self.clock.now_ms(),
                })?;
                record.grant = None;
                Ok(effects)
            })
            .await?;
        expect_effect(effects, ComputerEffect::StopBackend)?;
        self.finish_stop_locked(id).await
    }

    async fn finish_stop_locked(&self, id: &str) -> Result<(), BrokerError> {
        let mut record = self.record(id).await?;
        let epoch = record.lifecycle.epoch;
        if record.backend.is_none() {
            let secret = self.store.lock().await.worker_secret(id)?;
            let spec = ComputerSpec {
                computer_id: id.into(),
                ownership_id: record.ownership_id.clone(),
                profile: record.profile.clone(),
                worker_token: secret,
            };
            let observed = match self.inspect_record(&record, &spec).await {
                Ok(observed) => observed,
                Err(error) => {
                    self.fail_backend(id, epoch, &error).await?;
                    return Err(BrokerError::backend(error));
                }
            };
            if let Some(backend) = &observed
                && (backend.computer_id != id || backend.ownership_id != record.ownership_id)
            {
                self.fail(id, epoch).await?;
                return Err(BrokerError::storage(
                    "backend inspection returned a different computer owner",
                ));
            }
            self.update(id, |record| {
                record.backend = observed.clone();
                Ok(())
            })
            .await?;
            record.backend = observed;
        }
        if let Some(backend) = &record.backend
            && let Err(error) = self.backend.stop(backend).await
        {
            self.fail_backend(id, epoch, &error).await?;
            return Err(BrokerError::backend(error));
        }
        self.update(id, |record| {
            record.lifecycle.apply(ComputerInput::Stopped { epoch })
        })
        .await
    }

    pub async fn reap(&self) -> Result<Vec<String>, BrokerError> {
        let ids: Vec<String> = self
            .store
            .lock()
            .await
            .state
            .computers
            .keys()
            .cloned()
            .collect();
        let mut stopped = Vec::new();
        let mut failures = Vec::new();
        for id in ids {
            let lock = self.computer_lock(&id).await;
            let Ok(_guard) = lock.try_lock() else {
                continue;
            };
            let result: Result<(), BrokerError> = async {
                let now_ms = self.clock.now_ms();
                let effects = self
                    .update(&id, |record| {
                        let effects = record.lifecycle.apply_with_effects(ComputerInput::Reap {
                            now_ms,
                            idle_ms: record.profile.idle_stop_seconds.saturating_mul(1000),
                        })?;
                        if !effects.is_empty() {
                            record.grant = None;
                        }
                        Ok(effects)
                    })
                    .await?;
                for effect in effects {
                    match effect {
                        ComputerEffect::FenceWorker => self.finish_revocation_locked(&id).await?,
                        ComputerEffect::StopBackend => {
                            self.finish_stop_locked(&id).await?;
                            stopped.push(id.clone());
                        }
                        ComputerEffect::StartBackend
                        | ComputerEffect::InstallWorkerGrant
                        | ComputerEffect::DestroyBackend => {
                            return Err(BrokerError::storage("reap emitted an unrelated effect"));
                        }
                    }
                }
                Ok(())
            }
            .await;
            if let Err(error) = result {
                failures.push(error);
            }
        }
        if !failures.is_empty() {
            let effect = if failures.iter().any(|error| error.effect == Effect::Unknown) {
                Effect::Unknown
            } else {
                Effect::None
            };
            return Err(BrokerError {
                code: failures[0].code,
                message: format!(
                    "idle cleanup failed for {} computer(s); {} stopped successfully; first error: {}",
                    failures.len(),
                    stopped.len(),
                    failures[0].message
                ),
                effect,
                metrics: None,
                backend_failure: failures[0].backend_failure,
            });
        }
        Ok(stopped)
    }
}

fn worker_lease(record: &ComputerRecord) -> Result<WorkerLease, BrokerError> {
    let grant = record
        .grant
        .as_ref()
        .ok_or_else(BrokerError::unauthorized)?;
    Ok(WorkerLease {
        lease_id: grant.lease_id.clone(),
        epoch: record.lifecycle.epoch,
        expires_at_ms: record.lifecycle.expires_at_ms,
        capabilities: record.profile.capabilities.clone(),
        observation: record.profile.observation.clone(),
    })
}

fn expect_effect(
    effects: Vec<ComputerEffect>,
    expected: ComputerEffect,
) -> Result<(), BrokerError> {
    if effects == vec![expected] {
        Ok(())
    } else {
        Err(BrokerError::storage(
            "generated transition emitted an unexpected effect",
        ))
    }
}

fn lease_view(record: &ComputerRecord) -> Option<LeaseView> {
    if record.lifecycle.phase != ComputerPhase::Leased {
        return None;
    }
    let grant = record.grant.as_ref()?;
    Some(LeaseView {
        computer_id: record.computer_id.clone(),
        lease_id: grant.lease_id.clone(),
        epoch: record.lifecycle.epoch,
        expires_at_ms: record.lifecycle.expires_at_ms,
        session_id: grant.session_id.clone(),
        runtime_generation: grant.runtime_generation,
        capabilities: record.profile.capabilities.clone(),
    })
}

fn validate_identifier(value: &str, field: &str) -> Result<(), BrokerError> {
    if value.is_empty()
        || value.len() > 256
        || !value.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | ':' | '.')
        })
    {
        return Err(BrokerError::invalid(format!(
            "{field} is not a valid identifier"
        )));
    }
    Ok(())
}
fn expiry(now_ms: u64, ttl_seconds: u64) -> Result<u64, BrokerError> {
    if !(1..=86400).contains(&ttl_seconds) {
        return Err(BrokerError::invalid("lease ttl_seconds must be 1-86400"));
    }
    now_ms
        .checked_add(ttl_seconds * 1000)
        .ok_or_else(|| BrokerError::invalid("lease expiry overflow"))
}
pub(crate) fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn random_secret() -> String {
    format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    )
}

fn tool_capabilities(name: &str) -> Option<&'static [&'static str]> {
    match name {
        "computer_observe" | "computer_act" => Some(&["browser", "desktop"]),
        "computer_apps" => Some(&["desktop"]),
        "browser_navigate" | "browser_tabs" | "browser_downloads" => Some(&["browser"]),
        "browser_upload" => Some(&["browser_semantics"]),
        "workspace_exec" => Some(&["exec"]),
        "workspace_read" | "workspace_write" => Some(&["files"]),
        "computer_files" => Some(&["file_import", "file_export"]),
        "computer_upload" => Some(&["file_import"]),
        "computer_download" => Some(&["file_export"]),
        _ => None,
    }
}

fn tool_definitions() -> Vec<Value> {
    let action = json!({"type":"object","required":["type"],"additionalProperties":false,"properties":{
        "type":{"type":"string","enum":["click","fill","type","key","scroll","drag","focus","invoke"]},"button":{"type":"string","enum":["left","middle","right"]},"click_count":{"type":"integer","minimum":1,"maximum":2},"action_name":{"type":"string"},"ref":{"type":"string"},"x":{"type":"number"},"y":{"type":"number"},"text":{"type":"string","maxLength":16384},"key":{"type":"string"},"delta_x":{"type":"number","minimum":-10000,"maximum":10000},"delta_y":{"type":"number","minimum":-10000,"maximum":10000},"end_x":{"type":"number"},"end_y":{"type":"number"}
    }});
    vec![
        json!({"name":"computer_files","description":"List metadata for files staged for this computer or exported from it. No file contents or host paths are returned.","annotations":{"readOnlyHint":true,"destructiveHint":false},"inputSchema":{"type":"object","additionalProperties":false,"properties":{}}}),
        json!({"name":"computer_upload","description":"Import a staged file_id into a guest path. Relative paths start in the guest workspace; absolute paths are on the guest disk. Overwrite is false by default. An unknown outcome must not be replayed.","annotations":{"readOnlyHint":false,"destructiveHint":true},"inputSchema":{"type":"object","required":["file_id","path"],"additionalProperties":false,"properties":{"file_id":{"type":"string","format":"uuid"},"path":{"type":"string","maxLength":4096},"overwrite":{"type":"boolean","default":false}}}}),
        json!({"name":"computer_download","description":"Export a guest file to the private operator outbox. Relative paths start in the guest workspace; absolute paths are on the guest disk. filename is a display name only. Returns metadata, never bytes or host paths.","annotations":{"readOnlyHint":false,"destructiveHint":false},"inputSchema":{"type":"object","required":["path","filename"],"additionalProperties":false,"properties":{"path":{"type":"string","maxLength":4096},"filename":{"type":"string","maxLength":255}}}}),
        json!({"name":"browser_upload","description":"Set files on an observed browser file input using absolute guest file paths. Requires a current browser observation and ref. Uploading may disclose these files to the website.","annotations":{"readOnlyHint":false,"destructiveHint":true},"inputSchema":{"type":"object","required":["observation_id","ref","paths"],"additionalProperties":false,"properties":{"observation_id":{"type":"string","minLength":1,"maxLength":128},"ref":{"type":"string","minLength":1,"maxLength":100},"paths":{"type":"array","minItems":1,"maxItems":16,"items":{"type":"string","minLength":1,"maxLength":4096,"pattern":"^/"}}}}}),
        json!({"name":"browser_downloads","description":"List downloaded browser files and their absolute guest paths for computer_download export. Returns metadata only.","annotations":{"readOnlyHint":true,"destructiveHint":false},"inputSchema":{"type":"object","additionalProperties":false,"properties":{}}}),
        json!({"name":"computer_observe","description":"Observe your assigned computer. Choose browser viewport or OS desktop; omission uses the profile default. Compact detail prioritizes useful targets and bounds application text; full detail uses the profile ceilings. Expand a native desktop subtree with a current scope_ref and its observation_id. Each observation replaces prior refs. Use the latest observation_id for the next action.","inputSchema":{"type":"object","properties":{"surface":{"type":"string","enum":["browser","desktop"]},"detail":{"type":"string","enum":["compact","full"]},"max_elements":{"type":"integer","minimum":1,"maximum":300,"description":"Returned element limit, at most the profile ceiling. Compact defaults to 60; full uses the ceiling."},"max_text_chars":{"type":"integer","minimum":256,"maximum":24000,"description":"Application text character budget, at most the profile ceiling. Compact defaults to 6000; full uses the ceiling. Screenshots are separate."},"scope_ref":{"type":"string","minLength":1,"maxLength":100,"description":"Current desktop ref to expand; requires its observation_id. Not supported on the browser surface."},"observation_id":{"type":"string","minLength":1,"maxLength":128,"description":"Observation owning scope_ref; both scope fields must be supplied together."}},"dependentRequired":{"scope_ref":["observation_id"],"observation_id":["scope_ref"]},"additionalProperties":false}}),
        json!({"name":"computer_act","description":"Perform one UI action against the latest observation. Coordinates use the returned surface: browser viewport or desktop screen pixels. Desktop invoke requires an advertised action_name. Never replay an action with an unknown outcome.","inputSchema":{"type":"object","required":["observation_id","action"],"additionalProperties":false,"properties":{"observation_id":{"type":"string"},"action":action}}}),
        json!({"name":"computer_apps","description":"List installed native applications or launch one by observed app_id. Launch returns a desktop observation; verify window readiness from observations.","inputSchema":{"type":"object","required":["action"],"additionalProperties":false,"properties":{"action":{"type":"string","enum":["list","launch"]},"app_id":{"type":"string"}}}}),
        json!({"name":"browser_navigate","description":"Navigate the current tab and observe the result.","inputSchema":{"type":"object","required":["url"],"additionalProperties":false,"properties":{"url":{"type":"string"}}}}),
        json!({"name":"browser_tabs","description":"List, create, select or close a browser tab.","inputSchema":{"type":"object","required":["action"],"additionalProperties":false,"properties":{"action":{"type":"string","enum":["list","new","select","close"]},"tab_id":{"type":"string"},"url":{"type":"string"}}}}),
        json!({"name":"workspace_exec","description":"Run a bounded command inside your assigned guest. This capability grants guest command execution, not a filesystem sandbox.","inputSchema":{"type":"object","required":["command"],"additionalProperties":false,"properties":{"command":{"type":"string"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":120}}}}),
        json!({"name":"workspace_read","description":"Read a text file under the configured guest workspace.","inputSchema":{"type":"object","required":["path"],"additionalProperties":false,"properties":{"path":{"type":"string"}}}}),
        json!({"name":"workspace_write","description":"Write a text file under the configured guest workspace.","inputSchema":{"type":"object","required":["path","text"],"additionalProperties":false,"properties":{"path":{"type":"string"},"text":{"type":"string","maxLength":1048576}}}}),
    ]
}
