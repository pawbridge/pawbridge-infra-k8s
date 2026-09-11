#!/usr/bin/env python3
"""Build five disposable images and execute rendered Job commands; no live endpoints."""
import argparse
import hashlib
import json
import re
import uuid
import zipfile
from pathlib import Path
from check_contracts import render, SERVICES
from recovery_drill import run, sql, scalar, ready, MYSQL_IMAGE, PASSWORD

ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = ROOT / "infra/migrations/Dockerfile"
LABEL = "pawbridge.flyway-package-drill"

def require(ok, message):
    if not ok:
        raise AssertionError(message)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-worktree", required=True, type=Path)
    parser.add_argument("--run-disposable", action="store_true")
    args = parser.parse_args()
    if not args.run_disposable:
        parser.error("--run-disposable is required")
    backend = args.backend_worktree.resolve()
    bases = re.findall(r"^FROM (\S+)$", DOCKERFILE.read_text(), re.MULTILINE)
    require(len(bases) == 1 and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", bases[0]),
            "Dockerfile must use one digest-pinned runtime")
    runtime = bases[0]
    print(f"RUNTIME {runtime}", flush=True)
    distributions = {}
    for service in SERVICES:
        project = backend / f"{service}-service"
        dist = project / "build/migration"
        jars = list((dist / "lib").glob("*-migration.jar"))
        require(len(jars) == 1, f"Expected one migration jar: {service}")
        with zipfile.ZipFile(jars[0]) as archive:
            entries = archive.namelist()
            require(not any(x.startswith(("fixture/", "BOOT-INF/")) for x in entries), "Unexpected jar content")
            sqls = {p.name: p.read_bytes() for p in (project / "src/migration/resources/db/migration").glob("V*.sql")}
            require(sqls and set(sqls) == {x.rsplit("/", 1)[-1] for x in entries if x.endswith(".sql")}, "SQL inventory mismatch")
            for name, data in sqls.items():
                require(archive.read("db/migration/" + name) == data, f"Stale packaged SQL: {service}")
        distributions[service] = dist
        print(f"INPUT {service} jar SHA256={hashlib.sha256(jars[0].read_bytes()).hexdigest()}", flush=True)
    for image in (MYSQL_IMAGE, runtime, "alpine/helm:3.15.4"):
        run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    nonce = uuid.uuid4().hex[:12]
    database, java = f"pawbridge-package-{nonce}-mysql", f"pawbridge-package-{nonce}-java"
    tags = []
    try:
        run(["docker", "run", "-d", "--pull", "never", "--network", "none", "--name", database,
             "--label", LABEL + "=" + nonce, "--memory", "768m", "--cpus", "1",
             "-e", "MYSQL_ROOT_PASSWORD=" + PASSWORD, "-e", "MYSQL_ROOT_HOST=%", MYSQL_IMAGE])
        ready(database)
        for service in SERVICES:
            sql(database, f"CREATE DATABASE pawbridge_{service};")
            sql(database, (ROOT / f"infra/migrations/accounts/{service}.sql").read_text())
            tag = f"pawbridge-flyway-local-test:{nonce}-{service}"
            tags.append(tag)
            run(["env", "DOCKER_BUILDKIT=0", "docker", "build", "--pull=false", "--network", "none",
                 "--label", LABEL + "=" + nonce,
                 "-f", str(DOCKERFILE), "-t", tag, str(distributions[service])], timeout=180)
            digest = "sha256:" + "a" * 64
            docs = render(service, {"image": {"digest": digest, "tag": "sha-" + "c" * 40},
                "env": {"SPRING_JPA_HIBERNATE_DDL_AUTO": "validate"},
                "schemaMigration": {"enabled": True, "image": "example.invalid/test@" + digest,
                    "apiImageDigest": digest, "sourceRevision": "c" * 40,
                    "existingSchemaVerified": True, "recoveryReference": "synthetic-package-drill"}})
            job = next(x for x in docs if x["kind"] == "Job" and x["metadata"]["name"].endswith("-schema-migrate"))
            container = job["spec"]["template"]["spec"]["containers"][0]
            env = {x["name"]: x["value"].replace("mysql.databases.svc.cluster.local", "127.0.0.1")
                   if "value" in x else PASSWORD for x in container["env"]}
            prefix = service.upper() + "_MIGRATION_"

            def execute(command=None, changes=None, success=True):
                variables = dict(env)
                variables.update(changes or {})
                env_args = [v for key, value in variables.items() for v in ("-e", key + "=" + value)]
                command_args = list(container["args"])
                if command:
                    command_args[-1] = command
                result = run(["docker", "run", "--rm", "--pull", "never", "--name", java,
                    "--label", LABEL + "=" + nonce, "--network", "container:" + database,
                    "--user", "1000:1000", "--read-only", "--cap-drop", "ALL",
                    "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:rw,size=64m,mode=1777",
                    "--memory", "512m", "--cpus", "0.5", *env_args,
                    "--entrypoint", container["command"][0], tag, *command_args], expected=None, timeout=180)
                require((result.returncode == 0) == success, f"{service}: unexpected exit {result.returncode}")
                return result.stdout.decode()

            execute(success=False)
            require(scalar(database, f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='pawbridge_{service}';") == "0", "Locked account changed schema")
            sql(database, f"ALTER USER 'pawbridge_{service}_migrator'@'%' IDENTIFIED BY '{PASSWORD}' ACCOUNT UNLOCK;")
            execute(changes={prefix + "CONFIRM_TARGET": ""}, success=False)
            require("Migrations executed: 1" in execute(), "Initial migration not applied")
            require("Migrations executed: 0" in execute(), "Repeated migration not idempotent")
            execute(command="validate")
            execute(changes={prefix + "JDBC_URL": "jdbc:mysql://127.0.0.1:3306/wrong_schema"}, success=False)
            require(scalar(database, f"SELECT COUNT(*) FROM pawbridge_{service}.flyway_schema_history WHERE success=1;") == "1", "Wrong history count")
            print(f"PASS {service}: build, rendered command, locked-account/confirmation refusal, migrate/repeat/validate, wrong-schema refusal", flush=True)
        print("SCOPE: local image/Job-command integration only; not Argo, VSO or production verification", flush=True)
    finally:
        for name in (java, database):
            item = run(["docker", "inspect", "--format", "{{json .Config.Labels}}", name], expected=None)
            if item.returncode == 0:
                require(json.loads(item.stdout).get(LABEL) == nonce, "Container cleanup label mismatch")
                run(["docker", "rm", "-fv", name])
        for tag in tags:
            item = run(["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", tag], expected=None)
            if item.returncode == 0:
                require(json.loads(item.stdout).get(LABEL) == nonce, "Image cleanup label mismatch")
                run(["docker", "image", "rm", tag])
        print("Removed own containers, anonymous volumes and tagged test images; no global prune", flush=True)

if __name__ == "__main__":
    main()
