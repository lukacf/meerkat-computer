//! Operator CLI host file handling against a local synthetic broker.
use axum::{Json, Router, body::Bytes, extract::Query, http::HeaderMap, routing::get};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashMap, process::Output, time::Duration};
use tokio::process::Command;

// Computers use the broker's existing simple UUID encoding; transfer IDs use hyphens.
const COMPUTER: &str = "e6a3b5f7f9a44fe2a3bf52e0d8318285";
const FILE: &str = "84d151b1-c535-4777-b0d3-029b180b5d6f";
const TOKEN: &str = "synthetic-file-cli-operator-token";
const TOKEN_ENV: &str = "MEERKAT_FILE_CLI_TEST_TOKEN";

fn authorized(headers: &HeaderMap) {
    assert_eq!(headers["authorization"], format!("Bearer {TOKEN}"));
}

async fn invoke(url: &str, args: &[&str]) -> Output {
    tokio::time::timeout(
        Duration::from_secs(10),
        Command::new(env!("CARGO_BIN_EXE_rkat-computer"))
            .args([
                "files",
                "--broker-url",
                url,
                "--computer-id",
                COMPUTER,
                "--operator-token-env",
                TOKEN_ENV,
            ])
            .args(args)
            .env(TOKEN_ENV, TOKEN)
            .kill_on_drop(true)
            .output(),
    )
    .await
    .expect("file CLI must finish within its test deadline")
    .unwrap()
}

fn success(output: Output) -> Value {
    assert!(
        output.status.success(),
        "CLI failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

async fn fixture(corrupt_export: bool) -> (String, tokio::task::JoinHandle<()>, Vec<u8>) {
    let payload: Vec<u8> = (0..=255).cycle().take(256 * 1024).collect();
    let digest = format!("{:x}", Sha256::digest(&payload));
    let size = payload.len();
    let route = format!("/v1/computers/{COMPUTER}/files");
    let export = json!({
        "file_id":FILE,"filename":"report.bin","direction":"export",
        "size_bytes":size,"sha256":digest,"created_at_ms":123
    });
    let expected = payload.clone();
    let mut returned = payload.clone();
    if corrupt_export {
        returned[0] ^= 1;
    }
    let app = Router::new()
        .route(
            &route,
            get(move |headers: HeaderMap| async move {
                authorized(&headers);
                Json(json!({"files":[export]}))
            })
            .post(
                move |headers: HeaderMap,
                      Query(query): Query<HashMap<String, String>>,
                      bytes: Bytes| async move {
                    authorized(&headers);
                    assert_eq!(headers["content-type"], "application/octet-stream");
                    assert_eq!(bytes.as_ref(), expected);
                    Json(json!({
                        "file_id":FILE,"filename":query["filename"],"direction":"import",
                        "size_bytes":bytes.len(),"sha256":format!("{:x}",Sha256::digest(&bytes)),
                        "created_at_ms":123
                    }))
                },
            ),
        )
        .route(
            &format!("{route}/{FILE}"),
            get(move |headers: HeaderMap| async move {
                authorized(&headers);
                returned
            }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (url, task, payload)
}

#[tokio::test]
async fn cli_stages_binary_and_saves_verified_private_export_without_overwrite() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source.bin");
    let output = dir.path().join("output.bin");
    let (url, serving, payload) = fixture(false).await;
    std::fs::write(&source, &payload).unwrap();
    let metadata = success(
        invoke(
            &url,
            &[
                "stage",
                "--file",
                source.to_str().unwrap(),
                "--filename",
                "report #1.bin",
            ],
        )
        .await,
    );
    assert_eq!(metadata["filename"], "report #1.bin");
    assert_eq!(metadata["size_bytes"], payload.len());
    let args = [
        "get",
        "--file-id",
        FILE,
        "--output",
        output.to_str().unwrap(),
    ];
    let result = invoke(&url, &args).await;
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert_eq!(std::fs::read(&output).unwrap(), payload);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(&output).unwrap().permissions().mode() & 0o777,
            0o600
        );
    }
    std::fs::write(&output, b"operator work must remain").unwrap();
    assert!(!invoke(&url, &args).await.status.success());
    assert_eq!(
        std::fs::read(&output).unwrap(),
        b"operator work must remain"
    );
    serving.abort();
}

#[tokio::test]
async fn cli_never_creates_output_for_a_corrupted_export() {
    let dir = tempfile::tempdir().unwrap();
    let output = dir.path().join("must-not-exist.bin");
    let (url, serving, _) = fixture(true).await;
    let result = invoke(
        &url,
        &[
            "get",
            "--file-id",
            FILE,
            "--output",
            output.to_str().unwrap(),
        ],
    )
    .await;
    assert!(!result.status.success());
    assert!(!output.exists());
    serving.abort();
}

#[cfg(unix)]
#[tokio::test]
async fn cli_refuses_symlink_and_fifo_sources_without_connecting() {
    use std::os::unix::fs::symlink;
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source");
    let link = dir.path().join("link");
    let fifo = dir.path().join("fifo");
    std::fs::write(&source, b"private").unwrap();
    symlink(&source, &link).unwrap();
    let status = Command::new("mkfifo").arg(&fifo).status().await.unwrap();
    assert!(status.success());
    for path in [&link, &fifo] {
        let output = invoke(
            "http://127.0.0.1:1",
            &["stage", "--file", path.to_str().unwrap()],
        )
        .await;
        assert!(!output.status.success());
        assert!(
            !String::from_utf8_lossy(&output.stderr).contains("File staging outcome is unknown")
        );
    }
}

#[tokio::test]
async fn cli_refuses_remote_operator_endpoints_before_transfer() {
    let output = invoke("https://example.invalid", &["list"]).await;
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("loopback HTTP URL"));
}
