//! Full execution-state fork effects. Lifecycle authority stays in the broker.
use super::*;

pub(super) const FORK_POOL_KEY: &str = "user.meerkat-computer.fork-pool";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackendCheckpoint {
    pub source: BackendComputer,
    pub checkpoint_id: String,
    pub snapshot_name: String,
}

pub(super) fn unsupported() -> BackendError {
    BackendError::Unsupported {
        capability: "full execution-state computer fork".into(),
    }
}

pub(super) fn branch_project(computer_id: &str) -> Result<String, BackendError> {
    instance_name(computer_id)?;
    Ok(format!("mcf-{computer_id}"))
}

pub(super) fn matches_branch_project(
    pool: &str,
    project: &str,
    computer_id: &str,
) -> Result<bool, BackendError> {
    Ok(project == branch_project(computer_id)? || project == format!("{pool}-fork-{computer_id}"))
}

fn validate_fork_socket_path(project: &str, instance: &str) -> Result<(), BackendError> {
    if format!("/run/incus/{project}_{instance}/qemu.monitor").len() >= 108 {
        return Err(BackendError::Unsupported {
            capability: "fork project and instance exceed the QEMU Unix socket path limit".into(),
        });
    }
    Ok(())
}

fn snapshot_name(checkpoint_id: &str) -> Result<String, BackendError> {
    instance_name(checkpoint_id)?;
    Ok(format!("fork-{checkpoint_id}"))
}

fn validate_checkpoint(checkpoint: &BackendCheckpoint, value: &Value) -> Result<(), BackendError> {
    let expected = snapshot_name(&checkpoint.checkpoint_id)?;
    if checkpoint.snapshot_name != expected
        || value["name"] != expected
        || value["stateful"] != true
        || value["config"][MANAGED_KEY] != "1"
        || value["config"][COMPUTER_KEY] != checkpoint.source.computer_id
        || value["config"][OWNER_KEY] != checkpoint.source.ownership_id
        || value["config"]["volatile.uuid"] != checkpoint.source.instance_id
    {
        return Err(BackendError::InvalidBackendState(
            "checkpoint memory state or exact source differs".into(),
        ));
    }
    Ok(())
}

fn validate_control_receipt(request: &Value, value: &Value) -> Result<(), BackendError> {
    let operation = request["operation"].as_str().unwrap_or_default();
    let valid = value["protocol_version"] == 1
        && match operation {
            "fork_capabilities" => value["full_state_fork"] == true,
            "fork_prepare" | "fork_resume" => {
                value["operation"] == operation
                    && value["checkpoint_id"] == request["checkpoint_id"]
                    && value[if operation == "fork_prepare" {
                        "prepared"
                    } else {
                        "resumed"
                    }] == true
            }
            "fork_rebind" => {
                value["operation"] == operation
                    && value["rebound"] == true
                    && ["checkpoint_id", "fork_id", "computer_id", "incarnation_id"]
                        .iter()
                        .all(|key| value[*key] == request[*key])
            }
            _ => false,
        };
    if valid {
        Ok(())
    } else {
        Err(BackendError::InvalidBackendState(
            "private fork acknowledgement was invalid; do not replay".into(),
        ))
    }
}

impl IncusBackend {
    async fn fork_control(
        &self,
        computer: &BackendComputer,
        operation: &str,
        request: Value,
    ) -> Result<Value, BackendError> {
        self.inspect_handle(computer).await?;
        let input = serde_json::to_vec(&request).map_err(|_| {
            BackendError::InvalidConfiguration("cannot encode private fork control".into())
        })?;
        let output = self
            .command_in(
                self.project_for(computer)?,
                operation,
                &[
                    "exec".into(),
                    computer.instance_name.clone(),
                    "--".into(),
                    "python3".into(),
                    "-c".into(),
                    include_str!("../../python/meerkat_computer/private_client.py").into(),
                    "/var/lib/meerkat-computer/state/secure-input.sock".into(),
                    "fork_control".into(),
                ],
                Some(input),
            )
            .await?;
        if output.len() > 4096 {
            return Err(BackendError::InvalidBackendState(
                "private fork acknowledgement exceeded limit".into(),
            ));
        }
        let value: Value = serde_json::from_slice(&output).map_err(|_| {
            BackendError::InvalidBackendState(
                "private fork acknowledgement was invalid; do not replay".into(),
            )
        })?;
        validate_control_receipt(&request, &value)?;
        Ok(value)
    }

    async fn inspect_checkpoint(
        &self,
        checkpoint: &BackendCheckpoint,
    ) -> Result<Value, BackendError> {
        self.inspect_handle(&checkpoint.source).await?;
        if snapshot_name(&checkpoint.checkpoint_id)? != checkpoint.snapshot_name {
            return Err(BackendError::InvalidConfiguration(
                "checkpoint name differs from durable intent".into(),
            ));
        }
        let path = format!(
            "/1.0/instances/{}/snapshots/{}?project={}",
            checkpoint.source.instance_name,
            checkpoint.snapshot_name,
            self.project_for(&checkpoint.source)?
        );
        let output = self
            .simple("checkpoint inspection", &["query", &path])
            .await?;
        let value: Value = serde_json::from_slice(&output)
            .map_err(|_| BackendError::InvalidBackendState("invalid checkpoint response".into()))?;
        let value = value.get("metadata").cloned().unwrap_or(value);
        validate_checkpoint(checkpoint, &value)?;
        Ok(value)
    }

