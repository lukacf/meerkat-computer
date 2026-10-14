//! Host-owned credential material and explicit durable-identity assignments.
//!
//! The complete snapshot is authenticated and encrypted. Only the trusted host
//! loads the separate key; guests receive admitted values, never the vault key.
//! This protects storage and other identities, not a value after delivery to an
//! administrator-controlled guest.

use crate::domain::{Effect, ErrorCode};
use crate::{BrokerError, ComputerOwner};
use aes_gcm::aead::{Aead, KeyInit, OsRng, Payload, rand_core::RngCore};
use aes_gcm::{Aes256Gcm, Nonce};
use fs2::FileExt;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use zeroize::{Zeroize, Zeroizing};

const MAGIC: &[u8; 8] = b"MCVAULT\x01";
const NONCE_BYTES: usize = 12;
const MAX_SNAPSHOT_BYTES: u64 = 16 * 1024 * 1024;
const MAX_SECRET_BYTES: usize = 16 * 1024;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialMetadata {
    pub id: String,
    pub realm: String,
    pub label: String,
    pub origin: String,
    pub version: u64,
    pub assignments: Vec<ComputerOwner>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CredentialDraft {
    pub realm: String,
    pub label: String,
    pub origin: String,
    pub username: Option<String>,
    pub secret: String,
}

impl fmt::Debug for CredentialDraft {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("CredentialDraft { [REDACTED] }")
    }
}

/// A private delivery payload. Deliberately has no public Serialize or Display.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SecretValues {
    pub username: Option<String>,
    pub secret: String,
}

