//! Computer-owned acceptance consumer, compiled inside an exact pinned Meta
//! source archive. The scripted model chooses tools; all results, Apps lineage,
//! sessions and Console projections come from the real runtime.
use std::collections::{BTreeMap, HashMap};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

use axum::{
    Json, Router,
    routing::{get, post},
};
use meerkat_client::{LlmClient, LlmDoneOutcome, LlmError, LlmEvent, LlmRequest, types::LlmStream};
use meerkat_core::{Message, Provider, StopReason, TurnUsage, Usage};
use meerkat_meta::identity_first::{
    AgentAddressability, AgentIdentity, CompletionCursor, DurableAgentSpec, LocalContinuityStore,
    LocalLeaseProvider, MutableRosterProvider,
};
use meerkat_meta::{
    AccessController, ConsolePolicy, RuntimeDecisionState, SqliteConsoleLogStore, UnifiedRuntime,
};
use meerkat_mob::{MobDefinition, ProfileName};
use serde_json::{Value, json};
use tokio::sync::Notify;

#[derive(Default)]
struct State {
    requests: Mutex<Vec<Value>>,
    held: AtomicBool,
    released: AtomicBool,
    release: Notify,
}

struct ScriptedModel {
    state: Arc<State>,
    fixture_url: String,
}