    pub(super) async fn fork_capability(
        &self,
        spec: &ComputerSpec,
        computer: &BackendComputer,
    ) -> Result<(), BackendError> {
        if spec.profile.kind != ComputerKind::VirtualMachine {
            return Err(unsupported());
        }
        let actual = self.inspect_handle(computer).await?;
        actual.assert_profile(&spec.profile)?;
        if actual.kind != "virtual-machine"
            || actual.status != "Running"
            || actual
                .effective_config()
                .get("migration.stateful")
                .map(String::as_str)
                != Some("true")
        {
            return Err(BackendError::Unsupported {
                capability:
                    "full-state fork requires a running VM created with migration.stateful=true"
                        .into(),
            });
        }
        self.inspect_network_boundary(&actual).await?;
        self.fork_control(
            computer,
            "fork capability inspection",
            serde_json::json!({"operation":"fork_capabilities"}),
        )
        .await?;
        Ok(())
    }

    pub(super) async fn resume_source(
        &self,
        computer: &BackendComputer,
        checkpoint_id: &str,
    ) -> Result<(), BackendError> {
        snapshot_name(checkpoint_id)?;
        let _guard = self.effects.lock().await;
        self.fork_control(
            computer,
            "fork resume",
            serde_json::json!({"operation":"fork_resume","checkpoint_id":checkpoint_id}),
        )
        .await?;
        Ok(())
    }

    pub(super) async fn create_checkpoint(
        &self,
        computer: &BackendComputer,
        checkpoint_id: &str,
    ) -> Result<BackendCheckpoint, BackendError> {
        let checkpoint = BackendCheckpoint {
            source: computer.clone(),
            checkpoint_id: checkpoint_id.into(),
            snapshot_name: snapshot_name(checkpoint_id)?,
        };
        let _guard = self.effects.lock().await;
        let actual = self.inspect_handle(computer).await?;
        if actual.kind != "virtual-machine"
            || actual.status != "Running"
            || actual
                .effective_config()
                .get("migration.stateful")
                .map(String::as_str)
                != Some("true")
        {
            return Err(unsupported());
        }
        self.fork_control(
            computer,
            "fork prepare",
            serde_json::json!({"operation":"fork_prepare","checkpoint_id":checkpoint_id}),
        )
        .await?;
        // The worker's closed gate is part of the RAM checkpoint. Any failed or
        // lost acknowledgement stops this sequence without replaying an effect.
        self.simple_in(
            self.project_for(computer)?,
            "checkpoint create",
            &[
                "snapshot",
                "create",
                &computer.instance_name,
                &checkpoint.snapshot_name,
                "--stateful",
            ],
        )
        .await?;
        self.inspect_checkpoint(&checkpoint).await?;
        self.fork_control(
            computer,
            "fork resume",
            serde_json::json!({"operation":"fork_resume","checkpoint_id":checkpoint_id}),
        )
        .await?;
        Ok(checkpoint)
    }

    pub(super) async fn release_checkpoint(
        &self,
        checkpoint: &BackendCheckpoint,
    ) -> Result<(), BackendError> {
        let _guard = self.effects.lock().await;
        self.inspect_checkpoint(checkpoint).await?;
        self.simple_in(
            self.project_for(&checkpoint.source)?,
            "checkpoint release",
            &[
                "snapshot",
                "delete",
                &checkpoint.source.instance_name,
                &checkpoint.snapshot_name,
            ],
        )
        .await?;
        Ok(())
    }

    pub(super) async fn inspect_branch_project(
        &self,
        handle: &BackendComputer,
    ) -> Result<(), BackendError> {
        let project = self.project_for(handle)?;
        let output = self
            .simple(
                "fork capability inspection",
                &["query", &format!("/1.0/projects/{project}")],
            )
            .await?;
        let value: Value = serde_json::from_slice(&output).map_err(|_| {
            BackendError::InvalidBackendState("invalid fork project response".into())
        })?;
        let value = value.get("metadata").unwrap_or(&value);
        if value["name"] != project
            || value["config"][FORK_POOL_KEY] != self.config.incus_project
            || value["config"][COMPUTER_KEY] != handle.computer_id
            || value["config"][OWNER_KEY] != handle.ownership_id
        {
            return Err(BackendError::OwnershipMismatch {
                instance: handle.instance_name.clone(),
            });
        }
        Ok(())
    }
}

const FORK_CHECKPOINT_KEY: &str = "user.meerkat-computer.fork-checkpoint";
const FORK_SOURCE_KEY: &str = "user.meerkat-computer.fork-source";
const FORK_READY_KEY: &str = "user.meerkat-computer.fork-ready";

fn bridge_name(computer_id: &str) -> Result<String, BackendError> {
    instance_name(computer_id)?;
    let compact: String = computer_id.chars().filter(|c| *c != '-').collect();
    let prefix = compact.get(..12).ok_or_else(|| {
        BackendError::InvalidConfiguration(
            "computer ID has fewer than twelve hexadecimal digits".into(),
        )
    })?;
    Ok(format!("mcf{prefix}"))
}

