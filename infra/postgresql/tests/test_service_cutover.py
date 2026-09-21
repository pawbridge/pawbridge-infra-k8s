"""Real Helm rendering and cross-resource credential/CDC contracts; no cluster calls."""
import json
import os
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[3]
SERVICES=("animal","user","community","store","payment")
DIGEST="sha256:"+"1"*64  # render-only fixture, never deploy

def render(service, *, candidate=True, production=False, changes=(), digest=True):
    args=[os.getenv("HELM_BIN","helm"),"template",service+"-service","charts/"+service+"-service","--namespace","pawbridge","-f","environments/dev/values/"+service+"-service.yaml"]
    if candidate:
        args += ["-f","environments/postgresql-candidate/values/"+service+"-service.yaml"]
        if digest: args += ["--set-string","image.digest="+DIGEST]
    if production:
        args += ["-f","environments/postgresql/values/"+service+"-service.yaml"]
    for change in changes: args += ["--set",change]
    return subprocess.run(args,cwd=ROOT,capture_output=True,text=True)


class ServiceCutoverTests(unittest.TestCase):
    def test_five_service_credentials_profiles_and_connection_budget(self):
        total=0
        for service in SERVICES:
            with self.subTest(service=service):
                result=render(service)
                self.assertEqual(0,result.returncode,result.stderr)
                docs=[x for x in yaml.safe_load_all(result.stdout) if x]
                deploy=next(x for x in docs if x["kind"]=="Deployment")
                self.assertEqual(0,deploy["spec"]["replicas"])
                container=deploy["spec"]["template"]["spec"]["containers"][0]
                entries=container["env"];env={x["name"]:x for x in entries}
                self.assertEqual(len(entries),len(env),"Duplicate env overrides")
                self.assertEqual("dev,postgresql",env["SPRING_PROFILES_ACTIVE"]["value"])
                self.assertEqual("org.postgresql.Driver",env["SPRING_DATASOURCE_DRIVER_CLASS_NAME"]["value"])
                self.assertEqual("org.hibernate.dialect.PostgreSQLDialect",env["SPRING_JPA_PROPERTIES_HIBERNATE_DIALECT"]["value"])
                self.assertEqual("validate",env["SPRING_JPA_HIBERNATE_DDL_AUTO"]["value"])
                self.assertEqual("pawbridge_"+service,env["SPRING_DATASOURCE_HIKARI_SCHEMA"]["value"])
                self.assertEqual({"name":service+"-postgresql-auth","key":"postgres-password"},env["SPRING_DATASOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"])
                self.assertNotIn("mysql",json.dumps(deploy).lower())
                self.assertNotIn("SPRING_ELASTICSEARCH_URIS",env)
                self.assertEqual("SET TIME ZONE 'UTC'",env["SPRING_DATASOURCE_HIKARI_CONNECTIONINITSQL"]["value"])
                self.assertNotIn("SPRING_DATASOURCE_HIKARI_CONNECTION_INIT_SQL",env)
                self.assertFalse(any(x["kind"] in ("HorizontalPodAutoscaler","CronJob","Job") for x in docs))
                self.assertTrue(container["image"].endswith("@"+DIGEST))
                total+=int(env["SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE"]["value"])
        self.assertEqual(30,total)
        self.assertEqual(94,2*total+4+20+10)

    def test_mixed_or_unbounded_candidate_is_rejected(self):
        changes=[("env.SPRING_DATASOURCE_HIKARI_CONNECTION_INIT_SQL=SELECT 1","canonical"),
                 ("mysqlSecretRef=old-auth","mutually exclusive"),
                 ("env.SPRING_DATASOURCE_URL=jdbc:mysql://wrong/db","JDBC URL"),
                 ("env.SPRING_DATASOURCE_DRIVER_CLASS_NAME=com.mysql.cj.jdbc.Driver","driver"),
                 ("env.SPRING_JPA_PROPERTIES_HIBERNATE_DIALECT=org.hibernate.dialect.MySQL8Dialect","dialect"),
                 ("env.SPRING_JPA_HIBERNATE_DDL_AUTO=update","DDL"),
                 ("env.SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE=30","budget"),
                 ("autoscaling.enabled=true","HPA"),
                 ("replicaCount=2","steady replica"),
                 ("env.SPRING_DATASOURCE_HIKARI_MINIMUMIDLE=31","minimum idle"),
                 ("env.SPRING_DATASOURCE_PASSWORD=fixture","selected Secret"),
                 ("elasticsearchSecretRef=old-es","Elasticsearch")]
        for service in SERVICES:
            for change,message in changes:
                with self.subTest(service=service,change=change):
                    result=render(service,changes=(change,))
                    self.assertNotEqual(0,result.returncode)
                    self.assertIn(message,result.stderr)
            with self.subTest(service=service,digest="missing"):
                result=render(service,digest=False)
                self.assertNotEqual(0,result.returncode)
                self.assertIn("image digest",result.stderr)

    def test_production_overlays_activate_postgresql_and_preserve_background_features(self):
        for service in (*SERVICES,"python-ai"):
            with self.subTest(service=service):
                result=render(service,candidate=False,production=True)
                self.assertEqual(0,result.returncode,result.stderr)
                docs=[x for x in yaml.safe_load_all(result.stdout) if x]
                deployment=next(x for x in docs if x["kind"]=="Deployment")
                self.assertEqual(1,deployment["spec"]["replicas"])
                env={x["name"]:x for x in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
                if service=="python-ai":
                    self.assertEqual("postgresql",env["LOST_STORAGE_BACKEND"]["value"])
                    self.assertNotIn("ES_PASSWORD",env)
                else:
                    self.assertTrue(env["SPRING_DATASOURCE_URL"]["value"].startswith("jdbc:postgresql:"))
                    self.assertNotIn("SPRING_DATASOURCE_HIKARI_CONNECTION_INIT_SQL",env)
                self.assertFalse(any(x["kind"]=="HorizontalPodAutoscaler" for x in docs))
                if service=="animal":
                    self.assertEqual("true",env["APMS_PHOTO_ARCHIVE_ENABLED"]["value"])
                    self.assertEqual("true",env["LOST_GALLERY_FEED_ENABLED"]["value"])
                    self.assertFalse(next(x for x in docs if x["kind"]=="CronJob")["spec"]["suspend"])

    def test_store_gitops_cannot_restore_hpa_or_ignore_replica_budget(self):
        app=yaml.safe_load((ROOT/"gitops/argocd/store-pilot/application.yaml").read_text())
        helm=app["spec"]["source"]["helm"]
        parameters={p["name"]:p["value"] for p in helm["parameters"]}
        self.assertEqual("false",parameters["autoscaling.enabled"])
        self.assertIn("../../environments/postgresql/values/store-service.yaml",helm["valueFiles"])
        self.assertFalse(any("/spec/replicas" in rule.get("jsonPointers",[]) for rule in app["spec"].get("ignoreDifferences",[])))

    def test_default_mysql_render_is_preserved(self):
        for service in SERVICES:
            with self.subTest(service=service):
                result=render(service,candidate=False)
                self.assertEqual(0,result.returncode,result.stderr)
                docs=[x for x in yaml.safe_load_all(result.stdout) if x]
                deploy=next(x for x in docs if x["kind"]=="Deployment")
                env={x["name"]:x for x in deploy["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertTrue(env["SPRING_DATASOURCE_URL"]["value"].startswith("jdbc:mysql://"))
                self.assertEqual(service+"-mysql-auth",env["SPRING_DATASOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"])

if __name__ == "__main__":
    unittest.main()
