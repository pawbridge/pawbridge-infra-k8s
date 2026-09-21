"""Offline Vault rendering; no secret values or cluster calls."""
from pathlib import Path
import subprocess
import unittest
import yaml
ROOT = Path(__file__).resolve().parents[3]
SERVICES = ("animal", "user", "community", "store", "payment")
def render(path):
    return list(yaml.safe_load_all(subprocess.check_output(["kubectl", "kustomize", str(ROOT / path)], text=True)))
kustomize = render

class VaultTests(unittest.TestCase):
    def test_vault_only_exports_admin_key_without_automatic_restart(self):
        resources = render("gitops/security/postgresql-admin-vso")
        secret = next(x for x in resources if x["kind"] == "VaultStaticSecret")["spec"]
        self.assertEqual("pawbridge/dev/postgresql/admin", secret["path"])
        self.assertNotIn("rolloutRestartTargets", secret)
        self.assertFalse(secret["destination"]["overwrite"])
        self.assertEqual({"excludeRaw": True, "includes": ["^postgres-password$"]}, secret["destination"]["transformation"])
        connection = next(x for x in resources if x["kind"] == "VaultConnection")["spec"]
        self.assertFalse(connection["skipTLSVerify"])
        auth = next(x for x in resources if x["kind"] == "VaultAuth")
        self.assertEqual("databases", auth["metadata"]["namespace"])
        self.assertEqual("postgresql-admin-read", auth["spec"]["kubernetes"]["role"])

    def test_vault_service_and_cdc_credentials_are_separate(self):
        identities=set()
        for category,namespace,suffix,keys in [("runtime","pawbridge","postgresql",["^postgres-password$"]),("cdc","kafka","postgresql-cdc",["^username$","^password$"])]:
            docs=kustomize("gitops/security/postgresql-"+category+"-vso")
            secrets=[x for x in docs if x["kind"]=="VaultStaticSecret"]
            self.assertEqual(5,len(secrets))
            for service in SERVICES:
                spec=next(x for x in secrets if x["metadata"]["name"]==service+"-"+suffix+"-auth")["spec"]
                self.assertEqual("pawbridge/dev/"+service+"/"+suffix,spec["path"])
                self.assertEqual({"excludeRaw":True,"includes":keys},spec["destination"]["transformation"])
                self.assertFalse(spec["destination"]["overwrite"])
                self.assertNotIn("rolloutRestartTargets",spec)
                auth=next(x for x in docs if x["kind"]=="VaultAuth" and x["metadata"]["name"]==spec["vaultAuthRef"])
                self.assertEqual(namespace,auth["metadata"]["namespace"])
                identities.add(auth["spec"]["kubernetes"]["role"])
        self.assertEqual(10,len(identities))

if __name__ == "__main__":
    unittest.main()
