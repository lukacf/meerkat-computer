//! Frozen version-1 wire examples checked before and after the Core 0.9 upgrade.
//! They exercise restored authority, including fencing and uncertain effects.

use super::fork::{ForkInput, ForkSnapshot};
use super::host::{HostInput, HostSnapshot};
use super::*;

#[test]
fn version_one_computer_snapshot_recovers_with_new_fence() {
    let raw = serde_json::json!({
        "phase": "leased", "epoch": 7, "expires_at_ms": 500, "last_active_ms": 100
    });
    let mut state: LifecycleSnapshot = serde_json::from_value(raw.clone()).unwrap();
    assert_eq!(serde_json::to_value(&state).unwrap(), raw);
    state.apply(ComputerInput::Recover { now_ms: 200 }).unwrap();
    assert_eq!(state.phase_label(), "faulted");
    assert_eq!(state.epoch, 8);
    assert_eq!(state.expires_at_ms, 0);
    assert!(
        state
            .apply(ComputerInput::Touch {
                epoch: 7,
                now_ms: 201
            })
            .is_err()
    );
}

#[test]
fn version_one_host_snapshot_preserves_uncertain_work_on_recovery() {
    let raw = serde_json::json!({
        "phase": "awake", "active": 1, "last_activity_ms": 100, "inhibited": false
    });
    let mut state: HostSnapshot = serde_json::from_value(raw.clone()).unwrap();
    assert_eq!(serde_json::to_value(&state).unwrap(), raw);
    assert!(state.apply(HostInput::Recover).unwrap().is_empty());
    assert_eq!(state.active, 0);
    assert!(state.inhibited);
    assert!(
        state
            .apply(HostInput::Reap {
                now_ms: 1000,
                idle_ms: 1
            })
            .is_err()
    );
}

#[test]
fn version_one_pending_fork_recovers_without_replaying_checkpoint() {
    let raw = serde_json::json!({"phase": "restoring", "total": 2, "ready": 1});
    let mut state: ForkSnapshot = serde_json::from_value(raw.clone()).unwrap();
    assert_eq!(serde_json::to_value(&state).unwrap(), raw);
    assert!(state.apply(ForkInput::Recover).unwrap().is_empty());
    assert_eq!(state.phase_label(), "unknown");
    assert!(state.apply(ForkInput::ChildReady).is_err());
}

#[test]
fn version_one_inflight_action_becomes_unknown_on_recovery() {
    let raw = serde_json::json!({"phase": "in_flight"});
    let mut state: ActionSnapshot = serde_json::from_value(raw.clone()).unwrap();
    assert_eq!(serde_json::to_value(&state).unwrap(), raw);
    state.apply(ActionInput::Recover).unwrap();
    assert_eq!(serde_json::to_value(&state).unwrap()["phase"], "unknown");
    assert!(state.apply(ActionInput::Complete).is_err());
}

#[test]
fn retirement_snapshot_recovery_never_replays_a_destructive_effect() {
    for (phase, recovered) in [
        ("retiring", "retirement_unknown"),
        ("retirement_unknown", "retirement_unknown"),
        ("retired", "retired"),
    ] {
        let raw =
            serde_json::json!({"phase":phase,"epoch":9,"expires_at_ms":0,"last_active_ms":100});
        let mut state: LifecycleSnapshot = serde_json::from_value(raw.clone()).unwrap();
        assert_eq!(serde_json::to_value(&state).unwrap(), raw);
        assert!(
            state
                .apply_with_effects(ComputerInput::Recover { now_ms: 200 })
                .unwrap()
                .is_empty()
        );
        assert_eq!(state.phase_label(), recovered);
        assert!(
            state
                .apply_with_effects(ComputerInput::Reap {
                    now_ms: 10000,
                    idle_ms: 0
                })
                .unwrap()
                .is_empty()
        );
        assert_eq!(state.phase_label(), recovered);
        assert!(state.apply(ComputerInput::Stop { now_ms: 200 }).is_err());
        assert!(
            state
                .apply(ComputerInput::Attach {
                    now_ms: 200,
                    expires_at_ms: 400,
                    takeover: true
                })
                .is_err()
        );
    }
}
