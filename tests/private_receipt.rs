//! The private transport publishes a narrow continuation, never arbitrary guest content.
use meerkat_computer::{backend::private_input_receipt, domain::ErrorCode};
use serde_json::{Value, json};

const SECRET: &str = "fixture-password-must-not-enter-a-receipt";
const OBSERVATION: &str = "0123456789abcdef0123456789abcdef";
const ACTION: &str = "abcdef0123456789abcdef0123456789";

fn continuation() -> Value {
    json!({
        "filled": true,
        "submitted": false,
        "surface": "browser",
        "observation_id": OBSERVATION,
        "action_refs": ["ref_0123456789abcdef"],
        "privacy": {"active": true, "message": "Guest explanation"}
    })
}

fn receipt(body: Value) -> Value {
    json!({
        "content": [{"type": "text", "text": body.to_string()}],
        "metrics": {
            "action_id": ACTION,
            "duration_ms": 45,
            "delta": {"ui_command_attempts": 2, "ui_commands": 2, "secure_fields_filled": 2},
            "totals": {"secure_input_attempts": 1, "secure_input_completed": 1, "clicks": 7},
        }
    })
}

fn unknown(value: Value) {
    let error = private_input_receipt(&serde_json::to_vec(&value).unwrap()).unwrap_err();
    assert_eq!(error.code, ErrorCode::UnknownEffect);
    assert!(!format!("{error:?}").contains(SECRET));
}

#[test]
fn arbitrary_guest_text_and_images_are_never_successful_private_receipts() {
    unknown(json!({"content": [{"type": "text", "text": SECRET}]}));
    unknown(json!({"content": [{"type": "image", "data": SECRET, "mimeType": "image/png"}]}));
    let mut value = receipt(continuation());
    value["content"]
        .as_array_mut()
        .unwrap()
        .push(json!({"type": "image", "data": SECRET}));
    unknown(value);
}

