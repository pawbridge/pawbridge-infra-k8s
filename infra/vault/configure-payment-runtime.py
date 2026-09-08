#!/usr/bin/env python3
"""Bootstrap the Payment runtime's Vault and MySQL contracts without deployment.

Run on the approved control plane with Python 3 and the sibling
``configure-animal-python-runtime.py`` present.  This deliberately imports
that script to reuse its proven hidden-input, exact-absence, isolated-login,
and private-snapshot transport safety boundaries; distribute both files
together.  ``validate`` is offline.  ``check`` creates and revokes only an
isolated Vault login token; it never changes Vault policies, KV data, or
MySQL.  ``apply`` snapshots Vault before its first persistent change.

Toss input is accepted only when it has the current API-individual integration
test-secret prefix ``test_sk_``.  That is a local format check, not an online
validity check; see https://docs.tosspayments.com/reference/using-api/api-keys.
Before writes, the core Payment schema mapped by Backend ``origin/dev``
``8f224780`` is checked through ``information_schema``.  This is a structural
gate, not a replacement for a final application/JPA validation.  ``apply`` may
write the required policy/role, create missing KV data and the dedicated MySQL
user, and add missing table grants. It never issues schema DDL, ``ALTER USER``,
password rotation, grant revocation, or user deletion.
"""

import argparse
import base64
import importlib.util
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys


