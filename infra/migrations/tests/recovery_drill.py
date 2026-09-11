#!/usr/bin/env python3
"""Synthetic MySQL/Flyway failure and isolated-restore drill; never accepts a live endpoint."""
import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MYSQL_IMAGE = "container-registry.oracle.com/mysql/community-server:8.4.12@sha256:7dcc4add9183664de3a214daf85a50c3ba6cccfd7534f700b6561bf5b41885be"
JAVA_IMAGE = "dorosiya/pawbridge-animal-service:k8s-v1"
PASSWORD = "local_recovery_drill_only"
LABEL = "pawbridge.flyway-recovery-drill"

def run(args, data=None, expected=0, timeout=120):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if expected is not None and result.returncode != expected:
        raise RuntimeError(f"Local drill command failed: {args[0]} (exit {result.returncode})")
    return result

def sql(name, query, expected=0):
    return run(["docker", "exec", "-i", "-e", "MYSQL_PWD=" + PASSWORD, name,
                "mysql", "-uroot", "--batch", "--skip-column-names"],
               query.encode(), expected=expected)

def scalar(name, query):
    return sql(name, query).stdout.decode().strip()

def ready(name):
    for attempt in range(90):
        if sql(name, "SELECT 1;", expected=None).returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError("Disposable MySQL readiness timeout")