#[test]
fn valid_native_shape_is_reconstructed_without_guest_prose_or_unknown_metadata() {
    let mut body = continuation();
    body["privacy"]["message"] = json!(SECRET);
    body["password"] = json!(SECRET);
    let mut value = receipt(body);
    value["content"][0]["diagnostic"] = json!(SECRET);
    value["metrics"]["diagnostic"] = json!(SECRET);
    value["metrics"]["delta"][SECRET] = json!(3);
    value["metrics"]["totals"]["secret"] = json!(SECRET);
    let result = private_input_receipt(&serde_json::to_vec(&value).unwrap()).unwrap();
    let serialized = serde_json::to_string(&result).unwrap();
    assert!(!serialized.contains(SECRET));
    assert_eq!(result.content.len(), 1);
    let view: Value = serde_json::from_str(result.content[0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(view["filled"], true);
    assert_eq!(view["submitted"], false);
    assert_eq!(view["surface"], "browser");
    assert_eq!(view["observation_id"], OBSERVATION);
    assert_eq!(view["action_refs"], json!(["ref_0123456789abcdef"]));
    assert_eq!(view["privacy"]["active"], true);
    assert!(
        view["privacy"]["message"]
            .as_str()
            .unwrap()
            .contains("withheld")
    );
    assert!(view.get("password").is_none());
    assert_eq!(result.metrics["action_id"], ACTION);
    assert_eq!(result.metrics["duration_ms"], 45);
    assert_eq!(result.metrics["delta"]["secure_fields_filled"], 2);
    assert_eq!(result.metrics["totals"]["clicks"], 7);
    assert!(result.metrics["totals"].get("secret").is_none());
}

#[test]
fn malformed_continuation_identifiers_and_flags_fail_closed() {
    for (key, bad) in [
        ("observation_id", json!(SECRET)),
        ("observation_id", json!("012345")),
        ("action_refs", json!([SECRET])),
        (
            "action_refs",
            json!(["ref_0123456789abcdef", "ref_0123456789abcdef"]),
        ),
        ("filled", json!(false)),
        ("submitted", json!(true)),
        ("surface", json!("desktop")),
        ("privacy", json!({"active": false, "message": SECRET})),
    ] {
        let mut body = continuation();
        body[key] = bad;
        unknown(receipt(body));
    }
    let mut value = receipt(continuation());
    value["metrics"]["action_id"] = json!(SECRET);
    unknown(value);
}

#[test]
fn metrics_only_forward_bounded_numeric_known_counters() {
    let mut value = receipt(continuation());
    value["metrics"]["delta"]["ui_commands"] = json!(SECRET);
    value["metrics"]["delta"]["clicks"] = json!(-2);
    value["metrics"]["delta"]["observations"] = json!(1.5);
    value["metrics"]["delta"]["navigations"] = json!(true);
    value["metrics"]["totals"]["file_bytes_read"] = json!(u64::MAX);
    value["metrics"]["counts_are_lower_bounds"] = json!(true);
    let result = private_input_receipt(&serde_json::to_vec(&value).unwrap()).unwrap();
    assert_eq!(result.metrics["counts_are_lower_bounds"], true);
    assert_eq!(
        result.metrics["delta"],
        json!({"ui_command_attempts": 2, "secure_fields_filled": 2})
    );
    assert!(result.metrics["totals"].get("file_bytes_read").is_none());
    assert!(!serde_json::to_string(&result).unwrap().contains(SECRET));
}

#[test]
fn guest_error_messages_are_replaced_without_losing_effect_classification() {
    for (effect, code) in [
        ("none", ErrorCode::InvalidInput),
        ("unknown", ErrorCode::UnknownEffect),
    ] {
        let value = json!({"error": {"message": SECRET, "effect": effect}, "diagnostic": SECRET});
        let error = private_input_receipt(&serde_json::to_vec(&value).unwrap()).unwrap_err();
        assert_eq!(error.code, code);
        assert!(!format!("{error:?}").contains(SECRET));
    }
    unknown(json!({"error": {"message": SECRET, "effect": "arbitrary"}}));
}

#[test]
fn guest_error_keeps_safe_numeric_telemetry_and_marks_uncertainty() {
    let mut value = receipt(continuation());
    value["error"] = json!({"message": SECRET, "effect": "unknown"});
    value["metrics"]["delta"]["secure_fields_filled"] = json!(1);
    value["metrics"]["delta"]["clicks"] = json!(SECRET);
    value["metrics"]["totals"][SECRET] = json!(5);
    value["metrics"]["diagnostic"] = json!(SECRET);
    value["metrics"]["counts_are_lower_bounds"] = json!(false);
    let error = private_input_receipt(&serde_json::to_vec(&value).unwrap()).unwrap_err();
    assert_eq!(error.code, ErrorCode::UnknownEffect);
    let metrics = error
        .metrics
        .as_ref()
        .expect("safe attempt telemetry is retained");
    assert_eq!(metrics["action_id"], ACTION);
    assert_eq!(metrics["duration_ms"], 45);
    assert_eq!(metrics["delta"]["secure_fields_filled"], 1);
    assert_eq!(metrics["counts_are_lower_bounds"], true);
    assert!(metrics["delta"].get("clicks").is_none());
    assert!(!format!("{error:?}").contains(SECRET));

    value["error"]["effect"] = json!("none");
    let error = private_input_receipt(&serde_json::to_vec(&value).unwrap()).unwrap_err();
    assert_eq!(error.code, ErrorCode::InvalidInput);
    assert_eq!(error.metrics.unwrap()["delta"]["secure_fields_filled"], 1);
}

#[test]
fn oversized_receipts_and_reference_lists_are_rejected() {
    let mut body = continuation();
    body["action_refs"] = json!(
        (0..201)
            .map(|n| format!("ref_{n:016x}"))
            .collect::<Vec<_>>()
    );
    unknown(receipt(body));
    let mut value = receipt(continuation());
    value["diagnostic"] = json!(SECRET.repeat(2000));
    unknown(value);
}
