#!/usr/bin/env python3
"""Offline contract checks for the resumed dev APMS and MySQL outbox rollout."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
CONNECTOR_PATHS = {
    "user-outbox-connector": ROOT / "gitops/stateful/user-outbox-source/connector.yaml",
    "animal-outbox-connector": ROOT / "gitops/stateful/animal-outbox-source/connector.yaml",
    "community-outbox-connector": ROOT / "gitops/stateful/community-outbox-source/connector.yaml",
    "payment-outbox-connector": ROOT / "gitops/stateful/payment-outbox-source/connector.yaml",
}
SECRET_REF = "${secrets:kafka/store-mysql-cdc-auth:"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def load(path):
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def verify_connectors():
    expected = {
        "user-outbox-connector": {
            "database.server.id": 2001,
            "topic.prefix": "user",
            "database.include.list": "pawbridge_user",
            "table.include.list": "pawbridge_user.outbox_events",
            "schema.history.internal.kafka.topic": "schema-changes.user.outbox",
            "transforms.outbox.table.field.event.id": "event_id",
            "transforms.outbox.table.fields.additional.placement": "event_type:header:eventType",
            "transforms.outbox.route.by.field": "topic",
            "transforms.outbox.route.topic.replacement": "${routedByValue}",
        },
        "animal-outbox-connector": {
            "database.server.id": 2002,
            "topic.prefix": "pawbridge-animal",
            "database.include.list": "pawbridge_animal",
            "table.include.list": "pawbridge_animal.outbox_events",
            "schema.history.internal.kafka.topic": "schema-changes.animal.outbox",
            "transforms.outbox.table.field.event.id": "event_id",
            "transforms.outbox.table.fields.additional.placement": "event_type:header:eventType",
            "transforms.outbox.route.by.field": "topic",
            "transforms.outbox.route.topic.replacement": "${routedByValue}",
        },
        "community-outbox-connector": {
            "database.server.id": 2003,
            "topic.prefix": "pawbridge-community",
            "database.include.list": "pawbridge_community",
            "table.include.list": "pawbridge_community.outbox_events",
            "schema.history.internal.kafka.topic": "schema-changes.community.outbox",
            "transforms.outbox.table.field.event.id": "event_id",
            "transforms.outbox.table.fields.additional.placement": "type:header:eventType",
            "transforms.outbox.route.by.field": "aggregate_type",
            "transforms.outbox.route.topic.replacement": "community.post.events",
        },
        "payment-outbox-connector": {
            "database.server.id": 2005,
            "topic.prefix": "payment",
            "database.include.list": "pawbridge_payment",
            "table.include.list": "pawbridge_payment.outbox",
            "schema.history.internal.kafka.topic": "schema-changes.payment",
            "transforms.outbox.table.field.event.id": "id",
            "transforms.outbox.table.fields.additional.placement": "event_type:header:eventType",
            "transforms.outbox.route.by.field": "aggregate_type",
            "transforms.outbox.route.topic.replacement": "payment.events",
        },
    }
    ids = set()
    expected_states = {"community-outbox-connector": "stopped"}
    for name, connector_path in CONNECTOR_PATHS.items():
        documents = load(connector_path)
        require(len(documents) == 1 and documents[0]["kind"] == "KafkaConnector",
                "Expected one connector source: " + name)
        require(yaml.safe_load((connector_path.parent / "kustomization.yaml").read_text())["resources"] ==
                ["connector.yaml"], "Wrong source Kustomization: " + name)
        connector = documents[0]
        config = connector["spec"]["config"]
        require(connector["metadata"]["name"] == name, "Wrong connector name")
        require(connector["metadata"]["namespace"] == "kafka", "Wrong connector namespace")
        require(connector["metadata"]["labels"] == {
            "strimzi.io/cluster": "pawbridge-connect",
            "app.kubernetes.io/name": name,
            "app.kubernetes.io/part-of": "pawbridge",
        }, "Wrong connector labels: " + name)
        require(connector["spec"]["class"] == "io.debezium.connector.mysql.MySqlConnector" and
                connector["spec"]["tasksMax"] == 1 and
                connector["spec"]["state"] == expected_states.get(name, "running"),
                "Wrong connector execution contract: " + name)
        require(config["database.hostname"] == "mysql.databases.svc.cluster.local" and
                config["database.port"] == 3306 and
                config["database.connectionTimeZone"] == "Asia/Seoul" and
                config["snapshot.mode"] == "no_data", "Wrong CDC source contract: " + name)
        require(config["database.user"] == SECRET_REF + "username}" and
                config["database.password"] == SECRET_REF + "password}",
                "Connector must use only the existing secret provider: " + name)
        require(config["schema.history.internal.kafka.bootstrap.servers"] ==
                "pawbridge-kafka-bootstrap.kafka.svc.cluster.local:9092", "Wrong history bootstrap: " + name)
        require(config["transforms"] == "outbox" and
                config["transforms.outbox.type"] == "io.debezium.transforms.outbox.EventRouter" and
                config["transforms.outbox.table.field.event.key"] == "aggregate_id" and
                config["transforms.outbox.table.field.event.timestamp"] == "created_at" and
                config["transforms.outbox.table.field.event.payload"] == "payload" and
                config["transforms.outbox.table.expand.json.payload"] is True,
                "Wrong EventRouter contract: " + name)
        require(config["key.converter"] == "org.apache.kafka.connect.json.JsonConverter" and
                config["value.converter"] == "org.apache.kafka.connect.json.JsonConverter" and
                config["key.converter.schemas.enable"] is False and
                config["value.converter.schemas.enable"] is False, "Wrong JSON converter: " + name)
        for field, value in expected[name].items():
            require(config[field] == value, "Wrong %s for %s" % (field, name))
        ids.add(config["database.server.id"])
    require(ids == {2001, 2002, 2003, 2005} and 2004 not in ids, "Connector server IDs must be unique from Store")
    for connector_path in CONNECTOR_PATHS.values():
        source = connector_path.read_text().lower()
        require("root" not in source and not any(line.lstrip().startswith(("data:", "stringdata:"))
                                             for line in source.splitlines()),
                "Plaintext or public secret material leaked: " + str(connector_path))
    community = yaml.safe_load(CONNECTOR_PATHS["community-outbox-connector"].read_text())
    require(community["spec"]["state"] == "stopped", "Community source must remain held for its consumer fix")

    histories = {topic["metadata"]["name"] for topic in load(ROOT / "gitops/stateful/kafka/topics-schema-history.yaml")}
    require({"schema-changes.user.outbox", "schema-changes.animal.outbox", "schema-changes.community.outbox",
             "schema-changes.payment"} <= histories, "Missing declared schema-history topic")
    secret_role = next(doc for doc in load(ROOT / "gitops/stateful/kafka-connect/secret-provider-rbac.yaml")
                       if doc["kind"] == "Role")
    require(secret_role["rules"] == [{"apiGroups": [""], "resources": ["secrets"],
                                      "resourceNames": ["store-search-writer-auth", "store-mysql-cdc-auth"],
                                      "verbs": ["get"]}], "Secret-reader scope changed")


def verify_argo():
    directory = ROOT / "gitops/argocd/outbox-connectors"
    require(yaml.safe_load((directory / "kustomization.yaml").read_text())["resources"] ==
            ["project.yaml", "application.yaml", "animal-application.yaml", "community-application.yaml",
             "payment-application.yaml"], "Outbox Argo resources changed")
    project = yaml.safe_load((directory / "project.yaml").read_text())["spec"]
    require(project["destinations"] == [{"server": "https://kubernetes.default.svc", "namespace": "kafka"}] and
            project["namespaceResourceWhitelist"] == [{"group": "kafka.strimzi.io", "kind": "KafkaConnector"}],
            "Outbox Argo project exceeds connector scope")
    applications = {
        "application.yaml": ("user-outbox-source", "gitops/stateful/user-outbox-source"),
        "animal-application.yaml": ("animal-outbox-source", "gitops/stateful/animal-outbox-source"),
        "community-application.yaml": ("community-outbox-source", "gitops/stateful/community-outbox-source"),
        "payment-application.yaml": ("payment-outbox-source", "gitops/stateful/payment-outbox-source"),
    }
    for filename, (name, path) in applications.items():
        document = yaml.safe_load((directory / filename).read_text())
        app = document["spec"]
        require(document["metadata"] == {"name": name, "namespace": "argocd"} and
                app["project"] == "pawbridge-outbox-connectors" and app["source"]["path"] == path and
                app["destination"] == {"server": "https://kubernetes.default.svc", "namespace": "kafka"} and
                app["syncPolicy"] == {"syncOptions": ["Prune=false", "FailOnSharedResource=true"]},
                "Outbox application must remain independently manual and non-pruning: " + name)


def verify_roles_and_timezones():
    roles = yaml.safe_load((ROOT / "gitops/stateful/elasticsearch/roles.yml").read_text())
    require(roles["pawbridge_store_reader"] == {"cluster": ["monitor"], "indices": [{
        "names": ["store-products-read"], "privileges": ["read", "view_index_metadata"]}]},
        "Store reader rights changed beyond monitor")
    require(roles["pawbridge_community_writer"] == {"cluster": ["monitor"], "indices": [{
        "names": ["posts"], "privileges": ["create_index", "manage", "read", "write", "view_index_metadata"]}]},
        "Community writer rights changed beyond monitor")
    require(roles["pawbridge_animal_writer"] == {"cluster": ["monitor"], "indices": [{
        "names": ["animals"], "privileges": ["read", "write", "view_index_metadata"]}]},
        "Animal writer rights changed beyond monitor")
    timezone_services = set()
    for service in ("animal-service", "user-service", "community-service", "store-service", "payment-service"):
        env = yaml.safe_load((ROOT / "environments/dev/values" / (service + ".yaml")).read_text())["env"]
        if "TZ" in env or "JAVA_TOOL_OPTIONS" in env:
            require(env.get("TZ") == "Asia/Seoul" and env.get("JAVA_TOOL_OPTIONS") == "-Duser.timezone=Asia/Seoul",
                    "Incomplete KST setting: " + service)
            timezone_services.add(service)
    require(timezone_services == {"animal-service", "user-service", "community-service", "store-service"},
            "Timezone must be limited to Animal, User, Community, and Store")


def verify_apms_cronjob():
    project = yaml.safe_load((ROOT / "gitops/argocd/animal-service-pilot/project.yaml").read_text())["spec"]
    require(project["destinations"] == [{"server": "https://kubernetes.default.svc", "namespace": "pawbridge"}],
            "Animal project destination must remain scoped to pawbridge")
    require(project["namespaceResourceWhitelist"] == [
        {"group": "apps", "kind": "Deployment"},
        {"group": "", "kind": "Service"},
        {"group": "batch", "kind": "CronJob"},
    ], "Animal project must permit its CronJob without widening other resource permissions")
    defaults = yaml.safe_load((ROOT / "charts/animal-service/values.yaml").read_text())["cronjob"]
    dev = yaml.safe_load((ROOT / "environments/dev/values/animal-service.yaml").read_text())
    require(defaults["enabled"] is True and defaults["suspend"] is True and
            defaults["timeZone"] == "Asia/Seoul" and defaults["imagePullPolicy"] == "IfNotPresent",
            "Default APMS CronJob must be declared and held")
    require(defaults["resources"] == {"requests": {"cpu": "25m", "memory": "64Mi"},
                                      "limits": {"cpu": "250m", "memory": "256Mi"}},
            "APMS CronJob resource defaults changed")
    require(dev["cronjob"] == {"enabled": True, "suspend": False, "timeZone": "Asia/Seoul"},
            "Dev APMS CronJob must be explicitly resumed")
    require(dev["env"]["SPRING_BATCH_JOB_ENABLED"] == "false", "Batch jobs must remain disabled at startup")
    template = (ROOT / "charts/animal-service/templates/cronjob.yaml").read_text()
    for expected in ("timeZone: {{ .Values.cronjob.timeZone | quote }}", "suspend: {{ .Values.cronjob.suspend }}",
                     "activeDeadlineSeconds: 600", "backoffLimit: 0", "restartPolicy: Never",
                     "automountServiceAccountToken: false", "enableServiceLinks: false",
                     "runAsUser: 100", "runAsGroup: 101",
                     "readOnlyRootFilesystem: true", "--max-time", '"300"'):
        require(expected in template, "Missing APMS CronJob guard: " + expected)


if __name__ == "__main__":
    verify_connectors()
    verify_argo()
    verify_roles_and_timezones()
    verify_apms_cronjob()
    print("Operational recovery contracts PASS")
