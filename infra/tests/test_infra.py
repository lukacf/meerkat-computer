"""Offline command and ownership contracts. No test invokes gcloud or Incus."""

import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

INFRA = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), INFRA / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gcp = load("gcp-host")
host = load("host-setup")
image = load("build-image")
chromium = load("chromium-profile")
desktop = load("desktop-session")


class GcpContracts(unittest.TestCase):
    def state(self):
        return gcp.new_state(
            {
                "cloud": {
                    "project": "explicit-project",
                    "account": "operator@example.test",
                    "zone": "europe-west4-a",
                    "machine_type": "n2-standard-16",
                    "network": "private-net",
                    "subnet": "private-subnet",
                    "nested_virtualization": False,
                }
            },
            "mc-host",
        )

    def test_every_cloud_command_has_explicit_identity_and_private_host(self):
        state = self.state()
        firewall, instance = gcp.specs(state)
        for spec in (firewall, instance):
            args = gcp.command(state, spec, "create")
            self.assertEqual(
                args[:6],
                [
                    "gcloud",
                    "--project",
                    "explicit-project",
                    "--account",
                    "operator@example.test",
                    "--quiet",
                ],
            )
            self.assertNotIn("--enable-nested-virtualization", args)
        args = gcp.command(state, instance, "create")
        self.assertIn("--no-address", args)
        self.assertIn("--no-service-account", args)
        self.assertIn("--instance-termination-action=STOP", args)
        self.assertIn("--source-ranges=35.235.240.0/20", gcp.command(state, firewall, "create"))
        self.assertFalse(
            any(
                "tcp:8000" in arg or "tcp:8787" in arg
                for arg in gcp.command(state, firewall, "create")
            )
        )

    def test_missing_identity_is_rejected_without_cli_calls(self):
        state = self.state()
        with patch.object(gcp.subprocess, "run", side_effect=AssertionError("unexpected CLI")):
            del state["cloud"]["account"]
            with self.assertRaises(ValueError):
                gcp.new_state({"cloud": state["cloud"]}, "mc-host")

    def test_saved_incarnation_and_creation_intent_are_required(self):
        state = self.state()
        spec = gcp.specs(state)[-1]
        resource = {
            "name": "mc-host",
            "id": "123",
            "labels": {"computer-owner": state["ownership_id"]},
        }
        with self.assertRaises(ValueError):
            gcp.verify_owner(state, spec, resource)
        state["resources"]["instance"] = {"pending": True}
        gcp.remember(state, spec, resource)
        resource["id"] = "456"
        with self.assertRaises(ValueError):
            gcp.verify_owner(state, spec, resource)
        resource["id"] = "123"
        resource["labels"]["computer-owner"] = "another-owner"
        with self.assertRaises(ValueError):
            gcp.verify_owner(state, spec, resource)

    def test_permission_error_is_never_absence(self):
        state = self.state()
        spec = gcp.specs(state)[-1]
        failure = subprocess.CalledProcessError(1, ["gcloud"], stderr="permission denied")
        with (
            patch.object(gcp, "run", side_effect=failure),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            gcp.describe(state, spec)

    def test_state_is_private_and_written_as_real_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            gcp.save(path, self.state())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["name"], "mc-host")

    def test_atomic_state_save_syncs_file_then_replacement_directory(self):
        events = []
        original_sync, original_replace = os.fsync, os.replace
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"old": true}')

            def sync(descriptor):
                info = os.fstat(descriptor)
                if stat.S_ISDIR(info.st_mode):
                    self.assertEqual(
                        (info.st_dev, info.st_ino),
                        (path.parent.stat().st_dev, path.parent.stat().st_ino),
                    )
                    self.assertEqual(json.loads(path.read_text())["name"], "mc-host")
                    events.append("directory sync")
                else:
                    events.append("file sync")
                original_sync(descriptor)

            def replace(source, target):
                original_replace(source, target)
                events.append("replace")

            with (
                patch.object(gcp.os, "fsync", side_effect=sync),
                patch.object(gcp.os, "replace", side_effect=replace),
            ):
                gcp.save(path, self.state())
        self.assertEqual(events, ["file sync", "replace", "directory sync"])


class GcpPowerContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "host.json"
        self.state = GcpContracts().state()
        self.state["resources"]["instance"] = {"name": "mc-host", "id": "123"}
        self.path.write_text(json.dumps(self.state))

    def invoke(self, action, statuses, *, power_error=None, apply=True, wait="3"):
        self.calls = []
        self.clock = 0.0
        observations = iter(statuses)
        last = None

        def run(args, *, timeout=600, **kwargs):
            nonlocal last
            operation = args[8]
            self.calls.append((operation, args, timeout))
            self.assertEqual(
                args[:6],
                [
                    "gcloud",
                    "--project",
                    "explicit-project",
                    "--account",
                    "operator@example.test",
                    "--quiet",
                ],
            )
            self.assertEqual(args[6:8], ["compute", "instances"])
            self.assertEqual(args[args.index("--zone") + 1], "europe-west4-a")
            self.assertEqual(args[9], "mc-host")
            if operation == "describe":
                last = next(observations, last)
                resource = {
                    "name": "mc-host",
                    "id": "123",
                    "labels": {"computer-owner": self.state["ownership_id"]},
                }
                resource.update(last if isinstance(last, dict) else {"status": last})
                return json.dumps(resource)
            self.assertIn(operation, {"start", "resume", "suspend"})
            self.assertIn("--async", args)
            # The exact operation must be durable before its request leaves the process.
            pending = json.loads(self.path.read_text())["resources"]["instance"]["pending_power"]
            self.assertEqual(pending["operation"], operation)
            self.assertEqual(pending["instance_id"], "123")
            if power_error is not None:
                raise power_error
            return json.dumps({"name": "accepted-operation", "status": "PENDING"})

        def sleep(seconds):
            self.clock += seconds

        argv = [
            "gcp-host.py",
            action,
            "--state",
            str(self.path),
            "--config",
            str(Path(self.directory.name) / "does-not-exist.toml"),
            "--wait-seconds",
            wait,
            "--poll-seconds",
            "1",
        ]
        if apply:
            argv.append("--apply")
        output = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(gcp, "run", side_effect=run),
            patch.object(
                gcp,
                "time",
                SimpleNamespace(
                    monotonic=lambda: self.clock,
                    sleep=sleep,
                ),
                create=True,
            ),
            redirect_stdout(output),
        ):
            gcp.main()
        return json.loads(output.getvalue())

    def powers(self):
        return [op for op, _, _ in self.calls if op != "describe"]

    def test_ensure_running_starts_only_terminated_and_resumes_only_suspended(self):
        for initial, operation in [("TERMINATED", "start"), ("SUSPENDED", "resume")]:
            with self.subTest(initial=initial):
                result = self.invoke("ensure-running", [initial, "STAGING", "RUNNING"])
                self.assertEqual(result["status"], "RUNNING")
                self.assertEqual(result["operation"], operation)
                self.assertEqual(self.powers(), [operation])
                self.assertNotIn(
                    "pending_power", json.loads(self.path.read_text())["resources"]["instance"]
                )

    def test_running_and_suspended_targets_are_noops(self):
        for action, status in [("ensure-running", "RUNNING"), ("suspend", "SUSPENDED")]:
            with self.subTest(action=action):
                result = self.invoke(action, [status])
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["operation"])
                self.assertEqual(self.powers(), [])

    def test_suspend_never_stops_or_reboots(self):
        result = self.invoke("suspend", ["RUNNING", "SUSPENDING", "SUSPENDED"])
        self.assertEqual(result["status"], "SUSPENDED")
        self.assertEqual(self.powers(), ["suspend"])
        with self.assertRaisesRegex(ValueError, "TERMINATED"):
            self.invoke("suspend", ["TERMINATED"])
        self.assertEqual(self.powers(), [])

    def test_transition_is_observed_before_choosing_one_power_action(self):
        result = self.invoke("ensure-running", ["STOPPING", "TERMINATED", "RUNNING"])
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(
            [op for op, _, _ in self.calls], ["describe", "describe", "start", "describe"]
        )
        self.assertGreater(self.clock, 0)

    def test_missing_saved_id_or_replaced_owner_never_gets_power(self):
        self.state["resources"]["instance"] = {"pending": True}
        self.path.write_text(json.dumps(self.state))
        with self.assertRaisesRegex(ValueError, "saved.*ID"):
            self.invoke("ensure-running", ["TERMINATED"])
        self.assertEqual(self.calls, [])
        self.state["resources"]["instance"] = {"id": "123", "name": "mc-host"}
        self.path.write_text(json.dumps(self.state))
        for resource in [
            {"status": "TERMINATED", "id": "456"},
            {"status": "TERMINATED", "labels": {"computer-owner": "foreign"}},
        ]:
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.invoke("ensure-running", [resource])
            self.assertEqual(self.powers(), [])

    def test_every_poll_rechecks_the_immutable_id(self):
        with self.assertRaisesRegex(ValueError, "ID changed"):
            self.invoke("ensure-running", ["TERMINATED", {"status": "RUNNING", "id": "456"}])
        self.assertEqual(self.powers(), ["start"])
        self.assertEqual(json.loads(self.path.read_text())["resources"]["instance"]["id"], "123")

    def test_failed_resume_is_not_replayed_or_changed_to_start_after_reentry(self):
        failure = subprocess.TimeoutExpired(["gcloud", "resume"], 1)
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            self.invoke("ensure-running", ["SUSPENDED"], power_error=failure)
        self.assertEqual(self.powers(), ["resume"])
        with self.assertRaisesRegex(TimeoutError, "unconfirmed"):
            self.invoke("ensure-running", ["SUSPENDED"])
        self.assertEqual(self.powers(), [])
        self.assertEqual(self.clock, 3)
        result = self.invoke("ensure-running", ["STAGING", "RUNNING"])
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(self.powers(), [])

    def test_refused_resume_does_not_discard_ram_with_a_fallback(self):
        failure = subprocess.CalledProcessError(1, ["gcloud", "resume"], stderr="no capacity")
        with self.assertRaisesRegex(RuntimeError, "unconfirmed"):
            self.invoke("ensure-running", ["SUSPENDED"], power_error=failure)
        self.assertEqual(self.powers(), ["resume"])

    def test_accepted_but_unobserved_suspend_remains_pending(self):
        with self.assertRaisesRegex(TimeoutError, "unconfirmed"):
            self.invoke("suspend", ["RUNNING", "SUSPENDING"])
        self.assertEqual(self.powers(), ["suspend"])
        result = self.invoke("suspend", ["SUSPENDED"])
        self.assertEqual(result["status"], "SUSPENDED")
        self.assertEqual(self.powers(), [])

    def test_transitional_deadline_and_cli_calls_share_the_same_bound(self):
        with self.assertRaises(TimeoutError):
            self.invoke("ensure-running", ["PROVISIONING"])
        self.assertEqual(self.powers(), [])
        self.assertEqual(self.clock, 3)
        self.assertTrue(all(0 < timeout <= 3 for _, _, timeout in self.calls))

    def test_persisting_intent_cannot_extend_the_power_request_deadline(self):
        original_save = gcp.save

        def slow_save(path, state):
            original_save(path, state)
            self.clock += 4

        with patch.object(gcp, "save", side_effect=slow_save), self.assertRaises(TimeoutError):
            self.invoke("ensure-running", ["TERMINATED"])
        self.assertEqual(self.powers(), [])

    def test_failed_directory_sync_prevents_any_power_request(self):
        original_sync = os.fsync
        directory_descriptors = []

        def fail_directory_sync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_descriptors.append(descriptor)
                raise OSError("directory sync failed")
            original_sync(descriptor)

        with (
            patch.object(gcp.os, "fsync", side_effect=fail_directory_sync),
            self.assertRaisesRegex(OSError, "directory sync failed"),
        ):
            self.invoke("ensure-running", ["SUSPENDED", "RUNNING"])
        self.assertEqual(self.powers(), [])
        self.assertEqual(len(directory_descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(directory_descriptors[0])

    def test_preview_has_no_cli_calls_or_state_writes(self):
        before = self.path.read_bytes()
        result = self.invoke("ensure-running", [], apply=False)
        self.assertEqual(result["action"], "ensure-running")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_wait_bounds_are_rejected_before_cloud_access(self):
        for wait in ["0", "-1", "nan", "inf"]:
            with self.subTest(wait=wait), self.assertRaises(ValueError):
                self.invoke("ensure-running", [], wait=wait)
            self.assertEqual(self.calls, [])


class HostContracts(unittest.TestCase):
    def state(self):
        return {
            "ownership_id": "owned-host",
            "project": "computer-project",
            "bridge": "mcbr0",
            "storage": "mc-storage",
            "storage_gib": 100,
            "address": "10.211.0.1/24",
            "intents": [],
        }

    def test_local_only_commands_and_isolated_guest_ports(self):
        state = self.state()
        for kind, args in host.commands(state):
            if kind == "project":
                # Query addresses the global project collection; Incus 6.0
                # explicitly rejects its --project flag on this API command.
                self.assertEqual(args[:2], ["incus", "--force-local"])
                self.assertNotIn("--project", args)
            else:
                self.assertEqual(args[:4], ["incus", "--force-local", "--project", "default"])
        nic = host.profile(state)["devices"]["eth0"]
        self.assertEqual(nic["security.port_isolation"], "true")
        self.assertEqual(nic["security.ipv4_filtering"], "true")
        self.assertEqual(nic["security.ipv6_filtering"], "true")
        self.assertEqual(nic["security.mac_filtering"], "true")
        self.assertEqual(set(host.profile(state)["devices"]), {"root", "eth0"})

    def test_project_creation_is_atomic_and_compatible_with_incus_6(self):
        state = self.state()
        args = dict(host.commands(state))["project"]
        self.assertNotIn("--description", args)
        self.assertNotIn("--project", args)
        self.assertEqual(args[2:7], ["query", "--request", "POST", "/1.0/projects", "--data"])
        payload = json.loads(args[7])
        self.assertEqual(
            payload,
            {
                "name": state["project"],
                "description": "meerkat-computer:" + state["ownership_id"],
                "config": {
                    "features.images": "true",
                    "features.profiles": "true",
                    "features.networks": "false",
                },
            },
        )
        state["intents"].append("project")
        host.verify_owner(state, "project", payload)

    def test_refuses_adoption_and_modified_bridge(self):
        state = self.state()
        actual = {
            "name": "mcbr0",
            "config": {
                host.OWNER_KEY: state["ownership_id"],
                "ipv4.address": state["address"],
                "ipv4.nat": "true",
                "ipv6.address": "none",
                **host.acl_settings(state),
            },
        }
        with self.assertRaises(ValueError):
            host.verify_owner(state, "network", actual)
        state["intents"].append("network")
        host.verify_owner(state, "network", actual)
        actual["config"]["ipv4.address"] = "10.1.0.1/24"
        with self.assertRaises(ValueError):
            host.verify_owner(state, "network", actual)

    def test_acl_blocks_peers_private_networks_and_metadata_but_preserves_host_access(self):
        import ipaddress

        state = self.state()
        rules = host.network_acl(state)
        self.assertEqual(
            rules["ingress"],
            [
                {
                    "action": "allow",
                    "state": "enabled",
                    "source": "10.211.0.1/32",
                }
            ],
        )
        denied = [
            ipaddress.ip_network(value) for value in rules["egress"][0]["destination"].split(",")
        ]
        for value in [
            "10.211.0.42",
            "192.168.1.4",
            "172.20.1.2",
            "169.254.169.254",
            "100.100.100.200",
            "127.0.0.1",
        ]:
            self.assertTrue(any(ipaddress.ip_address(value) in block for block in denied), value)
        self.assertFalse(any(ipaddress.ip_address("1.1.1.1") in block for block in denied))
        settings = host.acl_settings(state)
        self.assertEqual(settings["security.acls.default.ingress.action"], "reject")
        self.assertEqual(settings["security.acls.default.egress.action"], "allow")
        state["intents"].append("acl")
        actual = dict(rules, name=host.acl_name(state))
        host.verify_owner(state, "acl", actual)
        actual["egress"][0]["action"] = "allow"
        with self.assertRaises(ValueError):
            host.verify_owner(state, "acl", actual)

    def test_legacy_bridge_without_network_boundary_requires_fresh_pool(self):
        state = self.state()
        state["intents"].append("network")
        actual = {
            "name": state["bridge"],
            "config": {
                host.OWNER_KEY: state["ownership_id"],
                "ipv4.address": state["address"],
                "ipv4.nat": "true",
                "ipv6.address": "none",
            },
        }
        with self.assertRaises(ValueError):
            host.verify_owner(state, "network", actual)

    def test_address_and_name_validation(self):
        host.validate("computers", "mcbr0", "mc-storage", "10.211.0.1/24", 100)
        for address in [
            "8.8.8.8/24",
            "::1/64",
            "10.0.0.1/32",
            "127.0.0.1/24",
            "192.0.2.1/24",
            "10.0.0.0/24",
        ]:
            with self.assertRaises(ValueError):
                host.validate("computers", "mcbr0", "mc-storage", address, 100)
        with self.assertRaises(ValueError):
            host.validate("default", "mcbr0", "mc-storage", "10.211.0.1/24", 100)


class ImageContracts(unittest.TestCase):
    def test_archive_uses_guest_root_ownership_even_for_large_host_login_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "provision.sh"
            source.write_text("#!/bin/sh\nexit 0\n")
            source.chmod(0o755)
            archive_path = root / "payload.tgz"
            original = tarfile.TarFile.gettarinfo

            def host_login_metadata(archive, *args, **kwargs):
                member = original(archive, *args, **kwargs)
                member.uid = member.gid = 383532983
                member.uname = member.gname = "host-os-login"
                return member

            with (
                patch.object(image, "source_files", return_value=[source]),
                patch.object(tarfile.TarFile, "gettarinfo", host_login_metadata),
            ):
                image.archive_sources(root, archive_path)
            with tarfile.open(archive_path) as archive:
                member = archive.getmember("provision.sh")
                self.assertEqual((member.uid, member.gid), (0, 0))
                self.assertEqual((member.uname, member.gname), ("root", "root"))
                self.assertEqual(member.mode, 0o755)
                self.assertEqual(archive.extractfile(member).read(), source.read_bytes())

    def test_archive_cannot_contain_local_secrets_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in [
                "pyproject.toml",
                "uv.lock",
                "README.md",
                "LICENSE",
                "infra/provision-worker.sh",
                "infra/chromium-profile.py",
                "infra/desktop-session.py",
                "infra/image-check.py",
                "python/meerkat_computer/worker.py",
                "python/meerkat_computer/resources/preview-v1.html",
                ".state/operator.token",
                ".env",
            ]:
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("source" if not path.startswith(".") else "secret")
            (root / "python/meerkat_computer/operator.token").write_text("secret")
            archive = root / "payload.tgz"
            image.archive_sources(root, archive)
            with tarfile.open(archive) as stream:
                names = stream.getnames()
            self.assertEqual(
                set(names),
                {
                    "pyproject.toml",
                    "uv.lock",
                    "README.md",
                    "LICENSE",
                    "infra/provision-worker.sh",
                    "infra/chromium-profile.py",
                    "infra/desktop-session.py",
                    "infra/image-check.py",
                    "python/meerkat_computer/worker.py",
                    "python/meerkat_computer/resources/preview-v1.html",
                },
            )
            (root / "python/meerkat_computer/worker.py").unlink()
            (root / "python/meerkat_computer/worker.py").symlink_to(root / ".env")
            with self.assertRaises(ValueError):
                image.archive_sources(root, archive)

    def test_builder_replacement_cannot_be_published(self):
        state = {
            "name": "mc-image-v1",
            "kind": "container",
            "ownership_id": "owner",
            "instance_id": "first",
            "create_intent": True,
        }
        actual = {
            "name": "mc-image-v1",
            "type": "container",
            "config": {image.BUILDER_OWNER: "owner", "volatile.uuid": "first"},
        }
        image.verify_owner(state, actual)
        actual["config"]["volatile.uuid"] = "replacement"
        with self.assertRaises(ValueError):
            image.verify_owner(state, actual)

    def test_uncertain_image_publication_recovers_only_its_exact_intent(self):
        state = {
            "ownership_id": "owner",
            "publish_intent": True,
            "source_hash": "hash",
            "kind": "container",
        }
        published = {
            "fingerprint": "a" * 64,
            "public": False,
            "type": "container",
            "properties": {
                image.BUILDER_OWNER: "owner",
                image.PROTOCOL_KEY: "1",
                "user.meerkat-computer.source-sha256": "hash",
            },
        }
        self.assertEqual(image.published_image(state, [published]), published)
        self.assertIsNone(image.published_image(state, []))
        state["publish_intent"] = False
        with self.assertRaises(ValueError):
            image.published_image(state, [published])
        state["publish_intent"] = True
        published["public"] = True
        with self.assertRaises(ValueError):
            image.published_image(state, [published])

    def test_pending_publication_is_not_replayed_when_image_is_not_yet_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image-state.json"
            state = {
                "name": "mc-image-v1",
                "project": "meerkat-computer",
                "base_image": "images:ubuntu/24.04/cloud",
                "kind": "container",
                "desktop": False,
                "apps": "none",
                "source_hash": "source",
                "ownership_id": "owner",
                "schema_version": 1,
                "publish_intent": True,
                "create_intent": True,
                "instance_id": "existing-builder",
            }
            path.write_text(json.dumps(state))
            before = path.read_bytes()
            calls = []

            def pending(_state, *args, **kwargs):
                calls.append(args)
                if args == ("image", "list", "--format=json"):
                    return "[]"
                raise AssertionError("No further guest or publication call is authorized")

            with (
                patch.object(sys, "argv", ["build-image.py", "--state", str(path), "--apply"]),
                patch.object(image, "source_hash", return_value="source"),
                patch.object(image, "incus", side_effect=pending),
                self.assertRaisesRegex(ValueError, "unconfirmed"),
            ):
                image.main()
            self.assertEqual(calls, [("image", "list", "--format=json")])
            self.assertEqual(path.read_bytes(), before)

    def test_image_bundle_and_desktop_marker_are_part_of_publication_identity(self):
        state = {
            "ownership_id": "owner",
            "publish_intent": True,
            "source_hash": "hash",
            "kind": "container",
            "desktop": True,
            "apps": "full",
        }
        published = {
            "fingerprint": "a" * 64,
            "public": False,
            "type": "container",
            "properties": {
                image.BUILDER_OWNER: "owner",
                image.PROTOCOL_KEY: "1",
                "user.meerkat-computer.source-sha256": "hash",
                image.DESKTOP_KEY: "1",
                image.APPS_KEY: "full",
            },
        }
        self.assertEqual(image.published_image(state, [published]), published)
        for key, value in [(image.DESKTOP_KEY, "0"), (image.APPS_KEY, "minimal")]:
            changed = dict(published, properties=dict(published["properties"], **{key: value}))
            with self.assertRaises(ValueError):
                image.published_image(state, [changed])

    def test_chromium_userns_profile_is_bound_to_one_exact_executable(self):
        executable = "/opt/meerkat-computer/browsers/chromium_headless_shell-1234/chrome-linux/chrome-headless-shell"
        profile = chromium.render_profile(executable, 0)
        self.assertIn('"' + executable + '"', profile)
        self.assertIn("userns,", profile)
        for invalid in [
            "/bin/sh",
            executable + "*",
            executable.replace("1234", "/../outside"),
            executable + '" { unsafe, }',
        ]:
            with self.assertRaises(ValueError):
                chromium.render_profile(invalid, 0)


class DesktopContracts(unittest.TestCase):
    def test_guest_uses_stationary_scrollbars_without_changing_parent_environment(self):
        original = {"PATH": "/usr/bin", "GTK_OVERLAY_SCROLLING": "1"}
        env = desktop.session_environment(original, Path("/tmp/private-session"), 99)
        self.assertEqual(env["GTK_OVERLAY_SCROLLING"], "0")
        self.assertEqual(env["NO_AT_BRIDGE"], "0")
        self.assertEqual(original, {"PATH": "/usr/bin", "GTK_OVERLAY_SCROLLING": "1"})

    def test_session_never_inherits_an_existing_display_or_bus(self):
        env = desktop.session_environment(
            {
                "DISPLAY": ":0",
                "DBUS_SESSION_BUS_ADDRESS": "tcp:host=external",
                "DBUS_STARTER_ADDRESS": "tcp:host=external",
                "AT_SPI_BUS_ADDRESS": "tcp:host=external",
                "WAYLAND_DISPLAY": "wayland-0",
                "PATH": "/usr/bin",
                "HOME": "/home/computer",
            },
            Path("/tmp/private-session"),
            99,
        )
        self.assertEqual(env["DISPLAY"], ":99")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/tmp/private-session/session-bus"
        )
        self.assertNotIn("AT_SPI_BUS_ADDRESS", env)
        self.assertNotIn("DBUS_STARTER_ADDRESS", env)
        self.assertNotIn("WAYLAND_DISPLAY", env)
        self.assertEqual(env["HOME"], "/home/computer")
        command = desktop.xvfb_command(env, 1280, 720)
        self.assertIn("1280x720x24", command)
        self.assertEqual(command[command.index("-nolisten") + 1], "tcp")
        self.assertEqual(command[command.index("-auth") + 1], env["XAUTHORITY"])
        self.assertNotIn("-ac", command)

    def test_resolution_and_bus_addresses_fail_closed(self):
        for width, height in [(0, 720), (1280, 0), (10000, 720)]:
            with self.assertRaises(ValueError):
                desktop.dimensions(width, height)
        self.assertEqual(
            desktop.accessibility_address("('unix:path=/tmp/bus,guid=123',)"),
            "unix:path=/tmp/bus,guid=123",
        )
        for reply in [
            "('tcp:host=external',)",
            "('unix:path=/tmp/bus;tcp:host=external',)",
            "'unix:path=/tmp/bus'",
            "('__import__(os)',)",
        ]:
            with self.assertRaises(ValueError):
                desktop.accessibility_address(reply)

    def test_shutdown_is_bounded_even_if_a_child_ignores_term(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            desktop.stop_processes([process], grace_seconds=0.05)
            self.assertEqual(process.poll(), -signal.SIGKILL)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=1)
            process.stdout.close()

    def test_startup_commands_stop_at_the_deadline(self):
        session = desktop.Session({}, startup_seconds=-1)
        with (
            patch.object(desktop.subprocess, "run", side_effect=AssertionError("must not run")),
            self.assertRaises(TimeoutError),
        ):
            session.command(["true"])


if __name__ == "__main__":
    unittest.main()
