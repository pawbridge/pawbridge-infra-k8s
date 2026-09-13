"""Check the rendered runtime's isolation and activation boundary."""
import sys
from pathlib import Path
import yaml


def check(path):
    docs = [d for d in yaml.safe_load_all(Path(path).read_text()) if d]
    by_kind = {d["kind"]: d for d in docs}
    assert len(docs) == len(by_kind) == 4
    deployment = by_kind["Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert deployment["spec"]["replicas"] == 0
    assert deployment["spec"]["strategy"]["type"] == "Recreate"
    assert pod["automountServiceAccountToken"] is False
    assert container["resources"]["limits"]["memory"] == "512Mi"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert [e["name"] for e in container["env"]] == ["INTERNAL_API_KEY"]
    auth = by_kind["VaultStaticSecret"]["spec"]
    assert auth["destination"]["name"] == container["env"][0]["valueFrom"]["secretKeyRef"]["name"]
    assert auth["rolloutRestartTargets"] == [{"kind": "Deployment", "name": deployment["metadata"]["name"]}]
    assert auth["destination"]["transformation"]["includes"] == ["^INTERNAL_API_KEY$"]
    assert by_kind["Service"]["spec"]["type"] == "ClusterIP"
    policy = by_kind["NetworkPolicy"]["spec"]
    assert set(policy["policyTypes"]) == {"Ingress", "Egress"} and policy["egress"] == []
    assert policy["ingress"] == [{"from": [{"podSelector": {"matchLabels": {"app": "animal-service"}}}],
                                  "ports": [{"protocol": "TCP", "port": 8000}]}]
    assert policy["podSelector"] == deployment["spec"]["selector"]
    print("Photo render: zero replicas, auth rotation, resource ceiling and isolation PASS")


if __name__ == "__main__":
    check(sys.argv[1])
