//! HTTP projection of the library. Authentication does not choose computer ownership.

use axum::{
    Json, Router,
    body::to_bytes,
    extract::{DefaultBodyLimit, Path, Query, Request, State, rejection::JsonRejection},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use meerkat_computer::{
    AttachmentRequest, Broker, BrokerError, CallRequest, ComputerOwner, CredentialFulfillment,
    ForkRequest, ForkView, OperatorAccess, RetirementRequest, credentials::CredentialDraft,
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::Arc;
use subtle::ConstantTimeEq;

#[derive(Clone)]
struct AppState {
    broker: Arc<Broker>,
    operator_hash: [u8; 32],
}

pub fn router(broker: Arc<Broker>, operator_token: &str) -> Router {
    Router::new()
        .route(
            "/health",
            get(|| async { Json(json!({"protocol_version": 1, "service": "meerkat-computer"})) }),
        )
        .route("/v1/attachments", post(attach))
        .route("/v1/forks", post(fork))
        .route("/v1/forks/{request_id}", get(fork_status))
        .route(
            "/v1/forks/{request_id}/resume-source",
            post(fork_resume_source),
        )
        .route("/v1/computers", get(list))
        .route("/v1/computers/{computer_id}/access", get(access))
        .route("/v1/computers/{computer_id}/preview", get(preview))
        .route("/v1/computers/{computer_id}/stop", post(stop))
        .route("/v1/computers/{computer_id}/retire", post(retire))
        .route(
            "/v1/computers/{computer_id}/files",
            get(file_list).post(file_stage),
        )
        .route(
            "/v1/computers/{computer_id}/files/{file_id}",
            get(file_download).delete(file_delete),
        )
        .route("/v1/reap", post(reap))
        .route("/v1/tools", get(tools))
        .route("/v1/tools/call", post(call))
        .route("/v1/preview", get(grant_preview))
        .route("/v1/lease/release", post(release))
        .route("/v1/lease/renew", post(renew))
        .route("/credentials", get(credential_page))
        .route("/credentials.js", get(credential_script))
        .route("/credentials.css", get(credential_styles))
        .route("/preview", get(preview_page))
        .route("/preview.js", get(preview_script))
        .route("/preview.css", get(preview_styles))
        .route(
            "/v1/credentials",
            get(credential_list).post(credential_save),
        )
        .route(
            "/v1/credentials/{id}",
            axum::routing::put(credential_update).delete(credential_delete),
        )
        .route("/v1/credentials/assign", post(credential_assign))
        .route("/v1/credentials/unassign", post(credential_unassign))
        .route("/v1/credential-requests", get(credential_requests))
        .route(
            "/v1/credential-requests/{id}/fulfill",
            post(credential_fulfill),
        )
        .route(
            "/v1/credential-requests/{id}/cancel",
            post(credential_cancel),
        )
        .layer(middleware::from_fn(private_headers))
        .layer(DefaultBodyLimit::max(2 * 1024 * 1024))
        .with_state(AppState {
            broker,
            operator_hash: Sha256::digest(operator_token.as_bytes()).into(),
        })
}

struct HttpError(StatusCode, Value);
impl IntoResponse for HttpError {
    fn into_response(self) -> Response {
        (self.0, Json(self.1)).into_response()
    }
}
impl From<BrokerError> for HttpError {
    fn from(error: BrokerError) -> Self {
        let code = error.code();
        let status = match code {
            "unauthorized" | "invalid_token" | "lease_expired" => StatusCode::UNAUTHORIZED,
            "capability_denied" => StatusCode::FORBIDDEN,
            "not_found" => StatusCode::NOT_FOUND,
            "invalid_request" | "invalid_config" | "invalid_input" => {
                StatusCode::UNPROCESSABLE_ENTITY
            }
            "capacity_exhausted" | "capacity" => StatusCode::TOO_MANY_REQUESTS,
            "storage_error" | "storage_failure" | "internal" => StatusCode::INTERNAL_SERVER_ERROR,
            "backend_error" | "backend_failure" | "worker_unavailable" => StatusCode::BAD_GATEWAY,
            _ => StatusCode::CONFLICT,
        };
        Self(
            status,
            json!({"error": {"code": code, "message": error.to_string(), "effect": error.effect()}, "metrics": error.metrics}),
        )
    }
}
impl HttpError {
    /// Only authenticated operator routes expose backend diagnostic evidence.
    fn for_operator(error: BrokerError) -> Self {
        let diagnostic = error.backend_failure;
        let mut response = Self::from(error);
        if let Some(diagnostic) = diagnostic {
            response.1["error"]["backend_failure"] =
                serde_json::to_value(diagnostic).expect("diagnostic serialization is infallible");
        }
        response
    }
}

fn bearer(headers: &HeaderMap) -> Result<&str, HttpError> {
    headers.get("authorization").and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer ")).filter(|v| !v.is_empty())
        .ok_or_else(|| HttpError(StatusCode::UNAUTHORIZED, json!({"error": {"code": "unauthorized", "message": "A bearer credential is required", "effect": "none"}})))
}
fn operator(state: &AppState, headers: &HeaderMap) -> Result<(), HttpError> {
    let candidate: [u8; 32] = Sha256::digest(bearer(headers)?.as_bytes()).into();
    if bool::from(candidate.ct_eq(&state.operator_hash)) {
        Ok(())
    } else {
        Err(HttpError(
            StatusCode::UNAUTHORIZED,
            json!({"error": {"code": "unauthorized", "message": "Invalid operator credential", "effect": "none"}}),
        ))
    }
}

async fn attach(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<AttachmentRequest>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    let attachment = state
        .broker
        .attach(request)
        .await
        .map_err(HttpError::for_operator)?;
    Ok(Json(
        serde_json::to_value(attachment).expect("attachment serialization is infallible"),
    ))
}
async fn fork(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<ForkRequest>,
) -> Result<Json<ForkView>, HttpError> {
    operator(&state, &headers)?;
    // Disconnecting an HTTP client does not cancel a checkpoint or restore in progress.
    let task = tokio::spawn(async move { state.broker.fork(request).await });
    Ok(Json(
        task.await
            .map_err(|_| {
                HttpError::from(BrokerError::unknown(
                    "fork task was interrupted; inspect its request ID",
                ))
            })?
            .map_err(HttpError::for_operator)?,
    ))
}
async fn fork_status(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<ForkView>, HttpError> {
    operator(&state, &headers)?;
    Ok(Json(state.broker.fork_status(&id).await?))
}
async fn fork_resume_source(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    state
        .broker
        .resume_fork_source(&id)
        .await
        .map_err(HttpError::for_operator)?;
    Ok(Json(json!({"request_id": id, "source_resumed": true})))
}
async fn list(State(state): State<AppState>, headers: HeaderMap) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    Ok(Json(
        json!({"computers": state.broker.list_computers().await}),
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FileQuery {
    filename: String,
}
async fn file_stage(
    State(state): State<AppState>,
    Path(id): Path<String>,
    Query(query): Query<FileQuery>,
    request: Request,
) -> Result<Json<Value>, HttpError> {
    operator(&state, request.headers())?;
    if request
        .headers()
        .get("content-type")
        .and_then(|h| h.to_str().ok())
        != Some("application/octet-stream")
    {
        return Err(BrokerError::invalid("file staging requires application/octet-stream").into());
    }
    let bytes=to_bytes(request.into_body(),meerkat_computer::config::MAX_TRANSFER_BYTES as usize).await.map_err(|_|HttpError(StatusCode::PAYLOAD_TOO_LARGE,json!({"error":{"code":"invalid_input","message":"file exceeds transfer limit","effect":"none"}})))?;
    Ok(Json(json!(
        state
            .broker
            .stage_file(&id, &query.filename, &bytes)
            .await?
    )))
}
async fn file_list(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    Ok(Json(json!({"files":state.broker.list_files(&id).await?})))
}
async fn file_download(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((id, file_id)): Path<(String, String)>,
) -> Result<Response, HttpError> {
    operator(&state, &headers)?;
    let (metadata, bytes) = state.broker.download_file(&id, &file_id).await?;
    let encoded: String = metadata
        .filename
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-._".contains(&byte) {
                (byte as char).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect();
    Ok((
        [
            ("content-type", "application/octet-stream".to_owned()),
            (
                "content-disposition",
                format!("attachment; filename*=UTF-8''{encoded}"),
            ),
            ("x-content-sha256", metadata.sha256),
        ],
        bytes,
    )
        .into_response())
}
async fn file_delete(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((id, file_id)): Path<(String, String)>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    state.broker.delete_file(&id, &file_id).await?;
    Ok(Json(json!({"deleted":file_id})))
}
async fn stop(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    state
        .broker
        .stop(&id)
        .await
        .map_err(HttpError::for_operator)?;
    Ok(Json(json!({"stopped": id})))
}
async fn retire(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    body: Result<Json<RetirementRequest>, JsonRejection>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    let request = private_json(body)?;
    let target = id.clone();
    // A disconnected caller cannot cancel a dispatched destruction.
    let task = tokio::spawn(async move { state.broker.retire(&target, request).await });
    task.await
        .map_err(|_| {
            HttpError::from(BrokerError::unknown(
                "retirement interrupted; inspect the computer before further action",
            ))
        })?
        .map_err(HttpError::for_operator)?;
    Ok(Json(json!({"retired": id})))
}
async fn access(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<OperatorAccess>, HttpError> {
    operator(&state, &headers)?;
    Ok(Json(
        state
            .broker
            .operator_access(&id)
            .await
            .map_err(HttpError::for_operator)?,
    ))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PreviewQuery {
    after: Option<String>,
}
async fn preview(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    Query(query): Query<PreviewQuery>,
) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    Ok(Json(
        state
            .broker
            .computer_preview(&id, query.after.as_deref())
            .await?,
    ))
}
async fn grant_preview(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<PreviewQuery>,
) -> Result<Json<Value>, HttpError> {
    Ok(Json(
        state
            .broker
            .preview(bearer(&headers)?, query.after.as_deref())
            .await?,
    ))
}
async fn reap(State(state): State<AppState>, headers: HeaderMap) -> Result<Json<Value>, HttpError> {
    operator(&state, &headers)?;
    Ok(Json(
        json!({"stopped": state.broker.reap().await.map_err(HttpError::for_operator)?}),
    ))
}
async fn tools(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, HttpError> {
    Ok(Json(
        json!({"tools": state.broker.tools(bearer(&headers)?).await?}),
    ))
}
async fn call(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<CallRequest>,
) -> Result<Json<Value>, HttpError> {
    Ok(Json(
        serde_json::to_value(state.broker.call(bearer(&headers)?, request).await?)
            .expect("worker response serialization is infallible"),
    ))
}
async fn release(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, HttpError> {
    state.broker.release(bearer(&headers)?).await?;
    Ok(Json(json!({"released": true})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RenewRequest {
    ttl_seconds: u64,
}
async fn renew(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(request): Json<RenewRequest>,
) -> Result<Json<Value>, HttpError> {
    Ok(Json(
        json!({"lease": state.broker.renew(bearer(&headers)?, request.ttl_seconds).await?}),
    ))
}

async fn private_headers(request: Request, next: Next) -> Response {
    let preview =
        request.uri().path().starts_with("/preview") || request.uri().path().ends_with("/preview");
    let private = request.uri().path().starts_with("/credentials")
        || request.uri().path().starts_with("/v1/credential")
        || request.uri().path().starts_with("/preview")
        || request.uri().path().ends_with("/preview");
    let private = private
        || (request.uri().path().starts_with("/v1/computers/")
            && request.uri().path().contains("/files"));
    let mut response = next.run(request).await;
    if private {
        for (key, value) in [
            ("cache-control", "no-store"),
            ("pragma", "no-cache"),
            ("referrer-policy", "no-referrer"),
            ("x-content-type-options", "nosniff"),
            ("x-frame-options", "DENY"),
            (
                "content-security-policy",
                if preview {
                    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                } else {
                    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                },
            ),
        ] {
            response.headers_mut().insert(
                axum::http::HeaderName::from_static(key),
                axum::http::HeaderValue::from_static(value),
            );
        }
    }
    response
}
async fn preview_page() -> impl IntoResponse {
    axum::response::Html(include_str!("../ui/preview.html"))
}
async fn preview_script() -> impl IntoResponse {
    (
        [("content-type", "text/javascript; charset=utf-8")],
        include_str!("../ui/preview.js"),
    )
}
async fn preview_styles() -> impl IntoResponse {
    (
        [("content-type", "text/css; charset=utf-8")],
        include_str!("../ui/preview.css"),
    )
}
async fn credential_page() -> impl IntoResponse {
    axum::response::Html(include_str!("../ui/credentials.html"))
}
async fn credential_script() -> impl IntoResponse {
    (
        [("content-type", "text/javascript; charset=utf-8")],
        include_str!("../ui/credentials.js"),
    )
}
async fn credential_styles() -> impl IntoResponse {
    (
        [("content-type", "text/css; charset=utf-8")],
        include_str!("../ui/credentials.css"),
    )
}
fn credential_operator(state: &AppState, headers: &HeaderMap) -> Result<(), HttpError> {
    operator(state, headers)?;
    if headers
        .get("sec-fetch-site")
        .is_some_and(|v| v != "same-origin" && v != "none")
    {
        return Err(BrokerError::invalid("cross-origin credential request denied").into());
    }
    if let Some(origin) = headers.get("origin") {
        let source = origin
            .to_str()
            .ok()
            .and_then(|v| reqwest::Url::parse(v).ok());
        let host = headers.get("host").and_then(|v| v.to_str().ok());
        let valid = source.as_ref().is_some_and(|url| {
            matches!(url.scheme(), "http" | "https")
                && url.username().is_empty()
                && url.password().is_none()
                && url.path() == "/"
                && url.query().is_none()
                && url.fragment().is_none()
                && host.is_some_and(|host| {
                    reqwest::Url::parse(&format!("{}://{host}", url.scheme()))
                        .is_ok_and(|expected| expected.origin() == url.origin())
                })
        });
        if !valid {
            return Err(BrokerError::invalid("cross-origin credential request denied").into());
        }
    }
    Ok(())
}
fn private_json<T>(body: Result<Json<T>, JsonRejection>) -> Result<T, HttpError> {
    body.map(|Json(value)| value)
        .map_err(|_| BrokerError::invalid("invalid private input body").into())
}
async fn credential_list(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    Ok(Json(
        json!({"credentials":state.broker.list_credentials().await?}),
    ))
}
async fn credential_save(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<CredentialDraft>, JsonRejection>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    Ok(Json(json!(
        state.broker.save_credential(private_json(body)?).await?
    )))
}
async fn credential_update(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    body: Result<Json<CredentialDraft>, JsonRejection>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    Ok(Json(json!(
        state
            .broker
            .update_credential(&id, private_json(body)?)
            .await?
    )))
}
async fn credential_delete(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    state.broker.delete_credential(&id).await?;
    Ok(Json(json!({"deleted":id})))
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Assignments {
    credential_ids: Vec<String>,
    owners: Vec<ComputerOwner>,
}
async fn credential_assign(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<Assignments>, JsonRejection>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    let args = private_json(body)?;
    state
        .broker
        .assign_credentials(&args.credential_ids, &args.owners, false)
        .await?;
    Ok(Json(json!({"assigned":true})))
}
async fn credential_unassign(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<Assignments>, JsonRejection>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    let args = private_json(body)?;
    state
        .broker
        .assign_credentials(&args.credential_ids, &args.owners, true)
        .await?;
    Ok(Json(json!({"unassigned":true})))
}
async fn credential_requests(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    Ok(Json(
        json!({"requests":state.broker.credential_requests().await?}),
    ))
}
async fn credential_fulfill(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
    body: Result<Json<CredentialFulfillment>, JsonRejection>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    Ok(Json(json!(
        state
            .broker
            .fulfill_credential_request(&id, private_json(body)?)
            .await?
    )))
}
async fn credential_cancel(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, HttpError> {
    credential_operator(&state, &headers)?;
    state.broker.cancel_credential_request(&id).await?;
    Ok(Json(json!({"cancelled":id})))
}
