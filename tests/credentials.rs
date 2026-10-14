use meerkat_computer::ComputerOwner;
use meerkat_computer::credentials::{CredentialDraft, CredentialVault, canonical_origin};
use std::{fs, path::PathBuf};
use tempfile::TempDir;

struct Fixture {
    dir: TempDir,
    path: PathBuf,
    key: PathBuf,
    vault: CredentialVault,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("vault/credentials.enc");
        let key = dir.path().join("key");
        CredentialVault::init_key(&key).unwrap();
        let vault = CredentialVault::open(&path, &key).unwrap();
        Self {
            dir,
            path,
            key,
            vault,
        }
    }
}

fn owner(identity: &str) -> ComputerOwner {
    ComputerOwner::Identity {
        realm: "work".into(),
        namespace: "team".into(),
        identity: identity.into(),
    }
}

fn draft() -> CredentialDraft {
    CredentialDraft {
        realm: "work".into(),
        label: "private synthetic account".into(),
        origin: "https://example.com".into(),
        username: Some("synthetic-user@example.com".into()),
        secret: "synthetic secret with spaces".into(),
    }
}

#[test]
fn encrypted_snapshot_hides_all_record_material_and_debug_redacts() {
    let f = Fixture::new();
    let metadata = f.vault.save(draft()).unwrap();
    f.vault
        .assign(std::slice::from_ref(&metadata.id), &[owner("agent-a")])
        .unwrap();
    let bytes = fs::read(&f.path).unwrap();
    for value in [
        "private synthetic account",
        "synthetic-user@example.com",
        "synthetic secret with spaces",
        "https://example.com",
        "agent-a",
    ] {
        assert!(
            !bytes
                .windows(value.len())
                .any(|window| window == value.as_bytes())
        );
    }
    let values = f
        .vault
        .resolve(&metadata.id, &owner("agent-a"), "https://example.com")
        .unwrap();
    assert!(!format!("{values:?}").contains("synthetic"));
    assert!(!format!("{:?}", draft()).contains("synthetic"));
}

#[test]
fn restart_preserves_shared_assignment_and_update_changes_one_record() {
    let f = Fixture::new();
    let saved = f.vault.save(draft()).unwrap();
    let agents = [owner("agent-a"), owner("agent-b")];
    f.vault
        .assign(std::slice::from_ref(&saved.id), &agents)
        .unwrap();
    let mut replacement = draft();
    replacement.secret = "replacement credential".into();
    let updated = f.vault.update(&saved.id, replacement).unwrap();
    assert_eq!(updated.id, saved.id);
    assert_eq!(updated.version, saved.version + 1);
    assert_eq!(updated.assignments, agents);
    let reopened = CredentialVault::open(&f.path, &f.key).unwrap();
    assert_eq!(reopened.list().unwrap().len(), 1);
    for agent in agents {
        assert_eq!(
            reopened
                .resolve(&saved.id, &agent, "https://example.com")
                .unwrap()
                .secret,
            "replacement credential"
        );
    }
}

#[test]
fn bulk_assignment_is_atomic_for_missing_credentials_and_invalid_owners() {
    let f = Fixture::new();
    let one = f.vault.save(draft()).unwrap();
    let two = f.vault.save(draft()).unwrap();
    assert!(
        f.vault
            .assign(&[one.id.clone(), "missing".into()], &[owner("agent-a")])
            .is_err()
    );
    assert!(f.vault.assigned(&owner("agent-a")).unwrap().is_empty());
    let wrong_realm = ComputerOwner::Identity {
        realm: "other".into(),
        namespace: "team".into(),
        identity: "agent-b".into(),
    };
    assert!(
        f.vault
            .assign(
                &[one.id.clone(), two.id.clone()],
                &[owner("agent-a"), wrong_realm]
            )
            .is_err()
    );
    assert!(f.vault.assigned(&owner("agent-a")).unwrap().is_empty());
    f.vault
        .assign(&[one.id.clone(), two.id.clone()], &[owner("agent-a")])
        .unwrap();
    assert_eq!(f.vault.assigned(&owner("agent-a")).unwrap().len(), 2);
    assert!(
        f.vault
            .unassign(&[one.id, "missing".into()], &[owner("agent-a")])
            .is_err()
    );
    assert_eq!(f.vault.assigned(&owner("agent-a")).unwrap().len(), 2);
}

