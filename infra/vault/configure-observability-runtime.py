#!/usr/bin/env python3
"""Register Grafana/Slack Vault credentials, never deploy or send notifications.

Uses the sibling Animal/Python helper only for isolated Vault login, sanitized
transport and token cleanup. No Animal, MySQL, R2 or application method is called.
validate is offline; check only logs in and reads the selected contracts.
apply snapshots before persistent changes, creates absent values with CAS=0,
and refuses to replace existing policies, roles or credentials that differ.
The generated Grafana password is retrieved by the operator from Vault UI.
"""

import argparse
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys


source = Path(__file__).with_name("configure-animal-python-runtime.py")
spec = importlib.util.spec_from_file_location("observability_vault_transport", source)
if spec is None or spec.loader is None:
    raise SystemExit("Required sibling Vault transport helper is unavailable")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)
Failure = transport.Failure
require = transport.require
CONTEXT = transport.CONTEXT
PREFIX = "pawbridge/dev/observability/"
CONTRACTS = {
    "grafana": {"policy": "observability-grafana-read",
                "service_account": "observability-grafana-vault-auth",
                "keys": {"admin-user", "admin-password"}},
    "slack": {"policy": "observability-slack-read",
              "service_account": "observability-slack-vault-auth", "keys": {"url"}},
}


def validate_fields(target, data):
    require(target in CONTRACTS, "Unknown observability target")
    require(isinstance(data, dict) and set(data) == CONTRACTS[target]["keys"],
            target + ": unexpected credential fields")
    for value in data.values():
        require(isinstance(value, str) and value and value.strip() == value
                and all(32 <= ord(c) < 127 for c in value),
                target + ": empty, padded or unsupported credential")
    if target == "grafana":
        require(data["admin-user"] == "pawbridge-admin", "Grafana admin username differs")
        require(re.fullmatch(r"[A-Za-z0-9_-]{43}", data["admin-password"]) is not None,
                "Grafana generated password contract differs; no automatic rotation")
    else:
        require(re.fullmatch(r"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+",
                             data["url"]) is not None,
                "Slack incoming webhook must use the approved HTTPS host and path")