fn branch_address(network: &Value) -> Result<(String, String), BackendError> {
    let address = network["config"]["ipv4.address"]
        .as_str()
        .unwrap_or_default();
    let Some((gateway, "24")) = address.split_once('/') else {
        return Err(BackendError::InvalidBackendState(
            "fork bridge requires a private /24 network".into(),
        ));
    };
    let ip: Ipv4Addr = gateway.parse().map_err(|_| {
        BackendError::InvalidBackendState("fork bridge has invalid IPv4 address".into())
    })?;
    if !ip.is_private() || ip.octets()[3] != 1 {
        return Err(BackendError::InvalidBackendState(
            "fork bridge has invalid gateway".into(),
        ));
    }
    let mut octets = ip.octets();
    octets[3] = 2;
    Ok((gateway.into(), Ipv4Addr::from(octets).to_string()))
}

fn bridge_acl(name: &str, gateway: &str, ownership_id: &str) -> Value {
    serde_json::json!({"name":format!("{name}-isolation"),"config":{HOST_OWNER_KEY:ownership_id},
        "ingress":[{"action":"allow","state":"enabled","source":format!("{gateway}/32")}],
        "egress":[{"action":"reject","state":"enabled","destination":DENIED_EGRESS}]})
}

impl IncusBackend {
    async fn query_json(&self, operation: &str, path: &str) -> Result<Value, BackendError> {
        let output = self.simple(operation, &["query", path]).await?;
        let value: Value = serde_json::from_slice(&output)
            .map_err(|_| BackendError::InvalidBackendState("invalid Incus fork response".into()))?;
        Ok(value.get("metadata").cloned().unwrap_or(value))
    }

    async fn write_json(
        &self,
        operation: &str,
        method: &str,
        path: &str,
        body: &Value,
    ) -> Result<(), BackendError> {
        // Incus 6 query accepts literal JSON only. This helper is restricted to
        // infrastructure metadata; worker credentials use the private stdin pipe.
        self.command(
            operation,
            &[
                "query".into(),
                "--request".into(),
                method.into(),
                path.into(),
                "--data".into(),
                serde_json::to_string(body).map_err(|_| {
                    BackendError::InvalidConfiguration("cannot encode fork infrastructure".into())
                })?,
                "--wait".into(),
            ],
            None,
        )
        .await?;
        Ok(())
    }

    async fn project_exists(&self, project: &str) -> Result<bool, BackendError> {
        let value = self
            .query_json("fork capability inspection", "/1.0/projects?recursion=1")
            .await?;
        let projects = value
            .as_array()
            .ok_or_else(|| BackendError::InvalidBackendState("invalid project inventory".into()))?;
        Ok(projects.iter().any(|value| value["name"] == project))
    }

    async fn existing_branch_project(
        &self,
        computer_id: &str,
    ) -> Result<Option<String>, BackendError> {
        let compact = branch_project(computer_id)?;
        let legacy = format!("{}-fork-{computer_id}", self.config.incus_project);
        match (
            self.project_exists(&compact).await?,
            self.project_exists(&legacy).await?,
        ) {
            (true, false) => Ok(Some(compact)),
            (false, true) => Ok(Some(legacy)),
            (false, false) => Ok(None),
            (true, true) => Err(BackendError::InvalidBackendState(
                "multiple fork projects match one computer; explicit inspection required".into(),
            )),
        }
    }

    pub(super) async fn inspect_fork_child(
        &self,
        checkpoint: &BackendCheckpoint,
        spec: &ComputerSpec,
    ) -> Result<Option<BackendComputer>, BackendError> {
        let Some(project) = self.existing_branch_project(&spec.computer_id).await? else {
            return Ok(None);
        };
        let provisional = BackendComputer {
            instance_name: checkpoint.source.instance_name.clone(),
            incus_project: Some(project.clone()),
            worker_url: String::new(),
            computer_id: spec.computer_id.clone(),
            ownership_id: spec.ownership_id.clone(),
            instance_id: String::new(),
        };
        self.inspect_branch_project(&provisional).await?;
        let Some(actual) = self
            .inventory_in(&project)
            .await?
            .into_iter()
            .find(|value| value.name == checkpoint.source.instance_name)
        else {
            return Ok(None);
        };
        actual.assert_owner_named(
            &spec.computer_id,
            &spec.ownership_id,
            None,
            &checkpoint.source.instance_name,
        )?;
        actual.assert_profile(&spec.profile)?;
        if actual.config.get(FORK_CHECKPOINT_KEY) != Some(&checkpoint.checkpoint_id)
            || actual.config.get(FORK_SOURCE_KEY) != Some(&checkpoint.source.instance_id)
        {
            return Err(BackendError::OwnershipMismatch {
                instance: actual.name.clone(),
            });
        }
        let mut handle = actual.handle(&spec.computer_id, &spec.ownership_id);
        handle.incus_project = Some(project);
        Ok(Some(handle))
    }

