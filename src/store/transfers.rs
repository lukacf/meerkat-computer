use super::*;
use crate::domain::FileMetadata;
use crate::domain::transfers::{MAX_FILES_PER_COMPUTER, file_id, filename};
use std::io::Read;

impl Store {
    pub(crate) fn validate_files(&self) -> Result<(), BrokerError> {
        for (id, file) in &self.state.files {
            file_id(id)?;
            filename(&file.metadata.filename)?;
            let computer = self
                .state
                .computers
                .get(&file.computer_id)
                .ok_or_else(|| BrokerError::storage("file has no owning computer"))?;
            if id != &file.metadata.file_id
                || file.ownership_id != computer.ownership_id
                || file.metadata.size_bytes > computer.profile.transfers.max_file_bytes
                || file.metadata.sha256.len() != 64
                || !file
                    .metadata
                    .sha256
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            {
                return Err(BrokerError::storage("invalid persisted file metadata"));
            }
        }
        Ok(())
    }
    fn file_directory(&self, computer_id: &str) -> Result<PathBuf, BrokerError> {
        if uuid::Uuid::parse_str(computer_id).is_err() {
            return Err(BrokerError::storage("invalid computer file directory"));
        }
        let path = self.path.join("transfers").join(computer_id);
        private_dir(&path)?;
        Ok(path)
    }
    fn file_path(&self, file: &FileRecord) -> Result<PathBuf, BrokerError> {
        file_id(&file.metadata.file_id)?;
        Ok(self
            .file_directory(&file.computer_id)?
            .join(format!("{}.blob", file.metadata.file_id)))
    }
    pub(crate) fn recover_file_storage(&self) -> Result<(), BrokerError> {
        // Only this broker's generated blob names and atomic temporary names
        // are eligible for orphan cleanup, after the broker state lock is held.
        for directory in
            std::fs::read_dir(self.path.join("transfers")).map_err(BrokerError::storage)?
        {
            let directory = directory.map_err(BrokerError::storage)?;
            let computer_id = directory
                .file_name()
                .into_string()
                .map_err(|_| BrokerError::storage("invalid file storage name"))?;
            if uuid::Uuid::parse_str(&computer_id).is_err()
                || !directory
                    .file_type()
                    .map_err(BrokerError::storage)?
                    .is_dir()
            {
                return Err(BrokerError::storage(
                    "unexpected entry in private file storage",
                ));
            }
            let path = self.file_directory(&computer_id)?;
            for entry in std::fs::read_dir(&path).map_err(BrokerError::storage)? {
                let entry = entry.map_err(BrokerError::storage)?;
                let name = entry
                    .file_name()
                    .into_string()
                    .map_err(|_| BrokerError::storage("invalid blob name"))?;
                let is_temporary = name
                    .strip_prefix(".pending-")
                    .is_some_and(|id| file_id(id).is_ok());
                let id = name.strip_suffix(".blob").filter(|id| file_id(id).is_ok());
                if (!is_temporary && id.is_none())
                    || !entry.file_type().map_err(BrokerError::storage)?.is_file()
                {
                    return Err(BrokerError::storage(
                        "unexpected entry in private blob directory",
                    ));
                }
                if is_temporary
                    || !id.is_some_and(|id| {
                        self.state
                            .files
                            .get(id)
                            .is_some_and(|f| f.computer_id == computer_id)
                    })
                {
                    std::fs::remove_file(entry.path()).map_err(BrokerError::storage)?;
                }
            }
            sync_directory(&path)?;
        }
        Ok(())
    }
    pub(crate) fn files_for(&self, record: &ComputerRecord) -> Vec<FileMetadata> {
        self.state
            .files
            .values()
            .filter(|file| {
                file.computer_id == record.computer_id && file.ownership_id == record.ownership_id
            })
            .map(|file| file.metadata.clone())
            .collect()
    }
    pub(crate) fn file_for(
        &self,
        record: &ComputerRecord,
        id: &str,
    ) -> Result<&FileRecord, BrokerError> {
        self.state
            .files
            .get(id)
            .filter(|file| {
                file.computer_id == record.computer_id && file.ownership_id == record.ownership_id
            })
            .ok_or_else(|| BrokerError {
                code: crate::domain::ErrorCode::NotFound,
                message: "file not found".into(),
                effect: crate::domain::Effect::None,
                metrics: None,
                backend_failure: None,
            })
    }
    pub(crate) fn available_file_bytes(&self, record: &ComputerRecord) -> Result<u64, BrokerError> {
        let entries = self.files_for(record);
        let stored = entries
            .iter()
            .try_fold(0u64, |sum, file| sum.checked_add(file.size_bytes))
            .ok_or_else(|| BrokerError::storage("file quota overflow"))?;
        let mut actual_bytes = 0u64;
        let mut actual_count = 0usize;
        for entry in std::fs::read_dir(self.file_directory(&record.computer_id)?)
            .map_err(BrokerError::storage)?
        {
            let entry = entry.map_err(BrokerError::storage)?;
            let metadata = entry.metadata().map_err(BrokerError::storage)?;
            if !entry.file_type().map_err(BrokerError::storage)?.is_file() {
                return Err(BrokerError::storage(
                    "file storage contains nonregular data",
                ));
            }
            actual_bytes = actual_bytes
                .checked_add(metadata.len())
                .ok_or_else(|| BrokerError::storage("file quota overflow"))?;
            actual_count += 1;
        }
        if entries.len().max(actual_count) >= MAX_FILES_PER_COMPUTER {
            return Err(BrokerError::conflict(
                "computer file count quota is full; delete staged files",
            ));
        }
        Ok(record
            .profile
            .transfers
            .max_stored_bytes
            .saturating_sub(stored.max(actual_bytes))
            .min(record.profile.transfers.max_file_bytes))
    }
    pub(crate) fn put_file(
        &mut self,
        record: &ComputerRecord,
        metadata: FileMetadata,
        bytes: &[u8],
    ) -> Result<(), BrokerError> {
        file_id(&metadata.file_id)?;
        filename(&metadata.filename)?;
        if bytes.len() as u64 != metadata.size_bytes
            || crate::domain::digest(bytes) != metadata.sha256
            || metadata.size_bytes > self.available_file_bytes(record)?
        {
            return Err(BrokerError::invalid(
                "file exceeds the computer file size or storage quota",
            ));
        }
        if self.state.files.contains_key(&metadata.file_id) {
            return Err(BrokerError::conflict("file identifier is already present"));
        }
        let file = FileRecord {
            computer_id: record.computer_id.clone(),
            ownership_id: record.ownership_id.clone(),
            metadata,
        };
        let path = self.file_path(&file)?;
        if path.try_exists().map_err(BrokerError::storage)? {
            return Err(BrokerError::storage("generated blob path already exists"));
        }
        atomic_write(&path, bytes).map_err(|_| {
            BrokerError::unknown(
                "file publication may have completed; inspect staged files before continuing",
            )
        })?;
        let mut next = self.state.clone();
        next.files.insert(file.metadata.file_id.clone(), file);
        // If committing is uncertain, retain the blob. Its physical usage is
        // counted immediately, and startup removes it only if no record owns it.
        self.commit(next).map_err(|_| {
            BrokerError::unknown(
                "file publication could not be committed; inspect staged files before continuing",
            )
        })
    }
    pub(crate) fn read_file(&self, file: &FileRecord) -> Result<Vec<u8>, BrokerError> {
        let mut options = private_options();
        options.read(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
        }
        let input = options
            .open(self.file_path(file)?)
            .map_err(BrokerError::storage)?;
        let metadata = input.metadata().map_err(BrokerError::storage)?;
        if !metadata.is_file()
            || metadata.len() != file.metadata.size_bytes
            || metadata.len() > crate::config::MAX_TRANSFER_BYTES
        {
            return Err(BrokerError::storage(
                "stored file size differs from its metadata",
            ));
        }
        let mut bytes = Vec::new();
        input
            .take(file.metadata.size_bytes + 1)
            .read_to_end(&mut bytes)
            .map_err(BrokerError::storage)?;
        if bytes.len() as u64 != file.metadata.size_bytes
            || crate::domain::digest(&bytes) != file.metadata.sha256
        {
            return Err(BrokerError::storage("stored file integrity check failed"));
        }
        Ok(bytes)
    }
    pub(crate) fn delete_file(
        &mut self,
        record: &ComputerRecord,
        id: &str,
    ) -> Result<(), BrokerError> {
        let entry = self.file_for(record, id)?.clone();
        let path = self.file_path(&entry)?;
        match std::fs::remove_file(&path) {
            Ok(()) => (),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(error) => return Err(BrokerError::storage(error)),
        }
        sync_directory(path.parent().unwrap()).map_err(|_| {
            BrokerError::unknown(
                "file removal may have completed; inspect staged files before continuing",
            )
        })?;
        let mut next = self.state.clone();
        next.files.remove(id);
        self.commit(next).map_err(|_| {
            BrokerError::unknown(
                "file removal could not be committed; inspect staged files before continuing",
            )
        })
    }
}
