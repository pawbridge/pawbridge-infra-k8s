"""Offline observability registration contracts; no live Vault/Slack requests."""
import gzip
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "configure-observability-runtime.py"
spec = importlib.util.spec_from_file_location("observability_runtime", SOURCE)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def credential(target):
    if target == "grafana":
        return {"admin-user": "pawbridge-admin", "admin-password": "a" * 43}
    # Construct a synthetic Slack-shaped value; never sent to a network.
    return {"url": "https://" + "hooks.slack.com" + "/services/TFIXTURE/BFIXTURE/NOTAREALWEBHOOK"}


class RegistrationTests(unittest.TestCase):
    def test_validate_does_not_login_or_read_remote_data(self):
        with patch.object(runtime.sys, "argv", [str(SOURCE), "validate"]), \
                patch.object(runtime.ObservabilityBootstrap, "preflight") as preflight, \
                patch.object(runtime.ObservabilityBootstrap, "login") as login, \
                patch.object(runtime.ObservabilityBootstrap, "kube") as kube:
            runtime.main()
        preflight.assert_not_called(); login.assert_not_called(); kube.assert_not_called()

    def test_policy_tampering_fails_before_any_remote_access(self):
        target = runtime.ObservabilityBootstrap("apply")
        with patch.object(Path, "read_text", return_value='path "*" {}'), \
                patch.object(target, "read") as read:
            with self.assertRaises(runtime.Failure):
                target.policy_changes()
        read.assert_not_called()

    def test_missing_contract_plan_is_separate_and_least_privilege(self):
        target = runtime.ObservabilityBootstrap("apply")
        with patch.object(target, "read", return_value=None), patch.object(target, "write") as write:
            planned = dict(target.policy_changes())
        self.assertEqual(4, len(planned))
        write.assert_not_called()
        for name in ("grafana", "slack"):
            role = planned["auth/kubernetes/role/observability-" + name + "-read"]
            self.assertEqual(["monitoring"], role["bound_service_account_namespaces"])
            self.assertEqual(["observability-" + name + "-vault-auth"], role["bound_service_account_names"])
            self.assertEqual(["observability-" + name + "-read"], role["token_policies"])
            self.assertFalse(role["token_no_default_policy"])
            self.assertEqual((600, 600), (role["token_ttl"], role["token_max_ttl"]))

    def test_existing_mismatch_is_not_repaired(self):
        target = runtime.ObservabilityBootstrap("apply")
        with patch.object(target, "read", return_value={"policy": "unsafe"}), \
                patch.object(target, "write") as write, patch.object(target, "snapshot") as snapshot:
            with self.assertRaisesRegex(runtime.Failure, "differs"):
                target.register()
        write.assert_not_called(); snapshot.assert_not_called()

    def test_check_does_not_prompt_snapshot_or_write_missing_values(self):
        target = runtime.ObservabilityBootstrap("check", target="grafana")
        with patch.object(target, "policy_changes", return_value=[]), \
                patch.object(target, "read", return_value=None), \
                patch.object(target, "write") as write, patch.object(target, "snapshot") as snapshot, \
                patch.object(runtime.transport, "prompt_secret") as prompt:
            with self.assertRaisesRegex(runtime.Failure, "Missing Vault credential"):
                target.register()
        write.assert_not_called(); snapshot.assert_not_called(); prompt.assert_not_called()

    def test_permission_error_is_not_absence_and_does_not_leak_raw_stderr(self):
        target = runtime.ObservabilityBootstrap("apply")
        response = subprocess.CompletedProcess([], 2, b"", b"Code: 403.\npermission denied SECRET_MARKER")
        with patch.object(target, "vault", return_value=response):
            with self.assertRaises(runtime.Failure) as caught:
                target.inspect_credential("grafana")
        self.assertIn("permission-denied", str(caught.exception))
        self.assertNotIn("SECRET_MARKER", str(caught.exception))

    def test_deleted_value_is_not_recreated(self):
        target = runtime.ObservabilityBootstrap("apply")
        with patch.object(target, "read", side_effect=[{"current_version": 1}, {"data": None}]):
            with self.assertRaisesRegex(runtime.Failure, "deleted/unreadable"):
                target.inspect_credential("grafana")

    def test_idempotent_apply_does_not_generate_prompt_snapshot_or_write(self):
        target = runtime.ObservabilityBootstrap("apply")
        with patch.object(target, "policy_changes", return_value=[]), \
                patch.object(target, "inspect_credential", side_effect=credential), \
                patch.object(target, "write") as write, patch.object(target, "snapshot") as snapshot, \
                patch.object(runtime.secrets, "token_urlsafe") as generate, \
                patch.object(runtime.transport, "prompt_secret") as prompt:
            target.register()
        for mock in (write, snapshot, generate, prompt):
            mock.assert_not_called()

    def test_missing_values_are_created_only_after_snapshot_and_with_cas_zero(self):
        target = runtime.ObservabilityBootstrap("apply")
        events = []; documents = {}
        def write(path, value):
            events.append("write")
            self.assertEqual({"cas": 0}, value["options"])
            documents[path] = {"data": value["data"]}
        with patch.object(target, "policy_changes", return_value=[]), \
                patch.object(target, "inspect_credential", return_value=None), \
                patch.object(target, "require_new_grafana_install"), \
                patch.object(target, "snapshot", side_effect=lambda: events.append("snapshot")), \
                patch.object(target, "write", side_effect=write), \
                patch.object(target, "read", side_effect=lambda path: documents[path]), \
                patch.object(runtime.secrets, "token_urlsafe", return_value="a" * 43), \
                patch.object(runtime.transport, "prompt_secret", return_value=credential("slack")["url"]), \
                patch.object(runtime.sys, "stdout", io.StringIO()) as output:
            target.register()
        self.assertEqual(["snapshot", "write", "write"], events)
        self.assertNotIn(credential("slack")["url"], output.getvalue())
        self.assertNotIn("a" * 43, output.getvalue())

    def test_bad_slack_input_and_snapshot_failure_prevent_all_writes(self):
        for failed_step in ("input", "snapshot"):
            target = runtime.ObservabilityBootstrap("apply", target="slack")
            with self.subTest(failed_step=failed_step), \
                    patch.object(target, "policy_changes", return_value=[]), \
                    patch.object(target, "inspect_credential", return_value=None), \
                    patch.object(runtime.transport, "prompt_secret", return_value=(
                        "https://example.invalid/not-slack" if failed_step == "input" else credential("slack")["url"])), \
                    patch.object(target, "snapshot", side_effect=runtime.Failure("snapshot failed")) as snapshot, \
                    patch.object(target, "write") as write:
                with self.assertRaises(runtime.Failure):
                    target.register()
            write.assert_not_called()
            if failed_step == "input":
                snapshot.assert_not_called()

    def test_missing_vault_grafana_with_existing_secret_or_storage_refuses_new_password(self):
        import json
        for secret, items in [(b"secret/monitoring-grafana-admin", []), (b"", [{"kind": "PersistentVolumeClaim"}])]:
            target = runtime.ObservabilityBootstrap("apply", target="grafana")
            responses = [subprocess.CompletedProcess([], 0, secret, b""),
                         subprocess.CompletedProcess([], 0, json.dumps({"items": items}).encode(), b"")]
            with patch.object(target, "kube", side_effect=responses):
                with self.assertRaisesRegex(runtime.Failure, "restore credentials"):
                    target.require_new_grafana_install()

    def test_new_grafana_guard_accepts_only_absence_and_propagates_read_failure(self):
        target = runtime.ObservabilityBootstrap("apply", target="grafana")
        with patch.object(target, "kube", side_effect=[subprocess.CompletedProcess([], 0, b"", b""),
                subprocess.CompletedProcess([], 0, b'{"items": []}', b"")]):
            target.require_new_grafana_install()
        with patch.object(target, "kube", return_value=subprocess.CompletedProcess([], 1, b"", b"denied")):
            with self.assertRaises(runtime.Failure):
                target.require_new_grafana_install()

    def test_slack_url_rejects_credentials_port_queries_lookalikes_and_control_characters(self):
        good = credential("slack")["url"]
        runtime.validate_fields("slack", {"url": good})
        invalid = [good.replace("https:", "http:"), good + "?a=b", good + "#fragment", good + "\n",
                   good.replace("hooks.slack.com", "hooks.slack.com.evil.invalid"),
                   good.replace("hooks.slack.com", "user@hooks.slack.com"),
                   good.replace("hooks.slack.com", "hooks.slack.com:443"), "xoxb-not-a-webhook"]
        for value in invalid:
            with self.subTest(case=invalid.index(value)), self.assertRaises(runtime.Failure):
                runtime.validate_fields("slack", {"url": value})

    def test_grafana_only_does_not_request_slack(self):
        target = runtime.ObservabilityBootstrap("apply", target="grafana")
        with patch.object(target, "policy_changes", return_value=[]), \
                patch.object(target, "inspect_credential", return_value=credential("grafana")) as inspect, \
                patch.object(runtime.transport, "prompt_secret") as prompt:
            target.register()
        inspect.assert_called_once_with("grafana"); prompt.assert_not_called()

    def test_main_revokes_isolated_session_even_after_registration_failure(self):
        with patch.object(runtime.sys, "argv", [str(SOURCE), "check"]), \
                patch.object(runtime.ObservabilityBootstrap, "preflight"), \
                patch.object(runtime.ObservabilityBootstrap, "login"), \
                patch.object(runtime.ObservabilityBootstrap, "register", side_effect=runtime.Failure("failed")), \
                patch.object(runtime.ObservabilityBootstrap, "close") as close:
            with self.assertRaises(runtime.Failure):
                runtime.main()
        close.assert_called_once()

    def test_snapshot_is_private_non_overwriting_and_integrity_checked(self):
        target = runtime.ObservabilityBootstrap("apply")
        target.session = "/tmp/pawbridge-animal-python.fixture"
        data = gzip.compress(os.urandom(2048))
        with tempfile.TemporaryDirectory(prefix="pawbridge-observability-test-") as directory:
            target.backup_dir = directory
            with patch.object(target, "vault", return_value=subprocess.CompletedProcess([], 0, b"", b"")), \
                    patch.object(target, "kube", return_value=subprocess.CompletedProcess([], 0, data, b"")), \
                    patch.object(runtime.secrets, "token_hex", return_value="fixture"):
                previous = os.umask(0o077)
                try:
                    target.snapshot()
                    path = Path(directory) / "vault-pre-observability-fixture.snapshot.gz"
                    self.assertEqual(data, path.read_bytes())
                    self.assertEqual(0, path.stat().st_mode & 0o077)
                    with self.assertRaises(FileExistsError):
                        target.snapshot()
                finally:
                    os.umask(previous)


if __name__ == "__main__":
    unittest.main()
