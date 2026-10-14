//! Always-on, trusted demand gateway. Guest grants remain broker authority.

use crate::{
    Clock,
    machine::host::{HostEffect, HostInput, HostPhase, HostSnapshot},
    store,
};
use async_trait::async_trait;
use axum::{
    Router,
    body::to_bytes,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode},
    response::{IntoResponse, Response},
};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};
use subtle::ConstantTimeEq;
use tokio::sync::Mutex;

pub mod command;

pub const HOST_HEADER: &str = "x-meerkat-computer-host-token";

/// Trusted configuration only. None of these values comes from an agent call.
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GatewayConfig {
    pub listen: String,
    pub broker_url: String,
    pub state_dir: PathBuf,
    pub host_token_env: String,
    pub idle_suspend_seconds: u64,
    #[serde(default = "request_timeout")]
    pub request_timeout_seconds: u64,
    #[serde(default = "wake_timeout")]
    pub wake_timeout_seconds: u64,
    #[serde(default)]
    pub wake_command: Vec<String>,
    #[serde(default)]
    pub suspend_command: Vec<String>,
    #[serde(default)]
    pub tunnel_command: Vec<String>,
}
fn request_timeout() -> u64 {
    300
}
fn wake_timeout() -> u64 {
    420
}

impl GatewayConfig {
    pub fn load(path: &Path) -> Result<Self, String> {
        let config: Self = toml::from_str(
            &std::fs::read_to_string(path).map_err(|_| "gateway config unreadable")?,
        )
        .map_err(|_| "invalid gateway config")?;
        config.validate()?;
        if config.wake_command.is_empty() || config.suspend_command.is_empty() {
            return Err("gateway requires explicit wake and suspend commands".into());
        }
        Ok(config)
    }
    pub fn validate(&self) -> Result<(), String> {
        let listen: SocketAddr = self
            .listen
            .parse()
            .map_err(|_| "invalid gateway listen address")?;
        let url = reqwest::Url::parse(&self.broker_url).map_err(|_| "invalid broker URL")?;
        if !listen.ip().is_loopback()
            || listen.port() == 0
            || url.scheme() != "http"
            || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
            || url.port_or_known_default() == Some(0)
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || url.path() != "/"
            || url.port_or_known_default() == Some(listen.port())
            || self.request_timeout_seconds == 0
            || self.request_timeout_seconds > 3600
            || self.wake_timeout_seconds == 0
            || self.wake_timeout_seconds > 1800
            || self.idle_suspend_seconds > 604800
            || !self.state_dir.is_absolute()
            || self.host_token_env.is_empty()
            || !self
                .host_token_env
                .bytes()
                .enumerate()
                .all(|(i, b)| b.is_ascii_alphabetic() || b == b'_' || (i > 0 && b.is_ascii_digit()))
            || [
                &self.wake_command,
                &self.suspend_command,
                &self.tunnel_command,
            ]
            .into_iter()
            .any(|cmd| cmd.iter().any(|s| s.contains('\0')))
        {
            return Err("invalid gateway configuration".into());
        }
        Ok(())
    }
    #[doc(hidden)]
    pub fn for_test(broker_url: String, state_dir: PathBuf, idle_suspend_seconds: u64) -> Self {
        Self {
            listen: "127.0.0.1:18761".into(),
            broker_url,
            state_dir,
            host_token_env: "MC_HOST_TOKEN".into(),
            idle_suspend_seconds,
            request_timeout_seconds: 30,
            wake_timeout_seconds: 30,
            wake_command: vec![],
            suspend_command: vec![],
            tunnel_command: vec![],
        }
    }
}

/// Effects verify the exact provisioned host, including its immutable resource ID.
#[async_trait]
pub trait HostPower: Send + Sync {
    async fn ready(&self) -> bool {
        true
    }
    async fn wake(&self) -> Result<(), String>;
    async fn suspend(&self) -> Result<(), String>;
}