def _load_transport():
    source = Path(__file__).with_name("configure-animal-python-runtime.py")
    if not source.is_file():
        raise SystemExit("configure-animal-python-runtime.py is required beside this script")
    spec = importlib.util.spec_from_file_location("animal_python_runtime_transport", source)
    if spec is None or spec.loader is None:
        raise SystemExit("Unable to load required Vault transport helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = _load_transport()
Failure = transport.Failure
require = transport.require
checked = transport.checked
parse_json = transport.parse_json

CONTEXT = "pawbridge-vbox-k136"
PREFIX = "pawbridge/dev/"
DB = "pawbridge_payment"
DB_USER = "pawbridge_payment_app"
POLICY_NAME = "payment-runtime-read"
POLICY_PATHS = ("payment/mysql", "payment/runtime")
PAYMENT_TABLES = ("payments", "outbox")


def validate_fields(path, data):
    expected = {
        "payment/mysql": {"mysql-password"},
        "payment/runtime": {"TOSS_SECRET_KEY"},
    }.get(path)
    require(expected is not None, "Unsupported payment credential path")
    require(isinstance(data, dict) and set(data) == expected,
            path + ": unexpected field contract")
    for key, value in data.items():
        require(isinstance(value, str) and value and value.strip() == value,
                path + ": missing or padded field " + key)
        require(not any(ord(char) < 32 or ord(char) == 127 for char in value),
                path + ": control character in " + key)
    if path == "payment/mysql":
        require(re.fullmatch(r"[0-9a-f]{64}", data["mysql-password"]) is not None,
                "payment/mysql: generated key contract differs")
    else:
        secret = data["TOSS_SECRET_KEY"]
        suffix = secret.removeprefix("test_sk_")
        require(secret.startswith("test_sk_") and suffix
                and all("!" <= character <= "~" for character in suffix),
                "payment/runtime: API-individual integration test secret key is required")


def prompt_toss_test_secret():
    """Reuse the hidden helper while preserving its pre-strip input envelope."""
    received = {}
    original = transport.getpass.getpass

    def capture(*args, **kwargs):
        value = original(*args, **kwargs)
        received["value"] = value
        return value

    transport.getpass.getpass = capture
    try:
        value = transport.prompt_secret("Toss test secret key")
    finally:
        transport.getpass.getpass = original
    require(received.get("value") == value,
            "payment/runtime: Toss test secret key must not be padded")
    return value


class PaymentBootstrap(transport.Bootstrap):
    """Payment-specific contract over the shared safe Vault transport."""

    def kube(self, *args, data=None, timeout=40):
        return transport.execute(["kubectl", "--context", CONTEXT, "--request-timeout=15s", *args],
                                 data=data, timeout=timeout)

    def vault(self, *args, data=None):
        require(self.session.startswith("/tmp/pawbridge-payment-runtime."),
                "No isolated Payment Vault session")
        return self.kube("-n", "vault", "exec", "-i", "vault-0", "--",
                         "env", "HOME=" + self.session, "vault", *args, data=data)

    def preflight(self):
        current = checked(transport.execute(["kubectl", "config", "current-context"]),
                          "Context check").decode().strip()
        require(current == CONTEXT, "Unexpected active context; refusing to proceed")
        for namespace, pod in (("vault", "vault-0"), ("databases", "mysql-0")):
            obj = parse_json(checked(self.kube("-n", namespace, "get", "pod", pod, "-o", "json"),
                                     "Pod readiness"))
            require(any(condition["type"] == "Ready" and condition["status"] == "True"
                        for condition in obj.get("status", {}).get("conditions", [])),
                    namespace + " Pod not Ready")
        require(sys.stdin.isatty() and sys.stdout.isatty(), "Interactive terminal required")

    def login(self):
        username = input("Vault admin username: ").strip()
        require(re.fullmatch(r"[A-Za-z0-9._-]+", username), "Unsupported username")
        self.session = checked(self.kube("-n", "vault", "exec", "vault-0", "--", "mktemp", "-d",
                                         "/tmp/pawbridge-payment-runtime.XXXXXX"),
                               "Session creation").decode().strip()
        require(re.fullmatch(r"/tmp/pawbridge-payment-runtime\.[A-Za-z0-9]+", self.session),
                "Unexpected session path")
        # Vault itself reads the password; neither this process nor its argv sees it.
        result = subprocess.run([
            "kubectl", "--context", CONTEXT, "--request-timeout=15s", "-n", "vault", "exec", "-it",
            "vault-0", "--", "sh", "-c", 'HOME="$1" vault login -method=userpass username="$2" >/dev/null',
            "sh", self.session, username,
        ], timeout=180)
        require(result.returncode == 0, "Vault login failed; check credentials before retrying")
        mount = self.read("sys/mounts/secret")
        require(mount["type"] == "kv" and mount["options"].get("version") == "2",
                "Existing KV v2 mount required")
        auth = self.read("auth/kubernetes/config")
        require(auth["kubernetes_host"] == "https://kubernetes.default.svc:443"
                and auth["disable_local_ca_jwt"] is False
                and auth["disable_iss_validation"] is True,
                "Kubernetes auth contract differs; not modifying it")

    def policy_documents(self):
        expected = "\n\n".join(
            'path "secret/' + kind + '/' + PREFIX + path + '" {\n  capabilities = ["read"]\n}'
            for path in POLICY_PATHS for kind in ("data", "metadata")
        ) + "\n"
        policy_file = Path(__file__).parent / "policies" / (POLICY_NAME + ".hcl")
        require(policy_file.read_text().strip() == expected.strip(),
                "Policy file violates exact read-only paths: " + POLICY_NAME)
        return {POLICY_NAME: expected}

    def policies(self):
        # Validate local policy before the first remote policy read or write.
        expected = self.policy_documents()[POLICY_NAME]
        policy_path = "sys/policies/acl/" + POLICY_NAME
        existing = self.read(policy_path, optional=True)
        if not existing or existing.get("policy", "").strip() != expected.strip():
            require(self.mode == "apply", "Policy missing or differs: " + POLICY_NAME)
            self.write(policy_path, {"policy": expected})
        require(self.read(policy_path)["policy"].strip() == expected.strip(), "Policy readback failed")

        role = {
            "bound_service_account_names": ["payment-runtime-vault-auth"],
            "bound_service_account_namespaces": ["pawbridge"],
            "audience": "vault",
            "token_policies": [POLICY_NAME],
            "token_ttl": 600,
            "token_max_ttl": 600,
        }
        role_path = "auth/kubernetes/role/" + POLICY_NAME
        existing = self.read(role_path, optional=True)
        if not existing or any(existing.get(key) != value for key, value in role.items()):
            require(self.mode == "apply", "Role missing or differs: " + POLICY_NAME)
            self.write(role_path, role)
        actual = self.read(role_path)
        require(all(actual.get(key) == value for key, value in role.items()), "Role readback failed")
        print("Verified least-privilege policy and role:", POLICY_NAME)

    def mysql(self, username, password, sql, database=DB):
        require(password and "\n" not in password and "\r" not in password,
                "Invalid MySQL password envelope")
        # Password and SQL are stdin only, never process arguments or diagnostic output.
        return checked(self.kube(
            "-n", "databases", "exec", "-i", "mysql-0", "--", "sh", "-ec",
            'IFS= read -r MYSQL_PWD; export MYSQL_PWD; export MYSQL_HISTFILE=/dev/null; '
            'exec mysql --protocol=TCP --host=127.0.0.1 --user="$1" --batch --skip-column-names --raw "$2"',
            "sh", username, database, data=(password + "\n" + sql + "\n").encode()),
            "MySQL operation").decode().strip()

    def _root_password(self):
        encoded = checked(self.kube("-n", "databases", "get", "secret", "mysql-auth",
                                    "-o", "jsonpath={.data.mysql-root-password}"),
                          "MySQL root credential")
        return base64.b64decode(encoded, validate=True).decode()

    def _table_counts(self, username, password):
        counts = {}
        for table in PAYMENT_TABLES:
            count = self.mysql(username, password, "SELECT COUNT(*) FROM `" + table + "`;")
            require(count.isdigit(), "Invalid Payment row count")
            counts[table] = count
        return counts

    def _schema_columns(self, root):
        raw = self.mysql(
            "root", root,
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME), LOWER(DATA_TYPE), LOWER(COLUMN_TYPE), "
            "UPPER(IS_NULLABLE), COALESCE(CHARACTER_MAXIMUM_LENGTH, -1), "
            "LOWER(COALESCE(NULLIF(EXTRA, ''), '-')) "
            "FROM information_schema.columns WHERE TABLE_SCHEMA='pawbridge_payment' "
            "AND TABLE_NAME IN ('payments', 'outbox') ORDER BY TABLE_NAME, ORDINAL_POSITION;",
            "information_schema",
        )
        columns = {}
        for line in raw.splitlines():
            parts = line.split("\t")
            require(len(parts) == 7, "Unexpected Payment column metadata")
            table, name, data_type, column_type, nullable, length, extra = parts
            require(table in PAYMENT_TABLES and name and nullable in {"YES", "NO"},
                    "Unexpected Payment column metadata")
            try:
                length = int(length)
            except ValueError:
                raise Failure("Unexpected Payment column metadata") from None
            require((table, name) not in columns, "Duplicate Payment column metadata")
            columns[(table, name)] = {
                "data_type": data_type,
                "column_type": column_type,
                "nullable": nullable,
                "length": length,
                "extra": extra,
            }
        return columns

    def _primary_keys(self, root):
        raw = self.mysql(
            "root", root,
            "SELECT LOWER(tc.TABLE_NAME), LOWER(kcu.COLUMN_NAME) "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON tc.constraint_schema=kcu.constraint_schema AND tc.table_name=kcu.table_name "
            "AND tc.constraint_name=kcu.constraint_name "
            "WHERE tc.constraint_schema='pawbridge_payment' "
            "AND tc.constraint_type='PRIMARY KEY' AND tc.table_name IN ('payments', 'outbox') "
            "ORDER BY tc.table_name, kcu.ordinal_position;",
            "information_schema",
        )
        keys = {table: [] for table in PAYMENT_TABLES}
        for line in raw.splitlines():
            parts = line.split("\t")
            require(len(parts) == 2 and parts[0] in keys and parts[1],
                    "Unexpected Payment primary-key metadata")
            keys[parts[0]].append(parts[1])
        require(keys == {"payments": ["id"], "outbox": ["id"]},
                "Payment id primary-key contract differs")

    def _payment_key_is_uniquely_indexed(self, root):
        raw = self.mysql(
            "root", root,
            "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, LOWER(COLUMN_NAME), COALESCE(SUB_PART, -1) "
            "FROM information_schema.statistics WHERE TABLE_SCHEMA='pawbridge_payment' "
            "AND TABLE_NAME='payments' ORDER BY INDEX_NAME, SEQ_IN_INDEX;",
            "information_schema",
        )
        indexes = {}
        for line in raw.splitlines():
            parts = line.split("\t")
            require(len(parts) == 5, "Unexpected Payment index metadata")
            name, non_unique, sequence, column, sub_part = parts
            try:
                sequence, sub_part = int(sequence), int(sub_part)
            except ValueError:
                raise Failure("Unexpected Payment index metadata") from None
            require(name and non_unique in {"0", "1"} and sequence > 0 and column,
                    "Unexpected Payment index metadata")
            indexes.setdefault(name, []).append((sequence, non_unique, column, sub_part))
        for parts in indexes.values():
            ordered = sorted(parts)
            if ordered[0][1] == "0" and [part[2] for part in ordered] == ["payment_key"] \
                    and ordered[0][3] == -1:
                return True
        return False

    @staticmethod
    def _require_column(columns, table, name, data_types, *, not_null=False, minimum_length=None,
                        auto_increment=False):
        column = columns.get((table, name))
        require(column is not None, "Payment required column is missing: " + table + "." + name)
        require(column["data_type"] in data_types,
                "Payment column type differs: " + table + "." + name)
        if not_null:
            require(column["nullable"] == "NO", "Payment required column is nullable: " + table + "." + name)
        if minimum_length is not None:
            require(column["length"] >= minimum_length,
                    "Payment column length differs: " + table + "." + name)
        if auto_increment:
            require("auto_increment" in column["extra"],
                    "Payment id must be auto-increment: " + table + "." + name)

    def _schema_contract(self, root):
        """Read only the structural boundary needed by Payment JPA at 8f224780."""
        columns = self._schema_columns(root)
        required = self._require_column
        for table in PAYMENT_TABLES:
            required(columns, table, "id", {"bigint"}, not_null=True, auto_increment=True)
        required(columns, "payments", "payment_key", {"varchar"}, not_null=True, minimum_length=100)
        required(columns, "payments", "order_id", {"varchar"}, not_null=True, minimum_length=36)
        required(columns, "payments", "user_id", {"bigint"}, not_null=True)
        required(columns, "payments", "amount", {"bigint"}, not_null=True)
        status = columns.get(("payments", "status"))
        require(status is not None and status["nullable"] == "NO"
                and (status["data_type"] == "enum"
                     or status["data_type"] == "varchar" and status["length"] >= 20),
                "Payment status column contract differs")
        required(columns, "payments", "method", {"varchar"}, minimum_length=20)
        for name in ("requested_at", "approved_at", "created_at", "updated_at"):
            required(columns, "payments", name, {"datetime", "timestamp"})
        required(columns, "outbox", "aggregate_type", {"varchar"}, not_null=True, minimum_length=50)
        required(columns, "outbox", "aggregate_id", {"varchar"}, not_null=True, minimum_length=100)
        required(columns, "outbox", "event_type", {"varchar"}, not_null=True, minimum_length=50)
        required(columns, "outbox", "payload", {"json"}, not_null=True)
        required(columns, "outbox", "created_at", {"datetime", "timestamp"}, not_null=True)
        self._primary_keys(root)
        require(self._payment_key_is_uniquely_indexed(root),
                "Payment payment_key requires a standalone full unique index")

    @staticmethod
    def _expected_grants():
        return {
            "GRANT USAGE ON *.* TO `pawbridge_payment_app`@`%`",
            "GRANT SELECT, INSERT ON `pawbridge_payment`.`payments` TO `pawbridge_payment_app`@`%`",
            "GRANT SELECT, INSERT ON `pawbridge_payment`.`outbox` TO `pawbridge_payment_app`@`%`",
        }

    def _account_state(self, root):
        exists = self.mysql(
            "root", root,
            "SELECT COUNT(*) FROM mysql.user WHERE User='pawbridge_payment_app' AND Host='%';",
            "mysql",
        )
        require(exists in {"0", "1"}, "Unexpected MySQL account count")
        if exists == "0":
            return exists, set()
        return exists, set(self.mysql("root", root,
                                      "SHOW GRANTS FOR 'pawbridge_payment_app'@'%';").splitlines())

    def database_preflight(self, app_password):
        root = self._root_password()
        schema = self.mysql("root", root,
                            "SELECT COUNT(*) FROM information_schema.schemata "
                            "WHERE schema_name='pawbridge_payment';", "information_schema")
        require(schema == "1", "Payment schema is missing or differs; no schema creation allowed")
        tables = set(self.mysql(
            "root", root,
            "SELECT LOWER(TABLE_NAME) FROM information_schema.tables "
            "WHERE TABLE_SCHEMA='pawbridge_payment' AND TABLE_TYPE='BASE TABLE' "
            "AND TABLE_NAME IN ('payments', 'outbox');", "information_schema",
        ).splitlines())
        require(tables == set(PAYMENT_TABLES),
                "Payment payments/outbox BASE TABLE contract differs; no schema creation allowed")
        self._schema_contract(root)
        before = self._table_counts("root", root)
        exists, grants = self._account_state(root)
        expected = self._expected_grants()
        if exists == "0":
            require(self.mode == "apply", "Dedicated Payment MySQL account is missing")
        else:
            require(grants <= expected, "Unexpected DB privileges; no destructive automatic grant repair")
            # This is deliberately before any repair: an old account must prove
            # that the Vault password still logs in before missing grants are added.
            require(self.mysql(DB_USER, app_password, "SELECT 1;", "information_schema") == "1",
                    "Existing account password differs; refusing grant changes")
            if grants != expected:
                require(self.mode == "apply", "Payment DB grant is incomplete")
            else:
                require(self._table_counts(DB_USER, app_password) == before,
                        "App login/count mismatch; inspect before retrying")
        print("Verified Payment schema and preserved pre-change row counts:",
              ", ".join(table + "=" + before[table] for table in PAYMENT_TABLES))
        return root, before

    def database_apply(self, root, before, app_password):
        require(self.mode == "apply", "Database changes require apply mode")
        exists, grants = self._account_state(root)
        if exists == "0":
            self.mysql("root", root,
                       "CREATE USER 'pawbridge_payment_app'@'%' IDENTIFIED BY '" + app_password + "';",
                       "mysql")
            exists, grants = self._account_state(root)
        expected = self._expected_grants()
        require(exists == "1" and grants <= expected,
                "Unexpected DB privileges; no destructive automatic grant repair")
        if grants != expected:
            require(self.mysql(DB_USER, app_password, "SELECT 1;", "information_schema") == "1",
                    "Existing account password differs; refusing grant changes")
            grant_sql = {
                "GRANT USAGE ON *.* TO `pawbridge_payment_app`@`%`":
                    "GRANT USAGE ON *.* TO 'pawbridge_payment_app'@'%';",
                "GRANT SELECT, INSERT ON `pawbridge_payment`.`payments` TO `pawbridge_payment_app`@`%`":
                    "GRANT SELECT, INSERT ON `pawbridge_payment`.`payments` TO 'pawbridge_payment_app'@'%';",
                "GRANT SELECT, INSERT ON `pawbridge_payment`.`outbox` TO `pawbridge_payment_app`@`%`":
                    "GRANT SELECT, INSERT ON `pawbridge_payment`.`outbox` TO 'pawbridge_payment_app'@'%';",
            }
            for grant in expected - grants:
                self.mysql("root", root, grant_sql[grant], "mysql")
        _, actual = self._account_state(root)
        require(actual == expected, "DB grant readback differs")
        require(self._table_counts("root", root) == before,
                "Payment row count changed during bootstrap")
        require(self._table_counts(DB_USER, app_password) == before,
                "App login/count mismatch; inspect before retrying")
        print("Verified Payment DB account, table-only SELECT/INSERT grants and preserved row counts.")

    def inspect_credential(self, path):
        metadata = self.read("secret/metadata/" + PREFIX + path, optional=True)
        if metadata is None:
            require(self.mode == "apply", "Missing Vault credential: " + path)
            return None
        document = self.read("secret/data/" + PREFIX + path)
        require(document and document.get("data"), path + ": deleted/unreadable value; refusing replacement")
        validate_fields(path, document["data"])
        return document["data"]

    def write_credential(self, path, planned):
        existing = self.inspect_credential(path)
        if existing is not None:
            return existing
        validate_fields(path, planned)
        self.write("secret/data/" + PREFIX + path,
                   {"options": {"cas": 0}, "data": planned})
        saved = self.read("secret/data/" + PREFIX + path)["data"]
        require(saved == planned, "Credential readback differs: " + path)
        validate_fields(path, saved)
        print("Verified Vault credential fields:", path)
        return saved

    def close(self):
        if not self.session:
            return
        require(re.fullmatch(r"/tmp/pawbridge-payment-runtime\.[A-Za-z0-9]+", self.session),
                "Unsafe cleanup path")
        token = checked(self.kube("-n", "vault", "exec", "vault-0", "--", "sh", "-ec",
                                  'if test -s "$1/.vault-token"; then printf present; else printf absent; fi',
                                  "sh", self.session), "Temporary token inspection")
        require(token in {b"present", b"absent"}, "Unexpected token inspection result")
        if token == b"present":
            checked(self.vault("token", "revoke", "-self"),
                    "Temporary token revocation; do not delete helper before recovery")
        checked(self.kube("-n", "vault", "exec", "vault-0", "--", "rm", "-rf", "--", self.session),
                "Isolated session removal")
        self.session = ""
        print("Isolated temporary Vault session revoked and removed.")