#[test]
fn durable_identity_namespace_realm_and_exact_origin_are_required() {
    let f = Fixture::new();
    let saved = f.vault.save(draft()).unwrap();
    f.vault
        .assign(std::slice::from_ref(&saved.id), &[owner("agent-a")])
        .unwrap();
    for other in [
        owner("agent-b"),
        ComputerOwner::Identity {
            realm: "work".into(),
            namespace: "other".into(),
            identity: "agent-a".into(),
        },
        ComputerOwner::Identity {
            realm: "other".into(),
            namespace: "team".into(),
            identity: "agent-a".into(),
        },
        ComputerOwner::Session {
            realm: "work".into(),
            session_id: "agent-a".into(),
        },
    ] {
        assert!(
            f.vault
                .resolve(&saved.id, &other, "https://example.com")
                .is_err()
        );
    }
    assert!(
        f.vault
            .assign(
                std::slice::from_ref(&saved.id),
                &[ComputerOwner::Session {
                    realm: "work".into(),
                    session_id: "agent-a".into()
                }]
            )
            .is_err()
    );
    for origin in [
        "https://sub.example.com",
        "https://example.com:8443",
        "https://example.com.evil.test",
        "http://example.com",
    ] {
        assert!(
            f.vault
                .resolve(&saved.id, &owner("agent-a"), origin)
                .is_err()
        );
    }
}

#[test]
fn realm_cannot_change_and_revocation_is_visible_to_existing_handles() {
    let f = Fixture::new();
    let saved = f.vault.save(draft()).unwrap();
    f.vault
        .assign(std::slice::from_ref(&saved.id), &[owner("agent-a")])
        .unwrap();
    let second = CredentialVault::open(&f.path, &f.key).unwrap();
    let mut changed = draft();
    changed.realm = "other".into();
    assert!(f.vault.update(&saved.id, changed).is_err());
    f.vault
        .unassign(std::slice::from_ref(&saved.id), &[owner("agent-a")])
        .unwrap();
    assert!(
        second
            .resolve(&saved.id, &owner("agent-a"), "https://example.com")
            .is_err()
    );
    f.vault.delete(&saved.id).unwrap();
    assert!(second.list().unwrap().is_empty());
}

#[test]
fn missing_wrong_and_tampered_keys_or_ciphertexts_fail_closed() {
    let f = Fixture::new();
    f.vault.save(draft()).unwrap();
    assert!(CredentialVault::open(&f.path, &f.dir.path().join("missing-key")).is_err());
    assert!(!f.dir.path().join("missing-key").exists());
    let wrong = f.dir.path().join("wrong-key");
    CredentialVault::init_key(&wrong).unwrap();
    assert!(CredentialVault::open(&f.path, &wrong).is_err());
    let mut bytes = fs::read(&f.path).unwrap();
    *bytes.last_mut().unwrap() ^= 1;
    fs::write(&f.path, bytes).unwrap();
    assert!(CredentialVault::open(&f.path, &f.key).is_err());
    assert!(f.vault.save(draft()).is_err());
}

#[test]
fn canonical_origins_reject_components_and_insecure_nonliteral_hosts() {
    assert_eq!(
        canonical_origin("https://EXAMPLE.com:443/").unwrap(),
        "https://example.com"
    );
    assert_eq!(
        canonical_origin("http://127.0.0.1:8000").unwrap(),
        "http://127.0.0.1:8000"
    );
    assert_eq!(
        canonical_origin("http://[::1]:8000").unwrap(),
        "http://[::1]:8000"
    );
    for bad in [
        "http://localhost",
        "http://example.com",
        "http://192.168.0.1",
        "https://user:pass@example.com",
        "https://@example.com",
        "https://example.com/path",
        "https://example.com?",
        "https://example.com#",
        "https://*.example.com",
        "file:///tmp/site",
        " https://example.com",
        "https://example.com\\evil",
    ] {
        assert!(canonical_origin(bad).is_err(), "accepted {bad}");
    }
}

