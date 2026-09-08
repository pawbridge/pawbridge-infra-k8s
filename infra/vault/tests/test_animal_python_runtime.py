"""Offline safety checks; no Kubernetes/Vault/MySQL calls or credentials."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "configure-animal-python-runtime.py"
spec = importlib.util.spec_from_file_location("runtime", SOURCE)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def result(code=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class RuntimeSafetyTests(unittest.TestCase):
    def test_empty_paste_retries_and_only_displays_length(self):
        output = io.StringIO()
        with patch.object(runtime.getpass, "getpass", side_effect=["", " \t ", "  fixture-key-only  "]) as prompt, \
                patch("builtins.input", return_value="y") as confirm, patch.object(runtime.sys, "stdout", output):
            self.assertEqual("fixture-key-only", runtime.prompt_secret("APMS service key"))
        self.assertEqual(3, prompt.call_count)
        confirm.assert_called_once()
        self.assertIn("Received 16 characters", output.getvalue())
        self.assertEqual(2, output.getvalue().count("Received 0 characters"))
        self.assertNotIn("fixture-key-only", output.getvalue())

    def test_rejected_or_control_character_input_is_never_saved(self):
        target = runtime.Bootstrap("apply")
        saved = {"GEMINI_API_KEY": "accepted-fixture"}
        with patch.object(target, "read", side_effect=[None, {"data": saved}]), \
                patch.object(target, "write") as write, \
                patch.object(runtime.getpass, "getpass", side_effect=["bad\x1bkey", "rejected-fixture", "accepted-fixture"]), \
                patch("builtins.input", side_effect=["n", "y"]), patch.object(runtime.sys, "stdout", io.StringIO()) as output:
            self.assertEqual(saved, target.credential("python/runtime"))
            write.assert_called_once_with("secret/data/pawbridge/dev/python/runtime", {"options": {"cas": 0}, "data": saved})
            self.assertNotIn("accepted-fixture", output.getvalue())
            self.assertNotIn("rejected-fixture", output.getvalue())

    def test_hidden_input_warning_never_falls_back_to_visible_input(self):
        with patch.object(runtime.getpass, "getpass", side_effect=runtime.getpass.GetPassWarning()), \
                patch("builtins.input") as confirm, patch.object(runtime.sys, "stdout", io.StringIO()):
            with self.assertRaisesRegex(runtime.Failure, "Secure hidden input unavailable"):
                runtime.prompt_secret("APMS service key")
            confirm.assert_not_called()

    def test_invalid_second_policy_stops_before_any_remote_access(self):
        target = runtime.Bootstrap("apply")
        original_read = Path.read_text

        def read_file(path, *args, **kwargs):
            content = original_read(path, *args, **kwargs)
            return content.replace('["read"]', '["read", "update"]') if path.name == "python-runtime-read.hcl" else content

        with patch.object(Path, "read_text", read_file), patch.object(target, "read") as read, \
                patch.object(target, "write") as write:
            with self.assertRaisesRegex(runtime.Failure, "python-runtime-read"):
                target.policies()
            read.assert_not_called()
            write.assert_not_called()

    def test_offline_validation_never_logs_in_or_calls_kubernetes(self):
        with patch.object(runtime.sys, "argv", [str(SOURCE), "validate"]), \
                patch.object(runtime.Bootstrap, "preflight") as preflight, \
                patch.object(runtime.Bootstrap, "login") as login, \
                patch.object(runtime.Bootstrap, "kube") as kube:
            runtime.main()
            preflight.assert_not_called()
            login.assert_not_called()
            kube.assert_not_called()

    def test_invalid_policy_stops_apply_before_login_or_snapshot(self):
        with patch.object(runtime.sys, "argv", [str(SOURCE), "apply", "--backup-dir", "/tmp/pawbridge-test"]), \
                patch.object(Path, "read_text", return_value="invalid-policy"), \
                patch.object(runtime.Bootstrap, "login") as login, \
                patch.object(runtime.Bootstrap, "snapshot") as snapshot, \
                patch.object(runtime.Bootstrap, "kube") as kube:
            with self.assertRaises(runtime.Failure):
                runtime.main()
            login.assert_not_called()
            snapshot.assert_not_called()
            kube.assert_not_called()

    def test_apply_uses_real_policy_files_and_verifies_both_roles(self):
        target = runtime.Bootstrap("apply")
        documents = {}

        def read(path, **_kwargs):
            return documents.get(path)

        def write(path, data):
            documents[path] = data

        with patch.object(target, "read", side_effect=read), \
                patch.object(target, "write", side_effect=write) as writes:
            target.policies()
        self.assertEqual(4, writes.call_count)
        for service in ("animal", "python"):
            name = service + "-runtime-read"
            self.assertEqual(
                (SOURCE.parent / "policies" / (name + ".hcl")).read_text().strip(),
                documents["sys/policies/acl/" + name]["policy"].strip())
            self.assertEqual([name], documents["auth/kubernetes/role/" + name]["token_policies"])
        with patch.object(target, "read", side_effect=read), patch.object(target, "write") as writes:
            target.policies()
            writes.assert_not_called()

    def test_only_exact_not_found_can_be_treated_as_absence(self):
        target = runtime.Bootstrap("check")
        path = "secret/metadata/pawbridge/dev/animal/mysql"
        for trailer in ["", "command terminated with exit code 2\n"]:
            with self.subTest(trailer=trailer), patch.object(target, "vault", return_value=result(
                    2, stderr=("No value found at " + path + "\n" + trailer).encode())):
                self.assertIsNone(target.read(path, optional=True))
        for stderr in [b"permission denied", b"connection refused", b"No value found at another/path"]:
            with self.subTest(stderr=stderr), patch.object(target, "vault", return_value=result(2, stderr=stderr)):
                with self.assertRaises(runtime.Failure):
                    target.read(path, optional=True)

    def test_missing_result_with_extra_error_or_output_is_not_absence(self):
        target = runtime.Bootstrap("apply")
        path = "sys/policies/acl/animal-runtime-read"
        missing = ("No value found at " + path + "\n").encode()
        cases = [result(1, stderr=missing), result(2, stdout=b"unexpected", stderr=missing),
                 result(2, stderr=missing + b"command terminated with exit code 1\n"),
                 result(2, stderr=missing + b"permission denied\n"),
                 result(2, stderr=b"\xff" + missing),
                 result(2, stderr=b" " + missing),
                 result(2, stderr=b"connection refused\ncommand terminated with exit code 2\n")]
        for response in cases:
            with self.subTest(response=response), patch.object(target, "vault", return_value=response):
                with self.assertRaises(runtime.Failure):
                    target.read(path, optional=True)

    def test_permission_failure_is_categorized_without_echoing_response(self):
        target = runtime.Bootstrap("apply")
        response = result(2, stderr=b"Code: 403. Errors:\npermission denied\nprivate-fixture-value\ncommand terminated with exit code 2\n")
        with patch.object(target, "vault", return_value=response):
            with self.assertRaises(runtime.Failure) as failure:
                target.read("sys/policies/acl/animal-runtime-read", optional=True)
        self.assertIn("permission-denied", str(failure.exception))
        self.assertNotIn("private-fixture-value", str(failure.exception))

    def test_policy_and_role_creation_uses_real_kubectl_read_error_envelope(self):
        target = runtime.Bootstrap("apply")
        documents = {}

        def vault(*args, data=None):
            if args[0] == "read":
                path = args[2]
                if path not in documents:
                    return result(2, stderr=("No value found at " + path +
                                  "\ncommand terminated with exit code 2\n").encode())
                return result(stdout=json.dumps({"data": documents[path]}).encode())
            self.assertEqual("write", args[0])
            self.assertEqual("-", args[2])
            documents[args[1]] = json.loads(data)
            return result()

        with patch.object(target, "vault", side_effect=vault):
            target.policies()
        self.assertEqual(4, len(documents))

    def test_check_missing_credential_does_not_prompt_or_write(self):
        target = runtime.Bootstrap("check")
        with patch.object(target, "read", return_value=None), patch.object(target, "write") as write, \
                patch.object(runtime.getpass, "getpass") as prompt:
            with self.assertRaises(runtime.Failure):
                target.credential("animal/mysql")
            write.assert_not_called()
            prompt.assert_not_called()

    def test_existing_credential_is_reused_without_rotation(self):
        target = runtime.Bootstrap("apply")
        data = {"mysql-password": "a" * 64}
        with patch.object(target, "read", side_effect=[{"current_version": 7}, {"data": data}]), \
                patch.object(target, "write") as write, patch.object(runtime.secrets, "token_hex") as random:
            self.assertEqual(data, target.credential("animal/mysql"))
            write.assert_not_called()
            random.assert_not_called()

    def test_missing_credential_uses_create_only_cas(self):
        target = runtime.Bootstrap("apply")
        data = {"INTERNAL_API_KEY": "b" * 64}
        with patch.object(target, "read", side_effect=[None, {"data": data}]), \
                patch.object(target, "write") as write, \
                patch.object(runtime.secrets, "token_hex", return_value="b" * 64):
            self.assertEqual(data, target.credential("animal-python/internal"))
            write.assert_called_once_with("secret/data/pawbridge/dev/animal-python/internal",
                                          {"options": {"cas": 0}, "data": data})

    def test_deleted_credential_is_not_recreated(self):
        target = runtime.Bootstrap("apply")
        with patch.object(target, "read", side_effect=[{"current_version": 1}, {"data": None}]), \
                patch.object(target, "write") as write:
            with self.assertRaises(runtime.Failure):
                target.credential("animal/mysql")
            write.assert_not_called()

    def test_blank_or_extra_runtime_fields_are_rejected(self):
        for data in [{"GEMINI_API_KEY": ""}, {"GEMINI_API_KEY": "secret\n"},
                     {"GEMINI_API_KEY": "test-only", "EXTRA": "unexpected"}]:
            with self.subTest(data_keys=list(data)), self.assertRaises(runtime.Failure):
                runtime.validate_fields("python/runtime", data)

    def test_credentials_are_json_stdin_not_command_arguments(self):
        target = runtime.Bootstrap("apply")
        with patch.object(target, "vault", return_value=result()) as vault:
            target.write("secret/data/test", {"data": {"password": "test-only-value"}})
            args, kwargs = vault.call_args
            self.assertEqual(("write", "secret/data/test", "-"), args)
            self.assertEqual({"data": {"password": "test-only-value"}}, json.loads(kwargs["data"]))

    def test_failed_revocation_preserves_helper_for_recovery(self):
        target = runtime.Bootstrap("apply")
        target.session = "/tmp/pawbridge-animal-python.test123"
        with patch.object(target, "kube", return_value=result(stdout=b"present")) as kube, \
                patch.object(target, "vault", return_value=result(2)):
            with self.assertRaises(runtime.Failure):
                target.close()
            self.assertEqual(1, kube.call_count)
            self.assertNotIn("rm", kube.call_args.args)

    def test_unreachable_token_inspection_does_not_delete_session(self):
        target = runtime.Bootstrap("check")
        target.session = "/tmp/pawbridge-animal-python.test123"
        with patch.object(target, "kube", return_value=result(1)) as kube:
            with self.assertRaises(runtime.Failure):
                target.close()
            self.assertEqual(1, kube.call_count)

    def test_cleanup_rejects_broad_directory(self):
        target = runtime.Bootstrap("check")
        target.session = "/tmp"
        with patch.object(target, "kube") as kube:
            with self.assertRaises(runtime.Failure):
                target.close()
            kube.assert_not_called()

    def test_restored_tables_required_before_account_mutation(self):
        target = runtime.Bootstrap("apply")
        with patch.object(target, "kube", return_value=result(stdout=b"dGVzdC1vbmx5")), \
                patch.object(target, "mysql", return_value="animals") as mysql:
            with self.assertRaises(runtime.Failure):
                target.database("a" * 64)
            self.assertEqual(1, mysql.call_count)
            self.assertTrue(mysql.call_args.args[2].startswith("SELECT"))

    def test_unexpected_db_grants_are_not_repaired(self):
        target = runtime.Bootstrap("apply")
        responses = ["\n".join(runtime.BATCH_TABLES | {"animals"}), "51231", "1",
                     "GRANT ALL PRIVILEGES ON *.* TO `pawbridge_animal_app`@`%`"]
        with patch.object(target, "kube", return_value=result(stdout=b"dGVzdC1vbmx5")), \
                patch.object(target, "mysql", side_effect=responses) as mysql:
            with self.assertRaises(runtime.Failure):
                target.database("a" * 64)
            self.assertTrue(all(call.args[2].startswith(("SELECT", "SHOW")) for call in mysql.call_args_list))

    def test_interrupted_account_creation_resumes_with_additive_grant(self):
        target = runtime.Bootstrap("apply")
        usage = "GRANT USAGE ON *.* TO `pawbridge_animal_app`@`%`"
        crud = "GRANT SELECT, INSERT, UPDATE, DELETE ON `pawbridge_animal`.* TO `pawbridge_animal_app`@`%`"
        responses = ["\n".join(runtime.BATCH_TABLES | {"animals"}), "51231", "1", usage,
                     "1", "", usage + "\n" + crud, "51231"]
        with patch.object(target, "kube", return_value=result(stdout=b"dGVzdC1vbmx5")), \
                patch.object(target, "mysql", side_effect=responses) as mysql:
            target.database("a" * 64)
            changes = [c.args[2] for c in mysql.call_args_list if not c.args[2].startswith(("SELECT", "SHOW"))]
            self.assertEqual(["GRANT SELECT, INSERT, UPDATE, DELETE ON `pawbridge_animal`.* TO 'pawbridge_animal_app'@'%';"], changes)


if __name__ == "__main__":
    unittest.main()
