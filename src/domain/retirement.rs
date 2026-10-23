//! Explicit operator retirement, with the generated lifecycle owning every phase.
use super::*;

/// The trusted operator must name the complete immutable assignment.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RetirementRequest {
    pub owner: ComputerOwner,
    pub slot: String,
    pub profile: String,
}

impl Broker {
    /// Destroy one stopped non-fork guest, retaining its audit tombstone.
    /// An uncertain destruction is never retried, even after broker restart.
    pub async fn retire(
        &self,
        computer_id: &str,
        request: RetirementRequest,
    ) -> Result<(), BrokerError> {
        request.owner.validate()?;
        validate_identifier(&request.slot, "slot")?;
        validate_identifier(&request.profile, "profile")?;
        let lock = self.computer_lock(computer_id).await;
        let _guard = lock.lock().await;
        let record = self.record(computer_id).await?;
        if record.owner != request.owner
            || record.slot != request.slot
            || record.profile_name != request.profile
        {
            return Err(BrokerError::conflict(
                "retirement does not match the immutable assignment",
            ));
        }
        if record.grant.is_some() {
            return Err(BrokerError::conflict(
                "stop and revoke control before retirement",
            ));
        }
        // A fork source/child can retain checkpoint and branch provenance. Its
        // separate lifecycle needs a complete resource-retirement contract first.
        if record.fork_request_id.is_some()
            || self.store.lock().await.state.forks.values().any(|fork| {
                fork.source_computer_id == computer_id
                    || fork.children.iter().any(|child| child == computer_id)
            })
        {
            return Err(BrokerError::conflict(
                "fork-related computers cannot be retired",
            ));
        }
        let backend = record.backend.as_ref().ok_or_else(|| {
            BrokerError::conflict("retirement requires a confirmed backend incarnation")
        })?;
        if backend.computer_id != computer_id || backend.ownership_id != record.ownership_id {
            return Err(BrokerError::storage(
                "backend incarnation does not match the assignment",
            ));
        }
        let effects = self
            .update(computer_id, |record| {
                record.lifecycle.apply_with_effects(ComputerInput::Retire {
                    now_ms: self.clock.now_ms(),
                })
            })
            .await?;
        if effects.is_empty() {
            // The machine only emits no effect for a confirmed retired tombstone.
            return Ok(());
        }
        expect_effect(effects, ComputerEffect::DestroyBackend)?;
        let epoch = self.record(computer_id).await?.lifecycle.epoch;
        // The intent is durable before dispatch. The backend verifies its saved
        // owner/incarnation and physical stopped state before removing any disk.
        if let Err(error) = self.backend.destroy(backend).await {
            self.fail_backend(computer_id, epoch, &error)
                .await
                .map_err(|_| {
                    BrokerError::unknown(
                        "retirement outcome could not be persisted; inspect the assignment",
                    )
                })?;
            return Err(BrokerError::backend(error));
        }
        self.update(computer_id, |record| {
            record.lifecycle.apply(ComputerInput::Retired { epoch })
        })
        .await
        .map_err(|_| {
            BrokerError::unknown(
                "retirement confirmation could not be persisted; inspect the assignment",
            )
        })
    }
}
