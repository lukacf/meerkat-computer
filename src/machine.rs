//! Canonical lifecycle changes are generated from the Meerkat machine DSL.
//! Persistence structs below are mechanical snapshots, never reducers.

use meerkat_machine_dsl::machine;
use serde::{Deserialize, Serialize};

machine! {
    machine ComputerLifecycle {
        version: 2,
        rust: "meerkat-computer" / "machine",
        state {
            phase: ComputerPhase,
            epoch: u64,
            expires_at_ms: u64,
            last_active_ms: u64,
        }
        init(Stopped) { epoch = 0, expires_at_ms = 0, last_active_ms = 0 }
        terminal [Retired]
        phase ComputerPhase { Stopped, Preparing, Leased, Idle, Revoking, Stopping, Faulted, Forking, Retiring, Retired, RetirementUnknown }
        input ComputerInput {
            ReserveFork { now_ms: u64 },
            ForkReady { now_ms: u64 },
            Attach { now_ms: u64, expires_at_ms: u64, takeover: bool },
            BackendReady { epoch: u64 },
            Ready { epoch: u64, now_ms: u64 },
            Renew { epoch: u64, now_ms: u64, expires_at_ms: u64 },
            Touch { epoch: u64, now_ms: u64 },
            Revoke { now_ms: u64 },
            Fenced { epoch: u64 },
            Retire { now_ms: u64 },
            Retired { epoch: u64 },
            Stop { now_ms: u64 },
            Stopped { epoch: u64 },
            Failed { epoch: u64 },
            Recover { now_ms: u64 },
            Reap { now_ms: u64, idle_ms: u64 },
        }
        effect ComputerEffect { StartBackend, InstallWorkerGrant, FenceWorker, StopBackend, DestroyBackend }
        disposition StartBackend => external seam NoOwnerRealization,
        disposition InstallWorkerGrant => external seam NoOwnerRealization,
        disposition FenceWorker => external seam NoOwnerRealization,
        disposition StopBackend => external seam NoOwnerRealization,
        disposition DestroyBackend => external seam NoOwnerRealization,
        invariant epoch_in_use_is_positive {
            self.phase != Phase::Leased || self.epoch > 0
        }
        invariant active_has_expiry {
            self.phase != Phase::Leased || self.expires_at_ms > 0
        }
        transition ReserveForkChild {
            on input ReserveFork { now_ms }
            guard { self.phase == Phase::Stopped && self.epoch == 0 }
            update { self.last_active_ms = now_ms; }
            to Forking
        }
        transition ConfirmForkChild {
            on input ForkReady { now_ms }
            guard { self.phase == Phase::Forking }
            update { self.last_active_ms = now_ms; }
            to Idle
        }
        transition ReapPendingFork {
            on input Reap { now_ms, idle_ms }
            guard { self.phase == Phase::Forking }
            update {}
            to Forking
        }
        transition AdmitAttachment {
            on input Attach { now_ms, expires_at_ms, takeover }
            guard {
                self.epoch < 9223372036854775807
                && expires_at_ms > now_ms
                && (self.phase == Phase::Stopped || self.phase == Phase::Idle || self.phase == Phase::Faulted
                    || (self.phase == Phase::Leased && (self.expires_at_ms <= now_ms || takeover)))
            }
            update {
                self.epoch += 1;
                self.expires_at_ms = expires_at_ms;
                self.last_active_ms = now_ms;
            }
            to Preparing
            emit StartBackend
        }
        transition InstallGrantOnReadyBackend {
            on input BackendReady { epoch }
            guard { self.phase == Phase::Preparing && self.epoch == epoch }
            update {}
            to Preparing
            emit InstallWorkerGrant
        }
        transition ConfirmWorkerGrant {
            on input Ready { epoch, now_ms }
            guard { self.phase == Phase::Preparing && self.epoch == epoch && self.expires_at_ms > now_ms }
            update {}
            to Leased
        }
        transition RenewCurrentGrant {
            on input Renew { epoch, now_ms, expires_at_ms }
            guard { self.phase == Phase::Leased && self.epoch == epoch && self.expires_at_ms > now_ms && expires_at_ms > now_ms }
            update { self.expires_at_ms = expires_at_ms; self.last_active_ms = now_ms; }
            to Leased
            emit InstallWorkerGrant
        }
        transition AdmitCurrentAction {
            on input Touch { epoch, now_ms }
            guard { self.phase == Phase::Leased && self.epoch == epoch && self.expires_at_ms > now_ms }
            update { self.last_active_ms = now_ms; }
            to Leased
        }
        transition RevokeCurrentGrant {
            on input Revoke { now_ms }
            guard { self.epoch < 9223372036854775807 && (self.phase == Phase::Leased || self.phase == Phase::Idle || self.phase == Phase::Faulted) }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Revoking
            emit FenceWorker
        }
        transition ConfirmRevocation {
            on input Fenced { epoch }
            guard { self.phase == Phase::Revoking && self.epoch == epoch }
            update {}
            to Idle
        }
        transition RetireStoppedComputer {
            on input Retire { now_ms }
            guard { self.phase == Phase::Stopped && self.epoch < 9223372036854775807 }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Retiring
            emit DestroyBackend
        }
        transition ConfirmRetirement {
            on input Retired { epoch }
            guard { self.phase == Phase::Retiring && self.epoch == epoch }
            update {}
            to Retired
        }
        transition RepeatConfirmedRetirement {
            on input Retire { now_ms }
            guard { self.phase == Phase::Retired }
            update {}
            to Retired
        }
        transition RetirementFailed {
            on input Failed { epoch }
            guard { self.phase == Phase::Retiring && self.epoch == epoch }
            update { self.expires_at_ms = 0; }
            to RetirementUnknown
        }
        transition RecoverInterruptedRetirement {
            on input Recover { now_ms }
            guard { self.phase == Phase::Retiring }
            update { self.expires_at_ms = 0; }
            to RetirementUnknown
        }
        transition RecoverUnknownRetirement {
            on input Recover { now_ms }
            guard { self.phase == Phase::RetirementUnknown }
            update {}
            to RetirementUnknown
        }
        transition RecoverRetired {
            on input Recover { now_ms }
            guard { self.phase == Phase::Retired }
            update {}
            to Retired
        }
        transition ReapRetiring {
            on input Reap { now_ms, idle_ms }
            guard { self.phase == Phase::Retiring }
            update {}
            to Retiring
        }
        transition ReapRetirementUnknown {
            on input Reap { now_ms, idle_ms }
            guard { self.phase == Phase::RetirementUnknown }
            update {}
            to RetirementUnknown
        }
        transition ReapRetired {
            on input Reap { now_ms, idle_ms }
            guard { self.phase == Phase::Retired }
            update {}
            to Retired
        }
        transition StopComputer {
            on input Stop { now_ms }
            guard { self.epoch < 9223372036854775807 && self.phase != Phase::Retiring && self.phase != Phase::Retired && self.phase != Phase::RetirementUnknown }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Stopping
            emit StopBackend
        }
        transition ConfirmStopped {
            on input Stopped { epoch }
            guard { self.phase == Phase::Stopping && self.epoch == epoch }
            update {}
            to Stopped
        }
        transition RecordFailure {
            on input Failed { epoch }
            guard { self.epoch == epoch && self.phase != Phase::Stopped && self.phase != Phase::Retiring && self.phase != Phase::Retired && self.phase != Phase::RetirementUnknown }
            update { self.expires_at_ms = 0; }
            to Faulted
        }
        transition RecoverStopped {
            on input Recover { now_ms }
            guard { self.phase == Phase::Stopped && self.epoch < 9223372036854775807 }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Stopped
        }
        transition RecoverUncertain {
            on input Recover { now_ms }
            guard { self.phase != Phase::Stopped && self.phase != Phase::Retiring && self.phase != Phase::Retired && self.phase != Phase::RetirementUnknown && self.epoch < 9223372036854775807 }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Faulted
        }
        transition ReapExpiredGrant {
            on input Reap { now_ms, idle_ms }
            guard { self.phase == Phase::Leased && self.expires_at_ms <= now_ms && self.epoch < 9223372036854775807 }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Revoking
            emit FenceWorker
        }
        transition ReapIdleComputer {
            on input Reap { now_ms, idle_ms }
            guard { (self.phase == Phase::Idle || self.phase == Phase::Faulted) && self.epoch < 9223372036854775807 && now_ms >= self.last_active_ms && now_ms - self.last_active_ms >= idle_ms }
            update { self.epoch += 1; self.expires_at_ms = 0; self.last_active_ms = now_ms; }
            to Stopping
            emit StopBackend
        }
        transition ReapStillLeased {
            on input Reap { now_ms, idle_ms }
            guard { self.phase == Phase::Leased && self.expires_at_ms > now_ms }
            update {}
            to Leased
        }
        transition ReapWaitIdle {
            per_phase [Idle, Faulted]
            on input Reap { now_ms, idle_ms }
            guard { now_ms < self.last_active_ms || now_ms - self.last_active_ms < idle_ms }
            update {}
            to Idle
        }
        transition ReapNoAction {
            per_phase [Stopped, Preparing, Revoking, Stopping]
            on input Reap { now_ms, idle_ms }
            update {}
            to Stopped
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "ComputerPhase", rename_all = "snake_case")]
enum ComputerPhaseSerde {
    Stopped,
    Preparing,
    Leased,
    Idle,
    Revoking,
    Stopping,
    Faulted,
    Forking,
    Retiring,
    Retired,
    RetirementUnknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LifecycleSnapshot {
    #[serde(with = "ComputerPhaseSerde")]
    pub phase: ComputerPhase,
    pub epoch: u64,
    pub expires_at_ms: u64,
    pub last_active_ms: u64,
}

pub(crate) mod fork;
pub(crate) mod host;

#[cfg(test)]
mod compatibility;

impl Default for LifecycleSnapshot {
    fn default() -> Self {
        Self::from_state(ComputerLifecycleAuthority::new().state())
    }
}

impl LifecycleSnapshot {
    fn from_state(state: &ComputerLifecycleState) -> Self {
        Self {
            phase: state.phase,
            epoch: state.epoch,
            expires_at_ms: state.expires_at_ms,
            last_active_ms: state.last_active_ms,
        }
    }
    pub fn apply(&mut self, input: ComputerInput) -> Result<(), crate::BrokerError> {
        self.apply_with_effects(input).map(|_| ())
    }
    pub fn apply_with_effects(
        &mut self,
        input: ComputerInput,
    ) -> Result<Vec<ComputerEffect>, crate::BrokerError> {
        let mut authority =
            ComputerLifecycleAuthority::recover_from_state(ComputerLifecycleState {
                phase: self.phase,
                epoch: self.epoch,
                expires_at_ms: self.expires_at_ms,
                last_active_ms: self.last_active_ms,
            })
            .map_err(|_| crate::BrokerError::storage("invalid recovered computer authority"))?;
        let transition = ComputerLifecycleMutator::apply(&mut authority, input).map_err(|_| {
            crate::BrokerError::conflict("computer lifecycle refused this operation")
        })?;
        *self = Self::from_state(authority.state());
        Ok(transition.effects)
    }
    pub fn phase_label(&self) -> String {
        serde_json::to_value(self).expect("snapshot serialization")["phase"]
            .as_str()
            .expect("phase string")
            .into()
    }
}

mod operation {
    use meerkat_machine_dsl::machine;
    // The DSL elides an empty effect enum; this machine emits no effects.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum ActionEffect {}
    machine! {
        machine ActionLifecycle {
            version: 1,
            rust: "meerkat-computer" / "machine::operation",
            state { phase: ActionPhase }
            init(InFlight) {}
            terminal [Completed, Unknown]
            phase ActionPhase { InFlight, Completed, Unknown }
            input ActionInput { Complete, Uncertain, Recover }
            effect ActionEffect {}
            transition CompleteAction {
                on input Complete
                guard { self.phase == Phase::InFlight }
                update {}
                to Completed
            }
            transition LoseReceipt {
                on input Uncertain
                guard { self.phase == Phase::InFlight }
                update {}
                to Unknown
            }
            transition RecoverPending {
                on input Recover
                guard { self.phase == Phase::InFlight }
                update {}
                to Unknown
            }
            transition RecoverCompleted {
                on input Recover
                guard { self.phase == Phase::Completed }
                update {}
                to Completed
            }
            transition RecoverUnknown {
                on input Recover
                guard { self.phase == Phase::Unknown }
                update {}
                to Unknown
            }
        }
    }
}
pub(crate) use operation::{ActionInput, ActionPhase};

#[derive(Serialize, Deserialize)]
#[serde(remote = "ActionPhase", rename_all = "snake_case")]
enum ActionPhaseSerde {
    InFlight,
    Completed,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct ActionSnapshot {
    #[serde(with = "ActionPhaseSerde")]
    pub phase: ActionPhase,
}
impl Default for ActionSnapshot {
    fn default() -> Self {
        Self {
            phase: operation::ActionLifecycleAuthority::new().state().phase,
        }
    }
}
impl ActionSnapshot {
    pub fn apply(&mut self, input: ActionInput) -> Result<(), crate::BrokerError> {
        let mut authority = operation::ActionLifecycleAuthority::recover_from_state(
            operation::ActionLifecycleState { phase: self.phase },
        )
        .map_err(|_| crate::BrokerError::storage("invalid recovered action authority"))?;
        operation::ActionLifecycleMutator::apply(&mut authority, input)
            .map_err(|_| crate::BrokerError::conflict("action lifecycle refused this operation"))?;
        self.phase = authority.state().phase;
        Ok(())
    }
}
