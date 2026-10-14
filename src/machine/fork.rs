//! One owner for checkpoint and branch completion. The shell realizes effects.
use meerkat_machine_dsl::machine;
use serde::{Deserialize, Serialize};

machine! {
    machine ForkLifecycle {
        version: 1,
        rust: "meerkat-computer" / "machine::fork",
        state { phase: ForkPhase, total: u64, ready: u64 }
        init(Admitted) { total = 0, ready = 0 }
        terminal [Completed, Unknown]
        phase ForkPhase { Admitted, Capturing, Restoring, Releasing, Completed, Unknown }
        input ForkInput { Begin { total: u64 }, Captured, ChildReady, Release, Released, Uncertain, Recover, ResumeSource }
        effect ForkEffect { CaptureCheckpoint, RestoreChild, ReleaseCheckpoint, ResumeSource }
        disposition CaptureCheckpoint => external seam NoOwnerRealization,
        disposition RestoreChild => external seam NoOwnerRealization,
        disposition ReleaseCheckpoint => external seam NoOwnerRealization,
        disposition ResumeSource => external seam NoOwnerRealization,
        invariant ready_within_total { self.ready <= self.total }
        invariant complete_has_all_children { self.phase != Phase::Completed || (self.total > 0 && self.ready == self.total) }
        transition AdmitCapture {
            on input Begin { total }
            guard { self.phase == Phase::Admitted && total > 0 && total <= 16 }
            update { self.total = total; }
            to Capturing
            emit CaptureCheckpoint
        }
        transition RestoreFirst {
            on input Captured
            guard { self.phase == Phase::Capturing }
            update {}
            to Restoring
            emit RestoreChild
        }
        transition RestoreNext {
            on input ChildReady
            guard { self.phase == Phase::Restoring && self.ready + 1 < self.total }
            update { self.ready += 1; }
            to Restoring
            emit RestoreChild
        }
        transition RestoredAll {
            on input ChildReady
            guard { self.phase == Phase::Restoring && self.ready + 1 == self.total }
            update { self.ready += 1; }
            to Restoring
        }
        transition ReleaseCapturedState {
            on input Release
            guard { self.phase == Phase::Restoring && self.ready == self.total }
            update {}
            to Releasing
            emit ReleaseCheckpoint
        }
        transition CompleteFork {
            on input Released
            guard { self.phase == Phase::Releasing }
            update {}
            to Completed
        }
        transition LostOutcome {
            on input Uncertain
            guard { self.phase != Phase::Completed && self.phase != Phase::Unknown }
            update {}
            to Unknown
        }
        transition RecoverPending {
            on input Recover
            guard { self.phase != Phase::Completed && self.phase != Phase::Unknown }
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
        transition RecoverSourceControl {
            on input ResumeSource
            guard { self.phase == Phase::Unknown }
            update {}
            to Unknown
            emit ResumeSource
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "ForkPhase", rename_all = "snake_case")]
enum ForkPhaseSerde {
    Admitted,
    Capturing,
    Restoring,
    Releasing,
    Completed,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ForkSnapshot {
    #[serde(with = "ForkPhaseSerde")]
    pub phase: ForkPhase,
    pub total: u64,
    pub ready: u64,
}
impl Default for ForkSnapshot {
    fn default() -> Self {
        let authority = ForkLifecycleAuthority::new();
        let state = authority.state();
        Self {
            phase: state.phase,
            total: state.total,
            ready: state.ready,
        }
    }
}
impl ForkSnapshot {
    pub fn apply(&mut self, input: ForkInput) -> Result<Vec<ForkEffect>, crate::BrokerError> {
        let mut authority = ForkLifecycleAuthority::recover_from_state(ForkLifecycleState {
            phase: self.phase,
            total: self.total,
            ready: self.ready,
        })
        .map_err(|_| crate::BrokerError::storage("invalid recovered fork authority"))?;
        let transition = ForkLifecycleMutator::apply(&mut authority, input)
            .map_err(|_| crate::BrokerError::conflict("fork lifecycle refused operation"))?;
        let state = authority.state();
        *self = Self {
            phase: state.phase,
            total: state.total,
            ready: state.ready,
        };
        Ok(transition.effects)
    }
    pub fn phase_label(&self) -> String {
        serde_json::to_value(self).expect("fork snapshot serialization")["phase"]
            .as_str()
            .expect("fork phase string")
            .into()
    }
}