class ObservabilityBootstrap(transport.Bootstrap):
    def __init__(self, mode, backup_dir=None, target="all"):
        super().__init__(mode, backup_dir)
        require(target == "all" or target in CONTRACTS, "Unknown observability target")
        self.targets = tuple(CONTRACTS) if target == "all" else (target,)

    def preflight(self):
        current = transport.checked(transport.execute(["kubectl", "config", "current-context"]),
                                    "Context check").decode().strip()
        require(current == CONTEXT, "Unexpected active context; refusing to proceed")
        pod = transport.parse_json(transport.checked(
            self.kube("-n", "vault", "get", "pod", "vault-0", "-o", "json"), "Vault readiness"))
        require(any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", [])), "Vault Pod not Ready")
        require(sys.stdin.isatty() and sys.stdout.isatty(), "Interactive terminal required")

    def policy_documents(self):
        documents = {}
        for target in self.targets:
            name = CONTRACTS[target]["policy"]
            expected = "\n\n".join(
                'path "secret/' + kind + '/' + PREFIX + target + '" {\n  capabilities = ["read"]\n}'
                for kind in ("data", "metadata")) + "\n"
            actual = (Path(__file__).parent / "policies" / (name + ".hcl")).read_text()
            require(actual.strip() == expected.strip(), "Policy file violates exact read-only path: " + name)
            documents[name] = expected
        return documents

    def policy_changes(self):
        """Validate ALL selected existing contracts before creating any of them."""
        changes = []
        documents = self.policy_documents()
        for target in self.targets:
            contract = CONTRACTS[target]
            name = contract["policy"]
            role = {"bound_service_account_names": [contract["service_account"]],
                    "bound_service_account_namespaces": ["monitoring"], "audience": "vault",
                    "token_policies": [name], "token_ttl": 600, "token_max_ttl": 600,
                    # VSO renews its own token; preserve default self-management rights.
                    "token_no_default_policy": False}
            for path, expected in (("sys/policies/acl/" + name, {"policy": documents[name]}),
                                   ("auth/kubernetes/role/" + name, role)):
                actual = self.read(path, optional=True)
                if actual is None:
                    require(self.mode == "apply", "Missing Vault contract: " + path)
                    changes.append((path, expected))
                else:
                    self.verify_contract(path, actual, expected)
        return changes

    @staticmethod
    def verify_contract(path, actual, expected):
        require(isinstance(actual, dict), "Invalid Vault contract response")
        for key, value in expected.items():
            found = actual.get(key)
            if key == "policy" and isinstance(found, str):
                found, value = found.strip(), value.strip()
            require(found == value, "Existing Vault contract differs; inspect manually: " + path)

    def inspect_credential(self, target):
        metadata = self.read("secret/metadata/" + PREFIX + target, optional=True)
        if metadata is None:
            require(self.mode == "apply", "Missing Vault credential: " + target)
            return None
        document = self.read("secret/data/" + PREFIX + target)
        require(document and document.get("data"), target + ": deleted/unreadable value; refusing replacement")
        validate_fields(target, document["data"])
        return document["data"]

    def require_new_grafana_install(self):
        # A missing Vault key is not permission to replace an existing Grafana DB login.
        existing = transport.checked(self.kube("-n", "monitoring", "get", "secret",
            "monitoring-grafana-admin", "--ignore-not-found", "-o", "name"), "Grafana Secret presence")
        resources = transport.parse_json(transport.checked(self.kube("-n", "monitoring", "get",
            "deployments,pvc", "-l", "app.kubernetes.io/name=grafana", "-o", "json"), "Grafana resource presence"))
        require(not existing.strip() and resources.get("items") == [],
                "Grafana resources already exist without matching Vault data; restore credentials before registration")

    def snapshot(self):
        directory = Path(self.backup_dir).resolve()
        require(directory.is_dir() and str(directory).startswith("/tmp/pawbridge-")
                and directory.stat().st_uid == os.getuid() and directory.stat().st_mode & 0o077 == 0,
                "Backup directory must be operator-owned mode 700 under /tmp/pawbridge-*")
        path = directory / ("vault-pre-observability-" + secrets.token_hex(8) + ".snapshot.gz")
        transport.checked(self.vault("operator", "raft", "snapshot", "save", self.session + "/pre.snapshot"),
                          "Raft snapshot")
        data = transport.checked(self.kube("-n", "vault", "exec", "vault-0", "--", "gzip", "-c",
                                          self.session + "/pre.snapshot"), "Snapshot transfer")
        require(data.startswith(b"\x1f\x8b") and len(data) > 1024
                and len(gzip.decompress(data)) > 1024, "Invalid snapshot envelope or gzip integrity")
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        print("Pre-change Vault snapshot:", path)
        print("SHA-256:", hashlib.sha256(data).hexdigest())
        print("Copy and verify this snapshot off the VM before syncing Kubernetes resources.")

    def register(self):
        changes = self.policy_changes()
        pending = {}
        for target in self.targets:
            if self.inspect_credential(target) is None:
                if target == "grafana":
                    self.require_new_grafana_install()
                data = ({"admin-user": "pawbridge-admin", "admin-password": secrets.token_urlsafe(32)}
                        if target == "grafana" else {"url": transport.prompt_secret("Slack incoming webhook URL")})
                validate_fields(target, data)
                pending[target] = data
        if not changes and not pending:
            print("Selected Vault contracts already match. No persistent change or new snapshot needed.")
            return
        require(self.mode == "apply", "check cannot make persistent changes")
        self.snapshot()
        for path, expected in changes:
            # Recheck absence before a create. Vault policy/role writes have no CAS;
            # run one operator at a time; an unexpected existing contract is never repaired.
            actual = self.read(path, optional=True)
            if actual is None:
                self.write(path, expected)
            self.verify_contract(path, self.read(path), expected)
        for target, data in pending.items():
            self.write("secret/data/" + PREFIX + target, {"options": {"cas": 0}, "data": data})
            saved = self.read("secret/data/" + PREFIX + target)["data"]
            require(saved == data, "Credential readback differs: " + target)
            validate_fields(target, saved)
        print("Observability Vault registration verified. No deployment, restart or Slack send performed.")


def main():
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("validate", "check", "apply"))
    parser.add_argument("--target", choices=("all", "grafana", "slack"), default="all")
    parser.add_argument("--backup-dir", help="private pre-existing /tmp/pawbridge-* directory, required for apply")
    args = parser.parse_args()
    os.umask(0o077)
    bootstrap = ObservabilityBootstrap(args.mode, args.backup_dir, args.target)
    try:
        bootstrap.policy_documents()
        if args.mode == "validate":
            print("Offline observability policy validation passed. No remote access performed.")
            return
        require(args.mode != "apply" or args.backup_dir, "apply requires --backup-dir")
        bootstrap.preflight()
        bootstrap.login()
        bootstrap.register()
    finally:
        bootstrap.close()


if __name__ == "__main__":
    try:
        main()
    except (Failure, KeyboardInterrupt, EOFError, OSError, ValueError, KeyError, TypeError,
            subprocess.TimeoutExpired) as error:
        print("ERROR: " + (str(error) if isinstance(error, Failure)
                            else "Registration interrupted or invalid response; values suppressed"), file=sys.stderr)
        sys.exit(1)
