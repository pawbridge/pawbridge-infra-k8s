"""암호화 백업을 신규 격리 PostgreSQL에 복원한다. 운영 접속·기존 DB 덮어쓰기를 거부한다."""
from __future__ import annotations
import argparse
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import tempfile

from backup import BackupError, Config, PREFIX, FILES, digest, database_proof, role_signature, validate_marker, emit


def verify_inputs(directory):
    directory = Path(directory)
    data = (directory / "complete.json").read_bytes()
    if len(data) > 16384:
        raise BackupError("completion marker too large")
    raw = json.loads(data)
    marker, _ = validate_marker(PREFIX + raw.get("run", "") + "/complete.json", data)
    for name, artifact in marker["files"].items():
        path = directory / name
        if path.stat().st_size != artifact["size"] or digest(path) != artifact["sha256"]:
            raise BackupError("local encrypted object checksum mismatch")
    return marker


def verify_restored(target, manifest):
    with target.connect() as connection:
        actual = database_proof(connection, manifest["content_proof"])
    if actual != manifest["proof"]:
        raise BackupError("restored database content, application read permissions or extension proof differs")
    if role_signature(target) != manifest["roles_signature"]:
        raise BackupError("restored role credentials or grants differ")
    return actual


def restore_bundle(directory, identity, target, report):
    # 소켓만 허용한다. 운영 ClusterIP·NodePort·DNS·localhost TCP 입력은 받지 않는다.
    if not target.host.startswith("/") or target.user != "postgres":
        raise BackupError("a local isolated socket and fresh isolated postgres bootstrap role are required")
    if Path(report).exists():
        raise BackupError("refusing to overwrite restoration evidence")
    marker = verify_inputs(directory)
    with dataclasses.replace(target, database="postgres").connect(autocommit=True) as connection:
        if connection.execute("SELECT count(*) FROM pg_database WHERE datname=%s", (target.database,)).fetchone()[0]:
            raise BackupError("target database already exists; restore refused")
        roles = connection.execute("SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY rolname").fetchall()
        if roles != [(target.user,)]:
            raise BackupError("target has existing non-bootstrap roles; restore refused")
        version = int(connection.execute("SHOW server_version_num").fetchone()[0])
        if version // 10000 != 17:
            raise BackupError("isolated PostgreSQL 17 target is required")
    with tempfile.TemporaryDirectory(prefix="restore-", dir=target.workdir) as work:
        work = Path(work)
        for name in sorted(FILES):
            destination = work / name.removesuffix(".age")
            with destination.open("xb") as out:
                process = subprocess.run(["age", "--decrypt", "--identity", str(identity),
                                          str(Path(directory) / name)], stdout=out,
                                         stderr=subprocess.PIPE, timeout=target.timeout)
            if process.returncode:
                raise BackupError("backup decryption failed; target remains unmodified")
        manifest = json.loads((work / "manifest.json").read_text())
        if manifest.get("format") != 1 or manifest.get("run") != marker["run"] or manifest.get("database") != target.database:
            raise BackupError("encrypted manifest does not match the requested recovery target")
        # pg_restore의 목차 확인만으로 완전한 복원 성공을 판단하지 않는다.
        process = subprocess.run(["pg_restore", "--list", str(work / "database.dump")],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
        if process.returncode:
            raise BackupError("backup archive listing failed")
        # PG17 역할 grantor는 최초 bootstrap postgres와 일치해야 한다.
        # 신규 DB의 bootstrap 역할만 중복 생성에서 제외하고 속성·비밀번호·멤버십은 원본대로 복원한다.
        roles_path = work / "roles.sql"
        roles_text = roles_path.read_text()
        bootstrap_create = "CREATE ROLE postgres;\n"
        if roles_text.count(bootstrap_create) != 1:
            raise BackupError("expected single postgres bootstrap definition is missing")
        roles_path.write_text(roles_text.replace(bootstrap_create, "", 1))
        environment = dataclasses.replace(target, database="postgres").pg_env()
        for command in [["psql", "-X", "--no-password", "--set=ON_ERROR_STOP=1", "--file=" + str(work / "roles.sql")],
                        ["pg_restore", "--create", "--exit-on-error", "--no-password", "--dbname=postgres", str(work / "database.dump")]]:
            process = subprocess.run(command, env=environment, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, timeout=target.timeout)
            if process.returncode:
                raise BackupError("isolated restore failed; preserve target and use a fresh target for retry")
        actual = verify_restored(target, manifest)
        evidence = {"format": 1, "run": marker["run"], "verified_tables": len(actual["tables"]),
                    "content_proof": manifest["content_proof"], "roles_and_memberships_match": True,
                    "database_proof_matches": True,
                    "restart_verified": False, "application_role_reads_verified": False}
        with Path(report).open("x") as out:
            json.dump(evidence, out, indent=2)
        emit("isolated_restore_completed", run=marker["run"], verified_tables=evidence["verified_tables"],
             content_proof=evidence["content_proof"])
        return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--bootstrap-password-file", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--allow-isolated-restore", action="store_true")
    args = parser.parse_args()
    if not args.allow_isolated_restore:
        parser.error("explicit isolated restore acknowledgement is required")
    os.umask(0o077)
    target = Config(host=args.socket, password_file=args.bootstrap_password_file, endpoint="",
                    access_key_file="", secret_key_file="", recipient_file="",
                    user="postgres", workdir=args.workdir)
    try:
        restore_bundle(args.directory, args.identity, target, args.report)
    except Exception as exc:
        emit("isolated_restore_failed", error_type=type(exc).__name__,
             reason=str(exc) if isinstance(exc, BackupError) else "restore operation failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