#[cfg(unix)]
#[test]
fn key_and_vault_require_private_regular_files_outside_the_vault_directory() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let f = Fixture::new();
    f.vault.save(draft()).unwrap();
    assert_eq!(
        fs::metadata(&f.key).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(
        fs::metadata(&f.path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert!(CredentialVault::init_key(&f.key).is_err());
    fs::set_permissions(&f.key, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(CredentialVault::open(&f.path, &f.key).is_err());
    fs::set_permissions(&f.key, fs::Permissions::from_mode(0o600)).unwrap();
    let alias = f.dir.path().join("key-alias");
    symlink(&f.key, &alias).unwrap();
    assert!(CredentialVault::open(&f.path, &alias).is_err());
    let inner = f.path.parent().unwrap().join("inside-key");
    CredentialVault::init_key(&inner).unwrap();
    assert!(CredentialVault::open(&f.path, &inner).is_err());
    let target = f.path.with_file_name("target");
    fs::rename(&f.path, &target).unwrap();
    symlink(&target, &f.path).unwrap();
    assert!(CredentialVault::open(&f.path, &f.key).is_err());
}

#[test]
fn independent_handles_do_not_lose_updates() {
    let f = Fixture::new();
    let second = CredentialVault::open(&f.path, &f.key).unwrap();
    f.vault.save(draft()).unwrap();
    second.save(draft()).unwrap();
    assert_eq!(f.vault.list().unwrap().len(), 2);
}

#[test]
fn save_assigned_is_atomic_when_an_owner_is_invalid() {
    let f = Fixture::new();
    let invalid = ComputerOwner::Identity {
        realm: "other".into(),
        namespace: "team".into(),
        identity: "agent-b".into(),
    };
    assert!(
        f.vault
            .save_assigned(draft(), &[owner("agent-a"), invalid])
            .is_err()
    );
    assert!(f.vault.list().unwrap().is_empty());
    let saved = f
        .vault
        .save_assigned(draft(), &[owner("agent-a"), owner("agent-b")])
        .unwrap();
    assert_eq!(saved.assignments.len(), 2);
    assert_eq!(f.vault.assigned(&owner("agent-a")).unwrap().len(), 1);
}

#[test]
fn origin_normalization_cannot_hide_paths_wildcards_or_nonliteral_loopback() {
    for bad in [
        "https://example.com/a/..",
        "https://example.com/%2e",
        "https://%2A.example.com",
        "http://2130706433",
        "http://127.1",
        "http://%31%32%37.0.0.1",
    ] {
        assert!(canonical_origin(bad).is_err(), "accepted {bad}");
    }
}

#[test]
fn concurrent_independent_handles_preserve_every_committed_record() {
    let f = Fixture::new();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(8));
    let mut threads = Vec::new();
    for _ in 0..8 {
        let vault = CredentialVault::open(&f.path, &f.key).unwrap();
        let barrier = barrier.clone();
        threads.push(std::thread::spawn(move || {
            barrier.wait();
            vault.save(draft()).unwrap();
        }));
    }
    for thread in threads {
        thread.join().unwrap();
    }
    assert_eq!(f.vault.list().unwrap().len(), 8);
}

#[cfg(unix)]
#[test]
fn special_or_shared_key_files_fail_without_waiting_for_a_pipe_writer() {
    use std::os::unix::ffi::OsStrExt;
    let f = Fixture::new();
    let fifo = f.dir.path().join("key-pipe");
    let encoded = std::ffi::CString::new(fifo.as_os_str().as_bytes()).unwrap();
    // SAFETY: encoded is a valid terminated pathname and remains live for the call.
    assert_eq!(unsafe { libc::mkfifo(encoded.as_ptr(), 0o600) }, 0);
    let path = f.path.clone();
    let (sender, receiver) = std::sync::mpsc::channel();
    let thread = std::thread::spawn(move || {
        sender
            .send(CredentialVault::open(&path, &fifo).map(|_| ()))
            .unwrap();
    });
    assert!(
        receiver
            .recv_timeout(std::time::Duration::from_secs(2))
            .expect("opening a nonregular credential file must not block")
            .is_err()
    );
    thread.join().unwrap();
    let linked = f.dir.path().join("key-hardlink");
    fs::hard_link(&f.key, &linked).unwrap();
    assert!(CredentialVault::open(&f.path, &linked).is_err());
    assert!(CredentialVault::open(&f.path, &f.key).is_err());
}
