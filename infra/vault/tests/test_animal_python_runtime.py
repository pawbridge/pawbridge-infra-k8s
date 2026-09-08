"""Offline safety checks; no Kubernetes/Vault/MySQL calls or credentials."""
import importlib.util
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
    def test_only_exact_not_found_can_be_treated_as_absence(self):
        target = runtime.Bootstrap("check")
        path = "secret/metadata/pawbridge/dev/animal/mysql"
        with patch.object(target, "vault", return_value=result(2, stderr=("No value found at " + path + "\n").encode())):
            self.assertIsNone(target.read(path, optional=True))
        for stderr in [b"permission denied", b"connection refused", b"No value found at another/path"]:
            with self.subTest(stderr=stderr), patch.object(target, "vault", return_value=result(2, stderr=stderr)):
                with self.assertRaises(runtime.Failure):
                    target.read(path, optional=True)

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
