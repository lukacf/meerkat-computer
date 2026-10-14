//! Website credentials are owned here, never by provider authentication or protocol adapters.
use super::*;
use crate::credentials::{
    CredentialDraft, CredentialMetadata, CredentialStore, SecretValues, canonical_origin,
};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecureInputTarget {
    pub observation_id: String,
    pub origin: String,
    pub secret_ref: String,
    pub username_ref: Option<String>,
}
impl SecureInputTarget {
    fn validate(&mut self) -> Result<(), BrokerError> {
        self.origin = canonical_origin(&self.origin)?;
        for part in [&self.observation_id, &self.secret_ref]
            .into_iter()
            .chain(self.username_ref.iter())
        {
            validate_identifier(part, "observed field reference")?;
            if part.len() > 128 {
                return Err(BrokerError::invalid("field reference exceeds limit"));
            }
        }
        if self.username_ref.as_ref() == Some(&self.secret_ref) {
            return Err(BrokerError::invalid(
                "username and secret require distinct fields",
            ));
        }
        Ok(())
    }
}

/// Secret bytes may only be serialized into the private guest control pipe.
#[derive(Debug)]
pub struct SecureInputDelivery {
    pub lease: WorkerLease,
    pub action_id: String,
    pub target: SecureInputTarget,
    pub values: SecretValues,
}
impl SecureInputDelivery {
    pub fn private_wire_bytes(&self) -> Result<Vec<u8>, BrokerError> {
        #[derive(Serialize)]
        struct Values<'a> {
            username: &'a Option<String>,
            secret: &'a str,
        }
        #[derive(Serialize)]
        struct Wire<'a> {
            lease: &'a WorkerLease,
            action_id: &'a str,
            target: &'a SecureInputTarget,
            values: Values<'a>,
        }
        serde_json::to_vec(&Wire {
            lease: &self.lease,
            action_id: &self.action_id,
            target: &self.target,
            values: Values {
                username: &self.values.username,
                secret: &self.values.secret,
            },
        })
        .map_err(|_| BrokerError::invalid("cannot encode private input"))
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialFulfillment {
    pub values: SecretValues,
    pub save: Option<SaveCredential>,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SaveCredential {
    pub label: String,
}

mod lifecycle {
    use meerkat_machine_dsl::machine;
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub enum RequestEffect {}
    machine! {
        machine CredentialRequestLifecycle {
            version: 1,
            rust: "meerkat-computer" / "domain::secure::lifecycle",
            state { phase: RequestPhase, expires_at_ms: u64 }
            init(Pending) { expires_at_ms = 0 }
            terminal [Delivered, Failed, Unknown, Cancelled, Expired]
            phase RequestPhase { Pending, InFlight, Delivered, Failed, Unknown, Cancelled, Expired }
            input RequestInput { Open { expires_at_ms: u64 }, Admit { now_ms: u64 }, Deliver, Fail, Uncertain, Cancel, Expire { now_ms: u64 }, Recover }
            effect RequestEffect {}
            transition OpenRequest { on input Open { expires_at_ms } guard { self.phase == Phase::Pending && self.expires_at_ms == 0 && expires_at_ms > 0 } update { self.expires_at_ms = expires_at_ms; } to Pending }
            transition AdmitDelivery { on input Admit { now_ms } guard { self.phase == Phase::Pending && now_ms < self.expires_at_ms } update {} to InFlight }
            transition DeliveredInput { on input Deliver guard { self.phase == Phase::InFlight } update {} to Delivered }
            transition FailedInput { on input Fail guard { self.phase == Phase::InFlight } update {} to Failed }
            transition UncertainInput { on input Uncertain guard { self.phase == Phase::InFlight } update {} to Unknown }
            transition CancelInput { on input Cancel guard { self.phase == Phase::Pending } update {} to Cancelled }
            transition ExpireInput { on input Expire { now_ms } guard { self.phase == Phase::Pending && now_ms >= self.expires_at_ms } update {} to Expired }
            transition RecoverPending { on input Recover guard { self.phase == Phase::Pending } update {} to Cancelled }
            transition RecoverInFlight { on input Recover guard { self.phase == Phase::InFlight } update {} to Unknown }
            transition RecoverDelivered { on input Recover guard { self.phase == Phase::Delivered } update {} to Delivered }
            transition RecoverFailed { on input Recover guard { self.phase == Phase::Failed } update {} to Failed }
            transition RecoverUnknown { on input Recover guard { self.phase == Phase::Unknown } update {} to Unknown }
            transition RecoverCancelled { on input Recover guard { self.phase == Phase::Cancelled } update {} to Cancelled }
            transition RecoverExpired { on input Recover guard { self.phase == Phase::Expired } update {} to Expired }
        }
    }
}
use lifecycle::{RequestInput, RequestPhase};
#[derive(Serialize, Deserialize)]
#[serde(remote = "RequestPhase", rename_all = "snake_case")]
enum RequestPhaseSerde {
    Pending,
    InFlight,
    Delivered,
    Failed,
    Unknown,
    Cancelled,
    Expired,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct RequestLifecycle {
    #[serde(with = "RequestPhaseSerde")]
    pub phase: RequestPhase,
    pub expires_at_ms: u64,
}
impl RequestLifecycle {
    fn new(expires_at_ms: u64) -> Result<Self, BrokerError> {
        let state = lifecycle::CredentialRequestLifecycleAuthority::new()
            .state()
            .clone();
        let mut value = Self {
            phase: state.phase,
            expires_at_ms: state.expires_at_ms,
        };
        value.apply(RequestInput::Open { expires_at_ms })?;
        Ok(value)
    }
    fn apply(&mut self, input: RequestInput) -> Result<(), BrokerError> {
        let mut authority = lifecycle::CredentialRequestLifecycleAuthority::recover_from_state(
            lifecycle::CredentialRequestLifecycleState {
                phase: self.phase,
                expires_at_ms: self.expires_at_ms,
            },
        )
        .map_err(|_| BrokerError::storage("invalid credential request authority"))?;
        lifecycle::CredentialRequestLifecycleMutator::apply(&mut authority, input).map_err(
            |_| BrokerError::conflict("credential request cannot perform this operation"),
        )?;
        self.phase = authority.state().phase;
        self.expires_at_ms = authority.state().expires_at_ms;
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub(crate) struct SecureRequestRecord {
    pub id: String,
    pub computer_id: String,
    pub owner: ComputerOwner,
    pub action_id: String,
    pub lease_id: String,
    pub epoch: u64,
    pub target: SecureInputTarget,
    pub credential_id: Option<String>,
    pub lifecycle: RequestLifecycle,
    pub receipt: Option<WorkerCallResponse>,
    pub error: Option<BrokerError>,
}
impl SecureRequestRecord {
    pub(super) fn has_unresolved_effect(&self) -> bool {
        matches!(
            self.lifecycle.phase,
            RequestPhase::InFlight | RequestPhase::Unknown
        )
    }
    pub(crate) fn recover(&mut self) -> Result<(), BrokerError> {
        self.lifecycle.apply(RequestInput::Recover)
    }
    fn view(&self, listen: &str) -> Value {
        json!({"id":self.id,"computer_id":self.computer_id,"owner":self.owner,"target":self.target,
            "credential_id":self.credential_id,"phase":serde_json::to_value(&self.lifecycle).expect("request lifecycle serializes")["phase"],
            "expires_at_ms":self.lifecycle.expires_at_ms,"receipt":self.receipt,"operator_url":format!("http://{listen}/credentials#{}",self.id)})
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UseArgs {
    credential_id: String,
    target: SecureInputTarget,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RequestArgs {
    target: SecureInputTarget,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StatusArgs {
    request_id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EmptyArgs {}
fn decode<T: serde::de::DeserializeOwned>(value: Value) -> Result<T, BrokerError> {
    serde_json::from_value(value)
        .map_err(|_| BrokerError::invalid("invalid credential tool arguments; use references only"))
}
fn response(value: Value) -> WorkerCallResponse {
    WorkerCallResponse {
        content: vec![json!({"type":"text","text":value.to_string()})],
        metrics: json!({}),
    }
}

impl Broker {
    fn vault(&self) -> Result<&dyn CredentialStore, BrokerError> {
        self.vault
            .as_deref()
            .ok_or_else(|| BrokerError::invalid("credentials are not configured"))
    }
    pub async fn list_credentials(&self) -> Result<Vec<CredentialMetadata>, BrokerError> {
        self.vault()?.list()
    }
    pub async fn save_credential(
        &self,
        draft: CredentialDraft,
    ) -> Result<CredentialMetadata, BrokerError> {
        let _policy = self.credential_policy.lock().await;
        self.vault()?.save(draft)
    }
    pub async fn update_credential(
        &self,
        id: &str,
        draft: CredentialDraft,
    ) -> Result<CredentialMetadata, BrokerError> {
        let _policy = self.credential_policy.lock().await;
        self.vault()?.update(id, draft)
    }
    pub async fn delete_credential(&self, id: &str) -> Result<(), BrokerError> {
        let _policy = self.credential_policy.lock().await;
        self.vault()?.delete(id)
    }
    pub async fn assign_credentials(
        &self,
        ids: &[String],
        owners: &[ComputerOwner],
        remove: bool,
    ) -> Result<(), BrokerError> {
        let _policy = self.credential_policy.lock().await;
        if remove {
            self.vault()?.unassign(ids, owners)
        } else {
            self.vault()?.assign(ids, owners)
        }
    }
    async fn refresh_requests(&self) -> Result<(), BrokerError> {
        let now = self.clock.now_ms();
        let mut store = self.store.lock().await;
        let mut next = store.state.clone();
        let mut changed = false;
        for request in next.credential_requests.values_mut() {
            if request.lifecycle.phase != RequestPhase::Pending {
                continue;
            }
            let current = next.computers.get(&request.computer_id).is_some_and(|r| {
                r.lifecycle.phase == ComputerPhase::Leased
                    && r.lifecycle.epoch == request.epoch
                    && r.grant
                        .as_ref()
                        .is_some_and(|g| g.lease_id == request.lease_id)
            });
            if now >= request.lifecycle.expires_at_ms {
                request
                    .lifecycle
                    .apply(RequestInput::Expire { now_ms: now })?;
                changed = true;
            } else if !current {
                request.lifecycle.apply(RequestInput::Cancel)?;
                changed = true;
            }
        }
        if changed {
            store.commit(next)?;
        }
        Ok(())
    }
    pub async fn credential_requests(&self) -> Result<Vec<Value>, BrokerError> {
        self.vault()?;
        self.refresh_requests().await?;
        Ok(self
            .store
            .lock()
            .await
            .state
            .credential_requests
            .values()
            .map(|r| r.view(&self.config.server.listen))
            .collect())
    }
    async fn secure_request(&self, id: &str) -> Result<SecureRequestRecord, BrokerError> {
        self.store
            .lock()
            .await
            .state
            .credential_requests
            .get(id)
            .cloned()
            .ok_or_else(|| BrokerError {
                code: ErrorCode::NotFound,
                message: "credential request not found".into(),
                effect: Effect::None,
                metrics: None,
                backend_failure: None,
            })
    }
    async fn change_request(
        &self,
        id: &str,
        f: impl FnOnce(&mut SecureRequestRecord) -> Result<(), BrokerError>,
    ) -> Result<(), BrokerError> {
        let mut store = self.store.lock().await;
        let mut next = store.state.clone();
        let request = next
            .credential_requests
            .get_mut(id)
            .ok_or_else(BrokerError::missing)?;
        f(request)?;
        store.commit(next)
    }
    pub async fn cancel_credential_request(&self, id: &str) -> Result<(), BrokerError> {
        self.vault()?;
        let request = self.secure_request(id).await?;
        let lock = self.computer_lock(&request.computer_id).await;
        let _guard = lock.lock().await;
        self.refresh_requests().await?;
        self.change_request(id, |r| r.lifecycle.apply(RequestInput::Cancel))
            .await
    }
    pub async fn fulfill_credential_request(
        &self,
        id: &str,
        fulfillment: CredentialFulfillment,
    ) -> Result<WorkerCallResponse, BrokerError> {
        self.vault()?;
        let request = self.secure_request(id).await?;
        let lock = self.computer_lock(&request.computer_id).await;
        let _guard = lock.lock().await;
        self.refresh_requests().await?;
        let request = self.secure_request(id).await?;
        if request.lifecycle.phase != RequestPhase::Pending || request.credential_id.is_some() {
            return Err(BrokerError::conflict(
                "request is no longer pending manual input; it will not be replayed",
            ));
        }
        let record = self.record(&request.computer_id).await?;
        let lease = worker_lease(&record)?;
        self.check_request_lease(&request, &record, &lease)?;
        validate_values(&fulfillment.values)?;
        let _policy = self.credential_policy.lock().await;
        if fulfillment.save.is_some() && !matches!(record.owner, ComputerOwner::Identity { .. }) {
            return Err(BrokerError::unauthorized());
        }
        self.deliver_request(
            request,
            &record,
            lease,
            fulfillment.values,
            fulfillment.save,
        )
        .await
    }
    fn check_request_lease(
        &self,
        request: &SecureRequestRecord,
        record: &ComputerRecord,
        lease: &WorkerLease,
    ) -> Result<(), BrokerError> {
        if request.owner != record.owner
            || lease.lease_id != request.lease_id
            || lease.epoch != request.epoch
            || self.clock.now_ms() >= lease.expires_at_ms
            || record.lifecycle.phase != ComputerPhase::Leased
        {
            return Err(BrokerError::unauthorized());
        }
        Ok(())
    }
    async fn deliver_request(
        &self,
        request: SecureRequestRecord,
        record: &ComputerRecord,
        lease: WorkerLease,
        values: SecretValues,
        save: Option<SaveCredential>,
    ) -> Result<WorkerCallResponse, BrokerError> {
        validate_values(&values)?;
        if request.target.username_ref.is_some() != values.username.is_some() {
            return Err(BrokerError::invalid(
                "username requires an observed username field",
            ));
        }
        self.check_request_lease(&request, record, &lease)?;
        {
            // The preview barrier and private admission have one durable commit.
            let mut store = self.store.lock().await;
            let mut next = store.state.clone();
            next.credential_requests
                .get_mut(&request.id)
                .ok_or_else(BrokerError::missing)?
                .lifecycle
                .apply(RequestInput::Admit {
                    now_ms: self.clock.now_ms(),
                })?;
            next.computers
                .get_mut(&request.computer_id)
                .ok_or_else(BrokerError::missing)?
                .preview = Some(PreviewSource::PrivateInput {
                request_id: request.id.clone(),
            });
            store.commit(next)?;
        }
        let result = async {
            // Admission precedes both persistence and guest effects, so a retry never saves twice.
            if let Some(save) = save {
                let ComputerOwner::Identity { realm, .. } = &record.owner else {
                    return Err(BrokerError::unauthorized());
                };
                self.vault()?.save_assigned(
                    CredentialDraft {
                        realm: realm.clone(),
                        label: save.label,
                        origin: request.target.origin.clone(),
                        username: values.username.clone(),
                        secret: values.secret.clone(),
                    },
                    std::slice::from_ref(&record.owner),
                )?;
            }
            let backend = record
                .backend
                .as_ref()
                .ok_or_else(|| BrokerError::storage("computer has no backend handle"))?;
            let delivery = SecureInputDelivery {
                lease,
                action_id: request.id.clone(),
                target: request.target,
                values,
            };
            self.backend.deliver_secret(backend, &delivery).await
        }
        .await;
        let commit = self
            .change_request(&request.id, |r| {
                let input = match &result {
                    Ok(_) => RequestInput::Deliver,
                    Err(e) if e.effect == Effect::None => RequestInput::Fail,
                    Err(_) => RequestInput::Uncertain,
                };
                r.lifecycle.apply(input)?;
                r.receipt = result.as_ref().ok().cloned();
                r.error = result.as_ref().err().cloned();
                Ok(())
            })
            .await;
        if commit.is_err() {
            return Err(BrokerError::unknown(
                "private input outcome could not be persisted; do not replay",
            ));
        }
        result
    }
    pub(super) async fn credential_tool(
        &self,
        token: &str,
        call: CallRequest,
    ) -> Result<WorkerCallResponse, BrokerError> {
        self.vault()?;
        let id = self.authenticate(token).await?;
        let lock = self.computer_lock(&id).await;
        let _guard = lock.lock().await;
        self.authenticate(token).await?;
        let record = self.record(&id).await?;
        if !record
            .profile
            .capabilities
            .iter()
            .any(|c| c == "browser_semantics")
            || (!matches!(record.owner, ComputerOwner::Identity { .. })
                && !matches!(
                    call.name.as_str(),
                    "credentials_request" | "credentials_status"
                ))
        {
            return Err(BrokerError::unauthorized());
        }
        if !matches!(
            call.name.as_str(),
            "credentials_list" | "credentials_status" | "credentials_use" | "credentials_request"
        ) {
            return Err(BrokerError::invalid("unknown credential tool"));
        }
        self.update(&id, |r| {
            r.lifecycle.apply(ComputerInput::Touch {
                epoch: record.lifecycle.epoch,
                now_ms: self.clock.now_ms(),
            })
        })
        .await?;
        self.refresh_requests().await?;
        if call.name == "credentials_list" {
            let _: EmptyArgs = decode(call.arguments)?;
            let entries = self.vault()?.assigned(&record.owner)?;
            // Other agents' assignment edges are operator metadata, not model-visible data.
            return Ok(response(
                json!({"credentials":entries.into_iter().map(|c|json!({"id":c.id,"label":c.label,"origin":c.origin,"version":c.version})).collect::<Vec<_>>() }),
            ));
        }
        if call.name == "credentials_status" {
            let args: StatusArgs = decode(call.arguments)?;
            let request = self.secure_request(&args.request_id).await?;
            self.check_request_lease(&request, &record, &worker_lease(&record)?)?;
            return Ok(response(request.view(&self.config.server.listen)));
        }
        let (credential_id, mut target) = match call.name.as_str() {
            "credentials_use" => {
                let a: UseArgs = decode(call.arguments)?;
                validate_identifier(&a.credential_id, "credential_id")?;
                (Some(a.credential_id), a.target)
            }
            "credentials_request" => {
                let a: RequestArgs = decode(call.arguments)?;
                (None, a.target)
            }
            _ => return Err(BrokerError::invalid("unknown credential tool")),
        };
        target.validate()?;
        let lease = worker_lease(&record)?;
        if record.actions.contains_key(&call.action_id) {
            return Err(BrokerError::conflict(
                "action_id already belongs to another operation",
            ));
        }
        let old = self
            .store
            .lock()
            .await
            .state
            .credential_requests
            .values()
            .find(|r| r.computer_id == id && r.action_id == call.action_id)
            .cloned();
        if let Some(old) = old {
            self.check_request_lease(&old, &record, &lease)?;
            if old.target != target || old.credential_id != credential_id {
                return Err(BrokerError::conflict(
                    "action_id already used for different input references",
                ));
            }
            if credential_id.is_none() {
                return Ok(response(old.view(&self.config.server.listen)));
            }
            if let Some(receipt) = old.receipt {
                return Ok(receipt);
            }
            return Err(old.error.unwrap_or_else(|| {
                BrokerError::unknown("private input has no definitive receipt; do not replay")
            }));
        }
        let _policy = self.credential_policy.lock().await;
        let mut values = credential_id
            .as_ref()
            .map(|cid| self.vault()?.resolve(cid, &record.owner, &target.origin))
            .transpose()?;
        if target.username_ref.is_none()
            && let Some(values) = values.as_mut()
        {
            values.username = None;
        }
        let request = SecureRequestRecord {
            id: uuid::Uuid::new_v4().simple().to_string(),
            computer_id: id,
            owner: record.owner.clone(),
            action_id: call.action_id,
            lease_id: lease.lease_id.clone(),
            epoch: lease.epoch,
            target,
            credential_id,
            lifecycle: RequestLifecycle::new(
                lease
                    .expires_at_ms
                    .min(self.clock.now_ms().saturating_add(600_000)),
            )?,
            receipt: None,
            error: None,
        };
        {
            let mut store = self.store.lock().await;
            let pending = store
                .state
                .credential_requests
                .values()
                .filter(|r| {
                    r.computer_id == request.computer_id
                        && r.lifecycle.phase == RequestPhase::Pending
                })
                .count();
            if pending >= 16 {
                return Err(BrokerError::conflict(
                    "too many pending private input requests",
                ));
            }
            let mut next = store.state.clone();
            next.credential_requests
                .insert(request.id.clone(), request.clone());
            store.commit(next)?;
        }
        if let Some(values) = values {
            self.deliver_request(request, &record, lease, values, None)
                .await
        } else {
            Ok(response(request.view(&self.config.server.listen)))
        }
    }
}
fn validate_values(values: &SecretValues) -> Result<(), BrokerError> {
    if values.secret.is_empty()
        || values.secret.len() > 16384
        || values.username.as_ref().is_some_and(|s| s.len() > 1024)
    {
        return Err(BrokerError::invalid(
            "private input is empty or exceeds size limit",
        ));
    }
    Ok(())
}
pub(super) fn credential_tool_definitions() -> Vec<Value> {
    let target = json!({"type":"object","additionalProperties":false,"required":["origin","observation_id","secret_ref"],"properties":{"origin":{"type":"string","description":"Exact site origin, HTTPS or literal loopback HTTP."},"observation_id":{"type":"string"},"secret_ref":{"type":"string"},"username_ref":{"type":"string"}}});
    vec![
        json!({"name":"credentials_list","description":"List logins assigned to this durable identity. Returns references only, never secret values.","inputSchema":{"type":"object","properties":{},"additionalProperties":false}}),
        json!({"name":"credentials_use","description":"Fill an assigned login into observed browser fields through the private input channel. Use the returned safe observation_id and previously observed submit ref to submit. Screenshots and semantics stay withheld until a new document loads. Never pass secret bytes. Uncertain deliveries must not be replayed.","inputSchema":{"type":"object","required":["credential_id","target"],"additionalProperties":false,"properties":{"credential_id":{"type":"string"},"target":target}}}),
        json!({"name":"credentials_request","description":"Ask the operator for private one-time login or OTP input using the standalone operator link. This tool does not wait. Poll credentials_status; never ask for passwords in chat.","inputSchema":{"type":"object","required":["target"],"additionalProperties":false,"properties":{"target":target}}}),
        json!({"name":"credentials_status","description":"Read the state of a private input request belonging to this current control grant.","inputSchema":{"type":"object","required":["request_id"],"additionalProperties":false,"properties":{"request_id":{"type":"string"}}}}),
    ]
}
