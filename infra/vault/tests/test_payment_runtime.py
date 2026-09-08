"""Offline Payment Vault bootstrap safety tests; no Kubernetes, Vault, or MySQL calls."""

import base64
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch


SOURCE = Path(__file__).resolve().parents[1] / "configure-payment-runtime.py"
spec = importlib.util.spec_from_file_location("payment_runtime", SOURCE)
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def result(code=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def root_secret():
    return result(stdout=base64.b64encode(b"test-root-password"))


def valid_column_metadata():
    rows = [
        ("payments", "id", "bigint", "bigint", "NO", "-1", "auto_increment"),
        ("payments", "payment_key", "varchar", "varchar(100)", "NO", "100", ""),
        ("payments", "order_id", "varchar", "varchar(36)", "NO", "36", ""),
        ("payments", "user_id", "bigint", "bigint", "NO", "-1", ""),
        ("payments", "amount", "bigint", "bigint", "NO", "-1", ""),
        ("payments", "status", "varchar", "varchar(20)", "NO", "20", ""),
        ("payments", "method", "varchar", "varchar(20)", "YES", "20", ""),
        ("payments", "requested_at", "datetime", "datetime", "YES", "-1", ""),
        ("payments", "approved_at", "datetime", "datetime", "YES", "-1", ""),
        ("payments", "created_at", "datetime", "datetime", "YES", "-1", ""),
        ("payments", "updated_at", "datetime", "datetime", "YES", "-1", ""),
        ("outbox", "id", "bigint", "bigint", "NO", "-1", "auto_increment"),
        ("outbox", "aggregate_type", "varchar", "varchar(50)", "NO", "50", ""),
        ("outbox", "aggregate_id", "varchar", "varchar(100)", "NO", "100", ""),
        ("outbox", "event_type", "varchar", "varchar(50)", "NO", "50", ""),
        ("outbox", "payload", "json", "json", "NO", "-1", ""),
        ("outbox", "created_at", "datetime", "datetime", "NO", "-1", ""),
    ]
    return "\n".join("\t".join((*row[:-1], row[-1] or "-")) for row in rows)


def valid_primary_key_metadata():
    return "payments\tid\noutbox\tid"


def valid_payment_index_metadata():
    return "PRIMARY\t0\t1\tid\t-1\nuk_payments_payment_key\t0\t1\tpayment_key\t-1"


def valid_structure_responses():
    return [valid_column_metadata(), valid_primary_key_metadata(), valid_payment_index_metadata()]


class PaymentRuntimeSafetyTests(unittest.TestCase):
    def test_validate_is_offline_and_requires_the_real_policy_contract(self):
        with patch.object(runtime.sys, "argv", [str(SOURCE), "validate"]), \
                patch.object(runtime.PaymentBootstrap, "preflight") as preflight, \
                patch.object(runtime.PaymentBootstrap, "login") as login, \
                patch.object(runtime.PaymentBootstrap, "kube") as kube:
            runtime.main()
        preflight.assert_not_called()
        login.assert_not_called()
        kube.assert_not_called()

    def test_invalid_policy_stops_apply_before_login_snapshot_or_remote_writes(self):
        target = runtime.PaymentBootstrap("apply", "/tmp/pawbridge-test")
        with patch.object(Path, "read_text", return_value="invalid-policy"), \
                patch.object(target, "read") as read, patch.object(target, "write") as write:
            with self.assertRaisesRegex(runtime.Failure, "Policy file"):
                target.policies()
        read.assert_not_called()
        write.assert_not_called()

    def test_policy_and_role_are_exact_and_idempotent(self):
        target = runtime.PaymentBootstrap("apply")
        documents = {}

        def read(path, **_kwargs):
            return documents.get(path)

        def write(path, data):
            documents[path] = data

        with patch.object(target, "read", side_effect=read), patch.object(target, "write", side_effect=write) as writes:
            target.policies()
        self.assertEqual(2, writes.call_count)
        self.assertEqual(
            (SOURCE.parent / "policies" / "payment-runtime-read.hcl").read_text().strip(),
            documents["sys/policies/acl/payment-runtime-read"]["policy"].strip(),
        )
        role = documents["auth/kubernetes/role/payment-runtime-read"]
        self.assertEqual(["payment-runtime-vault-auth"], role["bound_service_account_names"])
        self.assertEqual(["pawbridge"], role["bound_service_account_namespaces"])
        self.assertEqual("vault", role["audience"])
        self.assertEqual(600, role["token_ttl"])
        self.assertEqual(600, role["token_max_ttl"])
        with patch.object(target, "read", side_effect=read), patch.object(target, "write") as writes:
            target.policies()
        writes.assert_not_called()

    def test_check_does_not_repair_missing_policy_or_role(self):
        target = runtime.PaymentBootstrap("check")
        with patch.object(target, "read", return_value=None), patch.object(target, "write") as write:
            with self.assertRaisesRegex(runtime.Failure, "Policy missing"):
                target.policies()
        write.assert_not_called()

    def test_toss_accepts_only_test_secret_prefixes_and_never_claims_validity(self):
        for value in ("test_sk_fixture",):
            with self.subTest(value=value):
                runtime.validate_fields("payment/runtime", {"TOSS_SECRET_KEY": value})
        for value in ("", " live_sk_fixture", "live_sk_fixture", "live_gsk_fixture", "test_gsk_fixture", "test_ck_fixture",
                      "test_sk_", "test_sk_fixture key", "test_sk_\uac00", "test_sk_fixture ",
                      "test_gsk_fixture\n", "client_key", "test_sk_bad\x1bvalue"):
            with self.subTest(value=value), self.assertRaises(runtime.Failure):
                runtime.validate_fields("payment/runtime", {"TOSS_SECRET_KEY": value})

    def test_hidden_toss_prompt_rejects_padding_before_the_value_can_be_saved(self):
        with patch.object(runtime.transport.getpass, "getpass", return_value=" test_sk_fixture"), \
                patch("builtins.input", return_value="y"), patch.object(runtime.sys, "stdout", io.StringIO()):
            with self.assertRaisesRegex(runtime.Failure, "must not be padded"):
                runtime.prompt_toss_test_secret()

    def test_existing_credentials_are_reused_without_rotation_or_prompt(self):
        target = runtime.PaymentBootstrap("apply")
        mysql = {"mysql-password": "a" * 64}
        toss = {"TOSS_SECRET_KEY": "test_sk_fixture"}
        with patch.object(target, "read", side_effect=[{"current_version": 7}, {"data": mysql},
                                                        {"current_version": 3}, {"data": toss}]), \
                patch.object(target, "write") as write, patch.object(runtime.secrets, "token_hex") as random, \
                patch.object(runtime.transport, "prompt_secret") as prompt:
            self.assertEqual(mysql, target.write_credential("payment/mysql", mysql))
            self.assertEqual(toss, target.write_credential("payment/runtime", toss))
        write.assert_not_called()
        random.assert_not_called()
        prompt.assert_not_called()

    def test_missing_credential_uses_create_only_cas_and_check_never_writes(self):
        apply = runtime.PaymentBootstrap("apply")
        mysql = {"mysql-password": "b" * 64}
        with patch.object(apply, "read", side_effect=[None, {"data": mysql}]), \
                patch.object(apply, "write") as write:
            self.assertEqual(mysql, apply.write_credential("payment/mysql", mysql))
        write.assert_called_once_with("secret/data/pawbridge/dev/payment/mysql",
                                      {"options": {"cas": 0}, "data": mysql})

        check = runtime.PaymentBootstrap("check")
        with patch.object(check, "read", return_value=None), patch.object(check, "write") as write:
            with self.assertRaisesRegex(runtime.Failure, "Missing Vault credential"):
                check.write_credential("payment/mysql", mysql)
        write.assert_not_called()

    def test_deleted_credential_is_not_recreated(self):
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "read", side_effect=[{"current_version": 1}, {"data": None}]), \
                patch.object(target, "write") as write:
            with self.assertRaisesRegex(runtime.Failure, "deleted/unreadable"):
                target.write_credential("payment/mysql", {"mysql-password": "c" * 64})
        write.assert_not_called()

    def test_apply_validates_toss_and_database_before_snapshot_and_persistent_writes(self):
        bootstrap = MagicMock()
        mysql = {"mysql-password": "a" * 64}
        toss = {"TOSS_SECRET_KEY": "test_sk_fixture"}
        before = {"payments": "0", "outbox": "0"}
        bootstrap.inspect_credential.side_effect = [None, None]
        bootstrap.database_preflight.return_value = ("root", before)
        bootstrap.write_credential.side_effect = [mysql, toss]
        with patch.object(runtime, "PaymentBootstrap", return_value=bootstrap), \
                patch.object(runtime.sys, "argv", [str(SOURCE), "apply", "--backup-dir", "/tmp/pawbridge-test"]), \
                patch.object(runtime, "prompt_toss_test_secret", return_value="test_sk_fixture"), \
                patch.object(runtime.secrets, "token_hex", return_value="a" * 64):
            runtime.main()

        sequence = bootstrap.method_calls
        self.assertLess(sequence.index(call.database_preflight("a" * 64)), sequence.index(call.snapshot()))
        self.assertLess(sequence.index(call.snapshot()), sequence.index(call.policies()))
        self.assertLess(sequence.index(call.policies()), sequence.index(
            call.write_credential("payment/mysql", mysql)))
        bootstrap.write_credential.assert_has_calls([
            call("payment/mysql", mysql),
            call("payment/runtime", toss),
        ])
        bootstrap.database_apply.assert_called_once_with("root", before, mysql["mysql-password"])
        bootstrap.close.assert_called_once()

    def test_main_check_calls_no_persistent_mutator_after_account_disappears(self):
        bootstrap = MagicMock()
        mysql = {"mysql-password": "a" * 64}
        toss = {"TOSS_SECRET_KEY": "test_sk_fixture"}
        # database_preflight represents the account observed before a later
        # deletion race. check must end before any re-read that could create it.
        bootstrap.inspect_credential.side_effect = [mysql, toss]
        bootstrap.database_preflight.return_value = ("root", {"payments": "0", "outbox": "0"})
        with patch.object(runtime, "PaymentBootstrap", return_value=bootstrap), \
                patch.object(runtime.sys, "argv", [str(SOURCE), "check"]):
            runtime.main()
        bootstrap.snapshot.assert_not_called()
        bootstrap.write_credential.assert_not_called()
        bootstrap.database_apply.assert_not_called()
        bootstrap.close.assert_called_once()

    def test_database_preflight_rejects_missing_base_table_before_account_access(self):
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "kube", return_value=root_secret()), \
                patch.object(target, "mysql", side_effect=["1", "payments"]) as mysql:
            with self.assertRaisesRegex(runtime.Failure, "BASE TABLE"):
                target.database_preflight("a" * 64)
        self.assertEqual(2, mysql.call_count)
        self.assertTrue(all(call.args[2].startswith("SELECT") for call in mysql.call_args_list))

    def test_schema_contract_rejects_missing_required_column_with_reads_only(self):
        target = runtime.PaymentBootstrap("apply")
        missing = "\n".join(
            line for line in valid_column_metadata().splitlines()
            if not line.startswith("payments\tpayment_key\t")
        )
        with patch.object(target, "mysql", return_value=missing) as mysql:
            with self.assertRaisesRegex(runtime.Failure, "payment_key"):
                target._schema_contract("root")
        self.assertTrue(all(call.args[2].startswith("SELECT") for call in mysql.call_args_list))

    def test_schema_columns_uses_nonempty_extra_sentinel_through_real_mysql_transport(self):
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "kube", return_value=result(stdout=valid_column_metadata().encode())) as kube:
            columns = target._schema_columns("root")
        self.assertEqual("-", columns[("outbox", "created_at")]["extra"])
        self.assertEqual(17, len(columns))
        self.assertIn("COALESCE(NULLIF(EXTRA, ''), '-')", kube.call_args.kwargs["data"].decode())

    def test_schema_contract_rejects_non_json_outbox_payload_with_reads_only(self):
        wrong_payload = valid_column_metadata().replace(
            "outbox\tpayload\tjson\tjson\tNO\t-1\t",
            "outbox\tpayload\tlongtext\tlongtext\tNO\t-1\t",
        )
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "mysql", return_value=wrong_payload) as mysql:
            with self.assertRaisesRegex(runtime.Failure, "outbox.payload"):
                target._schema_contract("root")
        self.assertTrue(all(call.args[2].startswith("SELECT") for call in mysql.call_args_list))

    def test_schema_contract_accepts_native_enum_payment_status(self):
        native_enum = valid_column_metadata().replace(
            "payments\tstatus\tvarchar\tvarchar(20)\tNO\t20\t",
            "payments\tstatus\tenum\tenum('ready','done','canceled','aborted')\tNO\t-1\t",
        )
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "mysql", side_effect=[
            native_enum, valid_primary_key_metadata(), valid_payment_index_metadata(),
        ]) as mysql:
            target._schema_contract("root")
        self.assertTrue(all(call.args[2].startswith("SELECT") for call in mysql.call_args_list))

    def test_schema_contract_rejects_composite_payment_key_unique_index(self):
        composite_only = "\n".join([
            "PRIMARY\t0\t1\tid\t-1",
            "uk_payments_payment_order\t0\t1\tpayment_key\t-1",
            "uk_payments_payment_order\t0\t2\torder_id\t-1",
        ])
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "mysql", side_effect=[
            valid_column_metadata(), valid_primary_key_metadata(), composite_only,
        ]) as mysql:
            with self.assertRaisesRegex(runtime.Failure, "standalone full unique"):
                target._schema_contract("root")
        self.assertTrue(all(call.args[2].startswith("SELECT") for call in mysql.call_args_list))

    def test_zero_rows_are_valid_and_unexpected_grants_are_never_repaired(self):
        target = runtime.PaymentBootstrap("apply")
        responses = [
            "1", "payments\noutbox", *valid_structure_responses(), "0", "0", "1",
            "GRANT ALL PRIVILEGES ON *.* TO `pawbridge_payment_app`@`%`",
        ]
        with patch.object(target, "kube", return_value=root_secret()), \
                patch.object(target, "mysql", side_effect=responses) as mysql:
            with self.assertRaisesRegex(runtime.Failure, "Unexpected DB privileges"):
                target.database_preflight("a" * 64)
        self.assertTrue(all(call.args[2].startswith(("SELECT", "SHOW")) for call in mysql.call_args_list))

    def test_missing_table_grant_is_added_only_after_existing_login_succeeds(self):
        target = runtime.PaymentBootstrap("apply")
        usage = "GRANT USAGE ON *.* TO `pawbridge_payment_app`@`%`"
        payments = "GRANT SELECT, INSERT ON `pawbridge_payment`.`payments` TO `pawbridge_payment_app`@`%`"
        outbox = "GRANT SELECT, INSERT ON `pawbridge_payment`.`outbox` TO `pawbridge_payment_app`@`%`"
        responses = [
            "1", usage + "\n" + payments,  # initial account/grants
            "1",                               # app login before repair
            "",                                # additive outbox grant
            "1", usage + "\n" + payments + "\n" + outbox,  # grant readback
            "0", "0", "0", "0",           # root then application counts
        ]
        with patch.object(target, "mysql", side_effect=responses) as mysql:
            target.database_apply("root", {"payments": "0", "outbox": "0"}, "a" * 64)
        commands = [call.args[2] for call in mysql.call_args_list]
        login_index = commands.index("SELECT 1;")
        grant_index = next(index for index, command in enumerate(commands) if command.startswith("GRANT"))
        self.assertLess(login_index, grant_index)
        self.assertEqual("GRANT SELECT, INSERT ON `pawbridge_payment`.`outbox` TO 'pawbridge_payment_app'@'%';",
                         commands[grant_index])

    def test_check_database_never_creates_or_grants(self):
        target = runtime.PaymentBootstrap("check")
        expected = "\n".join(sorted(target._expected_grants()))
        responses = ["1", "payments\noutbox", *valid_structure_responses(),
                     "0", "0", "1", expected, "1", "0", "0"]
        with patch.object(target, "kube", return_value=root_secret()), \
                patch.object(target, "mysql", side_effect=responses) as mysql:
            root, before = target.database_preflight("a" * 64)
        self.assertEqual("test-root-password", root)
        self.assertEqual({"payments": "0", "outbox": "0"}, before)
        self.assertTrue(all(call.args[2].startswith(("SELECT", "SHOW")) for call in mysql.call_args_list))

    def test_database_apply_refuses_check_before_any_sql(self):
        target = runtime.PaymentBootstrap("check")
        with patch.object(target, "mysql") as mysql:
            with self.assertRaisesRegex(runtime.Failure, "apply mode"):
                target.database_apply("root", {"payments": "0", "outbox": "0"}, "a" * 64)
        mysql.assert_not_called()

    def test_inherited_snapshot_keeps_the_private_directory_guard(self):
        target = runtime.PaymentBootstrap("apply", "/tmp")
        target.session = "/tmp/pawbridge-payment-runtime.fixture"
        with patch.object(target, "kube") as kube:
            with self.assertRaisesRegex(runtime.Failure, "Backup directory"):
                target.snapshot()
        kube.assert_not_called()

    def test_failed_token_revocation_preserves_the_isolated_session_for_recovery(self):
        target = runtime.PaymentBootstrap("apply")
        target.session = "/tmp/pawbridge-payment-runtime.fixture"
        with patch.object(target, "kube", return_value=result(stdout=b"present")) as kube, \
                patch.object(target, "vault", return_value=result(2)):
            with self.assertRaises(runtime.Failure):
                target.close()
        self.assertEqual(1, kube.call_count)
        self.assertNotIn("rm", kube.call_args.args)

    def test_vault_write_keeps_credential_data_on_json_stdin(self):
        target = runtime.PaymentBootstrap("apply")
        with patch.object(target, "vault", return_value=result()) as vault:
            target.write("secret/data/pawbridge/dev/payment/runtime",
                         {"data": {"TOSS_SECRET_KEY": "test_sk_fixture"}})
        args, kwargs = vault.call_args
        self.assertEqual(("write", "secret/data/pawbridge/dev/payment/runtime", "-"), args)
        self.assertEqual({"data": {"TOSS_SECRET_KEY": "test_sk_fixture"}}, json.loads(kwargs["data"]))


if __name__ == "__main__":
    unittest.main()