    pub(super) fn assert_branch_ready(
        &self,
        computer: &BackendComputer,
        instance: &Instance,
    ) -> Result<(), BackendError> {
        if computer.incus_project.is_some()
            && (!instance.config.contains_key(FORK_READY_KEY)
                || instance.config.get(FORK_READY_KEY) != instance.config.get(FORK_CHECKPOINT_KEY))
        {
            return Err(BackendError::InvalidBackendState(
                "fork has not completed private rebind and isolated network activation".into(),
            ));
        }
        Ok(())
    }

    pub(super) async fn delete_branch_resources(
        &self,
        computer: &BackendComputer,
    ) -> Result<(), BackendError> {
        if computer.incus_project.is_none() {
            return Ok(());
        }
        self.inspect_branch_project(computer).await?;
        let project = self.project_for(computer)?;
        if !self.inventory_in(project).await?.is_empty() {
            return Err(BackendError::InvalidBackendState(
                "fork project contains another instance; refusing resource cleanup".into(),
            ));
        }
        let bridge = bridge_name(&computer.computer_id)?;
        let path = format!("/1.0/networks/{bridge}?project=default");
        let network = self.query_json("fork network", &path).await?;
        let acl_path = format!("/1.0/network-acls/{bridge}-isolation?project=default");
        let acl = self.query_json("fork network", &acl_path).await?;
        if network["config"][HOST_OWNER_KEY] != computer.ownership_id
            || network["config"][COMPUTER_KEY] != computer.computer_id
            || network["config"][FORK_POOL_KEY] != self.config.incus_project
            || acl["config"][HOST_OWNER_KEY] != computer.ownership_id
        {
            return Err(BackendError::OwnershipMismatch {
                instance: computer.instance_name.clone(),
            });
        }
        let profiles = self
            .query_json(
                "fork network",
                &format!("/1.0/profiles?project={project}&recursion=1"),
            )
            .await?;
        if !profiles
            .as_array()
            .is_some_and(|profiles| profiles.len() == 1 && profiles[0]["name"] == "default")
        {
            return Err(BackendError::InvalidBackendState(
                "fork project contains unexpected profiles; refusing resource cleanup".into(),
            ));
        }
        self.write_json(
            "fork network",
            "PUT",
            &format!("/1.0/profiles/default?project={project}"),
            &serde_json::json!({"config":{},"devices":{}}),
        )
        .await?;
        let network = self.query_json("fork network", &path).await?;
        if !network["used_by"].as_array().is_some_and(Vec::is_empty) {
            return Err(BackendError::InvalidBackendState(
                "fork bridge is still used; refusing resource cleanup".into(),
            ));
        }
        self.write_json("fork network", "DELETE", &path, &serde_json::json!({}))
            .await?;
        self.write_json("fork network", "DELETE", &acl_path, &serde_json::json!({}))
            .await?;
        self.write_json(
            "fork copy",
            "DELETE",
            &format!("/1.0/projects/{project}"),
            &serde_json::json!({}),
        )
        .await?;
        Ok(())
    }

    async fn copy_fork_snapshot(
        &self,
        checkpoint: &BackendCheckpoint,
        spec: &ComputerSpec,
        project: &str,
        nic: &HashMap<String, String>,
    ) -> Result<(), BackendError> {
        let mac = nic.get("hwaddr").ok_or_else(|| {
            BackendError::InvalidBackendState("fork NIC has no preserved hardware address".into())
        })?;
        // A stateful snapshot is copied under its original internal instance
        // name. No --stateless flag and no disk-only recovery are permitted.
        let mut args = vec![
            "copy".into(),
            format!(
                "{}/{}",
                checkpoint.source.instance_name, checkpoint.snapshot_name
            ),
            checkpoint.source.instance_name.clone(),
            "--target-project".into(),
            project.into(),
            "--profile".into(),
            "default".into(),
            "--config".into(),
            format!("{COMPUTER_KEY}={}", spec.computer_id),
            "--config".into(),
            format!("{OWNER_KEY}={}", spec.ownership_id),
            "--config".into(),
            format!("{FORK_POOL_KEY}={}", self.config.incus_project),
            "--config".into(),
            format!("{FORK_CHECKPOINT_KEY}={}", checkpoint.checkpoint_id),
            "--config".into(),
            format!("{FORK_SOURCE_KEY}={}", checkpoint.source.instance_id),
            "--config".into(),
            format!("volatile.eth0.hwaddr={mac}"),
        ];
        // Incus copy creates a local device for overrides, which replaces the
        // entire inherited profile device. Carry its type and isolation too.
        let mut properties: Vec<_> = nic.iter().collect();
        properties.sort_unstable_by_key(|(key, _)| *key);
        for (key, value) in properties {
            args.extend(["--device".into(), format!("eth0,{key}={value}")]);
        }
        self.command_in(
            self.project_for(&checkpoint.source)?,
            "fork copy",
            &args,
            None,
        )
        .await?;
        Ok(())
    }