def main():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["validate", "check", "apply"])
    parser.add_argument("--backup-dir", help="existing private /tmp/pawbridge-* directory; required for apply")
    args = parser.parse_args()
    os.umask(0o077)
    bootstrap = PaymentBootstrap(args.mode, args.backup_dir)
    try:
        bootstrap.policy_documents()
        if args.mode == "validate":
            print("Offline policy validation passed. No login, backup or remote change performed.")
            return
        require(args.mode != "apply" or args.backup_dir, "apply requires --backup-dir")
        bootstrap.preflight()
        bootstrap.login()

        mysql_data = bootstrap.inspect_credential("payment/mysql")
        mysql_data = mysql_data or {"mysql-password": secrets.token_hex(32)}
        validate_fields("payment/mysql", mysql_data)
        toss_data = bootstrap.inspect_credential("payment/runtime")
        if toss_data is None:
            toss_data = {"TOSS_SECRET_KEY": prompt_toss_test_secret()}
        validate_fields("payment/runtime", toss_data)

        # All failure-prone schema, count, account, grant, and Toss-format checks
        # are complete before the snapshot and before policy/KV/account writes.
        root, before = bootstrap.database_preflight(mysql_data["mysql-password"])
        if args.mode == "apply":
            # Inherited verbatim private-directory validation and snapshot integrity checks.
            bootstrap.snapshot()
        bootstrap.policies()
        if args.mode == "check":
            print("Payment runtime check passed. No policy, KV, or MySQL mutation performed.")
            return
        mysql_data = bootstrap.write_credential("payment/mysql", mysql_data)
        toss_data = bootstrap.write_credential("payment/runtime", toss_data)
        bootstrap.database_apply(root, before, mysql_data["mysql-password"])
        print("Payment runtime bootstrap complete. No deployment or schema change performed.")
    finally:
        bootstrap.close()


if __name__ == "__main__":
    try:
        main()
    except (Failure, KeyboardInterrupt, EOFError, OSError, ValueError, KeyError, TypeError,
            subprocess.TimeoutExpired) as error:
        print("ERROR: " + (str(error) if isinstance(error, Failure)
                            else "Bootstrap interrupted or invalid response; values suppressed"), file=sys.stderr)
        sys.exit(1)
