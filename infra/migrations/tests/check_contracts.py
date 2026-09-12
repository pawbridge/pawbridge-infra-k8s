#!/usr/bin/env python3
"""Offline Helm rendering and secret-boundary checks. No cluster access or pulls."""
import copy
import json
import subprocess
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[3]
SERVICES = ("animal", "user", "community", "store", "payment")
checks = 0

def require(condition, message):
    global checks
    if not condition:
        raise AssertionError(message)
    checks += 1

def render(service, values=None, namespace="pawbridge", failure=None, dev_values=True):
    result = subprocess.run([
        "docker", "run", "-i", "--rm", "--pull", "never", "--network", "none",
        "--mount", f"type=bind,src={ROOT},dst=/repo,readonly",
        "alpine/helm:3.15.4", "template", f"{service}-service",
        f"/repo/charts/{service}-service", "--namespace", namespace,
        *(["-f", f"/repo/environments/dev/values/{service}-service.yaml"] if dev_values else []),
        "-f", "-"
    ], input=yaml.safe_dump(values or {}), text=True, capture_output=True, timeout=60)
    if failure:
        require(result.returncode != 0 and failure in result.stderr,
                f"{service}: expected rejection: {failure}; got {result.stderr}")
        return []
    require(result.returncode == 0, f"{service}: Helm failed: {result.stderr}")
    return [x for x in yaml.safe_load_all(result.stdout) if x]