struct GatewayState {
    snapshot: HostSnapshot,
    path: PathBuf,
    _lock: File,
}
impl GatewayState {
    fn open(path: &Path) -> Result<Self, String> {
        store::private_dir(path).map_err(|_| "gateway state directory unavailable")?;
        let lock = store::private_options()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path.join("gateway.lock"))
            .map_err(|_| "gateway lock unavailable")?;
        lock.try_lock_exclusive()
            .map_err(|_| "another gateway owns this state")?;
        let state_path = path.join("gateway.json");
        let snapshot = match std::fs::read(&state_path) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|_| "invalid gateway state")?,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => HostSnapshot::default(),
            Err(_) => return Err("gateway state unreadable".into()),
        };
        let mut this = Self {
            snapshot,
            path: state_path,
            _lock: lock,
        };
        this.apply(HostInput::Recover)?;
        Ok(this)
    }
    fn apply(&mut self, input: HostInput) -> Result<Vec<HostEffect>, String> {
        let mut next = self.snapshot.clone();
        let effects = next.apply(input)?;
        let bytes = serde_json::to_vec(&next).map_err(|_| "gateway state encoding failed")?;
        store::atomic_write(&self.path, &bytes).map_err(|_| "gateway state persistence failed")?;
        self.snapshot = next;
        Ok(effects)
    }
}

