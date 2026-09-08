#!/usr/bin/env python3
"""Validate Payment Helm output on stdin (dev/default) and bundled VSO wiring.

Requires PyYAML. Does not contact Kubernetes, Vault, MySQL or Toss.
"""
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[3]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(documents, mode):
    require(mode in {"dev", "default"}, "Unknown render mode")
    deployments = [d for d in documents if d["kind"] == "Deployment"]
    require(len(deployments) == 1, "Expected one Deployment")
    pod = deployments[0]["spec"]["template"]["spec"]
    container = pod["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    require(len(env) == len(container["env"]), "Duplicate environment variables")
    if mode == "default":
        require("nodeSelector" not in pod, "Default placement changed")
        require("SPRING_DATASOURCE_PASSWORD" not in env, "Default acquired runtime secret")
        require(container["envFrom"] == [{"secretRef": {
            "name": "payment-service-secrets", "optional": True}}], "Default secret changed")
        return
    require(len(documents) == 2 and {d["kind"] for d in documents} == {"Service", "Deployment"},
            "Dev must contain only Service and Deployment")
    require(pod["automountServiceAccountToken"] is False, "Unneeded API token mount")
    require(pod["nodeSelector"] == {"kubernetes.io/hostname": "pawbridge-k136-w2"}, "Wrong placement")
    image = yaml.safe_load((ROOT / "environments/dev/values/payment-service.yaml").read_text())["image"]
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"]), "Image digest must be immutable")
    require(container["image"] == image["repository"] + "@" + image["digest"], "Rendered image differs from desired digest")
    expected = {
        "SPRING_DATASOURCE_URL": "jdbc:mysql://mysql.databases.svc.cluster.local:3306/pawbridge_payment",
        "SPRING_DATASOURCE_USERNAME": "pawbridge_payment_app",
        "SPRING_JPA_HIBERNATE_DDL_AUTO": "validate",
        "SPRING_SQL_INIT_MODE": "never",
        "STORE_SERVICE_URL": "http://store-service.pawbridge.svc.cluster.local:8083",
        "MANAGEMENT_ENDPOINT_HEALTH_SHOW_DETAILS": "never",
    }
    for name, value in expected.items():
        require(env.get(name) == {"name": name, "value": value}, "Incorrect setting: " + name)
    require(env["SPRING_DATASOURCE_PASSWORD"] == {
        "name": "SPRING_DATASOURCE_PASSWORD", "valueFrom": {"secretKeyRef": {
            "name": "payment-mysql-auth", "key": "mysql-password"}}}, "Incorrect DB secret")
    require("TOSS_SECRET_KEY" not in env, "Toss key must come only from VSO")
    require(container["envFrom"] == [{"secretRef": {
        "name": "payment-runtime-auth", "optional": False}}], "Incorrect runtime secret")
    require(container["readinessProbe"]["httpGet"] == {
        "path": "/actuator/health/readiness", "port": 8084}, "Unsafe readiness route")
    require(next(d for d in documents if d["kind"] == "Service")["spec"]["type"] == "ClusterIP",
            "Payment must remain internal")


def validate_vso():
    path = ROOT / "gitops/security/payment-runtime-vso/payment.yaml"
    documents = list(yaml.safe_load_all(path.read_text()))
    require(len(documents) == 5, "Unexpected runtime resources")
    require(all(d["metadata"]["namespace"] == "pawbridge" for d in documents), "Wrong namespace")
    auth = next(d["spec"] for d in documents if d["kind"] == "VaultAuth")
    require(auth["kubernetes"] == {"role": "payment-runtime-read", "serviceAccount": "payment-runtime-vault-auth",
        "audiences": ["vault"], "tokenExpirationSeconds": 600}, "Vault auth differs from bootstrap")
    connection = next(d["spec"] for d in documents if d["kind"] == "VaultConnection")
    require(connection["skipTLSVerify"] is False and connection["caCertSecretRef"] == "vault-internal-ca",
            "Vault TLS validation disabled")
    for doc in [d for d in documents if d["kind"] == "VaultStaticSecret"]:
        spec = doc["spec"]
        name = doc["metadata"]["name"]
        suffix, key = {"payment-mysql-auth": ("mysql", "mysql-password"),
                       "payment-runtime-auth": ("runtime", "TOSS_SECRET_KEY")}[name]
        require(spec["path"] == "pawbridge/dev/payment/" + suffix, "Wrong Vault path")
        require(spec["vaultAuthRef"] == "payment-runtime-vault-auth", "Wrong VaultAuth reference")
        require(spec["mount"] == "secret" and spec["type"] == "kv-v2", "Wrong KV mount")
        require(spec["hmacSecretData"] is True, "Secret change detection disabled")
        require(spec["rolloutRestartTargets"] == [{"kind": "Deployment", "name": "payment-service"}],
                "Missing Payment restart target")
        require(spec["destination"] == {"create": True, "overwrite": False, "name": name,
            "transformation": {"excludeRaw": True, "includes": ["^" + key + "$"]}}, "Unbounded secret fields")
    argo = ROOT / "gitops/argocd/payment-runtime-vso"
    app = yaml.safe_load((argo / "application.yaml").read_text())["spec"]
    require(app["source"]["path"] == "gitops/security/payment-runtime-vso", "Wrong Argo path")
    require(app["syncPolicy"] == {"syncOptions": ["Prune=false", "FailOnSharedResource=true"]},
            "Runtime sync must stay manual and non-pruning")
    project = yaml.safe_load((argo / "project.yaml").read_text())["spec"]
    require({r["kind"] for r in project["namespaceResourceWhitelist"]} == {
        "ServiceAccount", "VaultConnection", "VaultAuth", "VaultStaticSecret"}, "Unbounded Argo resource kinds")


if __name__ == "__main__":
    validate([d for d in yaml.safe_load_all(sys.stdin) if d], sys.argv[1])
    validate_vso()
    print("Payment", sys.argv[1], "render and VSO contract PASS")
