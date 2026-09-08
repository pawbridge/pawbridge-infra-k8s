#!/usr/bin/env python3
"""Verify Helm output on stdin: SERVICE {dev,default}; requires PyYAML."""
import sys,yaml
service,mode=sys.argv[1:3]
docs=[d for d in yaml.safe_load_all(sys.stdin) if d]
deployment=next(d for d in docs if d["kind"]=="Deployment")
pod=deployment["spec"]["template"]["spec"]
container=pod["containers"][0]
env={v["name"]:v for v in container["env"]}
assert len(env)==len(container["env"]), "duplicate env"
if mode=="dev":
 assert pod["automountServiceAccountToken"] is False
 assert pod["nodeSelector"]=={"kubernetes.io/hostname":"pawbridge-k136-w2"}
 assert "@sha256:" in container["image"]
 assert {d["kind"] for d in docs}=={"Deployment","Service"} and len(docs)==2
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
 assert "volumes" not in pod
 assert all("value" in v for v in env.values())
print(service,mode,"render contract PASS",len(docs),"resources")
