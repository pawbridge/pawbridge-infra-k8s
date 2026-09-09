#!/usr/bin/env python3
"""Verify Helm output on stdin: SERVICE {dev,default}; requires PyYAML."""
import sys,yaml
from pathlib import Path
service,mode=sys.argv[1:3]
docs=[d for d in yaml.safe_load_all(sys.stdin) if d]
deployment=next(d for d in docs if d["kind"]=="Deployment")
pod=deployment["spec"]["template"]["spec"]
container=pod["containers"][0]
env={v["name"]:v for v in container["env"]}
assert len(env)==len(container["env"]), "duplicate env"
expected_kinds={"Deployment","Service"}
if service=="animal-service":
 expected_kinds.add("CronJob")
 if mode=="default":
  expected_kinds.update({"HorizontalPodAutoscaler","ServiceMonitor"})
cronjob=next((d for d in docs if d["kind"]=="CronJob"),None)
assert {d["kind"] for d in docs}==expected_kinds and len(docs)==len(expected_kinds)
if mode=="dev":
 assert pod["automountServiceAccountToken"] is False
 if service=="animal-service":
  assert not pod.get("nodeSelector"), "Animal must not be hard-pinned to w2"
  assert pod["affinity"]=={"nodeAffinity":{
   "requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[
    {"key":"kubernetes.io/hostname","operator":"In","values":["pawbridge-k136-w1","pawbridge-k136-w2"]}
   ]}]},
   "preferredDuringSchedulingIgnoredDuringExecution":[{"weight":100,"preference":{"matchExpressions":[
    {"key":"kubernetes.io/hostname","operator":"In","values":["pawbridge-k136-w2"]}
   ]}}]
  }}, "Animal must allow both workers and prefer w2"
  project=yaml.safe_load((Path(__file__).resolve().parents[3]/"gitops/argocd/animal-service-pilot/project.yaml").read_text())["spec"]
  allowed={(r["group"],r["kind"]) for r in project["namespaceResourceWhitelist"]}
  for document in docs:
   group=document["apiVersion"].split("/")[0] if "/" in document["apiVersion"] else ""
   assert (group,document["kind"]) in allowed, "Rendered resource is not permitted by Animal AppProject"
 else:
  assert pod["nodeSelector"]=={"kubernetes.io/hostname":"pawbridge-k136-w2"}
  assert "affinity" not in pod, "Python placement is outside this change"
 assert "@sha256:" in container["image"]
 assert all(x["secretRef"]["optional"] is False for x in container["envFrom"])
 assert container["volumeMounts"][0]["readOnly"] is True
 refs=[e["valueFrom"]["secretKeyRef"]["name"] for e in env.values() if "valueFrom" in e]
 assert not any("bootstrap" in ref for ref in refs), "privileged bootstrap secret injected"
 if service=="animal-service":
  assert env["SPRING_DATASOURCE_USERNAME"]["value"]=="pawbridge_animal_app"
  assert env["SPRING_DATASOURCE_PASSWORD"]["valueFrom"]["secretKeyRef"]=={"name":"animal-mysql-auth","key":"mysql-password"}
  assert env["SPRING_JPA_HIBERNATE_DDL_AUTO"]["value"]=="validate"
  assert env["SPRING_BATCH_JDBC_INITIALIZE_SCHEMA"]["value"]=="never"
  assert env["SPRING_BATCH_JOB_ENABLED"]["value"]=="false"
  assert env["SPRING_ELASTICSEARCH_URIS"]["value"]=="https://store-search-es-http.databases.svc:9200"
  assert env["SPRING_ELASTICSEARCH_USERNAME"]["valueFrom"]["secretKeyRef"]["name"]=="animal-search-writer-auth"
  assert env["SPRING_ELASTICSEARCH_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]=="password"
  assert env["SPRING_ELASTICSEARCH_RESTCLIENT_SSL_BUNDLE"]["value"]=="animalsearch"
  assert env["SPRING_SSL_BUNDLE_PEM_ANIMALSEARCH_TRUSTSTORE_CERTIFICATE"]["value"]=="/etc/pawbridge/animal-search-ca/ca.crt"
  assert env["PYTHON_AI_SERVICE_INTERNAL_API_KEY"]["valueFrom"]["secretKeyRef"]["name"]=="animal-python-internal-auth"
  assert [x["secretRef"]["name"] for x in container["envFrom"]]==["animal-runtime-auth","animal-r2-auth"]
 else:
  assert env["ES_URL"]["value"]=="https://store-search-es-http.databases.svc:9200"
  assert env["ES_USERNAME"]["valueFrom"]["secretKeyRef"]["name"]=="python-search-writer-auth"
  assert env["ES_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]=="password"
  assert env["ES_CA_CERT_PATH"]["value"]=="/etc/pawbridge/python-search-ca/ca.crt"
  assert env["INTERNAL_API_KEY"]["valueFrom"]["secretKeyRef"]["name"]=="python-internal-auth"
  assert env["LLM_PROVIDER"]["value"]=="gemini"
else:
 assert "nodeSelector" not in pod
 assert "affinity" not in pod
 assert "volumes" not in pod
 assert all("value" in v for v in env.values())
if service=="animal-service":
 assert cronjob is not None
 spec=cronjob["spec"]
 assert spec["schedule"]=="*/30 * * * *"
 assert spec["timeZone"]=="Asia/Seoul"
 assert spec["suspend"] is True
 assert spec["concurrencyPolicy"]=="Forbid"
 assert spec["successfulJobsHistoryLimit"]==3 and spec["failedJobsHistoryLimit"]==3
 job=spec["jobTemplate"]["spec"]
 assert job["activeDeadlineSeconds"]==600 and job["backoffLimit"]==0
 batch_pod=job["template"]["spec"]
 assert batch_pod["restartPolicy"]=="Never"
 assert batch_pod["automountServiceAccountToken"] is False
 assert batch_pod["enableServiceLinks"] is False
 assert batch_pod["securityContext"]=={"runAsNonRoot":True,"runAsUser":100,"runAsGroup":101,"seccompProfile":{"type":"RuntimeDefault"}}
 batch=batch_pod["containers"][0]
 assert batch["image"]=="curlimages/curl:8.5.0" and batch["imagePullPolicy"]=="IfNotPresent"
 assert batch["resources"]=={"requests":{"cpu":"25m","memory":"64Mi"},"limits":{"cpu":"250m","memory":"256Mi"}}
 assert batch["securityContext"]=={"allowPrivilegeEscalation":False,"capabilities":{"drop":["ALL"]},"readOnlyRootFilesystem":True,"runAsNonRoot":True}
 assert batch["args"]==["--fail","--max-time","300","-X","POST","http://animal-service.pawbridge.svc.cluster.local:8081/api/v1/batch/apms/sync"]
print(service,mode,"render contract PASS",len(docs),"resources")
