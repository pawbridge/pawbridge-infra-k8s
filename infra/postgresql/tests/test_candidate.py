"""Rendered safety contracts; kubectl kustomize is entirely local."""
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[3]

def render(path):
    return list(yaml.safe_load_all(subprocess.check_output(["kubectl", "kustomize", str(ROOT / path)], text=True)))


class CandidateTests(unittest.TestCase):
    def test_database_is_active_retained_and_resource_bounded(self):
        resources = render("gitops/stateful/postgresql")
        stateful = next(x for x in resources if x["kind"] == "StatefulSet")
        spec = stateful["spec"]
        self.assertEqual(1, spec["replicas"])
        self.assertEqual({"whenDeleted": "Retain", "whenScaled": "Retain"}, spec["persistentVolumeClaimRetentionPolicy"])
        self.assertEqual("OnDelete", spec["updateStrategy"]["type"])
        pod = spec["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(999, pod["securityContext"]["runAsUser"])
        container = pod["containers"][0]
        self.assertEqual({"cpu": "2", "memory": "2Gi"}, container["resources"]["limits"])
        self.assertRegex(container["image"], r"^pgvector/pgvector@sha256:[a-f0-9]{64}$")
        self.assertEqual("pawbridge-postgresql-admin-auth", next(v for v in pod["volumes"] if v["name"] == "auth")["secret"]["secretName"])
        config = next(x for x in resources if x["kind"] == "ConfigMap")
        self.assertEqual(config["metadata"]["name"], next(v for v in pod["volumes"] if v["name"] == "config")["configMap"]["name"])
        self.assertIn("max_slot_wal_keep_size = '2GB'", config["data"]["postgresql.conf"])
        self.assertEqual("128Mi", next(v for v in pod["volumes"] if v["name"] == "dshm")["emptyDir"]["sizeLimit"])
        self.assertNotIn("volumes", spec["volumeClaimTemplates"][0]["spec"])
        service = next(x for x in resources if x["kind"] == "Service" and x["metadata"]["name"] == "pawbridge-postgresql")
        self.assertEqual(spec["selector"]["matchLabels"], service["spec"]["selector"])
        self.assertEqual("ClusterIP", service["spec"]["type"])
        self.assertEqual(5432, service["spec"]["ports"][0]["port"])
        # Database lifecycle remains manual; application GitOps must not implicitly own it.
        for path in (ROOT / "gitops/argocd").rglob("*.yaml"):
            self.assertNotIn("gitops/stateful/postgresql", path.read_text())

if __name__ == "__main__":
    unittest.main()
