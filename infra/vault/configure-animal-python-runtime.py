#!/usr/bin/env python3
"""One interactive, isolated Vault session for the Animal/Python runtime.

Run on the approved control plane with Python 3 (standard library only).
check never changes policies, roles, KV data or MySQL accounts. Both modes
create/revoke an isolated login token. apply saves a fresh Raft snapshot before
any persistent change. Copy the resulting backup off the VM before rollout.
Existing KV values are reused, never rotated. No deployment or ES mutation.
validate checks the bundled policy files offline, without login or backup.
"""

import argparse
import base64
import getpass
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import warnings
from urllib.parse import urlparse

CONTEXT = "pawbridge-vbox-k136"
PREFIX = "pawbridge/dev/"
DB = "pawbridge_animal"
DB_USER = "pawbridge_animal_app"
POLICY_PATHS = {
    "animal": ["animal/mysql", "animal/runtime", "store/r2", "animal-python/internal"],
    "python": ["python/runtime", "animal-python/internal"],
}
BATCH_TABLES = {
    "batch_job_instance", "batch_job_execution", "batch_job_execution_params",
    "batch_step_execution", "batch_step_execution_context", "batch_job_execution_context",
    "batch_step_execution_seq", "batch_job_execution_seq", "batch_job_seq",
}


class Failure(Exception):
    """Only sanitized, operator-actionable messages may cross this boundary."""


def require(condition, message):
    if not condition:
        raise Failure(message)


