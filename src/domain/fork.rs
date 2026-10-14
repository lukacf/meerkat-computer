//! Trusted-host full-state fork orchestration. No model can choose an owner here.
use super::*;
use crate::{
    backend::BackendCheckpoint,
    machine::fork::{ForkEffect, ForkInput, ForkPhase, ForkSnapshot},
    store::State,
};
use std::collections::BTreeSet;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ForkTarget {
    pub owner: ComputerOwner,
    #[serde(default = "default_slot")]
    pub slot: String,
}

/// Full execution state is mandatory. Unsupported backends cannot fall back to a disk copy.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ForkRequest {
    pub request_id: String,
    pub source_computer_id: String,
    pub source_lease_id: String,
    pub source_epoch: u64,
    pub targets: Vec<ForkTarget>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ForkView {
    pub request_id: String,
    pub source_computer_id: String,
    pub phase: String,
    /// Stable target order. Only the ready_children prefix is attachable.
    pub children: Vec<String>,
    pub ready_children: u64,
    pub started_at_ms: u64,
    pub updated_at_ms: u64,
    pub error: Option<BrokerError>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ForkRecord {
    pub request_id: String,
    pub digest: String,
    pub source_computer_id: String,
    pub children: Vec<String>,
    pub source_backend: BackendComputer,
    pub checkpoint: Option<BackendCheckpoint>,
    pub lifecycle: ForkSnapshot,
    pub started_at_ms: u64,
    pub updated_at_ms: u64,
    pub error: Option<BrokerError>,
}
impl ForkRecord {
    pub(crate) fn validate(&self, id: &str, state: &State) -> Result<(), BrokerError> {
        let source = state
            .computers
            .get(&self.source_computer_id)
            .ok_or_else(|| BrokerError::storage("fork source is missing"))?;
        if !canonical_request_id(id).is_ok_and(|canonical| canonical == id)
            || self.request_id != id
            || self.children.is_empty()
            || self.children.len() > 16
            || self.lifecycle.total != self.children.len() as u64
            || self.lifecycle.ready > self.lifecycle.total
            || self.source_backend.computer_id != source.computer_id
            || self.source_backend.ownership_id != source.ownership_id
            || (self.lifecycle.ready > 0 && self.checkpoint.is_none())
        {
            return Err(BrokerError::storage(
                "invalid persisted fork identity or progress",
            ));
        }
        let mut seen = BTreeSet::new();
        for child in &self.children {
            let record = state
                .computers
                .get(child)
                .ok_or_else(|| BrokerError::storage("fork child is missing"))?;
            if !seen.insert(child)
                || record.fork_request_id.as_deref() != Some(id)
                || realm(&record.owner) != realm(&source.owner)
                || record.profile != source.profile
            {
                return Err(BrokerError::storage("invalid persisted fork child binding"));
            }
        }
        if let Some(checkpoint) = &self.checkpoint
            && (checkpoint.source != self.source_backend || checkpoint.checkpoint_id != id)
        {
            return Err(BrokerError::storage("invalid persisted checkpoint binding"));
        }
        Ok(())
    }
    fn view(&self) -> ForkView {
        ForkView {
            request_id: self.request_id.clone(),
            source_computer_id: self.source_computer_id.clone(),
            phase: self.lifecycle.phase_label(),
            children: self.children.clone(),
            ready_children: self.lifecycle.ready,
            started_at_ms: self.started_at_ms,
            updated_at_ms: self.updated_at_ms,
            error: self.error.clone(),
        }
    }
    pub(crate) fn recover(&mut self) -> Result<(), BrokerError> {
        self.lifecycle.apply(ForkInput::Recover)?;
        if self.lifecycle.phase == ForkPhase::Unknown && self.error.is_none() {
            self.error = Some(BrokerError::unknown(
                "fork was interrupted; inspect the reserved computers and do not replay it",
            ));
        }
        Ok(())
    }
}

pub(super) fn require_attachable(
    state: &State,
    computer: &ComputerRecord,
) -> Result<(), BrokerError> {
    if let Some(id) = &computer.fork_request_id {
        let fork = state
            .forks
            .get(id)
            .ok_or_else(|| BrokerError::storage("missing fork provenance"))?;
        let index = fork
            .children
            .iter()
            .position(|id| id == &computer.computer_id)
            .ok_or_else(|| BrokerError::storage("invalid fork child provenance"))?;
        if index as u64 >= fork.lifecycle.ready || computer.backend.is_none() {
            return Err(BrokerError::unknown(
                "fork child has no confirmed full-state restore; a fresh computer cannot replace it",
            ));
        }
    }
    Ok(())
}

impl Broker {
    pub async fn fork_status(&self, request_id: &str) -> Result<ForkView, BrokerError> {
        let id = canonical_request_id(request_id)?;
        self.store
            .lock()
            .await
            .state
            .forks
            .get(&id)
            .map(ForkRecord::view)
            .ok_or_else(BrokerError::missing)
    }

    /// The embedding host resolves target identities. Returned computers need new attachments.
    pub async fn fork(&self, mut request: ForkRequest) -> Result<ForkView, BrokerError> {
        request.request_id = canonical_request_id(&request.request_id)?;
        if request.targets.is_empty() || request.targets.len() > 16 {
            return Err(BrokerError::invalid(
                "a full-state fork requires 1-16 targets",
            ));
        }
        let mut targets = BTreeSet::new();
        for target in &request.targets {
            target.owner.validate()?;
            validate_identifier(&target.slot, "slot")?;
            if !targets.insert((target.owner.clone(), target.slot.clone())) {
                return Err(BrokerError::invalid(
                    "fork targets must have distinct owner and slot bindings",
                ));
            }
        }
        let request_digest = digest(&serde_json::to_vec(&request).map_err(BrokerError::storage)?);
        // Serializes fork reservations. Ordinary unrelated computer operations remain independent.
        let _fork_guard = self.forks.lock().await;
        {
            let mut store = self.store.lock().await;
            if let Some(mut saved) = store.state.forks.get(&request.request_id).cloned() {
                if saved.digest != request_digest {
                    return Err(BrokerError::conflict(
                        "fork request ID was already used for different inputs",
                    ));
                }
                // Holding the fork lock proves no previous in-process driver remains.
                // Recover its receipt, never its physical effects.
                if !matches!(
                    saved.lifecycle.phase,
                    ForkPhase::Completed | ForkPhase::Unknown
                ) {
                    saved.recover()?;
                    let mut next = store.state.clone();
                    next.forks.insert(saved.request_id.clone(), saved.clone());
                    store.commit(next)?;
                }
                return Ok(saved.view());
            }
        }
        let source_lock = self.computer_lock(&request.source_computer_id).await;
        let _source_guard = source_lock.lock().await;
        let source = self.record(&request.source_computer_id).await?;
        let lease = lease_view(&source).ok_or_else(BrokerError::unauthorized)?;
        if source.lifecycle.phase != ComputerPhase::Leased
            || lease.lease_id != request.source_lease_id
            || lease.epoch != request.source_epoch
            || lease.expires_at_ms <= self.clock.now_ms()
        {
            return Err(BrokerError::unauthorized());
        }
        if source
            .actions
            .values()
            .any(|action| action.lifecycle.phase != ActionPhase::Completed)
        {
            return Err(BrokerError::conflict(
                "resolve uncertain source actions before forking",
            ));
        }
        if self
            .store
            .lock()
            .await
            .state
            .credential_requests
            .values()
            .any(|request| {
                request.computer_id == source.computer_id && request.has_unresolved_effect()
            })
        {
            return Err(BrokerError::conflict(
                "resolve uncertain private input before forking",
            ));
        }
        for target in &request.targets {
            if realm(&target.owner) != realm(&source.owner) {
                return Err(BrokerError::invalid(
                    "full-state fork targets must stay in the source realm",
                ));
            }
        }
        let source_backend = source
            .backend
            .as_ref()
            .ok_or_else(|| BrokerError::conflict("source backend is not ready"))?;
        let source_secret = self.store.lock().await.worker_secret(&source.computer_id)?;
        let source_spec = ComputerSpec {
            computer_id: source.computer_id.clone(),
            ownership_id: source.ownership_id.clone(),
            profile: source.profile.clone(),
            worker_token: source_secret,
        };
        self.backend
            .supports_full_state_fork(&source_spec, source_backend)
            .await
            .map_err(|error| {
                let mut error = BrokerError::backend(error);
                error.effect = Effect::None;
                error
            })?;
        let now = self.clock.now_ms();
        if lease.expires_at_ms <= now {
            return Err(BrokerError::unauthorized());
        }
        let mut fork = ForkRecord {
            request_id: request.request_id.clone(),
            digest: request_digest,
            source_computer_id: source.computer_id.clone(),
            source_backend: source_backend.clone(),
            children: Vec::new(),
            checkpoint: None,
            lifecycle: ForkSnapshot::default(),
            started_at_ms: now,
            updated_at_ms: now,
            error: None,
        };
        let effects = fork.lifecycle.apply(ForkInput::Begin {
            total: request.targets.len() as u64,
        })?;
        expect_fork_effect(effects, ForkEffect::CaptureCheckpoint)?;
        // Lock newly generated child IDs before publishing their reservations.
        // A stop, attach, or reaper cannot race restoration of a later sibling.
        let mut child_guards = Vec::new();
        for _ in &request.targets {
            let id = uuid::Uuid::new_v4().simple().to_string();
            child_guards.push(self.computer_lock(&id).await.lock_owned().await);
            fork.children.push(id);
        }
        {
            let mut store = self.store.lock().await;
            let mut next = store.state.clone();
            if next
                .computers
                .values()
                .any(|record| targets.contains(&(record.owner.clone(), record.slot.clone())))
            {
                return Err(BrokerError::conflict(
                    "a fork target already has a computer in this slot",
                ));
            }
            for (target, id) in request.targets.into_iter().zip(&fork.children) {
                store.create_worker_secret(id, &random_secret())?;
                let mut lifecycle = LifecycleSnapshot::default();
                lifecycle.apply(ComputerInput::ReserveFork { now_ms: now })?;
                next.computers.insert(
                    id.clone(),
                    ComputerRecord {
                        computer_id: id.clone(),
                        ownership_id: uuid::Uuid::new_v4().simple().to_string(),
                        owner: target.owner,
                        slot: target.slot,
                        profile_name: source.profile_name.clone(),
                        profile: source.profile.clone(),
                        lifecycle,
                        backend: None,
                        grant: None,
                        actions: BTreeMap::new(),
                        preview: None,
                        last_backend_failure: None,
                        fork_request_id: Some(fork.request_id.clone()),
                    },
                );
            }
            next.forks.insert(fork.request_id.clone(), fork.clone());
            store.commit(next)?;
        }
        let checkpoint = match self
            .backend
            .checkpoint_create(source_backend, &fork.request_id)
            .await
        {
            Ok(checkpoint) => checkpoint,
            Err(error) => return self.fork_unknown(&fork.request_id, error).await,
        };
        if checkpoint.source != *source_backend || checkpoint.checkpoint_id != fork.request_id {
            return self
                .fork_unknown(
                    &fork.request_id,
                    BackendError::InvalidBackendState("checkpoint identity mismatch".into()),
                )
                .await;
        }
        fork.checkpoint = Some(checkpoint.clone());
        expect_fork_effect(
            fork.lifecycle.apply(ForkInput::Captured)?,
            ForkEffect::RestoreChild,
        )?;
        self.save_fork(&fork).await?;
        for (index, id) in fork.children.clone().iter().enumerate() {
            let record = self.record(id).await?;
            let secret = self.store.lock().await.worker_secret(id)?;
            let spec = ComputerSpec {
                computer_id: id.clone(),
                ownership_id: record.ownership_id.clone(),
                profile: record.profile.clone(),
                worker_token: secret,
            };
            let restored = match self.backend.fork_restore(&checkpoint, &spec).await {
                Ok(restored) => restored,
                Err(error) => return self.fork_unknown(&fork.request_id, error).await,
            };
            if restored.computer_id != *id || restored.ownership_id != record.ownership_id {
                return self
                    .fork_unknown(
                        &fork.request_id,
                        BackendError::InvalidBackendState(
                            "restored child identity mismatch".into(),
                        ),
                    )
                    .await;
            }
            let effects = fork.lifecycle.apply(ForkInput::ChildReady)?;
            if index + 1 < fork.children.len() {
                expect_fork_effect(effects, ForkEffect::RestoreChild)?;
            } else if !effects.is_empty() {
                return Err(BrokerError::storage("unexpected fork completion effect"));
            }
            fork.updated_at_ms = self.clock.now_ms();
            let mut store = self.store.lock().await;
            let mut next = store.state.clone();
            let child = next
                .computers
                .get_mut(id)
                .ok_or_else(BrokerError::missing)?;
            child.lifecycle.apply(ComputerInput::ForkReady {
                now_ms: self.clock.now_ms(),
            })?;
            child.backend = Some(restored);
            next.forks.insert(fork.request_id.clone(), fork.clone());
            store.commit(next).map_err(uncertain_storage)?;
        }
        expect_fork_effect(
            fork.lifecycle.apply(ForkInput::Release)?,
            ForkEffect::ReleaseCheckpoint,
        )?;
        self.save_fork(&fork).await?;
        if let Err(error) = self.backend.checkpoint_release(&checkpoint).await {
            return self.fork_unknown(&fork.request_id, error).await;
        }
        fork.lifecycle.apply(ForkInput::Released)?;
        self.save_fork(&fork).await?;
        self.fork_status(&fork.request_id).await
    }

    async fn save_fork(&self, fork: &ForkRecord) -> Result<(), BrokerError> {
        let mut store = self.store.lock().await;
        let mut next = store.state.clone();
        let mut fork = fork.clone();
        fork.updated_at_ms = self.clock.now_ms();
        next.forks.insert(fork.request_id.clone(), fork);
        store.commit(next).map_err(uncertain_storage)
    }
    /// Explicit recovery of the source control gate. Does not retry a snapshot or restore.
    pub async fn resume_fork_source(&self, request_id: &str) -> Result<(), BrokerError> {
        let id = canonical_request_id(request_id)?;
        let _fork_guard = self.forks.lock().await;
        let mut fork = self
            .store
            .lock()
            .await
            .state
            .forks
            .get(&id)
            .cloned()
            .ok_or_else(BrokerError::missing)?;
        let source_lock = self.computer_lock(&fork.source_computer_id).await;
        let _source_guard = source_lock.lock().await;
        if !matches!(
            fork.lifecycle.phase,
            ForkPhase::Completed | ForkPhase::Unknown
        ) {
            fork.recover()?;
            self.save_fork(&fork).await?;
        }
        expect_fork_effect(
            fork.lifecycle.apply(ForkInput::ResumeSource)?,
            ForkEffect::ResumeSource,
        )?;
        self.backend
            .resume_fork_source(&fork.source_backend, &fork.request_id)
            .await
            .map_err(BrokerError::backend)
    }
    async fn fork_unknown(&self, id: &str, error: BackendError) -> Result<ForkView, BrokerError> {
        let mut store = self.store.lock().await;
        let mut next = store.state.clone();
        let fork = next.forks.get_mut(id).ok_or_else(BrokerError::missing)?;
        fork.lifecycle.apply(ForkInput::Uncertain)?;
        let mut failure = BrokerError::unknown(
            "full-state fork outcome is uncertain; inspect reserved computers and do not replay it",
        );
        failure.backend_failure = Some(error.diagnostic());
        fork.error = Some(failure);
        fork.updated_at_ms = self.clock.now_ms();
        let view = fork.view();
        store.commit(next)?;
        Ok(view)
    }
    pub(super) async fn inspect_record(
        &self,
        record: &ComputerRecord,
        spec: &ComputerSpec,
    ) -> Result<Option<BackendComputer>, BackendError> {
        if let Some(id) = &record.fork_request_id {
            let checkpoint = self
                .store
                .lock()
                .await
                .state
                .forks
                .get(id)
                .and_then(|fork| fork.checkpoint.clone());
            return match checkpoint {
                Some(checkpoint) => self.backend.inspect_fork(&checkpoint, spec).await,
                // No restore starts until the checkpoint receipt is durably stored.
                None => Ok(None),
            };
        }
        self.backend.inspect(spec).await
    }
}
fn uncertain_storage(mut error: BrokerError) -> BrokerError {
    error.effect = Effect::Unknown;
    error
}
fn realm(owner: &ComputerOwner) -> &str {
    match owner {
        ComputerOwner::Identity { realm, .. } | ComputerOwner::Session { realm, .. } => realm,
    }
}
fn canonical_request_id(id: &str) -> Result<String, BrokerError> {
    uuid::Uuid::parse_str(id)
        .map(|id| id.to_string())
        .map_err(|_| BrokerError::invalid("fork request_id must be a UUID"))
}
fn expect_fork_effect(effects: Vec<ForkEffect>, expected: ForkEffect) -> Result<(), BrokerError> {
    if effects == [expected] {
        Ok(())
    } else {
        Err(BrokerError::storage(
            "fork authority emitted an unexpected effect",
        ))
    }
}