    pub(super) async fn restore_fork(
        &self,
        checkpoint: &BackendCheckpoint,
        spec: &ComputerSpec,
    ) -> Result<BackendComputer, BackendError> {
        let work = async {
            if spec.profile.kind != ComputerKind::VirtualMachine
                || spec.computer_id == checkpoint.source.computer_id
            {
                return Err(unsupported());
            }
            init_args(spec)?;
            let _guard = self.effects.lock().await;
            self.inspect_checkpoint(checkpoint).await?;
            let source = self.inspect_handle(&checkpoint.source).await?;
            source.assert_profile(&spec.profile)?;
            let project = branch_project(&spec.computer_id)?;
            validate_fork_socket_path(&project, &checkpoint.source.instance_name)?;
            if self
                .existing_branch_project(&spec.computer_id)
                .await?
                .is_some()
            {
                return Err(BackendError::InvalidBackendState(
                    "fork project already exists; inspect the recorded intent, do not replay"
                        .into(),
                ));
            }
            let bridge = bridge_name(&spec.computer_id)?;
            let mac = source
                .expanded_devices
                .get("eth0")
                .and_then(|nic| nic.get("hwaddr"))
                .or_else(|| source.effective_config().get("volatile.eth0.hwaddr"))
                .filter(|value| {
                    value.len() == 17
                        && value
                            .bytes()
                            .all(|byte| byte.is_ascii_hexdigit() || byte == b':')
                })
                .ok_or_else(|| {
                    BackendError::InvalidBackendState(
                        "source has no stable guest NIC address".into(),
                    )
                })?
                .clone();
            self.admit(spec.profile.memory_mib).await?;
            self.write_json("fork copy", "POST", "/1.0/projects", &serde_json::json!({"name":project,"config":{
                "features.images":"false","features.profiles":"true","features.networks":"false",
                FORK_POOL_KEY:self.config.incus_project,COMPUTER_KEY:spec.computer_id,OWNER_KEY:spec.ownership_id}})).await?;
            // No guest uses this bridge until its deny-all quarantine is complete.
            self.write_json("fork network", "POST", "/1.0/networks?project=default", &serde_json::json!({"name":bridge,"type":"bridge","config":{
                HOST_OWNER_KEY:spec.ownership_id,FORK_POOL_KEY:self.config.incus_project,COMPUTER_KEY:spec.computer_id,
                "ipv4.address":"auto","ipv4.nat":"true","ipv6.address":"none",
                "security.acls.default.ingress.action":"reject","security.acls.default.egress.action":"reject"}})).await?;
            let mut network = self
                .query_json(
                    "fork network",
                    &format!("/1.0/networks/{bridge}?project=default"),
                )
                .await?;
            let (gateway, address) = branch_address(&network)?;
            let acl = bridge_acl(&bridge, &gateway, &spec.ownership_id);
            self.write_json(
                "fork network",
                "POST",
                "/1.0/network-acls?project=default",
                &acl,
            )
            .await?;
            network["config"]["security.acls"] = format!("{bridge}-isolation").into();
            self.write_json(
                "fork network",
                "PUT",
                &format!("/1.0/networks/{bridge}?project=default"),
                &serde_json::json!({"config":network["config"]}),
            )
            .await?;
            let mut devices = source.expanded_devices.clone();
            let nic = devices.get_mut("eth0").ok_or_else(|| {
                BackendError::InvalidBackendState("source network device is missing".into())
            })?;
            nic.remove("parent");
            nic.insert("network".into(), bridge.clone());
            nic.insert("hwaddr".into(), mac.clone());
            nic.insert("ipv4.address".into(), address.clone());
            let fork_nic = nic.clone();
            self.write_json(
                "fork copy",
                "PUT",
                &format!("/1.0/profiles/default?project={project}"),
                &serde_json::json!({"config":{},"devices":devices}),
            )
            .await?;
            self.copy_fork_snapshot(checkpoint, spec, &project, &fork_nic)
                .await?;
            let child = self
                .inspect_fork_child(checkpoint, spec)
                .await?
                .ok_or_else(|| {
                    BackendError::InvalidBackendState("copied fork is missing".into())
                })?;
            let copied = self.inspect_handle(&child).await?;
            if copied.status != "Stopped"
                || !copied.stateful
                || child.instance_id == checkpoint.source.instance_id
            {
                return Err(BackendError::InvalidBackendState(
                    "copied fork lacks stopped RAM state or fresh incarnation".into(),
                ));
            }
            self.simple_in(&project, "start", &["start", &child.instance_name])
                .await?;
            loop {
                self.inspect_handle(&child).await?;
                match self
                    .simple_in(
                        &project,
                        "guest agent readiness",
                        &["exec", &child.instance_name, "--", "true"],
                    )
                    .await
                {
                    Ok(_) => break,
                    Err(BackendError::CommandFailed { .. }) => {
                        sleep(Duration::from_millis(250)).await
                    }
                    Err(error) => return Err(error),
                }
            }
            self.fork_control(&child,"fork rebind",serde_json::json!({"operation":"fork_rebind","checkpoint_id":checkpoint.checkpoint_id,
                "fork_id":spec.computer_id,"computer_id":spec.computer_id,"incarnation_id":child.instance_id,"worker_token":spec.worker_token})).await?;
            self.command_in(
                &project,
                "fork network",
                &[
                    "exec".into(),
                    child.instance_name.clone(),
                    "--".into(),
                    "python3".into(),
                    "-c".into(),
                    READDRESS_GUEST.into(),
                ],
                Some(
                    serde_json::to_vec(
                        &serde_json::json!({"mac":mac,"gateway":gateway,"address":address}),
                    )
                    .map_err(|_| {
                        BackendError::InvalidConfiguration(
                            "cannot encode private network binding".into(),
                        )
                    })?,
                ),
            )
            .await?;
            network["config"]["security.acls.default.egress.action"] = "allow".into();
            self.write_json(
                "fork network",
                "PUT",
                &format!("/1.0/networks/{bridge}?project=default"),
                &serde_json::json!({"config":network["config"]}),
            )
            .await?;
            let actual = self.inspect_handle(&child).await?;
            self.inspect_network_boundary(&actual).await?;
            self.simple_in(
                &project,
                "fork rebind",
                &[
                    "config",
                    "set",
                    &child.instance_name,
                    &format!("{FORK_READY_KEY}={}", checkpoint.checkpoint_id),
                ],
            )
            .await?;
            self.ready(child).await
        };
        timeout(Duration::from_secs(self.config.ready_timeout_seconds), work)
            .await
            .map_err(|_| BackendError::CommandTimeout {
                operation: "fork copy".into(),
            })?
    }
}