def prompt_secret(label):
    """Confirm receipt without echoing keys or treating empty paste as failure."""
    while True:
        print(label + ": paste once (Shift+Insert), then press Enter. Ctrl+C cancels.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass(label + " (hidden): ").strip()
        except getpass.GetPassWarning:
            raise Failure("Secure hidden input unavailable; refusing visible input") from None
        if not value:
            print("Received 0 characters. Nothing saved; please paste again.")
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            print("Unsupported control character. Nothing saved; please paste again.")
            continue
        print("Received " + str(len(value)) + " characters (content hidden).")
        print("This confirms input length only, not API key validity.")
        if input("Use this value? Type y and Enter; anything else retries: ").strip().lower() == "y":
            return value
        print("Input discarded. Please paste again.")


def execute(args, *, data=None, timeout=40):
    # Never print CalledProcessError: argv/stdin/stderr may contain credentials.
    try:
        return subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise Failure("Command unavailable or timed out; no automatic retry") from None


def checked(result, label):
    require(result.returncode == 0, label + " failed (raw output suppressed)")
    return result.stdout


def parse_json(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        raise Failure("Unexpected JSON response (content suppressed)") from None


def validate_fields(path, data):
    expected = {
        "animal/mysql": {"mysql-password"},
        "animal/runtime": {"APMS_API_BASE_URL", "APMS_API_SERVICE_KEY", "CHATBOT_IP_HASH_SECRET"},
        "animal-python/internal": {"INTERNAL_API_KEY"},
        "python/runtime": {"GEMINI_API_KEY"},
    }[path]
    require(isinstance(data, dict) and set(data) == expected, path + ": unexpected field contract")
    for key, value in data.items():
        require(isinstance(value, str) and value.strip() == value and value,
                path + ": missing or padded field " + key)
        require(not any(ord(c) < 32 for c in value), path + ": control character in " + key)
        if key in {"mysql-password", "CHATBOT_IP_HASH_SECRET", "INTERNAL_API_KEY"}:
            require(re.fullmatch(r"[0-9a-f]{64}", value) is not None,
                    path + ": generated key contract differs")
    if path == "animal/runtime":
        url = urlparse(data["APMS_API_BASE_URL"])
        require(url.scheme == "https" and url.hostname == "apis.data.go.kr"
                and url.path == "/1543061/abandonmentPublicService_v2"
                and not url.query and not url.fragment and not url.username
                and not url.password and url.port is None,
                "APMS URL must match the approved HTTPS API base")


class Bootstrap:
    def __init__(self, mode, backup_dir=None):
        self.mode = mode
        self.backup_dir = backup_dir
        self.session = ""

    def kube(self, *args, data=None, timeout=40):
        return execute(["kubectl", "--context", CONTEXT, "--request-timeout=15s", *args],
                       data=data, timeout=timeout)

    def vault(self, *args, data=None):
        require(self.session.startswith("/tmp/pawbridge-animal-python."), "No isolated Vault session")
        return self.kube("-n", "vault", "exec", "-i", "vault-0", "--",
                         "env", "HOME=" + self.session, "vault", *args, data=data)

    def read(self, path, *, optional=False):
        result = self.vault("read", "-format=json", path)
        # An authorization/server/network error MUST NOT be mistaken for absence.
        # kubectl exec adds its own exact exit trailer to Vault's stderr.
        missing = ("No value found at " + path + "\n").encode()
        envelopes = (missing, missing + b"command terminated with exit code 2\n")
        if optional and result.returncode == 2 and not result.stdout and result.stderr in envelopes:
            return None
        if result.returncode != 0:
            # Report only fixed categories/codes, never raw remote error text.
            status = re.search(rb"(?m)^Code: ([0-9]{3})\.", result.stderr)
            category = "http-" + status.group(1).decode() if status else "unclassified"
            if b"permission denied" in result.stderr:
                category = "permission-denied"
            elif b"connection refused" in result.stderr:
                category = "connection-refused"
            elif result.stderr in envelopes:
                category = "missing-or-unexpected-envelope"
            raise Failure("Vault read " + path + " failed (exit=" + str(result.returncode)
                          + ", category=" + category + "; raw output suppressed)")
        return parse_json(checked(result, "Vault read " + path))["data"]

    def write(self, path, data):
        checked(self.vault("write", path, "-", data=json.dumps(data).encode()), "Vault write " + path)

    def preflight(self):
        current = checked(execute(["kubectl", "config", "current-context"]), "Context check").decode().strip()
        require(current == CONTEXT, "Unexpected active context; refusing to proceed")
        for namespace, pod in [("vault", "vault-0"), ("databases", "mysql-0")]:
            obj = parse_json(checked(self.kube("-n", namespace, "get", "pod", pod, "-o", "json"), "Pod readiness"))
            require(any(c["type"] == "Ready" and c["status"] == "True"
                        for c in obj.get("status", {}).get("conditions", [])), namespace + " Pod not Ready")
        require(sys.stdin.isatty() and sys.stdout.isatty(), "Interactive terminal required")

    def login(self):
        username = input("Vault admin username: ").strip()
        require(re.fullmatch(r"[A-Za-z0-9._-]+", username), "Unsupported username")
        self.session = checked(self.kube("-n", "vault", "exec", "vault-0", "--", "mktemp", "-d",
                                         "/tmp/pawbridge-animal-python.XXXXXX"), "Session creation").decode().strip()
        require(re.fullmatch(r"/tmp/pawbridge-animal-python\.[A-Za-z0-9]+", self.session), "Unexpected session path")
        # Vault itself reads the password; neither this process nor its argv sees it.
        result = subprocess.run(["kubectl", "--context", CONTEXT, "--request-timeout=15s",
            "-n", "vault", "exec", "-it", "vault-0", "--", "sh", "-c",
            'HOME="$1" vault login -method=userpass username="$2" >/dev/null',
            "sh", self.session, username], timeout=180)
        require(result.returncode == 0, "Vault login failed; check credentials before retrying")
        mount = self.read("sys/mounts/secret")
        require(mount["type"] == "kv" and mount["options"].get("version") == "2", "Existing KV v2 mount required")
        auth = self.read("auth/kubernetes/config")
        require(auth["kubernetes_host"] == "https://kubernetes.default.svc:443"
                and auth["disable_local_ca_jwt"] is False
                and auth["disable_iss_validation"] is True, "Kubernetes auth contract differs; not modifying it")

    def snapshot(self):
        directory = Path(self.backup_dir).resolve()
        require(directory.is_dir() and str(directory).startswith("/tmp/pawbridge-"),
                "Backup directory must already exist under /tmp/pawbridge-*")
        require(directory.stat().st_uid == os.getuid() and directory.stat().st_mode & 0o077 == 0,
                "Backup directory must be owned by the operator and private (mode 700)")
        path = directory / ("vault-pre-animal-python-" + secrets.token_hex(8) + ".snapshot.gz")
        checked(self.vault("operator", "raft", "snapshot", "save", self.session + "/pre.snapshot"), "Raft snapshot")
        data = checked(self.kube("-n", "vault", "exec", "vault-0", "--", "gzip", "-c",
                                 self.session + "/pre.snapshot"), "Snapshot transfer")
        require(data.startswith(b"\x1f\x8b") and len(data) > 1024, "Invalid snapshot envelope")
        require(len(gzip.decompress(data)) > 1024, "Snapshot gzip integrity check failed")
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        print("Pre-change Vault snapshot:", path)
        print("SHA-256:", hashlib.sha256(data).hexdigest())
        print("Copy this snapshot off the VM before application rollout.")

    def credential(self, path):
        metadata = self.read("secret/metadata/" + PREFIX + path, optional=True)
        if metadata is not None:
            document = self.read("secret/data/" + PREFIX + path)
            require(document and document.get("data"), path + ": deleted/unreadable value; refusing replacement")
            data = document["data"]
        else:
            require(self.mode == "apply", "Missing Vault credential: " + path)
            if path == "animal/mysql":
                data = {"mysql-password": secrets.token_hex(32)}
            elif path == "animal-python/internal":
                data = {"INTERNAL_API_KEY": secrets.token_hex(32)}
            elif path == "animal/runtime":
                data = {"APMS_API_BASE_URL": "https://apis.data.go.kr/1543061/abandonmentPublicService_v2",
                        "APMS_API_SERVICE_KEY": prompt_secret("APMS service key"),
                        "CHATBOT_IP_HASH_SECRET": secrets.token_hex(32)}
            else:
                data = {"GEMINI_API_KEY": prompt_secret("Gemini API key")}
            validate_fields(path, data)
            self.write("secret/data/" + PREFIX + path, {"options": {"cas": 0}, "data": data})
            saved = self.read("secret/data/" + PREFIX + path)["data"]
            require(saved == data, "Credential readback differs: " + path)
        validate_fields(path, data)
        print("Verified Vault credential fields:", path)
        return data

    def policy_documents(self):
        documents = {}
        for service, paths in POLICY_PATHS.items():
            name = service + "-runtime-read"
            expected = "\n\n".join('path "secret/' + kind + '/' + PREFIX + path + '" {\n  capabilities = ["read"]\n}'
                                     for path in paths for kind in ["data", "metadata"]) + "\n"
            policy_file = Path(__file__).parent / "policies" / (name + ".hcl")
            require(policy_file.read_text().strip() == expected.strip(),
                    "Policy file violates exact read-only paths: " + name)
            documents[name] = expected
        return documents

    def policies(self):
        # Validate both local files before the first remote read or write.
        for name, expected in self.policy_documents().items():
            service = name.removesuffix("-runtime-read")
            existing = self.read("sys/policies/acl/" + name, optional=True)
            if not existing or existing["policy"].strip() != expected.strip():
                require(self.mode == "apply", "Policy missing or differs: " + name)
                self.write("sys/policies/acl/" + name, {"policy": expected})
            require(self.read("sys/policies/acl/" + name)["policy"].strip() == expected.strip(), "Policy readback failed")
            role = {"bound_service_account_names": [service + "-runtime-vault-auth"],
                    "bound_service_account_namespaces": ["pawbridge"], "audience": "vault",
                    "token_policies": [name], "token_ttl": 600, "token_max_ttl": 600}
            role_path = "auth/kubernetes/role/" + name
            existing = self.read(role_path, optional=True)
            if not existing or any(existing.get(k) != v for k, v in role.items()):
                require(self.mode == "apply", "Role missing or differs: " + name)
                self.write(role_path, role)
            actual = self.read(role_path)
            require(all(actual.get(k) == v for k, v in role.items()), "Role readback failed")
            print("Verified least-privilege policy and role:", name)

    def mysql(self, username, password, sql, database=DB):
        require(password and "\n" not in password and "\r" not in password, "Invalid MySQL password envelope")
        # Password and SQL are stdin only, never process arguments or diagnostic output.
        return checked(self.kube("-n", "databases", "exec", "-i", "mysql-0", "--", "sh", "-ec",
            'IFS= read -r MYSQL_PWD; export MYSQL_PWD; export MYSQL_HISTFILE=/dev/null; '
            'exec mysql --protocol=TCP --host=127.0.0.1 --user="$1" --batch --skip-column-names --raw "$2"',
            "sh", username, database, data=(password + "\n" + sql + "\n").encode()), "MySQL operation").decode().strip()

    def database(self, app_password):
        encoded = checked(self.kube("-n", "databases", "get", "secret", "mysql-auth",
                          "-o", "jsonpath={.data.mysql-root-password}"), "MySQL root credential")
        root = base64.b64decode(encoded, validate=True).decode()
        tables = set(self.mysql("root", root, "SELECT LOWER(TABLE_NAME) FROM information_schema.tables "
                    "WHERE TABLE_SCHEMA='pawbridge_animal';").splitlines())
        require({"animals"} | BATCH_TABLES <= tables, "Restored Animal or Batch tables missing; no schema creation allowed")
        before = self.mysql("root", root, "SELECT COUNT(*) FROM animals;")
        require(before.isdigit() and int(before) > 0, "Historical animals must be present before bootstrap")
        # Never rotate an existing account or remove grants. Only resume an
        # interrupted create when login succeeds and no unexpected grants exist.
        exists = self.mysql("root", root, "SELECT COUNT(*) FROM mysql.user WHERE User='pawbridge_animal_app' AND Host='%';")
        require(exists in {"0", "1"}, "Unexpected MySQL account count")
        if exists == "0":
            require(self.mode == "apply", "Dedicated Animal MySQL account is missing")
            self.mysql("root", root, "CREATE USER 'pawbridge_animal_app'@'%' IDENTIFIED BY '" + app_password + "';")
        grants = self.mysql("root", root, "SHOW GRANTS FOR 'pawbridge_animal_app'@'%';").splitlines()
        expected = {"GRANT USAGE ON *.* TO `pawbridge_animal_app`@`%`",
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON `pawbridge_animal`.* TO `pawbridge_animal_app`@`%`"}
        require(set(grants) <= expected, "Unexpected DB privileges; no destructive automatic grant repair")
        if set(grants) != expected:
            require(self.mode == "apply", "Animal DB grant is incomplete")
            # information_schema is readable without an application DB grant.
            require(self.mysql(DB_USER, app_password, "SELECT 1;", "information_schema") == "1",
                    "Existing account password differs; refusing grant changes")
            self.mysql("root", root, "GRANT SELECT, INSERT, UPDATE, DELETE ON `pawbridge_animal`.* TO 'pawbridge_animal_app'@'%';")
        require(set(self.mysql("root", root, "SHOW GRANTS FOR 'pawbridge_animal_app'@'%';").splitlines()) == expected,
                "DB grant readback differs")
        require(self.mysql(DB_USER, app_password, "SELECT COUNT(*) FROM animals;") == before,
                "App login/count mismatch; inspect before retrying")
        print("Verified Animal DB account, CRUD-only grants and preserved row count:", before)

    def close(self):
        if not self.session:
            return
        require(re.fullmatch(r"/tmp/pawbridge-animal-python\.[A-Za-z0-9]+", self.session), "Unsafe cleanup path")
        token = checked(self.kube("-n", "vault", "exec", "vault-0", "--", "sh", "-ec",
            'if test -s "$1/.vault-token"; then printf present; else printf absent; fi',
            "sh", self.session), "Temporary token inspection")
        require(token in {b"present", b"absent"}, "Unexpected token inspection result")
        if token == b"present":
            checked(self.vault("token", "revoke", "-self"), "Temporary token revocation; do not delete helper before recovery")
        checked(self.kube("-n", "vault", "exec", "vault-0", "--", "rm", "-rf", "--", self.session), "Isolated session removal")
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
    bootstrap = Bootstrap(args.mode, args.backup_dir)
    try:
        bootstrap.policy_documents()
        if args.mode == "validate":
            print("Offline policy validation passed. No login, backup or remote change performed.")
            return
        require(args.mode != "apply" or args.backup_dir, "apply requires --backup-dir")
        bootstrap.preflight()
        bootstrap.login()
        # Existing shared R2 must already be present; this command never reads or rewrites its values.
        require(bootstrap.read("secret/metadata/" + PREFIX + "store/r2"), "Existing shared R2 is required")
        if args.mode == "apply":
            bootstrap.snapshot()
        bootstrap.policies()
        password = bootstrap.credential("animal/mysql")["mysql-password"]
        bootstrap.database(password)
        for path in ["animal-python/internal", "animal/runtime", "python/runtime"]:
            bootstrap.credential(path)
        print("Animal/Python runtime bootstrap complete. No deployment, batch or Elasticsearch change performed.")
    finally:
        bootstrap.close()


if __name__ == "__main__":
    try:
        main()
    except (Failure, KeyboardInterrupt, EOFError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        # Never include unexpected exception repr: it can contain captured credential data.
        print("ERROR: " + (str(error) if isinstance(error, Failure) else "Bootstrap interrupted or invalid response; values suppressed"), file=sys.stderr)
        sys.exit(1)