def verify_hash(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Backup hash mismatch; restore refused")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-disposable", action="store_true")
    parser.add_argument("--distribution", type=Path, required=True,
                        help="Built animal-service build/migration directory, mounted read-only")
    args = parser.parse_args()
    if not args.run_disposable:
        parser.error("--run-disposable is required; this creates and removes test containers")
    distribution = args.distribution.resolve()
    if not list((distribution / "lib").glob("*-migration.jar")):
        parser.error("Build migrationDistribution first")
    nonce = uuid.uuid4().hex[:12]
    names = {role: f"pawbridge-flyway-drill-{nonce}-{role}" for role in ("source", "restore", "java")}
    # Refuse missing images before creating any resources. Never pull.
    for image in (MYSQL_IMAGE, JAVA_IMAGE):
        run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    try:
        for role in ("source", "restore"):
            run(["docker", "run", "-d", "--pull", "never", "--network", "none",
                 "--name", names[role], "--label", LABEL + "=" + nonce,
                 "--memory", "768m", "--cpus", "1", "-e", "MYSQL_ROOT_PASSWORD=" + PASSWORD,
                 "-e", "MYSQL_ROOT_HOST=%", MYSQL_IMAGE])
            ready(names[role])
        source, target = names["source"], names["restore"]
        print("PASS disposable MySQL instances ready (no published ports)", flush=True)
        for service in ("animal", "user", "community", "store", "payment"):
            sql(source, f"CREATE DATABASE pawbridge_{service};")
            sql(source, (ROOT / f"infra/migrations/accounts/{service}.sql").read_text())
        assert scalar(source, "SELECT COUNT(*) FROM mysql.user WHERE User LIKE 'pawbridge%_migrator' AND account_locked='Y';") == "5"
        sql(source, "ALTER USER 'pawbridge_animal_migrator'@'%' IDENTIFIED BY '" + PASSWORD + "' ACCOUNT UNLOCK;")

        def flyway(name, command, failure=False, fixture=False, migrator=False):
            mounts = ["--mount", f"type=bind,src={distribution},dst=/opt/migration,readonly"]
            if fixture:
                mounts += ["--mount", f"type=bind,src={ROOT / 'infra/migrations/tests/fixtures'},dst=/probe,readonly"]
            result = run(["docker", "run", "--rm", "--pull", "never",
                          "--name", names["java"], "--label", LABEL + "=" + nonce,
                          "--network", "container:" + name, "--user", "1000:1000",
                          "--memory", "384m", "--cpus", "1",
                          *mounts,
                          "-e", "ANIMAL_MIGRATION_JDBC_URL=jdbc:mysql://127.0.0.1:3306/pawbridge_animal",
                          "-e", "ANIMAL_MIGRATION_CONFIRM_TARGET=jdbc:mysql://127.0.0.1:3306/pawbridge_animal",
                          "-e", "ANIMAL_MIGRATION_USERNAME=" + ("pawbridge_animal_migrator" if migrator else "root"),
                          "-e", "ANIMAL_MIGRATION_PASSWORD=" + PASSWORD,
                          "--entrypoint", "java", JAVA_IMAGE, "-cp",
                          "/probe:/opt/migration/lib/*" if fixture else "/opt/migration/lib/*",
                          "com.pawbridge.animalservice.migration.AnimalSchemaMigration", command],
                         expected=None)
            assert (result.returncode != 0) if failure else (result.returncode == 0), "Unexpected Flyway result"

        flyway(source, "migrate", migrator=True)
        sql(source, "INSERT INTO pawbridge_animal.processed_events VALUES ('recovery-marker','test',NOW(6));")
        denied = run(["docker", "exec", "-i", "-e", "MYSQL_PWD=" + PASSWORD, source,
                      "mysql", "-upawbridge_animal_migrator", "--batch"],
                     b"CREATE TABLE pawbridge_user.forbidden (id INT);", expected=None)
        assert denied.returncode != 0
        print("PASS five accounts initially locked; animal migration succeeds with scoped grants; cross-schema DDL refused", flush=True)
        backup = run(["docker", "exec", "-e", "MYSQL_PWD=" + PASSWORD, source,
                      "mysqldump", "-uroot", "--single-transaction", "--no-tablespaces",
                      "--set-gtid-purged=OFF", "--routines", "--events", "--triggers",
                      "--databases", "pawbridge_animal"]).stdout
        assert backup
        digest = hashlib.sha256(backup).hexdigest()
        verify_hash(backup, digest)
        try:
            verify_hash(backup + b"corrupted", digest)
        except ValueError:
            pass
        else:
            raise AssertionError("Corrupt backup accepted")
        flyway(source, "migrate", failure=True, fixture=True, migrator=True)
        assert scalar(source, "SELECT id FROM pawbridge_animal.recovery_probe;") == "73"
        assert scalar(source, "SELECT COUNT(*) FROM pawbridge_animal.flyway_schema_history WHERE version='2' AND success=0;") == "1"
        flyway(source, "migrate", failure=True, fixture=True, migrator=True)
        assert scalar(source, "SELECT COUNT(*) FROM pawbridge_animal.recovery_probe;") == "1"
        print("PASS partial DDL survives failure; failed history blocks repeat; corrupt backup refused", flush=True)

        # Restore into the SECOND isolated instance, never overwrite the failed source.
        sql(target, "CREATE DATABASE pawbridge_user; CREATE TABLE pawbridge_user.keep_marker(id INT PRIMARY KEY); INSERT INTO pawbridge_user.keep_marker VALUES(91);")
        verify_hash(backup, digest)
        run(["docker", "exec", "-i", "-e", "MYSQL_PWD=" + PASSWORD, target, "mysql", "-uroot"], backup)
        def verify_restore():
            assert scalar(target, "SELECT COUNT(*) FROM pawbridge_animal.processed_events WHERE event_id='recovery-marker';") == "1"
            assert scalar(target, "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='pawbridge_animal' AND table_name='recovery_probe';") == "0"
            assert scalar(target, "SELECT COUNT(*) FROM pawbridge_animal.flyway_schema_history WHERE version='1' AND success=1;") == "1"
            assert scalar(target, "SELECT id FROM pawbridge_user.keep_marker;") == "91"
            flyway(target, "validate")
        verify_restore()
        run(["docker", "restart", target])
        ready(target)
        verify_restore()
        print("PASS isolated restore, Flyway validate, unrelated schema preservation and restart persistence", flush=True)
        print("SCOPE: synthetic animal DB only; not production backup, Argo failure gate, CDC recovery or 5-service full restore", flush=True)
    finally:
        for name in reversed(list(names.values())):
            item = run(["docker", "inspect", "--format", "{{json .Config.Labels}}", name], expected=None)
            if item.returncode == 0:
                if json.loads(item.stdout).get(LABEL) != nonce:
                    raise RuntimeError("Cleanup refused: resource label mismatch")
                run(["docker", "rm", "-fv", name])
        print("Removed this drill's containers and anonymous volumes; no operational data used", flush=True)

if __name__ == "__main__":
    main()
