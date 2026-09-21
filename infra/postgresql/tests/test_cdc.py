"""Offline production CDC connector and restricted RBAC contracts."""
from pathlib import Path
import subprocess
import unittest
import yaml
ROOT = Path(__file__).resolve().parents[3]
SERVICES = ("animal", "user", "community", "store", "payment")
def render(path):
    return list(yaml.safe_load_all(subprocess.check_output(["kubectl", "kustomize", str(ROOT / path)], text=True)))
kustomize = render

class CdcTests(unittest.TestCase):
    def test_cdc_preserves_wire_and_scopes_secret_access(self):
        docs=kustomize("gitops/stateful/postgresql-outbox")
        connectors=[x for x in docs if x["kind"]=="KafkaConnector"]
        self.assertEqual(5,len(connectors))
        topics={x["spec"]["topicName"]:x for x in docs if x["kind"]=="KafkaTopic"}
        self.assertEqual(5,len(topics))
        slots=set()
        for service in SERVICES:
            spec=next(x for x in connectors if x["metadata"]["name"]==service+"-outbox-postgresql-connector")["spec"]
            self.assertEqual("running",spec["state"])
            c=spec["config"]
            self.assertEqual("${secrets:kafka/"+service+"-postgresql-cdc-auth:password}",c["database.password"])
            self.assertEqual("pawbridge",c["database.dbname"])
            self.assertEqual("no_data",c["snapshot.mode"])
            self.assertEqual("disabled",c["publication.autocreate.mode"])
            self.assertEqual("false",c["slot.drop.on.stop"])
            self.assertEqual("none",c["errors.tolerance"])
            self.assertEqual("org.apache.kafka.connect.json.JsonConverter",c["key.converter"])
            self.assertEqual("false",c["key.converter.schemas.enable"])
            self.assertNotIn("transforms.outbox.table.field.event.timestamp",c)
            self.assertNotIn("connector.class",c)
            self.assertEqual("UPDATE pawbridge_"+service+".cdc_heartbeat SET touched_at = clock_timestamp() WHERE id = 1",c["heartbeat.action.query"])
            self.assertIn("cdc_heartbeat",c["table.include.list"])
            self.assertEqual("outbox,dropHeartbeat",c["transforms"])
            self.assertEqual("org.apache.kafka.connect.transforms.Filter",c["transforms.dropHeartbeat.type"])
            topic=topics["__debezium-heartbeat.pawbridge-pg-"+service]
            self.assertEqual(1,topic["spec"]["partitions"])
            self.assertEqual("compact",topic["spec"]["config"]["cleanup.policy"])
            slots.add(c["slot.name"])
        self.assertEqual(5,len(slots))
        role=next(x for x in docs if x["kind"]=="Role")
        self.assertEqual(["get"],role["rules"][0]["verbs"])
        self.assertEqual({s+"-postgresql-cdc-auth" for s in SERVICES},set(role["rules"][0]["resourceNames"]))
        for path in (ROOT/"gitops/argocd").rglob("*.yaml"):
            self.assertNotIn("postgresql-candidate",path.read_text())
            self.assertNotIn("gitops/stateful/postgresql-outbox",path.read_text())

if __name__ == "__main__":
    unittest.main()