#[async_trait::async_trait]
impl LlmClient for ScriptedModel {
    fn provider(&self) -> Provider {
        Provider::Other
    }
    fn project_replay_messages(&self, messages: &[Message]) -> Result<Vec<Message>, LlmError> {
        Ok(messages.to_vec())
    }
    fn stream<'a>(&'a self, request: &'a LlmRequest) -> LlmStream<'a> {
        self.state
            .requests
            .lock()
            .unwrap()
            .push(serde_json::to_value(request).unwrap());
        let start = request.messages.iter().rposition(|message| {
            matches!(message, Message::User(user) if user.transcript_role.is_conversational())
        });
        let active = start.and_then(|index| match &request.messages[index] {
            Message::User(user) if user.text_content().starts_with("[computer-native]") => {
                Some(&request.messages[index..])
            }
            _ => None,
        });
        let steps = [
            (
                "computer-load",
                "tool_catalog_load",
                json!({"names": ["browser_navigate", "computer_observe"]}),
            ),
            (
                "computer-navigate",
                "browser_navigate",
                json!({"url": self.fixture_url}),
            ),
            ("computer-observe", "computer_observe", json!({})),
        ];
        let mut call = None;
        let mut failure = false;
        if let Some(messages) = active {
            for (id, name, arguments) in steps {
                let result = messages.iter().find_map(|message| match message {
                    Message::ToolResults { results, .. } => {
                        results.iter().find(|r| r.tool_use_id == id)
                    }
                    _ => None,
                });
                if let Some(result) = result {
                    if result.is_error {
                        failure = true;
                        break;
                    }
                } else {
                    call = Some((id, name, arguments));
                    break;
                }
            }
        }
        let hold = active.is_some() && call.is_none() && !failure;
        let usage = TurnUsage::host_declared(
            meerkat_models::infer_provider(&request.model).unwrap_or(Provider::Other),
            &request.model,
            Usage::default(),
        );
        Box::pin(async_stream::stream! {
            if hold {
                self.state.held.store(true, Ordering::SeqCst);
                loop {
                    let released = self.state.release.notified();
                    tokio::pin!(released);
                    released.as_mut().enable();
                    if self.state.released.load(Ordering::SeqCst) { break; }
                    released.await;
                }
                self.state.held.store(false, Ordering::SeqCst);
            }
            let reason = if let Some((id, name, args)) = call {
                yield Ok(LlmEvent::ToolCallComplete { id: id.into(), name: name.into(), args, meta: None });
                StopReason::ToolUse
            } else {
                let text = if failure { "Computer acceptance tool failed." }
                    else if active.is_some() { "Computer acceptance complete." }
                    else { "Ready." };
                yield Ok(LlmEvent::TextDelta { delta: text.into(), meta: None });
                StopReason::EndTurn
            };
            yield Ok(LlmEvent::UsageUpdate { usage });
            yield Ok(LlmEvent::Done { outcome: LlmDoneOutcome::Success { stop_reason: reason } });
        })
    }
    async fn health_check(&self) -> Result<(), LlmError> {
        Ok(())
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let state_dir = std::path::PathBuf::from(std::env::var("COMPUTER_CONSUMER_STATE")?);
    std::fs::create_dir_all(&state_dir)?;
    let broker = std::env::var("COMPUTER_BROKER_URL")?;
    let mut definition = MobDefinition::from_toml(
        r#"
[mob]
id = "computer-acceptance"
[profiles.computer]
model = "gpt-5.5"
external_addressable = true
[profiles.computer.tools]
comms = true
"#,
    )?;
    definition
        .profiles
        .get_mut(&ProfileName::from("computer"))
        .unwrap()
        .as_inline_mut()
        .unwrap()
        .tools
        .mcp_servers
        .push(meerkat_core::mcp_config::McpServerConfig::stdio(
            "computer",
            std::env::var("COMPUTER_FIXTURE_PYTHON")?,
            vec![
                "-m".into(),
                "meerkat_computer.mcp_adapter".into(),
                "--broker-url".into(),
                broker.clone(),
                "--binding-file".into(),
                std::env::var("COMPUTER_BINDING_FILE")?,
                "--mcp-apps".into(),
            ],
            HashMap::new(),
        ));
    let state = Arc::new(State::default());
    let spec = DurableAgentSpec {
        identity: AgentIdentity::parse("router:main")?,
        profile: ProfileName::from("computer"),
        addressability: AgentAddressability::Addressable,
        display_name: None,
        labels: BTreeMap::new(),
        context: None,
        additional_instructions: vec![],
        initial_message: None,
        runtime_mode_override: Some(meerkat_mob::MobRuntimeMode::TurnDriven),
        backend: None,
        binding: None,
        placement: None,
    };
    let runtime = Arc::new(
        Box::pin(
            UnifiedRuntime::builder()
                .definition(definition)
                .access_controller(AccessController::disabled())
                .mcp_apps(true)
                .with_console_log_store(Arc::new(SqliteConsoleLogStore::open(
                    state_dir.join("console.db"),
                )?))
                .persistent_state(&state_dir)
                .default_llm_client(Arc::new(ScriptedModel {
                    state: state.clone(),
                    fixture_url: format!("{broker}/fixture"),
                }))
                .roster_provider(Arc::new(MutableRosterProvider::new(vec![spec])))
                .continuity_store(Arc::new(LocalContinuityStore::open(
                    state_dir.join("continuity.db"),
                )?))
                .lease_provider(Arc::new(LocalLeaseProvider::new()))
                .build(),
        )
        .await?,
    );
    let mut decisions = RuntimeDecisionState::local_console(
        ConsolePolicy {
            require_app_auth: false,
            ..Default::default()
        },
        None,
    );
    decisions.console.ui.mcp_apps_sandbox_url = Some(std::env::var("COMPUTER_SANDBOX_URL")?);
    let read_state = state.clone();
    let control_state = state.clone();
    // Keep a released owning view; the fixture observes native completion
    // directly instead of exposing the general runtime RPC dispatcher.
    let completion_runtime = runtime
        .identity_first_context()
        .ok_or("native acceptance requires the runtime's identity context")?
        .runtime();
    let wait_runtime = completion_runtime.clone();
    let completion_identity = AgentIdentity::parse("router:main")?;
    let wait_identity = completion_identity.clone();
    let app = runtime.build_reference_app_router(decisions).nest(
        "/__acceptance",
        Router::new()
            .route(
                "/state",
                get(move || {
                    let state = read_state.clone();
                    async move {
                        Json(json!({"held": state.held.load(Ordering::SeqCst),
                "requests": *state.requests.lock().unwrap()}))
                    }
                }),
            )
            .route(
                "/completion",
                get(move || {
                    let runtime = completion_runtime.clone();
                    let identity = completion_identity.clone();
                    async move {
                        Json(json!({
                            "completion_cursor": runtime.completion_cursor(&identity).await,
                        }))
                    }
                })
                .post(move |Json(after): Json<CompletionCursor>| {
                    let runtime = wait_runtime.clone();
                    let identity = wait_identity.clone();
                    async move {
                        let observed = runtime
                            .await_completion(&identity, Some(after), Duration::from_secs(4))
                            .await;
                        Json(json!({
                            "outcome": observed.wire_str(),
                            "completion_cursor": observed.cursor(),
                        }))
                    }
                }),
            )
            .route(
                "/release",
                post(move || {
                    let state = control_state.clone();
                    async move {
                        state.released.store(true, Ordering::SeqCst);
                        state.release.notify_waiters();
                        Json(json!({"released": true}))
                    }
                }),
            ),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    println!(
        "COMPUTER_CONSUMER_READY {}",
        json!({"url": format!("http://{}", listener.local_addr()?)})
    );
    let drain = runtime.clone().spawn_event_drain_task();
    let result = axum::serve(listener, app).await;
    drain.abort();
    result?;
    Ok(())
}
