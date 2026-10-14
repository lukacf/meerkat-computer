mod server;

use clap::{Parser, Subcommand};
use meerkat_computer::{Broker, Config, backend::IncusBackend};
use reqwest::{Client, Method};
use serde_json::{Value, json};
use std::{
    error::Error,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;

#[derive(Parser)]
#[command(
    name = "rkat-computer",
    version,
    about = "Identity-owned computers for Meerkat agents"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Stage inputs and collect exports using trusted operator access.
    Files {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        computer_id: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
        #[command(subcommand)]
        command: FileCommand,
    },
    /// Run the always-on trusted gateway for demand wake and idle suspension.
    Gateway {
        #[arg(long)]
        config: PathBuf,
    },
    /// Create a private local vault key outside the broker state directory. Never prints key bytes.
    InitVaultKey {
        #[arg(long)]
        key_file: PathBuf,
    },
    /// Run the broker on the Incus host. Use SSH forwarding for remote clients.
    Serve {
        #[arg(long)]
        config: PathBuf,
    },
    /// Validate configuration without touching infrastructure.
    Validate {
        #[arg(long)]
        config: PathBuf,
    },
    /// Attach a trusted owner to a computer and save a private execution binding.
    Attach {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        request: PathBuf,
        #[arg(long)]
        binding_file: PathBuf,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    /// Fork one running VM into independent targets using one full-state checkpoint.
    Fork {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        request: PathBuf,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    /// Read an existing fork receipt. Never repeats its side effects.
    ForkStatus {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        request_id: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    /// Explicitly reopen source control after an uncertain checkpoint operation.
    ForkResumeSource {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        request_id: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    List {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    /// Inspect a verified guest and print root-shell argv for the Incus host operator.
    Access {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        computer_id: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    Stop {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        computer_id: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    Reap {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long, default_value = "MEERKAT_COMPUTER_OPERATOR_TOKEN")]
        operator_token_env: String,
    },
    Release {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        binding_file: PathBuf,
    },
    Renew {
        #[arg(long, default_value = "http://127.0.0.1:18760")]
        broker_url: String,
        #[arg(long)]
        binding_file: PathBuf,
        #[arg(long, default_value_t = 3600)]
        ttl_seconds: u64,
    },
}

#[derive(Subcommand)]
enum FileCommand {
    /// Copy one host file into private staging; returns the ID the agent may import.
    Stage {
        #[arg(long)]
        file: PathBuf,
        #[arg(long)]
        filename: Option<String>,
    },
    /// List staged input and exported output metadata.
    List,
    /// Save a verified exported file to a new private host file. Never overwrites.
    Get {
        #[arg(long)]
        file_id: String,
        #[arg(long)]
        output: PathBuf,
    },
    /// Delete a staged input or collected export from broker storage.
    Delete {
        #[arg(long)]
        file_id: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    match cli.command {
        Command::Files {
            broker_url,
            computer_id,
            operator_token_env,
            command,
        } => {
            file_command(
                &broker_url,
                &computer_id,
                &credential(&operator_token_env)?,
                command,
            )
            .await?;
        }
        Command::InitVaultKey { key_file } => {
            meerkat_computer::credentials::CredentialVault::init_key(&key_file)?;
            println!("Created private vault key: {}", key_file.display());
        }
        Command::Gateway { config } => {
            use meerkat_computer::gateway::{Gateway, GatewayConfig, command::CommandHostPower};
            struct GatewayClock;
            impl meerkat_computer::Clock for GatewayClock {
                fn now_ms(&self) -> u64 {
                    std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_millis()
                        .try_into()
                        .unwrap_or(u64::MAX)
                }
            }
            let config = GatewayConfig::load(&config).map_err(std::io::Error::other)?;
            let token = credential(&config.host_token_env)?;
            let address = config.listen.clone();
            let power =
                Arc::new(CommandHostPower::new(config.clone()).map_err(std::io::Error::other)?);
            let gateway = Gateway::open(config, &token, power, Arc::new(GatewayClock))
                .map_err(std::io::Error::other)?;
            let listener = tokio::net::TcpListener::bind(&address).await?;
            let app = gateway.router();
            let reaper = tokio::spawn(async move {
                let mut interval = tokio::time::interval(Duration::from_secs(10));
                loop {
                    interval.tick().await;
                    if gateway.reap().await.is_err() {
                        tracing::warn!("host idle suspension refused; inspect gateway status");
                    }
                }
            });
            #[cfg(unix)]
            let mut terminate =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
            let result = axum::serve(listener, app)
                .with_graceful_shutdown(async move {
                    #[cfg(unix)]
                    tokio::select! {
                        _ = tokio::signal::ctrl_c() => {},
                        _ = terminate.recv() => {},
                    }
                    #[cfg(not(unix))]
                    {
                        let _ = tokio::signal::ctrl_c().await;
                    }
                })
                .await;
            reaper.abort();
            // Wait for its Gateway ownership to drop after the server drains,
            // so the owned tunnel group is terminated before process exit.
            let _ = reaper.await;
            result?;
        }
        Command::Serve { config } => {
            let config = Config::load(config)?;
            let address: std::net::SocketAddr = config.server.listen.parse()?;
            if !address.ip().is_loopback() {
                return Err(
                    "The broker must bind loopback; use an SSH tunnel for remote access".into(),
                );
            }
            let token = credential(&config.server.operator_token_env)?;
            tracing_subscriber::fmt()
                .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
                .with_writer(std::io::stderr)
                .init();
            let backend = Arc::new(IncusBackend::new(config.pool.clone())?);
            let broker = Arc::new(Broker::open(config, backend).await?);
            let app = server::router(broker.clone(), &token);
            let listener = tokio::net::TcpListener::bind(address).await?;
            eprintln!("meerkat-computer listening on {address}");
            let reaper = tokio::spawn(async move {
                let mut timer = tokio::time::interval(Duration::from_secs(30));
                loop {
                    timer.tick().await;
                    if let Err(error) = broker.reap().await {
                        tracing::warn!(code = error.code(), "idle cleanup failed");
                    }
                }
            });
            let result = axum::serve(listener, app)
                .with_graceful_shutdown(async {
                    let _ = tokio::signal::ctrl_c().await;
                })
                .await;
            reaper.abort();
            result?;
        }
        Command::Validate { config } => {
            Config::load(config)?;
            println!("Configuration valid");
        }
        Command::Attach {
            broker_url,
            request: request_file,
            binding_file,
            operator_token_env,
        } => {
            let body: meerkat_computer::AttachmentRequest =
                serde_json::from_slice(&fs::read(request_file)?)?;
            let operator_token = credential(&operator_token_env)?;
            let mut output = private_create(&binding_file)?;
            let result = request(
                &broker_url,
                Method::POST,
                "/v1/attachments",
                &operator_token,
                Some(serde_json::to_value(body)?),
            )
            .await;
            match result {
                Ok(binding) => {
                    serde_json::to_writer_pretty(&mut output, &binding)?;
                    output.write_all(b"\n")?;
                    output.sync_all()?;
                    println!("Binding saved to {}", binding_file.display());
                    println!(
                        "Computer: {}",
                        binding["computer_id"].as_str().unwrap_or("unknown")
                    );
                }
                Err(error) => {
                    drop(output);
                    let _ = fs::remove_file(&binding_file);
                    return Err(error);
                }
            }
        }
        Command::Fork {
            broker_url,
            request: request_file,
            operator_token_env,
        } => {
            let body: meerkat_computer::ForkRequest =
                serde_json::from_slice(&fs::read(request_file)?)?;
            print_json(
                request(
                    &broker_url,
                    Method::POST,
                    "/v1/forks",
                    &credential(&operator_token_env)?,
                    Some(serde_json::to_value(body)?),
                )
                .await?,
            );
        }
        Command::ForkStatus {
            broker_url,
            request_id,
            operator_token_env,
        } => {
            let id = uuid::Uuid::parse_str(&request_id)?;
            print_json(
                request(
                    &broker_url,
                    Method::GET,
                    &format!("/v1/forks/{id}"),
                    &credential(&operator_token_env)?,
                    None,
                )
                .await?,
            );
        }
        Command::ForkResumeSource {
            broker_url,
            request_id,
            operator_token_env,
        } => {
            let id = uuid::Uuid::parse_str(&request_id)?;
            print_json(
                request(
                    &broker_url,
                    Method::POST,
                    &format!("/v1/forks/{id}/resume-source"),
                    &credential(&operator_token_env)?,
                    None,
                )
                .await?,
            );
        }
        Command::List {
            broker_url,
            operator_token_env,
        } => print_json(
            request(
                &broker_url,
                Method::GET,
                "/v1/computers",
                &credential(&operator_token_env)?,
                None,
            )
            .await?,
        ),
        Command::Access {
            broker_url,
            computer_id,
            operator_token_env,
        } => {
            let id = uuid::Uuid::parse_str(&computer_id)?;
            print_json(
                request(
                    &broker_url,
                    Method::GET,
                    &format!("/v1/computers/{id}/access"),
                    &credential(&operator_token_env)?,
                    None,
                )
                .await?,
            );
        }
        Command::Stop {
            broker_url,
            computer_id,
            operator_token_env,
        } => {
            if !computer_id
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-')
                || computer_id.is_empty()
            {
                return Err("Invalid computer ID".into());
            }
            print_json(
                request(
                    &broker_url,
                    Method::POST,
                    &format!("/v1/computers/{computer_id}/stop"),
                    &credential(&operator_token_env)?,
                    None,
                )
                .await?,
            );
        }
        Command::Reap {
            broker_url,
            operator_token_env,
        } => print_json(
            request(
                &broker_url,
                Method::POST,
                "/v1/reap",
                &credential(&operator_token_env)?,
                None,
            )
            .await?,
        ),
        Command::Release {
            broker_url,
            binding_file,
        } => {
            let binding = read_binding(&binding_file)?;
            print_json(
                request(
                    &broker_url,
                    Method::POST,
                    "/v1/lease/release",
                    binding_token(&binding)?,
                    None,
                )
                .await?,
            );
        }
        Command::Renew {
            broker_url,
            binding_file,
            ttl_seconds,
        } => {
            let binding = read_binding(&binding_file)?;
            print_json(
                request(
                    &broker_url,
                    Method::POST,
                    "/v1/lease/renew",
                    binding_token(&binding)?,
                    Some(json!({"ttl_seconds": ttl_seconds})),
                )
                .await?,
            );
        }
    }
    Ok(())
}

fn credential(name: &str) -> Result<String> {
    let value = std::env::var(name)
        .map_err(|_| format!("Set the credential environment variable {name}"))?;
    if value.len() < 32 || value.chars().any(char::is_whitespace) {
        return Err(format!(
            "{name} must contain a secret of at least 32 characters without whitespace"
        )
        .into());
    }
    Ok(value)
}
fn private_create(path: &Path) -> Result<File> {
    if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
        fs::create_dir_all(parent)?;
    }
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}
fn read_binding(path: &Path) -> Result<Value> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_file() || metadata.len() > 1024 * 1024 {
        return Err("Binding must be a regular private JSON file".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return Err("Binding file must have mode 0600".into());
        }
    }
    Ok(serde_json::from_slice(&fs::read(path)?)?)
}
fn binding_token(binding: &Value) -> Result<&str> {
    binding
        .get("token")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| "Binding has no token".into())
}
async fn request(
    base: &str,
    method: Method,
    path: &str,
    token: &str,
    body: Option<Value>,
) -> Result<Value> {
    let (client, url) = broker_connection(base, path)?;
    let mut call = client.request(method, url.join(path)?).bearer_auth(token);
    if let Some(body) = body {
        call = call.json(&body);
    }
    let response = call.send().await.map_err(
        |_| "Broker transport failed; do not replay an operation with an uncertain effect",
    )?;
    let status = response.status();
    let body: Value = response
        .json()
        .await
        .map_err(|_| "Broker returned an invalid response")?;
    if !status.is_success() {
        // Only the typed error code is printed, never arbitrary response content.
        let code = body
            .pointer("/error/code")
            .and_then(Value::as_str)
            .unwrap_or("request_failed");
        return Err(format!("Broker request failed ({status}, {code})").into());
    }
    Ok(body)
}

fn broker_connection(base: &str, path: &str) -> Result<(Client, reqwest::Url)> {
    let url = reqwest::Url::parse(base)?;
    if url.scheme() != "http"
        || !matches!(url.host_str(), Some("127.0.0.1" | "localhost" | "[::1]"))
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
    {
        return Err(
            "Broker URL must be a plain loopback HTTP URL; forward remote access over SSH".into(),
        );
    }
    let client = Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(if path == "/v1/forks" {
            3600
        } else {
            300
        }))
        .build()?;
    Ok((client, url))
}

async fn bounded_bytes(mut response: reqwest::Response, limit: usize) -> Result<Vec<u8>> {
    if !response.status().is_success() {
        return Err("Broker refused file transfer".into());
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| "File transfer was interrupted")?
    {
        if bytes.len().saturating_add(chunk.len()) > limit {
            return Err("File transfer exceeded its byte limit".into());
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

async fn file_command(
    base: &str,
    computer_id: &str,
    token: &str,
    command: FileCommand,
) -> Result<()> {
    use sha2::{Digest, Sha256};
    const LIMIT: usize = 16 * 1024 * 1024;
    uuid::Uuid::parse_str(computer_id)?;
    // Validate the ID without changing the broker's persisted UUID encoding.
    let path = format!("/v1/computers/{computer_id}/files");
    let (client, url) = broker_connection(base, &path)?;
    match command {
        FileCommand::List => print_json(request(base, Method::GET, &path, token, None).await?),
        FileCommand::Delete { file_id } => {
            let id = uuid::Uuid::parse_str(&file_id)?;
            print_json(request(base, Method::DELETE, &format!("{path}/{id}"), token, None).await?);
        }
        FileCommand::Stage { file, filename } => {
            let mut options = OpenOptions::new();
            options.read(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
            }
            let stream = options.open(&file)?;
            let info = stream.metadata()?;
            if !info.is_file() || info.len() > LIMIT as u64 {
                return Err("Source must be a regular file of at most 16 MiB".into());
            }
            let mut bytes = Vec::new();
            stream.take(LIMIT as u64 + 1).read_to_end(&mut bytes)?;
            if bytes.len() > LIMIT {
                return Err("Source exceeded the transfer byte limit".into());
            }
            let filename = filename
                .or_else(|| file.file_name()?.to_str().map(str::to_owned))
                .ok_or("Supply a UTF-8 display filename")?;
            let expected_hash = format!("{:x}", Sha256::digest(&bytes));
            let expected_size = bytes.len();
            let response = client
                .post(url.join(&path)?)
                .bearer_auth(token)
                .query(&[("filename", &filename)])
                .header("Content-Type", "application/octet-stream")
                .body(bytes)
                .send()
                .await
                .map_err(
                    |_| "File staging outcome is unknown; inspect the file list before retrying",
                )?;
            let body: Value = serde_json::from_slice(&bounded_bytes(response, 65536).await?)?;
            if body["sha256"] != expected_hash
                || body["size_bytes"] != expected_size
                || body["filename"] != filename
                || body["direction"] != "import"
                || body["file_id"]
                    .as_str()
                    .and_then(|s| uuid::Uuid::parse_str(s).ok())
                    .is_none()
            {
                return Err("Broker returned an invalid file staging receipt".into());
            }
            print_json(body);
        }
        FileCommand::Get { file_id, output } => {
            let id = uuid::Uuid::parse_str(&file_id)?.to_string();
            let listing = request(base, Method::GET, &path, token, None).await?;
            let metadata = listing["files"]
                .as_array()
                .and_then(|files| {
                    files
                        .iter()
                        .find(|item| item["file_id"] == id && item["direction"] == "export")
                })
                .ok_or("Exported file is unavailable")?;
            let size = metadata["size_bytes"]
                .as_u64()
                .filter(|size| *size <= LIMIT as u64)
                .ok_or("Invalid export size")?;
            let response = client
                .get(url.join(&format!("{path}/{id}"))?)
                .bearer_auth(token)
                .send()
                .await
                .map_err(|_| "Export retrieval failed")?;
            let bytes = bounded_bytes(response, size as usize).await?;
            if bytes.len() as u64 != size
                || metadata["sha256"] != format!("{:x}", Sha256::digest(&bytes))
            {
                return Err("Export size or digest did not match its receipt".into());
            }
            let mut file = private_create(&output)?;
            if file
                .write_all(&bytes)
                .and_then(|()| file.sync_all())
                .is_err()
            {
                drop(file);
                let _ = fs::remove_file(&output);
                return Err("Could not save the exported file".into());
            }
            println!("Saved {} bytes to {}", size, output.display());
        }
    }
    Ok(())
}
fn print_json(value: Value) {
    println!(
        "{}",
        serde_json::to_string_pretty(&value).unwrap_or_else(|_| "{}".into())
    );
}