pub struct Gateway {
    config: GatewayConfig,
    host_hash: [u8; 32],
    state: Mutex<GatewayState>,
    power: Arc<dyn HostPower>,
    clock: Arc<dyn Clock>,
    http: reqwest::Client,
}
impl Gateway {
    pub fn open(
        config: GatewayConfig,
        host_token: &str,
        power: Arc<dyn HostPower>,
        clock: Arc<dyn Clock>,
    ) -> Result<Arc<Self>, String> {
        config.validate()?;
        if host_token.len() < 32
            || host_token.len() > 4096
            || host_token.bytes().any(|b| !b.is_ascii_graphic())
        {
            return Err("invalid gateway credential".into());
        }
        let state = GatewayState::open(&config.state_dir)?;
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(config.request_timeout_seconds))
            .build()
            .map_err(|_| "gateway HTTP setup failed")?;
        Ok(Arc::new(Self {
            config,
            host_hash: Sha256::digest(host_token.as_bytes()).into(),
            state: Mutex::new(state),
            power,
            clock,
            http,
        }))
    }
    pub fn router(self: &Arc<Self>) -> Router {
        Router::new().fallback(handle).with_state(self.clone())
    }

    async fn begin(&self, activity: bool) -> Result<(), &'static str> {
        // Serialize the entire wake/suspend effect. Concurrent demand joins this wake.
        // Bound waiting before admission. Once admitted, the owned task finishes even
        // if its HTTP caller disconnects; cancelling a cloud effect is not a rollback.
        let mut state = tokio::time::timeout(
            Duration::from_secs(self.config.wake_timeout_seconds.min(30)),
            self.state.lock(),
        )
        .await
        .map_err(|_| "host_busy")?;
        if activity && state.snapshot.phase == HostPhase::Awake && !self.power.ready().await {
            state
                .apply(HostInput::Unavailable)
                .map_err(|_| "host_busy")?;
        }
        let effects = state
            .apply(if activity {
                HostInput::Demand {
                    now_ms: self.clock.now_ms(),
                }
            } else {
                HostInput::Passive
            })
            .map_err(|_| {
                if activity {
                    "host_unavailable"
                } else {
                    "host_asleep"
                }
            })?;
        for effect in effects {
            if effect != HostEffect::Wake {
                return Err("host_unavailable");
            }
            if self.power.wake().await.is_err() {
                state
                    .apply(HostInput::WakeFailed)
                    .map_err(|_| "host_unavailable")?;
                return Err("host_unavailable");
            }
            state
                .apply(HostInput::Ready)
                .map_err(|_| "host_unavailable")?;
        }
        Ok(())
    }
    async fn finish(&self, activity: bool, uncertain: bool) -> Result<(), String> {
        self.state
            .lock()
            .await
            .apply(HostInput::Finish {
                now_ms: self.clock.now_ms(),
                activity,
                uncertain,
            })
            .map(|_| ())
    }
    /// Single periodic pass. Zero disables automatic suspension.
    pub async fn reap(&self) -> Result<bool, String> {
        let Ok(mut state) = self.state.try_lock() else {
            return Ok(false);
        };
        let effects = match state.apply(HostInput::Reap {
            now_ms: self.clock.now_ms(),
            idle_ms: self.config.idle_suspend_seconds.saturating_mul(1000),
        }) {
            Ok(effects) => effects,
            Err(e) if e == "host transition refused" => return Ok(false),
            Err(e) => return Err(e),
        };
        if effects != vec![HostEffect::Suspend] {
            return Err("unexpected host effect".into());
        }
        if self.power.suspend().await.is_err() {
            state.apply(HostInput::SuspendFailed)?;
            return Err("host suspension failed; automatic suspension inhibited".into());
        }
        state.apply(HostInput::Suspended)?;
        Ok(true)
    }

    async fn forward(
        self: Arc<Self>,
        method: Method,
        path: String,
        headers: HeaderMap,
        body: Vec<u8>,
        activity: bool,
    ) -> Response {
        if let Err(code) = self.begin(activity).await {
            return error(code, "none");
        }
        if path == "/host/wake" {
            return match self.finish(true, false).await {
                Ok(()) => axum::Json(json!({"ready":true})).into_response(),
                Err(_) => error("host_unavailable", "none"),
            };
        }
        // Credentials terminate at their intended authority. Never forward host_token.
        let mut builder = self
            .http
            .request(
                method.clone(),
                format!("{}{}", self.config.broker_url.trim_end_matches('/'), path),
            )
            .body(body);
        for key in ["authorization", "content-type", "accept"] {
            if let Some(value) = headers.get(key) {
                builder = builder.header(key, value);
            }
        }
        let response = builder.send().await;
        let read_only = matches!(method, Method::GET | Method::HEAD | Method::OPTIONS);
        let mut uncertain = !read_only;
        let outcome = match response {
            Ok(mut response) => {
                let status = response.status();
                let response_headers = response.headers().clone();
                let mut data = Vec::new();
                let mut complete = true;
                let response_limit = if method == Method::GET && file_route(&path) == Some(true) {
                    crate::config::MAX_TRANSFER_BYTES as usize
                } else {
                    32 * 1024 * 1024
                };
                loop {
                    match response.chunk().await {
                        Ok(Some(chunk)) if data.len() + chunk.len() <= response_limit => {
                            data.extend_from_slice(&chunk)
                        }
                        Ok(None) => break,
                        _ => {
                            complete = false;
                            break;
                        }
                    }
                }
                if complete && method == Method::HEAD {
                    (status, [("cache-control", "no-store")], Vec::<u8>::new()).into_response()
                } else if complete
                    && method == Method::GET
                    && file_route(&path) == Some(true)
                    && status.is_success()
                {
                    if response_headers
                        .get("content-type")
                        .is_some_and(|value| value == "application/octet-stream")
                        && data.len() as u64 <= crate::config::MAX_TRANSFER_BYTES
                    {
                        let mut reply = (status, data).into_response();
                        for key in ["content-type", "content-disposition", "x-content-sha256"] {
                            if let Some(value) = response_headers.get(key) {
                                reply.headers_mut().insert(key, value.clone());
                            }
                        }
                        reply
                            .headers_mut()
                            .insert("cache-control", HeaderValue::from_static("no-store"));
                        reply.headers_mut().insert(
                            "x-content-type-options",
                            HeaderValue::from_static("nosniff"),
                        );
                        reply
                    } else {
                        error("host_effect_unknown", "none")
                    }
                } else if complete
                    && let Ok(value) = serde_json::from_slice::<serde_json::Value>(&data)
                {
                    // An explicit unknown broker receipt is not quiescence proof.
                    uncertain = !read_only && !confirmed_response(&path, status, &value);
                    (
                        status,
                        [
                            ("content-type", "application/json"),
                            ("cache-control", "no-store"),
                        ],
                        data,
                    )
                        .into_response()
                } else {
                    error(
                        "host_effect_unknown",
                        if uncertain { "unknown" } else { "none" },
                    )
                }
            }
            Err(_) => error(
                "host_effect_unknown",
                if uncertain { "unknown" } else { "none" },
            ),
        };
        if self.finish(activity, uncertain).await.is_err() {
            return error(
                "host_effect_unknown",
                if read_only { "none" } else { "unknown" },
            );
        }
        outcome
    }
}

