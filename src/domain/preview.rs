//! Read-only projection of the latest admitted operation, never an independent capture path.
use super::*;
use base64::{Engine, engine::general_purpose::STANDARD};

/// Only a pointer. Action/request lifecycle and saved receipts remain authoritative.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum PreviewSource {
    Action {
        action_id: String,
        received_at_ms: Option<u64>,
    },
    PrivateInput {
        request_id: String,
    },
}

fn frame(
    response: &WorkerCallResponse,
    received_at_ms: u64,
    now_ms: u64,
    after: Option<&str>,
) -> Option<Value> {
    let mut observation = None;
    let mut image = None;
    for block in &response.content {
        match block.get("type")?.as_str()? {
            "text" => {
                let value: Value = serde_json::from_str(block.get("text")?.as_str()?).ok()?;
                if value
                    .get("privacy")
                    .and_then(|privacy| privacy.get("active"))
                    == Some(&Value::Bool(true))
                {
                    return None;
                }
                if value.get("observation_id").is_some() {
                    if observation.is_some() {
                        return None;
                    }
                    observation = Some(value);
                }
            }
            "image" => {
                if image.is_some() {
                    return None;
                }
                image = Some(block);
            }
            _ => return None,
        }
    }
    let observation = observation?;
    let id = observation.get("observation_id")?.as_str()?;
    if id.len() != 32
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return None;
    }
    let surface = observation.get("surface")?.as_str()?;
    let coordinate_space = match surface {
        "browser" => "viewport",
        "desktop" => "screen",
        _ => return None,
    };
    if observation.get("coordinate_space")?.as_str()? != coordinate_space
        || !matches!(observation.get("mode")?.as_str()?, "visual" | "hybrid")
    {
        return None;
    }
    let width = observation.get("width")?.as_u64()?;
    let height = observation.get("height")?.as_u64()?;
    if !(1..=3840).contains(&width) || !(1..=2160).contains(&height) {
        return None;
    }
    let image = image?;
    if image.get("mimeType")?.as_str()? != "image/png" {
        return None;
    }
    let data = image.get("data")?.as_str()?;
    if data.len() > 10_666_668 {
        return None;
    }
    let bytes = STANDARD.decode(data).ok()?;
    if bytes.len() < 33
        || bytes.len() > 8_000_000
        || &bytes[..8] != b"\x89PNG\r\n\x1a\n"
        || &bytes[8..16] != b"\0\0\0\rIHDR"
        || u32::from_be_bytes(bytes[16..20].try_into().ok()?) as u64 != width
        || u32::from_be_bytes(bytes[20..24].try_into().ok()?) as u64 != height
    {
        return None;
    }
    Some(
        json!({"observation_id":id,"surface":surface,"width":width,"height":height,
        "mime_type":"image/png","data":if after == Some(id) { None } else { Some(data) },
        "received_at_ms":received_at_ms,"age_ms":now_ms.saturating_sub(received_at_ms)}),
    )
}

impl Broker {
    /// Inspect a saved agent capture. Does not call a guest or change any authority/state.
    pub async fn computer_preview(
        &self,
        id: &str,
        after: Option<&str>,
    ) -> Result<Value, BrokerError> {
        let store = self.store.lock().await;
        let record = store
            .state
            .computers
            .get(id)
            .ok_or_else(BrokerError::missing)?;
        let mut view = saved_preview(&store, record, self.clock.now_ms(), after)?;
        view["computer"] = json!(ComputerView {
            computer_id: record.computer_id.clone(),
            owner: record.owner.clone(),
            slot: record.slot.clone(),
            profile: record.profile_name.clone(),
            phase: record.lifecycle.phase_label(),
            epoch: record.lifecycle.epoch,
            lease: lease_view(record),
            last_backend_failure: record.last_backend_failure,
        });
        Ok(view)
    }

    /// Read only the current grant's saved preview, without operator metadata.
    pub async fn preview(&self, token: &str, after: Option<&str>) -> Result<Value, BrokerError> {
        let store = self.store.lock().await;
        // Grant validation and receipt projection share this lock. Revocation,
        // takeover and private-input admission cannot interleave between them.
        let record = self.authenticate_record(&store, token)?;
        let mut view = saved_preview(&store, record, self.clock.now_ms(), after)?;
        view["computer_id"] = json!(record.computer_id);
        Ok(view)
    }
}

fn saved_preview(
    store: &Store,
    record: &ComputerRecord,
    now_ms: u64,
    after: Option<&str>,
) -> Result<Value, BrokerError> {
    if after.is_some_and(|id| id.len() > 128) {
        return Err(BrokerError::invalid("preview cursor is too long"));
    }
    let mut view = json!({"status":"unavailable","reason":"no_capture","frame":null});
    let Some(source) = &record.preview else {
        return Ok(view);
    };
    view["status"] = json!("withheld");
    match source {
        PreviewSource::PrivateInput { .. } => view["reason"] = json!("private_input"),
        PreviewSource::Action {
            action_id,
            received_at_ms,
        } => {
            view["reason"] = json!("no_safe_capture");
            if let Some(action) = record.actions.get(action_id) {
                if action.lifecycle.phase == ActionPhase::InFlight {
                    view["reason"] = json!("operation_in_flight");
                } else if action.lifecycle.phase == ActionPhase::Unknown {
                    view["reason"] = json!("outcome_unknown");
                } else if action.error.is_none()
                    && let (Some(path), Some(received_at_ms)) =
                        (&action.receipt_path, received_at_ms)
                    && let Ok(receipt) = store.read_receipt(path)
                    && let Some(frame) = frame(&receipt, *received_at_ms, now_ms, after)
                {
                    view["status"] = json!("available");
                    view["reason"] = Value::Null;
                    view["frame"] = frame;
                }
            }
        }
    }
    Ok(view)
}
