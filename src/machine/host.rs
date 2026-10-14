//! Demand belongs to this generated authority. Cloud observations are external effects.
use meerkat_machine_dsl::machine;
use serde::{Deserialize, Serialize};

machine! {
    machine HostLifecycle {
        version: 1,
        rust: "meerkat-computer" / "machine::host",
        state { phase: HostPhase, active: u64, last_activity_ms: u64, inhibited: bool }
        init(Cold) { active = 0, last_activity_ms = 0, inhibited = false }
        terminal []
        phase HostPhase { Cold, Waking, Awake, Suspending, Asleep }
        input HostInput {
            Recover,
            Demand { now_ms: u64 },
            Passive,
            Ready,
            WakeFailed,
            Finish { now_ms: u64, activity: bool, uncertain: bool },
            Reap { now_ms: u64, idle_ms: u64 },
            Suspended,
            SuspendFailed,
            ClearInhibit,
            Inhibit,
            Unavailable,
        }
        effect HostEffect { Wake, Suspend }
        disposition Wake => external seam NoOwnerRealization,
        disposition Suspend => external seam NoOwnerRealization,
        transition RecoverProcess {
            on input Recover
            update {
                self.inhibited = self.inhibited || self.active > 0 || self.phase == Phase::Suspending;
                self.active = 0;
            }
            to Cold
        }
        transition WakeForDemand {
            on input Demand { now_ms }
            guard { (self.phase == Phase::Cold || self.phase == Phase::Asleep) && self.active == 0 }
            update { self.active = 1; self.last_activity_ms = now_ms; }
            to Waking
            emit Wake
        }
        transition LostAvailability {
            on input Unavailable
            guard { self.phase == Phase::Awake && self.active == 0 }
            update {}
            to Cold
        }
        transition AdmitDemand {
            on input Demand { now_ms }
            guard { self.phase == Phase::Awake && self.active < 1000000 }
            update { self.active += 1; self.last_activity_ms = now_ms; }
            to Awake
        }
        transition AdmitPassive {
            on input Passive
            guard { self.phase == Phase::Awake && self.active < 1000000 }
            update { self.active += 1; }
            to Awake
        }
        transition ConfirmReady {
            on input Ready
            guard { self.phase == Phase::Waking && self.active == 1 }
            update {}
            to Awake
        }
        transition FailedWake {
            on input WakeFailed
            guard { self.phase == Phase::Waking }
            update { self.active = 0; self.inhibited = true; }
            to Cold
        }
        transition FinishRequest {
            on input Finish { now_ms, activity, uncertain }
            guard { self.phase == Phase::Awake && self.active > 0 }
            update {
                self.active -= 1;
                self.inhibited = self.inhibited || uncertain;
                self.last_activity_ms = if activity { now_ms } else { self.last_activity_ms };
            }
            to Awake
        }
        transition SuspendIdle {
            on input Reap { now_ms, idle_ms }
            guard { (self.phase == Phase::Awake || self.phase == Phase::Cold) && self.active == 0 && !self.inhibited && self.last_activity_ms > 0 && idle_ms > 0 && now_ms >= self.last_activity_ms && now_ms - self.last_activity_ms >= idle_ms }
            update {}
            to Suspending
            emit Suspend
        }
        transition ConfirmSuspended {
            on input Suspended
            guard { self.phase == Phase::Suspending }
            update {}
            to Asleep
        }
        transition FailedSuspend {
            on input SuspendFailed
            guard { self.phase == Phase::Suspending }
            update { self.inhibited = true; }
            to Cold
        }
        transition OperatorReconciled {
            per_phase [Cold, Awake, Asleep]
            on input ClearInhibit
            guard { self.active == 0 && self.phase != Phase::Waking && self.phase != Phase::Suspending }
            update { self.inhibited = false; }
            to Cold
        }
        transition OperatorInhibited {
            per_phase [Cold, Awake, Asleep]
            on input Inhibit
            update { self.inhibited = true; }
            to Cold
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(remote = "HostPhase", rename_all = "snake_case")]
enum HostPhaseSerde {
    Cold,
    Waking,
    Awake,
    Suspending,
    Asleep,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct HostSnapshot {
    #[serde(with = "HostPhaseSerde")]
    pub phase: HostPhase,
    pub active: u64,
    pub last_activity_ms: u64,
    pub inhibited: bool,
}
impl Default for HostSnapshot {
    fn default() -> Self {
        Self::from_state(HostLifecycleAuthority::new().state())
    }
}
impl HostSnapshot {
    fn from_state(s: &HostLifecycleState) -> Self {
        Self {
            phase: s.phase,
            active: s.active,
            last_activity_ms: s.last_activity_ms,
            inhibited: s.inhibited,
        }
    }
    pub fn apply(&mut self, input: HostInput) -> Result<Vec<HostEffect>, String> {
        let mut a = HostLifecycleAuthority::recover_from_state(HostLifecycleState {
            phase: self.phase,
            active: self.active,
            last_activity_ms: self.last_activity_ms,
            inhibited: self.inhibited,
        })
        .map_err(|_| "invalid host authority")?;
        let result =
            HostLifecycleMutator::apply(&mut a, input).map_err(|_| "host transition refused")?;
        *self = Self::from_state(a.state());
        Ok(result.effects)
    }
}