// Completion is established by the existing broker wire contract. A JSON object
// or an HTTP status alone does not prove that a dispatched operation finished.
fn confirmed_response(path: &str, status: StatusCode, value: &serde_json::Value) -> bool {
    let path = path.split('?').next().unwrap_or(path);
    if let Some(error) = value.get("error") {
        // Fork views include a nullable error field in their established contract.
        if path != "/v1/forks" || !error.is_null() {
            return error.get("effect").and_then(|e| e.as_str()) == Some("none");
        }
    }
    if !status.is_success() {
        return false;
    }
    match path {
        p if p == "/v1/tools/call"
            || (p.starts_with("/v1/credential-requests/") && p.ends_with("/fulfill")) =>
        {
            value
                .get("content")
                .and_then(|v| v.as_array())
                .is_some_and(|blocks| {
                    blocks
                        .iter()
                        .all(|block| match block.get("type").and_then(|v| v.as_str()) {
                            Some("text") => block.get("text").is_some_and(|v| v.is_string()),
                            Some("image") => {
                                block.get("data").is_some_and(|v| v.is_string())
                                    && block.get("mimeType").is_some_and(|v| v.is_string())
                            }
                            _ => false,
                        })
                })
        }
        "/v1/attachments" => {
            serde_json::from_value::<crate::domain::Attachment>(value.clone()).is_ok()
        }
        "/v1/forks" => serde_json::from_value::<crate::domain::ForkView>(value.clone()).is_ok(),
        "/v1/lease/renew" => value
            .get("lease")
            .is_some_and(|v| serde_json::from_value::<crate::domain::LeaseView>(v.clone()).is_ok()),
        "/v1/lease/release" => value.get("released") == Some(&json!(true)),
        "/v1/reap" => value.get("stopped").is_some_and(|v| v.is_array()),
        "/v1/credentials/assign" => value.get("assigned") == Some(&json!(true)),
        "/v1/credentials/unassign" => value.get("unassigned") == Some(&json!(true)),
        p if p.ends_with("/resume-source") => value.get("source_resumed") == Some(&json!(true)),
        p if p.ends_with("/stop") => value.get("stopped").is_some_and(|v| v.is_string()),
        p if p.starts_with("/v1/credential") => {
            serde_json::from_value::<crate::credentials::CredentialMetadata>(value.clone()).is_ok()
                || value.get("deleted").is_some_and(|v| v.is_string())
                || value.get("cancelled").is_some_and(|v| v.is_string())
        }
        p if file_route(p) == Some(false) => {
            serde_json::from_value::<crate::FileMetadata>(value.clone()).is_ok()
        }
        p if file_route(p) == Some(true) => value
            .get("deleted")
            .and_then(|v| v.as_str())
            .is_some_and(|id| p.ends_with(&format!("/{id}")) && uuid::Uuid::parse_str(id).is_ok()),
        _ => false,
    }
}