def main():
    for service in SERVICES:
        default = render(service, dev_values=False)
        require(not any(x["kind"] == "Job" and x["metadata"]["name"].endswith("-schema-migrate")
                        for x in default), f"{service}: enabled by default")
        release = render(service)
        release_jobs = [x for x in release if x["kind"] == "Job"
                        and x["metadata"]["name"].endswith("-schema-migrate")]
        require(len(release_jobs) == 1, f"{service}: dev migration missing")
        release_values = yaml.safe_load((ROOT / f"environments/dev/values/{service}-service.yaml").read_text())
        migration = release_values["schemaMigration"]
        require(release_jobs[0]["spec"]["template"]["spec"]["containers"][0]["image"] == migration["image"],
                f"{service}: published migration digest mismatch")
        digest = "sha256:" + "a" * 64
        good = {
            "image": {"digest": digest, "tag": "sha-" + "c" * 40},
            "env": {"SPRING_JPA_HIBERNATE_DDL_AUTO": "validate"},
            "schemaMigration": {
                "enabled": True, "image": "example.invalid/migration@" + digest,
                "apiImageDigest": digest, "existingSchemaVerified": True,
                "sourceRevision": "c" * 40,
                "recoveryReference": "synthetic-test-evidence"
            }
        }
        docs = render(service, good)
        jobs = [x for x in docs if x["kind"] == "Job" and x["metadata"]["name"].endswith("-schema-migrate")]
        require(len(jobs) == 1, f"{service}: expected one migration Job")
        job = jobs[0]
        annotations = job["metadata"]["annotations"]
        require(annotations["argocd.argoproj.io/hook"] == "PreSync", "not PreSync")
        require(annotations["pawbridge.kr/source-revision"] == "c" * 40, "source evidence lost")
        require(annotations["argocd.argoproj.io/sync-wave"] == "10", "migration runs before preflight")
        require(annotations["argocd.argoproj.io/hook-delete-policy"] == "HookSucceeded",
                "failed job must remain")
        require(job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 300,
                "retry/deadline changed")
        require("ttlSecondsAfterFinished" not in job["spec"], "failure evidence expires")
        pod = job["spec"]["template"]["spec"]
        require(pod["restartPolicy"] == "Never" and not pod["automountServiceAccountToken"], "unsafe pod")
        container = pod["containers"][0]
        env = {x["name"]: x for x in container["env"]}
        prefix = service.upper() + "_MIGRATION_"
        url = f"jdbc:mysql://mysql.databases.svc.cluster.local:3306/pawbridge_{service}"
        require(env[prefix + "JDBC_URL"]["value"] == url == env[prefix + "CONFIRM_TARGET"]["value"],
                "cross-schema target")
        require(env[prefix + "USERNAME"]["value"] == f"pawbridge_{service}_migrator", "wrong account")
        require(env[prefix + "PASSWORD"]["valueFrom"]["secretKeyRef"] ==
                {"name": f"{service}-schema-migration", "key": "password", "optional": False}, "wrong secret")
        require("envFrom" not in container and container["args"][-1] == "migrate", "broad secret or command")
        for doc in docs:
            if doc["kind"] in ("Deployment", "CronJob"):
                require(f"{service}-schema-migration" not in yaml.safe_dump(doc),
                        "migration credentials leaked to application")
        project_name = "store-pilot" if service == "store" else f"{service}-service-pilot"
        project = yaml.safe_load((ROOT / "gitops/argocd" / project_name / "project.yaml").read_text())
        require({"group": "batch", "kind": "Job"} in project["spec"]["namespaceResourceWhitelist"],
                "Job forbidden by AppProject")
        cases = [
            ("existingSchemaVerified", False, "baseline/equivalence"),
            ("recoveryReference", "", "recovery drill"),
            ("image", "example.invalid/migration:latest", "immutable sha256"),
            ("apiImageDigest", "sha256:" + "b" * 64, "image pair"),
            ("sourceRevision", "", "full commit SHA"),
            ("sourceRevision", "d" * 40, "source revision must match"),
        ]
        for key, value, error in cases:
            bad = copy.deepcopy(good)
            bad["schemaMigration"][key] = value
            render(service, bad, failure=error)
        bad = copy.deepcopy(good)
        bad["env"]["SPRING_JPA_HIBERNATE_DDL_AUTO"] = "update"
        render(service, bad, failure="Hibernate must use validate")
        render(service, good, namespace="wrong-namespace", failure="pawbridge namespace")
        vso = list(yaml.safe_load_all((ROOT / f"gitops/security/schema-migration-vso/{service}.yaml").read_text()))
        auth, secret = vso[1]["spec"], vso[2]["spec"]
        require(auth["kubernetes"]["role"] == f"{service}-schema-migration-read", "wrong Vault role")
        require(secret["path"] == f"pawbridge/dev/{service}/schema-migration", "wrong Vault path")
        require(secret["destination"]["transformation"] == {"excludeRaw": True, "includes": ["^password$"]},
                "broad Vault projection")
        require("rolloutRestartTargets" not in secret, "VSO unexpectedly restarts application")
        role = json.loads((ROOT / f"infra/migrations/vault-roles/{service}.json").read_text())
        require(role["bound_service_account_names"] == [f"{service}-schema-migration-vault"]
                and role["bound_service_account_namespaces"] == ["pawbridge"], "broad Vault binding")
        require(role["audience"] == "vault" and role["token_max_ttl"] == 600, "Vault token boundary")
        require(role["token_policies"] == [f"{service}-schema-migration-read"], "wrong role policy")
        policy = (ROOT / f"infra/vault/policies/{service}-schema-migration-read.hcl").read_text()
        require(policy.count("path ") == 1 and f"secret/data/pawbridge/dev/{service}/schema-migration" in policy
                and 'capabilities = ["read"]' in policy and "*" not in policy, "broad Vault policy")
        sql = (ROOT / f"infra/migrations/accounts/{service}.sql").read_text()
        require("ACCOUNT LOCK;" in sql and "IF NOT EXISTS" not in sql, "unsafe account bootstrap")
        require(f"ON `pawbridge_{service}`.*" in sql and "ON *.*" not in sql, "broad grant")
        print(f"PASS {service}: render, rejection gates, account and Vault boundaries", flush=True)
    app = yaml.safe_load((ROOT / "gitops/argocd/schema-migration-vso/application.yaml").read_text())
    require("automated" not in app["spec"]["syncPolicy"], "secret application must be manual")
    print(f"PASS {checks} contract assertions (Helm 3.15.4); no cluster calls")

if __name__ == "__main__":
    main()