const READDRESS_GUEST: &str = r#"
import ipaddress, json, os, pathlib, subprocess, sys, tempfile
p = json.load(sys.stdin)
mac = p['mac'].lower()
address = str(ipaddress.IPv4Address(p['address']))
gateway = str(ipaddress.IPv4Address(p['gateway']))
nics = [v.parent.name for v in pathlib.Path('/sys/class/net').glob('*/address') if v.read_text().strip().lower() == mac]
if len(nics) != 1:
    raise SystemExit(2)
nic = nics[0]
def run(*args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
# The first matching networkd file wins over generated netplan DHCP files.
# Bind to the preserved MAC, not a guessed guest interface name. This remains
# authoritative after DHCP renewal, worker restart and a later cold boot.
network_dir = pathlib.Path('/etc/systemd/network')
network_dir.mkdir(parents=True, exist_ok=True)
content = ('[Match]\nMACAddress=' + mac + '\n[Network]\nDHCP=no\nLinkLocalAddressing=no\nIPv6AcceptRA=no\nAddress='
           + address + '/24\nGateway=' + gateway + '\nDNS=' + gateway + '\nDomains=~.\n')
fd, temporary = tempfile.mkstemp(prefix='.meerkat-fork-', dir=network_dir)
try:
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, network_dir / '00-meerkat-computer-fork.network')
    descriptor = os.open(network_dir, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
run('networkctl', 'reload')
run('networkctl', 'reconfigure', nic)
run('ip', 'link', 'set', 'dev', nic, 'up')
run('ip', '-4', 'address', 'flush', 'dev', nic)
run('ip', '-4', 'address', 'replace', address + '/24', 'dev', nic)
run('ip', '-4', 'route', 'replace', 'default', 'via', gateway, 'dev', nic)
run('resolvectl', 'dns', nic, gateway)
run('resolvectl', 'domain', nic, '~.')
"#;

#[cfg(test)]
mod tests {
    use super::*;

    fn handle() -> BackendComputer {
        BackendComputer {
            instance_name: "mc-1234567890abcdef1234567890abcdef".into(),
            incus_project: None,
            worker_url: "http://10.211.0.2:18765".into(),
            computer_id: "1234567890abcdef1234567890abcdef".into(),
            ownership_id: "source-owner".into(),
            instance_id: "source-incarnation".into(),
        }
    }

    #[cfg(unix)]
    fn fixture(fail_snapshot: bool, disk_only: bool) -> (tempfile::TempDir, IncusBackend) {
        let directory = tempfile::tempdir().unwrap();
        let source = handle();
        std::fs::write(directory.path().join("settings.json"), serde_json::to_vec(&serde_json::json!({"source":source,"fail_snapshot":fail_snapshot,"disk_only":disk_only})).unwrap()).unwrap();
        let executable = super::super::tests::write_incus_fixture(
            directory.path(),
            r#"#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path(__file__).parent
settings = json.loads((root / 'settings.json').read_text())
source = settings['source']
args = sys.argv[1:]
assert args.pop(0) == '--force-local'
if args[:1] == ['--project']:
    assert args[1] == 'computers'
    args = args[2:]
operation = args.pop(0)
config = {'user.meerkat-computer.managed':'1', 'user.meerkat-computer.id':source['computer_id'], 'user.meerkat-computer.owner':source['ownership_id'], 'volatile.uuid':source['instance_id'], 'migration.stateful':'true'}
def log(value):
    with (root / 'calls.jsonl').open('a') as stream:
        stream.write(json.dumps(value) + '\n')
if operation == 'list':
    print(json.dumps([{'name':source['instance_name'],'status':'Running','type':'virtual-machine','config':config}]))
elif operation == 'exec':
    request = json.load(sys.stdin)
    assert args[0] == source['instance_name']
    assert args[-1] == 'fork_control'
    log(request['operation'])
    if request['operation'] == 'fork_prepare':
        (root / 'gated').write_text(request['checkpoint_id'])
        result = {'prepared':True}
    elif request['operation'] == 'fork_resume':
        assert (root / 'gated').read_text() == request['checkpoint_id']
        (root / 'gated').unlink()
        result = {'resumed':True}
    else:
        raise AssertionError(request)
    print(json.dumps(dict(result, protocol_version=1, operation=request['operation'], checkpoint_id=request['checkpoint_id'])))
elif operation == 'copy':
    devices = {}
    for index, argument in enumerate(args):
        if argument == '--device':
            name, property_value = args[index + 1].split(',', 1)
            key, value = property_value.split('=', 1)
            devices.setdefault(name, {})[key] = value
    # Incus copy creates a local device from overrides when the source device
    # is inherited; the local device replaces the entire profile device.
    assert devices['eth0'].get('type') == 'nic', 'Invalid device type'
    for key in ['security.ipv4_filtering', 'security.ipv6_filtering', 'security.mac_filtering', 'security.port_isolation']:
        assert devices['eth0'].get(key) == 'true', 'Missing network isolation'
    (root / 'copy-args.json').write_text(json.dumps(sys.argv[1:]))
elif operation == 'snapshot':
    log('snapshot-' + args[0])
    assert args[0] == 'create' and args[-1] == '--stateful'
    assert (root / 'gated').exists()
    if settings['fail_snapshot']:
        sys.exit(9)
elif operation == 'query':
    assert '/snapshots/' in args[0]
    log('checkpoint-inspect')
    print(json.dumps({'name':args[0].split('/snapshots/')[1].split('?')[0], 'stateful':not settings['disk_only'], 'config':config}))
else:
    raise AssertionError(operation)
"#,
        );
        let backend = IncusBackend::new(PoolConfig {
            backend: "incus".into(),
            incus_binary: executable.to_str().unwrap().into(),
            incus_project: "computers".into(),
            max_running: 4,
            min_available_memory_mib: 0,
            worker_port: 18765,
            ready_timeout_seconds: 5,
        })
        .unwrap();
        (directory, backend)
    }

    #[cfg(unix)]
    fn calls(directory: &tempfile::TempDir) -> Vec<String> {
        std::fs::read_to_string(directory.path().join("calls.jsonl"))
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn fork_copy_passes_each_nic_override_as_a_separate_incus_argument() {
        let (directory, backend) = fixture(false, false);
        let checkpoint = BackendCheckpoint {
            source: handle(),
            checkpoint_id: "0123456789abcdef0123456789abcdef".into(),
            snapshot_name: "fork-0123456789abcdef0123456789abcdef".into(),
        };
        let spec = ComputerSpec {
            computer_id: "abcdef0123456789abcdef0123456789".into(),
            ownership_id: "child-owner".into(),
            worker_token: "secret-must-not-reach-copy-arguments".into(),
            profile: serde_json::from_value(serde_json::json!({
                "image":"a".repeat(64), "kind":"virtual-machine", "cpus":2,
                "memory_mib":4096, "disk_gib":20, "capabilities":["browser"],
                "observation":{"mode":"visual","width":1280,"height":800},
                "idle_stop_seconds":900
            }))
            .unwrap(),
        };
        let project = branch_project(&spec.computer_id).unwrap();
        let nic = HashMap::from([
            ("type".into(), "nic".into()),
            ("name".into(), "eth0".into()),
            ("network".into(), "mcfabcdef012345".into()),
            ("hwaddr".into(), "00:16:3e:12:34:56".into()),
            ("ipv4.address".into(), "10.211.1.2".into()),
            ("security.ipv4_filtering".into(), "true".into()),
            ("security.ipv6_filtering".into(), "true".into()),
            ("security.mac_filtering".into(), "true".into()),
            ("security.port_isolation".into(), "true".into()),
            ("limits.ingress".into(), "100Mbit".into()),
        ]);
        backend
            .copy_fork_snapshot(&checkpoint, &spec, &project, &nic)
            .await
            .unwrap();
        let arguments: Vec<String> = serde_json::from_slice(
            &std::fs::read(directory.path().join("copy-args.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            arguments,
            [
                "--force-local",
                "--project",
                "computers",
                "copy",
                "mc-1234567890abcdef1234567890abcdef/fork-0123456789abcdef0123456789abcdef",
                "mc-1234567890abcdef1234567890abcdef",
                "--target-project",
                "mcf-abcdef0123456789abcdef0123456789",
                "--profile",
                "default",
                "--config",
                "user.meerkat-computer.id=abcdef0123456789abcdef0123456789",
                "--config",
                "user.meerkat-computer.owner=child-owner",
                "--config",
                "user.meerkat-computer.fork-pool=computers",
                "--config",
                "user.meerkat-computer.fork-checkpoint=0123456789abcdef0123456789abcdef",
                "--config",
                "user.meerkat-computer.fork-source=source-incarnation",
                "--config",
                "volatile.eth0.hwaddr=00:16:3e:12:34:56",
                "--device",
                "eth0,hwaddr=00:16:3e:12:34:56",
                "--device",
                "eth0,ipv4.address=10.211.1.2",
                "--device",
                "eth0,limits.ingress=100Mbit",
                "--device",
                "eth0,name=eth0",
                "--device",
                "eth0,network=mcfabcdef012345",
                "--device",
                "eth0,security.ipv4_filtering=true",
                "--device",
                "eth0,security.ipv6_filtering=true",
                "--device",
                "eth0,security.mac_filtering=true",
                "--device",
                "eth0,security.port_isolation=true",
                "--device",
                "eth0,type=nic",
            ]
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn full_state_capture_closes_gate_before_snapshot_and_resumes_exact_source() {
        let (directory, backend) = fixture(false, false);
        let checkpoint = backend
            .checkpoint_create(&handle(), "0123456789abcdef0123456789abcdef")
            .await
            .unwrap();
        assert_eq!(checkpoint.source, handle());
        assert_eq!(
            calls(&directory),
            [
                "fork_prepare",
                "snapshot-create",
                "checkpoint-inspect",
                "fork_resume"
            ]
        );
        assert!(!directory.path().join("gated").exists());
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn failed_snapshot_is_never_replayed_or_silently_resumed() {
        let (directory, backend) = fixture(true, false);
        let error = backend
            .checkpoint_create(&handle(), "0123456789abcdef0123456789abcdef")
            .await
            .unwrap_err();
        assert!(
            matches!(error, BackendError::CommandFailed { operation, .. } if operation == "checkpoint create")
        );
        assert_eq!(calls(&directory), ["fork_prepare", "snapshot-create"]);
        assert!(directory.path().join("gated").exists());
        backend
            .resume_fork_source(&handle(), "0123456789abcdef0123456789abcdef")
            .await
            .unwrap();
        assert_eq!(
            calls(&directory),
            ["fork_prepare", "snapshot-create", "fork_resume"]
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn disk_only_snapshot_is_rejected_without_child_or_automatic_recovery() {
        let (directory, backend) = fixture(false, true);
        let error = backend
            .checkpoint_create(&handle(), "0123456789abcdef0123456789abcdef")
            .await
            .unwrap_err();
        assert!(matches!(error, BackendError::InvalidBackendState(_)));
        assert_eq!(
            calls(&directory),
            ["fork_prepare", "snapshot-create", "checkpoint-inspect"]
        );
        assert!(directory.path().join("gated").exists());
    }

    #[test]
    fn branch_projects_preserve_identity_within_qemu_socket_path_limit() {
        let id = "4f606be3493b4572acd88ea413e9ee16";
        let project = branch_project(id).unwrap();
        assert_eq!(project, format!("mcf-{id}"));
        let path = format!("/run/incus/{project}_mc-db77f7e5dd4e4b25be1fa379e0067df6/qemu.monitor");
        assert!(path.len() < 108, "{path}");
        assert_ne!(
            project,
            branch_project("4f606be3493b4572acd88ea413e9ee17").unwrap()
        );
        validate_fork_socket_path(&project, "mc-db77f7e5dd4e4b25be1fa379e0067df6").unwrap();
        assert!(
            validate_fork_socket_path(
                &format!("meerkat-computer-fork-{id}"),
                "mc-db77f7e5dd4e4b25be1fa379e0067df6"
            )
            .is_err()
        );
        assert!(validate_fork_socket_path(&"p".repeat(47), &"i".repeat(35)).is_ok());
        assert!(validate_fork_socket_path(&"p".repeat(48), &"i".repeat(35)).is_err());
    }

    #[test]
    fn branch_network_names_reject_identifiers_without_enough_hex_digits() {
        assert!(bridge_name("----------------").is_err());
        assert!(bridge_name("a---------------").is_err());
        assert_eq!(
            bridge_name("01234567-89ab-cdef-0123-456789abcdef").unwrap(),
            "mcf0123456789ab"
        );
    }

    #[test]
    fn checkpoint_requires_explicit_memory_state_and_exact_source() {
        let checkpoint = BackendCheckpoint {
            source: handle(),
            checkpoint_id: "0123456789abcdef0123456789abcdef".into(),
            snapshot_name: "fork-0123456789abcdef0123456789abcdef".into(),
        };
        let mut value = serde_json::json!({"name":checkpoint.snapshot_name,"stateful":true,"config":{MANAGED_KEY:"1",COMPUTER_KEY:checkpoint.source.computer_id,OWNER_KEY:checkpoint.source.ownership_id,"volatile.uuid":checkpoint.source.instance_id}});
        validate_checkpoint(&checkpoint, &value).unwrap();
        value["stateful"] = false.into();
        assert!(validate_checkpoint(&checkpoint, &value).is_err());
        value["stateful"] = true.into();
        value["config"][OWNER_KEY] = "other-owner".into();
        assert!(validate_checkpoint(&checkpoint, &value).is_err());
    }

    #[test]
    fn private_fork_receipts_are_bound_to_exact_operation_and_checkpoint() {
        let request = serde_json::json!({"operation":"fork_prepare","checkpoint_id":"0123456789abcdef0123456789abcdef"});
        let valid = serde_json::json!({"protocol_version":1,"operation":"fork_prepare","checkpoint_id":"0123456789abcdef0123456789abcdef","prepared":true});
        validate_control_receipt(&request, &valid).unwrap();
        let mut wrong = valid.clone();
        wrong["checkpoint_id"] = "abcdef0123456789abcdef0123456789".into();
        assert!(validate_control_receipt(&request, &wrong).is_err());
        wrong = valid;
        wrong["prepared"] = false.into();
        assert!(validate_control_receipt(&request, &wrong).is_err());
    }
}