impl fmt::Debug for SecretValues {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("SecretValues { [REDACTED] }")
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredCredential {
    metadata: CredentialMetadata,
    username: Option<String>,
    secret: String,
}

impl Drop for StoredCredential {
    fn drop(&mut self) {
        self.secret.zeroize();
        if let Some(username) = self.username.as_mut() {
            username.zeroize();
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    schema: u32,
    records: BTreeMap<String, StoredCredential>,
}

impl Default for Snapshot {
    fn default() -> Self {
        Self {
            schema: 1,
            records: BTreeMap::new(),
        }
    }
}

pub struct CredentialVault {
    path: PathBuf,
    directory: PathBuf,
    lock_path: PathBuf,
    key: Zeroizing<[u8; 32]>,
    gate: Mutex<()>,
}

/// Trusted-host injection seam. Backends own one credential library and its
/// assignment facts; protocol/UI adapters do not maintain a second copy.
pub trait CredentialStore: Send + Sync {
    fn save(&self, draft: CredentialDraft) -> Result<CredentialMetadata, BrokerError>;
    fn save_assigned(
        &self,
        draft: CredentialDraft,
        owners: &[ComputerOwner],
    ) -> Result<CredentialMetadata, BrokerError>;
    fn update(&self, id: &str, draft: CredentialDraft) -> Result<CredentialMetadata, BrokerError>;
    fn list(&self) -> Result<Vec<CredentialMetadata>, BrokerError>;
    fn assigned(&self, owner: &ComputerOwner) -> Result<Vec<CredentialMetadata>, BrokerError>;
    fn assign(&self, ids: &[String], owners: &[ComputerOwner]) -> Result<(), BrokerError>;
    fn unassign(&self, ids: &[String], owners: &[ComputerOwner]) -> Result<(), BrokerError>;
    fn resolve(
        &self,
        id: &str,
        owner: &ComputerOwner,
        origin: &str,
    ) -> Result<SecretValues, BrokerError>;
    fn delete(&self, id: &str) -> Result<(), BrokerError>;
}

impl CredentialStore for CredentialVault {
    fn save(&self, draft: CredentialDraft) -> Result<CredentialMetadata, BrokerError> {
        CredentialVault::save(self, draft)
    }
    fn save_assigned(
        &self,
        draft: CredentialDraft,
        owners: &[ComputerOwner],
    ) -> Result<CredentialMetadata, BrokerError> {
        CredentialVault::save_assigned(self, draft, owners)
    }
    fn update(&self, id: &str, draft: CredentialDraft) -> Result<CredentialMetadata, BrokerError> {
        CredentialVault::update(self, id, draft)
    }
    fn list(&self) -> Result<Vec<CredentialMetadata>, BrokerError> {
        CredentialVault::list(self)
    }
    fn assigned(&self, owner: &ComputerOwner) -> Result<Vec<CredentialMetadata>, BrokerError> {
        CredentialVault::assigned(self, owner)
    }
    fn assign(&self, ids: &[String], owners: &[ComputerOwner]) -> Result<(), BrokerError> {
        CredentialVault::assign(self, ids, owners)
    }
    fn unassign(&self, ids: &[String], owners: &[ComputerOwner]) -> Result<(), BrokerError> {
        CredentialVault::unassign(self, ids, owners)
    }
    fn resolve(
        &self,
        id: &str,
        owner: &ComputerOwner,
        origin: &str,
    ) -> Result<SecretValues, BrokerError> {
        CredentialVault::resolve(self, id, owner, origin)
    }
    fn delete(&self, id: &str) -> Result<(), BrokerError> {
        CredentialVault::delete(self, id)
    }
}

/// Normalize an exact web origin, never an arbitrary URL or wildcard pattern.
/// HTTP is allowed only for literal loopback addresses used by local fixtures.
pub fn canonical_origin(value: &str) -> Result<String, BrokerError> {
    let invalid = || BrokerError::invalid("credential origin must be an exact HTTPS origin");
    if value.trim() != value || value.chars().any(char::is_control) || value.contains(['\\', '*']) {
        return Err(invalid());
    }
    let url = reqwest::Url::parse(value).map_err(|_| invalid())?;
    let (_, tail) = value.split_once("://").ok_or_else(invalid)?;
    let authority = tail.split(['/', '?', '#']).next().unwrap_or_default();
    let remainder = &tail[authority.len()..];
    if authority.is_empty()
        || authority.contains('@')
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
        || url.path() != "/"
        || url.host_str().is_none()
        || url.host_str().is_some_and(|host| host.contains('*'))
        || (!remainder.is_empty() && remainder != "/")
    {
        return Err(invalid());
    }
    let raw_host = if let Some(bracketed) = authority.strip_prefix('[') {
        bracketed
            .split_once(']')
            .map(|(host, _)| host)
            .unwrap_or_default()
    } else {
        authority.split(':').next().unwrap_or_default()
    };
    let literal_loopback = raw_host
        .parse::<std::net::IpAddr>()
        .ok()
        .is_some_and(|address| address.is_loopback());
    if url.scheme() != "https" && !(url.scheme() == "http" && literal_loopback) {
        return Err(invalid());
    }
    Ok(url.origin().ascii_serialization())
}

impl CredentialVault {
    /// Explicit initialization only. Existing files are never overwritten.
    pub fn init_key(path: &Path) -> Result<(), BrokerError> {
        let directory = private_directory(parent(path)?)?;
        let filename = path
            .file_name()
            .ok_or_else(|| BrokerError::invalid("credential key requires a filename"))?;
        let path = directory.join(filename);
        let mut key = Zeroizing::new([0u8; 32]);
        OsRng
            .try_fill_bytes(&mut *key)
            .map_err(|_| storage("credential key randomness unavailable"))?;
        let mut file = private_open(&path, true)?;
        file.write_all(&*key)
            .and_then(|()| file.sync_all())
            .map_err(|_| storage("credential key write failed"))?;
        sync_directory(&directory)
    }

    pub fn open(path: &Path, key_file: &Path) -> Result<Self, BrokerError> {
        // A missing key does not initialize an apparently usable vault.
        let key_bytes = private_read(key_file, 32)?;
        let key: [u8; 32] = key_bytes
            .as_slice()
            .try_into()
            .map_err(|_| storage("credential key must contain exactly 32 bytes"))?;
        let directory = private_directory(parent(path)?)?;
        let canonical_key = fs::canonicalize(key_file)
            .map_err(|_| storage("credential key location cannot be resolved"))?;
        if canonical_key.starts_with(&directory) {
            return Err(BrokerError::invalid(
                "credential key must be outside the vault directory",
            ));
        }
        let filename = path
            .file_name()
            .ok_or_else(|| BrokerError::invalid("credential vault requires a filename"))?;
        let path = directory.join(filename);
        let lock_path = directory.join(format!(".{}.lock", filename.to_string_lossy()));
        let vault = Self {
            path,
            directory,
            lock_path,
            key: Zeroizing::new(key),
            gate: Mutex::new(()),
        };
        {
            let _guard = vault
                .gate
                .lock()
                .map_err(|_| storage("credential vault lock failed"))?;
            let _lock = vault.lock()?;
            match fs::symlink_metadata(&vault.path) {
                Ok(_) => {
                    vault.load()?;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    vault.persist(&Snapshot::default())?;
                }
                Err(_) => return Err(storage("credential vault cannot be inspected")),
            }
        }
        Ok(vault)
    }

    pub fn save(&self, draft: CredentialDraft) -> Result<CredentialMetadata, BrokerError> {
        self.save_assigned(draft, &[])
    }

    pub fn save_assigned(
        &self,
        draft: CredentialDraft,
        owners: &[ComputerOwner],
    ) -> Result<CredentialMetadata, BrokerError> {
        let mut record = record_from_draft(draft)?;
        record.metadata.assignments = validated_assignments(owners, &record.metadata.realm)?;
        self.transaction(true, |snapshot| {
            let metadata = record.metadata.clone();
            snapshot.records.insert(metadata.id.clone(), record);
            Ok(metadata)
        })
    }

    pub fn update(
        &self,
        id: &str,
        draft: CredentialDraft,
    ) -> Result<CredentialMetadata, BrokerError> {
        let mut replacement = record_from_draft(draft)?;
        self.transaction(true, |snapshot| {
            let old = snapshot.records.get(id).ok_or_else(missing)?;
            if old.metadata.realm != replacement.metadata.realm {
                return Err(BrokerError::invalid("credential realm cannot be changed"));
            }
            replacement.metadata.id = old.metadata.id.clone();
            replacement.metadata.version = old
                .metadata
                .version
                .checked_add(1)
                .ok_or_else(|| storage("credential version exhausted"))?;
            replacement.metadata.assignments = old.metadata.assignments.clone();
            let metadata = replacement.metadata.clone();
            snapshot.records.insert(id.to_owned(), replacement);
            Ok(metadata)
        })
    }

    pub fn list(&self) -> Result<Vec<CredentialMetadata>, BrokerError> {
        self.transaction(false, |snapshot| {
            Ok(snapshot
                .records
                .values()
                .map(|record| record.metadata.clone())
                .collect())
        })
    }

    pub fn assigned(&self, owner: &ComputerOwner) -> Result<Vec<CredentialMetadata>, BrokerError> {
        identity_realm(owner)?;
        self.transaction(false, |snapshot| {
            Ok(snapshot
                .records
                .values()
                .filter(|record| record.metadata.assignments.contains(owner))
                .map(|record| record.metadata.clone())
                .collect())
        })
    }

    pub fn assign(&self, ids: &[String], owners: &[ComputerOwner]) -> Result<(), BrokerError> {
        self.change_assignments(ids, owners, true)
    }

    pub fn unassign(&self, ids: &[String], owners: &[ComputerOwner]) -> Result<(), BrokerError> {
        self.change_assignments(ids, owners, false)
    }

    fn change_assignments(
        &self,
        ids: &[String],
        owners: &[ComputerOwner],
        add: bool,
    ) -> Result<(), BrokerError> {
        if ids.is_empty() || owners.is_empty() {
            return Err(BrokerError::invalid(
                "credential assignment requires credentials and identities",
            ));
        }
        self.transaction(true, |snapshot| {
            // Validate the whole batch before modifying any record.
            for id in ids {
                let record = snapshot.records.get(id).ok_or_else(missing)?;
                validated_assignments(owners, &record.metadata.realm)?;
            }
            for id in ids {
                let record = snapshot.records.get_mut(id).ok_or_else(missing)?;
                let mut assigned: BTreeSet<_> =
                    record.metadata.assignments.iter().cloned().collect();
                for owner in owners {
                    if add {
                        assigned.insert(owner.clone());
                    } else {
                        assigned.remove(owner);
                    }
                }
                record.metadata.assignments = assigned.into_iter().collect();
            }
            Ok(())
        })
    }

    /// Check current disk-backed assignments immediately before private delivery.
    pub fn resolve(
        &self,
        id: &str,
        owner: &ComputerOwner,
        origin: &str,
    ) -> Result<SecretValues, BrokerError> {
        identity_realm(owner)?;
        let origin = canonical_origin(origin)?;
        self.transaction(false, |snapshot| {
            let record = snapshot.records.get(id).ok_or_else(denied)?;
            if record.metadata.origin != origin || !record.metadata.assignments.contains(owner) {
                return Err(denied());
            }
            Ok(SecretValues {
                username: record.username.clone(),
                secret: record.secret.clone(),
            })
        })
    }

    pub fn delete(&self, id: &str) -> Result<(), BrokerError> {
        self.transaction(true, |snapshot| {
            snapshot.records.remove(id).ok_or_else(missing)?;
            Ok(())
        })
    }

    fn transaction<T>(
        &self,
        write: bool,
        operation: impl FnOnce(&mut Snapshot) -> Result<T, BrokerError>,
    ) -> Result<T, BrokerError> {
        let _guard = self
            .gate
            .lock()
            .map_err(|_| storage("credential vault lock failed"))?;
        let _lock = self.lock()?;
        let mut snapshot = self.load()?;
        let result = operation(&mut snapshot)?;
        if write {
            self.persist(&snapshot)?;
        }
        Ok(result)
    }

    fn lock(&self) -> Result<File, BrokerError> {
        let file = match private_open(&self.lock_path, true) {
            Ok(file) => file,
            Err(_) => private_open(&self.lock_path, false)?,
        };
        FileExt::lock_exclusive(&file).map_err(|_| storage("credential vault lock failed"))?;
        Ok(file)
    }

    fn load(&self) -> Result<Snapshot, BrokerError> {
        let bytes = private_read(&self.path, MAX_SNAPSHOT_BYTES)?;
        if bytes.len() < MAGIC.len() + NONCE_BYTES + 16 || &bytes[..MAGIC.len()] != MAGIC {
            return Err(storage("credential vault format is invalid"));
        }
        let cipher = Aes256Gcm::new_from_slice(&*self.key)
            .map_err(|_| storage("credential vault key is invalid"))?;
        let plaintext = Zeroizing::new(
            cipher
                .decrypt(
                    Nonce::from_slice(&bytes[MAGIC.len()..MAGIC.len() + NONCE_BYTES]),
                    Payload {
                        msg: &bytes[MAGIC.len() + NONCE_BYTES..],
                        aad: MAGIC,
                    },
                )
                .map_err(|_| storage("credential vault authentication failed"))?,
        );
        let snapshot: Snapshot = serde_json::from_slice(&plaintext)
            .map_err(|_| storage("credential vault payload is invalid"))?;
        if snapshot.schema != 1 {
            return Err(storage("credential vault schema is unsupported"));
        }
        for (id, record) in &snapshot.records {
            validate_record(id, record)?;
        }
        Ok(snapshot)
    }

    fn persist(&self, snapshot: &Snapshot) -> Result<(), BrokerError> {
        let plaintext = Zeroizing::new(
            serde_json::to_vec(snapshot)
                .map_err(|_| storage("credential vault encoding failed"))?,
        );
        if plaintext.len() as u64 + MAGIC.len() as u64 + NONCE_BYTES as u64 + 16
            > MAX_SNAPSHOT_BYTES
        {
            return Err(storage("credential vault capacity exceeded"));
        }
        let cipher = Aes256Gcm::new_from_slice(&*self.key)
            .map_err(|_| storage("credential vault key is invalid"))?;
        let mut nonce = [0u8; NONCE_BYTES];
        OsRng
            .try_fill_bytes(&mut nonce)
            .map_err(|_| storage("credential vault randomness unavailable"))?;
        let ciphertext = cipher
            .encrypt(
                Nonce::from_slice(&nonce),
                Payload {
                    msg: &plaintext,
                    aad: MAGIC,
                },
            )
            .map_err(|_| storage("credential vault encryption failed"))?;
        let tmp_path = self
            .directory
            .join(format!(".credential-{}.tmp", uuid::Uuid::new_v4()));
        let result = (|| {
            let mut file = private_open(&tmp_path, true)?;
            file.write_all(MAGIC)
                .and_then(|()| file.write_all(&nonce))
                .and_then(|()| file.write_all(&ciphertext))
                .and_then(|()| file.sync_all())
                .map_err(|_| storage("credential vault write failed"))?;
            fs::rename(&tmp_path, &self.path)
                .map_err(|_| storage("credential vault replacement failed"))?;
            sync_directory(&self.directory).map_err(|_| {
                BrokerError::unknown("credential vault commit durability is uncertain")
            })
        })();
        if result.is_err() {
            let _ = fs::remove_file(&tmp_path);
        }
        result
    }
}

fn record_from_draft(draft: CredentialDraft) -> Result<StoredCredential, BrokerError> {
    validate_text(&draft.realm, 512)?;
    validate_text(&draft.label, 256)?;
    validate_values(draft.username.as_deref(), &draft.secret)?;
    Ok(StoredCredential {
        metadata: CredentialMetadata {
            id: uuid::Uuid::new_v4().to_string(),
            realm: draft.realm,
            label: draft.label,
            origin: canonical_origin(&draft.origin)?,
            version: 1,
            assignments: Vec::new(),
        },
        username: draft.username,
        secret: draft.secret,
    })
}

fn validate_record(id: &str, record: &StoredCredential) -> Result<(), BrokerError> {
    let metadata = &record.metadata;
    let valid = || -> Result<(), BrokerError> {
        validate_text(&metadata.realm, 512)?;
        validate_text(&metadata.label, 256)?;
        validate_values(record.username.as_deref(), &record.secret)?;
        if metadata.id != id
            || uuid::Uuid::parse_str(id).is_err()
            || metadata.version == 0
            || canonical_origin(&metadata.origin)? != metadata.origin
            || validated_assignments(&metadata.assignments, &metadata.realm)?
                != metadata.assignments
        {
            return Err(storage("credential record is invalid"));
        }
        Ok(())
    };
    valid().map_err(|_| storage("credential vault record is invalid"))
}

fn validate_text(value: &str, limit: usize) -> Result<(), BrokerError> {
    if value.is_empty()
        || value.len() > limit
        || value.trim() != value
        || value.chars().any(char::is_control)
    {
        return Err(BrokerError::invalid("credential metadata is invalid"));
    }
    Ok(())
}

fn validate_values(username: Option<&str>, secret: &str) -> Result<(), BrokerError> {
    if secret.is_empty()
        || secret.len() > MAX_SECRET_BYTES
        || username.is_some_and(|name| name.len() > MAX_SECRET_BYTES)
    {
        return Err(BrokerError::invalid("credential value length is invalid"));
    }
    Ok(())
}

fn identity_realm(owner: &ComputerOwner) -> Result<&str, BrokerError> {
    owner.validate()?;
    match owner {
        ComputerOwner::Identity { realm, .. } => Ok(realm),
        ComputerOwner::Session { .. } => Err(BrokerError::invalid(
            "saved credentials require a durable identity assignment",
        )),
    }
}

fn validated_assignments(
    owners: &[ComputerOwner],
    realm: &str,
) -> Result<Vec<ComputerOwner>, BrokerError> {
    let mut unique = BTreeSet::new();
    for owner in owners {
        if identity_realm(owner)? != realm {
            return Err(BrokerError::invalid(
                "credential assignments must belong to the credential realm",
            ));
        }
        unique.insert(owner.clone());
    }
    Ok(unique.into_iter().collect())
}

fn parent(path: &Path) -> Result<&Path, BrokerError> {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| BrokerError::invalid("credential path requires a parent directory"))
}

#[cfg(unix)]
fn private_directory(path: &Path) -> Result<PathBuf, BrokerError> {
    use std::os::unix::fs::{DirBuilderExt, MetadataExt};
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)
        .map_err(|_| storage("credential directory cannot be created"))?;
    let canonical =
        fs::canonicalize(path).map_err(|_| storage("credential directory cannot be resolved"))?;
    let metadata = fs::metadata(&canonical)
        .map_err(|_| storage("credential directory cannot be inspected"))?;
    // SAFETY: geteuid has no argument, failure, or memory contract.
    if !metadata.is_dir()
        || metadata.mode() & 0o022 != 0
        || metadata.uid() != unsafe { libc::geteuid() }
    {
        return Err(storage(
            "credential directory must be owned and not writable by other users",
        ));
    }
    Ok(canonical)
}

#[cfg(not(unix))]
fn private_directory(_: &Path) -> Result<PathBuf, BrokerError> {
    Err(storage(
        "credential vault requires Unix private-file support",
    ))
}

#[cfg(unix)]
fn private_open(path: &Path, create: bool) -> Result<File, BrokerError> {
    use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
    let mut options = OpenOptions::new();
    // Nonblocking prevents a hostile FIFO from hanging before the regular-file
    // check. It does not change reads or writes to accepted regular files.
    options
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC | libc::O_NONBLOCK);
    if create {
        options.write(true).create_new(true).mode(0o600);
    }
    let file = options
        .open(path)
        .map_err(|_| storage("credential file cannot be opened safely"))?;
    let metadata = file
        .metadata()
        .map_err(|_| storage("credential file cannot be inspected"))?;
    // SAFETY: geteuid has no argument, failure, or memory contract.
    if !metadata.is_file()
        || metadata.mode() & 0o777 != 0o600
        || metadata.nlink() != 1
        || metadata.uid() != unsafe { libc::geteuid() }
    {
        return Err(storage(
            "credential file must be an owned private regular file",
        ));
    }
    Ok(file)
}

#[cfg(not(unix))]
fn private_open(_: &Path, _: bool) -> Result<File, BrokerError> {
    Err(storage(
        "credential vault requires Unix private-file support",
    ))
}

fn private_read(path: &Path, max: u64) -> Result<Zeroizing<Vec<u8>>, BrokerError> {
    let file = private_open(path, false)?;
    let mut bytes = Zeroizing::new(Vec::new());
    file.take(max + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| storage("credential file read failed"))?;
    if bytes.len() as u64 > max {
        return Err(storage("credential file is too large"));
    }
    Ok(bytes)
}

fn sync_directory(path: &Path) -> Result<(), BrokerError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|_| storage("credential directory sync failed"))
}

fn storage(message: &'static str) -> BrokerError {
    BrokerError::storage(message)
}
fn missing() -> BrokerError {
    BrokerError {
        code: ErrorCode::NotFound,
        message: "credential not found".into(),
        effect: Effect::None,
        metrics: None,
        backend_failure: None,
    }
}
fn denied() -> BrokerError {
    BrokerError {
        code: ErrorCode::CapabilityDenied,
        message: "credential is not assigned for this identity and origin".into(),
        effect: Effect::None,
        metrics: None,
        backend_failure: None,
    }
}
