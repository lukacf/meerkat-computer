use super::*;
use crate::config::MAX_TRANSFER_BYTES;

pub(crate) const MAX_FILES_PER_COMPUTER: usize = 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FileDirection {
    Import,
    Export,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileMetadata {
    pub file_id: String,
    pub filename: String,
    pub direction: FileDirection,
    pub size_bytes: u64,
    pub sha256: String,
    pub created_at_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FileRecord {
    pub computer_id: String,
    pub ownership_id: String,
    pub metadata: FileMetadata,
}

pub(crate) fn file_id(value: &str) -> Result<(), BrokerError> {
    if uuid::Uuid::parse_str(value).is_ok_and(|id| id.hyphenated().to_string() == value) {
        Ok(())
    } else {
        Err(BrokerError::invalid("invalid file identifier"))
    }
}
pub(crate) fn filename(value: &str) -> Result<(), BrokerError> {
    if value.is_empty()
        || value.len() > 255
        || matches!(value, "." | "..")
        || value.contains(['/', '\\'])
        || value.chars().any(char::is_control)
    {
        return Err(BrokerError::invalid(
            "filename must be a display basename of at most 255 bytes",
        ));
    }
    Ok(())
}
fn guest_path(value: &str) -> Result<(), BrokerError> {
    let components = value.strip_prefix('/').unwrap_or(value);
    if value.len() > 4096
        || value.contains('\\')
        || value.chars().any(char::is_control)
        || components
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        return Err(BrokerError::invalid(
            "path must name a canonical guest file",
        ));
    }
    Ok(())
}
fn receipt(value: Value) -> WorkerCallResponse {
    WorkerCallResponse {
        content: vec![json!({"type":"text","text":value.to_string()})],
        metrics: json!({}),
    }
}
// Count only transfers validated by this broker. No cumulative guest counters
// are copied or invented, and persisted receipt replay has no effect.
fn completed_transfer(metadata: &FileMetadata, path: &str) -> WorkerCallResponse {
    let mut value = json!(metadata);
    value["path"] = json!(path);
    let mut response = receipt(value);
    let delta = match metadata.direction {
        FileDirection::Import => json!({"file_imports":1,"file_imports_bytes":metadata.size_bytes}),
        FileDirection::Export => json!({"file_exports":1,"file_exports_bytes":metadata.size_bytes}),
    };
    response.metrics = json!({"delta":delta});
    response
}
fn clean_error(error: BrokerError) -> BrokerError {
    BrokerError {
        code: error.code,
        effect: error.effect,
        message: "guest file transfer failed; inspect its effect before continuing".into(),
        metrics: None,
        backend_failure: None,
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Import {
    file_id: String,
    path: String,
    #[serde(default)]
    overwrite: bool,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Export {
    path: String,
    filename: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TransferResult {
    file_id: String,
    path: String,
    size_bytes: u64,
    sha256: String,
}
fn transfer_result(
    response: WorkerCallResponse,
    id: &str,
    path: &str,
    max_bytes: u64,
) -> Result<TransferResult, BrokerError> {
    let invalid =
        || BrokerError::unknown("worker transfer metadata was not valid; do not replay the action");
    if response.content.len() != 1 {
        return Err(invalid());
    }
    let block = &response.content[0];
    if block["type"] != "text" || block.as_object().is_none_or(|b| b.len() != 2) {
        return Err(invalid());
    }
    let text = block["text"]
        .as_str()
        .filter(|t| t.len() <= 8192)
        .ok_or_else(invalid)?;
    let result: TransferResult = serde_json::from_str(text).map_err(|_| invalid())?;
    if result.file_id != id
        || result.path != path
        || result.size_bytes > max_bytes
        || result.sha256.len() != 64
        || !result
            .sha256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(invalid());
    }
    Ok(result)
}

impl Broker {
    /// Trusted operator input only. The filename never participates in a host path.
    pub async fn stage_file(
        &self,
        computer_id: &str,
        name: &str,
        bytes: &[u8],
    ) -> Result<FileMetadata, BrokerError> {
        filename(name)?;
        let lock = self.computer_lock(computer_id).await;
        let _guard = lock.lock().await;
        let record = self.record(computer_id).await?;
        let metadata = FileMetadata {
            file_id: uuid::Uuid::new_v4().to_string(),
            filename: name.into(),
            direction: FileDirection::Import,
            size_bytes: bytes.len() as u64,
            sha256: digest(bytes),
            created_at_ms: self.clock.now_ms(),
        };
        self.store
            .lock()
            .await
            .put_file(&record, metadata.clone(), bytes)?;
        Ok(metadata)
    }
    pub async fn list_files(&self, computer_id: &str) -> Result<Vec<FileMetadata>, BrokerError> {
        let store = self.store.lock().await;
        let record = store
            .state
            .computers
            .get(computer_id)
            .ok_or_else(BrokerError::missing)?;
        Ok(store.files_for(record))
    }
    pub async fn download_file(
        &self,
        computer_id: &str,
        id: &str,
    ) -> Result<(FileMetadata, Vec<u8>), BrokerError> {
        file_id(id)?;
        let store = self.store.lock().await;
        let record = store
            .state
            .computers
            .get(computer_id)
            .ok_or_else(BrokerError::missing)?;
        let entry = store.file_for(record, id)?;
        if entry.metadata.direction != FileDirection::Export {
            return Err(BrokerError::missing());
        }
        Ok((entry.metadata.clone(), store.read_file(entry)?))
    }
    pub async fn delete_file(&self, computer_id: &str, id: &str) -> Result<(), BrokerError> {
        file_id(id)?;
        let lock = self.computer_lock(computer_id).await;
        let _guard = lock.lock().await;
        let record = self.record(computer_id).await?;
        self.store.lock().await.delete_file(&record, id)
    }
    async fn cleanup_blob(
        &self,
        backend: &BackendComputer,
        secret: &str,
        lease: &WorkerLease,
        id: &str,
    ) {
        let _ = tokio::time::timeout(
            Duration::from_secs(5),
            self.worker.remove_file(backend, secret, lease, id),
        )
        .await;
    }
    pub(super) async fn transfer_call(
        &self,
        token: &str,
        record: &ComputerRecord,
        lease: &WorkerLease,
        request: &CallRequest,
        backend: &BackendComputer,
        secret: &str,
    ) -> Result<WorkerCallResponse, BrokerError> {
        match request.name.as_str() {
            "computer_files" => {
                if request.arguments != json!({}) {
                    return Err(BrokerError::invalid("computer_files accepts no arguments"));
                }
                let store = self.store.lock().await;
                self.authenticate_record(&store, token)?;
                let files: Vec<_> = store
                    .files_for(record)
                    .into_iter()
                    .filter(|file| {
                        record.profile.capabilities.iter().any(|cap| {
                            cap == if file.direction == FileDirection::Import {
                                "file_import"
                            } else {
                                "file_export"
                            }
                        })
                    })
                    .collect();
                Ok(receipt(json!({"files":files})))
            }
            "computer_upload" => {
                let args: Import = serde_json::from_value(request.arguments.clone())
                    .map_err(|_| BrokerError::invalid("invalid computer_upload arguments"))?;
                file_id(&args.file_id)?;
                guest_path(&args.path)?;
                let (metadata, bytes) = {
                    let store = self.store.lock().await;
                    let entry = store.file_for(record, &args.file_id)?;
                    if entry.metadata.direction != FileDirection::Import {
                        return Err(BrokerError::missing());
                    }
                    (entry.metadata.clone(), store.read_file(entry)?)
                };
                let ack = self
                    .worker
                    .stage_file(backend, secret, lease, &args.file_id, &bytes)
                    .await
                    .map_err(clean_error)?;
                if ack
                    != json!({"file_id":args.file_id,"size_bytes":metadata.size_bytes,"sha256":metadata.sha256})
                {
                    return Err(BrokerError::unknown(
                        "worker did not acknowledge the exact staged file",
                    ));
                }
                self.authenticate(token)
                    .await
                    .map_err(|_| BrokerError::unknown("control grant ended during file staging"))?;
                let internal = CallRequest {
                    action_id: request.action_id.clone(),
                    name: "transfer_import".into(),
                    arguments: json!({"file_id":args.file_id,"path":args.path,"sha256":metadata.sha256,"size_bytes":metadata.size_bytes,"overwrite":args.overwrite}),
                };
                let response = match self.worker.call(backend, secret, lease, &internal).await {
                    Ok(response) => response,
                    Err(error) => {
                        if error.effect == Effect::None {
                            self.cleanup_blob(backend, secret, lease, &args.file_id)
                                .await;
                        }
                        return Err(clean_error(error));
                    }
                };
                let result =
                    transfer_result(response, &args.file_id, &args.path, metadata.size_bytes)?;
                if result.size_bytes != metadata.size_bytes || result.sha256 != metadata.sha256 {
                    return Err(BrokerError::unknown(
                        "worker imported bytes with a different digest or size",
                    ));
                }
                Ok(completed_transfer(&metadata, &args.path))
            }
            "computer_download" => {
                let args: Export = serde_json::from_value(request.arguments.clone())
                    .map_err(|_| BrokerError::invalid("invalid computer_download arguments"))?;
                filename(&args.filename)?;
                guest_path(&args.path)?;
                let max_bytes = self.store.lock().await.available_file_bytes(record)?;
                if max_bytes == 0 {
                    return Err(BrokerError::conflict("computer file storage quota is full"));
                }
                let id = uuid::Uuid::new_v4().to_string();
                let internal = CallRequest {
                    action_id: request.action_id.clone(),
                    name: "transfer_export".into(),
                    arguments: json!({"file_id":id,"path":args.path,"max_bytes":max_bytes}),
                };
                let response = self
                    .worker
                    .call(backend, secret, lease, &internal)
                    .await
                    .map_err(clean_error)?;
                let result = transfer_result(response, &id, &args.path, max_bytes)?;
                let bytes = self
                    .worker
                    .fetch_file(backend, secret, lease, &id, result.size_bytes)
                    .await
                    .map_err(|_| {
                        BrokerError::unknown(
                            "export bytes were unavailable; do not replay the action",
                        )
                    })?;
                if bytes.len() as u64 != result.size_bytes || digest(&bytes) != result.sha256 {
                    return Err(BrokerError::unknown(
                        "export bytes did not match the acknowledged digest and size",
                    ));
                }
                let metadata = FileMetadata {
                    file_id: id.clone(),
                    filename: args.filename,
                    direction: FileDirection::Export,
                    size_bytes: result.size_bytes,
                    sha256: result.sha256,
                    created_at_ms: self.clock.now_ms(),
                };
                {
                    let mut store = self.store.lock().await;
                    self.authenticate_record(&store, token).map_err(|_| {
                        BrokerError::unknown("control grant ended during file export")
                    })?;
                    store
                        .put_file(record, metadata.clone(), &bytes)
                        .map_err(|_| {
                            BrokerError::unknown(
                                "export could not be persisted; do not replay the action",
                            )
                        })?;
                }
                // Blob cleanup is best effort after the durable broker copy exists.
                // It never turns a confirmed export into an uncertain guest effect.
                self.cleanup_blob(backend, secret, lease, &id).await;
                Ok(completed_transfer(&metadata, &args.path))
            }
            _ => Err(BrokerError::invalid("unknown file transfer tool")),
        }
    }
}

impl HttpWorkerClient {
    async fn transfer_response(
        &self,
        method: reqwest::Method,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        id: &str,
        bytes: Option<&[u8]>,
    ) -> Result<reqwest::Response, BrokerError> {
        file_id(id)?;
        let mut request = self
            .client
            .request(
                method,
                format!(
                    "{}/v1/transfers/{id}",
                    computer.worker_url.trim_end_matches('/')
                ),
            )
            .bearer_auth(token)
            .header("X-Meerkat-Lease-Id", &lease.lease_id)
            .header("X-Meerkat-Epoch", lease.epoch)
            .header("content-type", "application/octet-stream");
        if let Some(bytes) = bytes {
            request = request.body(bytes.to_vec());
        }
        let response = request
            .send()
            .await
            .map_err(|_| BrokerError::unknown("worker file transfer acknowledgement was lost"))?;
        if !response.status().is_success() {
            return Err(BrokerError::unknown(
                "worker file transfer was not acknowledged",
            ));
        }
        Ok(response)
    }
    pub(super) async fn stage_blob(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        id: &str,
        bytes: &[u8],
    ) -> Result<Value, BrokerError> {
        if bytes.len() as u64 > MAX_TRANSFER_BYTES {
            return Err(BrokerError::invalid("file exceeds transfer ceiling"));
        }
        let response = self
            .transfer_response(
                reqwest::Method::PUT,
                computer,
                token,
                lease,
                id,
                Some(bytes),
            )
            .await?;
        let bytes = Self::bounded_blob(response, 4096).await?;
        serde_json::from_slice(&bytes)
            .map_err(|_| BrokerError::unknown("invalid staged file acknowledgement"))
    }
    pub(super) async fn bounded_blob(
        mut response: reqwest::Response,
        max_bytes: u64,
    ) -> Result<Vec<u8>, BrokerError> {
        let limit = max_bytes.min(MAX_TRANSFER_BYTES);
        if response.content_length().is_some_and(|size| size > limit) {
            return Err(BrokerError::unknown("worker transfer exceeds size limit"));
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| BrokerError::unknown("worker file response was lost"))?
        {
            if bytes.len().saturating_add(chunk.len()) as u64 > limit {
                return Err(BrokerError::unknown("worker transfer exceeds size limit"));
            }
            bytes.extend_from_slice(&chunk);
        }
        Ok(bytes)
    }
    pub(super) async fn fetch_blob(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        id: &str,
        max_bytes: u64,
    ) -> Result<Vec<u8>, BrokerError> {
        Self::bounded_blob(
            self.transfer_response(reqwest::Method::GET, computer, token, lease, id, None)
                .await?,
            max_bytes,
        )
        .await
    }
    pub(super) async fn remove_blob(
        &self,
        computer: &BackendComputer,
        token: &str,
        lease: &WorkerLease,
        id: &str,
    ) -> Result<(), BrokerError> {
        self.transfer_response(reqwest::Method::DELETE, computer, token, lease, id, None)
            .await?;
        Ok(())
    }
}