// Exact broker route shape, not a substring or arbitrary upload endpoint.
fn file_route(path: &str) -> Option<bool> {
    let path = path.split('?').next()?;
    let parts: Vec<_> = path.split('/').collect();
    if !(parts.len() == 5 || parts.len() == 6)
        || !parts[0].is_empty()
        || parts[1] != "v1"
        || parts[2] != "computers"
        || parts[4] != "files"
        || uuid::Uuid::parse_str(parts[3]).is_err()
    {
        return None;
    }
    if parts.len() == 5 {
        Some(false)
    } else if uuid::Uuid::parse_str(parts[5]).is_ok() {
        Some(true)
    } else {
        None
    }
}

fn error(code: &'static str, effect: &'static str) -> Response {
    let status = if code == "unauthorized" {
        StatusCode::UNAUTHORIZED
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (
        status,
        [("cache-control", "no-store")],
        axum::Json(json!({"error":{"code":code,"effect":effect},"metrics":{}})),
    )
        .into_response()
}

fn activity(method: &Method, path: &str, body: &[u8]) -> bool {
    if matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS) {
        return false;
    }
    if path == "/v1/tools/call" {
        let name = serde_json::from_slice::<serde_json::Value>(body)
            .ok()
            .and_then(|v| v.get("name").and_then(|n| n.as_str()).map(str::to_owned));
        return !matches!(
            name.as_deref(),
            Some(
                "credentials_status" | "credentials_list" | "computer_files" | "browser_downloads"
            )
        );
    }
    !matches!(path, "/v1/lease/renew" | "/v1/lease/release" | "/v1/reap")
}

async fn handle(State(g): State<Arc<Gateway>>, request: Request) -> Response {
    let supplied = request
        .headers()
        .get(HOST_HEADER)
        .map(|h| h.as_bytes())
        .unwrap_or_default();
    let candidate: [u8; 32] = Sha256::digest(supplied).into();
    if !bool::from(candidate.ct_eq(&g.host_hash)) {
        return error("unauthorized", "none");
    }
    let (parts, body) = request.into_parts();
    let path = parts.uri.path();
    if path.starts_with("/host/") && parts.uri.query().is_some() {
        return StatusCode::BAD_REQUEST.into_response();
    }
    if path == "/host/status" && parts.method == Method::GET {
        return axum::Json(json!({"host":g.state.lock().await.snapshot})).into_response();
    }
    let limit = if parts.method == Method::POST && file_route(path) == Some(false) {
        crate::config::MAX_TRANSFER_BYTES as usize
    } else {
        2 * 1024 * 1024
    };
    let bytes = match to_bytes(body, limit).await {
        Ok(b) => b,
        Err(_) => return (StatusCode::PAYLOAD_TOO_LARGE, "").into_response(),
    };
    if matches!(path, "/host/inhibit" | "/host/clear-inhibit") && parts.method == Method::POST {
        let mut state = g.state.lock().await;
        let input = if path == "/host/inhibit" {
            HostInput::Inhibit
        } else {
            if serde_json::from_slice::<serde_json::Value>(&bytes)
                .ok()
                .and_then(|v| {
                    v.get("confirmed_no_inflight_effects")
                        .and_then(|v| v.as_bool())
                })
                != Some(true)
            {
                return error("host_busy", "none");
            }
            HostInput::ClearInhibit
        };
        return match state.apply(input) {
            Ok(_) => axum::Json(json!({"host":state.snapshot})).into_response(),
            Err(_) => error("host_busy", "none"),
        };
    }
    if !(path.starts_with("/v1/")
        || path == "/health"
        || (path == "/host/wake" && parts.method == Method::POST))
    {
        return StatusCode::NOT_FOUND.into_response();
    }
    let is_activity = activity(&parts.method, path, &bytes);
    let path = parts
        .uri
        .path_and_query()
        .map(|v| v.as_str())
        .unwrap_or("/")
        .to_owned();
    // Dropping the HTTP client does not cancel a dispatched action or release its hold.
    let task = tokio::spawn(g.forward(
        parts.method,
        path,
        parts.headers,
        bytes.to_vec(),
        is_activity,
    ));
    match task.await {
        Ok(response) => response,
        Err(_) => error("host_effect_unknown", "unknown"),
    }
}
