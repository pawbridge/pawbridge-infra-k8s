"""스케줄러와 독립된 PostgreSQL 논리 백업. 비밀값과 평문 덤프를 출력하지 않는다."""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import email.utils
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid

UTC = dt.timezone.utc
BUCKET = "pawbridge-backups"
PREFIX = "postgresql/v1/"
FILES = {"database.dump.age", "roles.sql.age", "manifest.json.age"}
RUN_RE = re.compile(r"(\d{8}T\d{6}Z)-([a-f0-9]{32})")


class BackupError(RuntimeError):
    pass


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def secret(path):
    data = Path(path).read_bytes()
    if not 0 < len(data) <= 8192:
        raise BackupError("invalid credential file length")
    value = data.decode().rstrip("\r\n")
    if not value or "\n" in value or "\r" in value:
        raise BackupError("invalid credential file")
    return value


@dataclasses.dataclass(frozen=True)
class Config:
    host: str
    password_file: str
    endpoint: str
    access_key_file: str
    secret_key_file: str
    recipient_file: str
    database: str = "pawbridge"
    user: str = "postgres"
    port: int = 5432
    workdir: str = "/work"
    retention_days: int = 7
    timeout: int = 2400
    content_proof: bool = False
    bucket: str = BUCKET
    prefix: str = PREFIX

    def validate(self):
        if not self.host or self.user != "postgres" or not 1 <= self.port <= 65535:
            raise BackupError("invalid PostgreSQL connection")
        if self.database != "pawbridge":
            raise BackupError("invalid database name")
        if not re.fullmatch(r"https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com", self.endpoint):
            raise BackupError("only the reviewed R2 HTTPS endpoint is allowed")
        if self.bucket != BUCKET or self.prefix != PREFIX:
            raise BackupError("only the reviewed backup bucket and PostgreSQL prefix are allowed")
        if not 1 <= self.retention_days <= 30 or not 10 <= self.timeout <= 2400:
            raise BackupError("invalid retention or timeout")
        recipient = secret(self.recipient_file)
        if not re.fullmatch(r"age1[0-9a-z]{58}", recipient):
            raise BackupError("configure an age X25519 public recipient before execution")
        for path in (self.password_file, self.access_key_file, self.secret_key_file):
            secret(path)
        return recipient

    @classmethod
    def from_env(cls):
        def need(name):
            value = os.environ.get(name, "")
            if not value:
                raise BackupError("missing setting: " + name)
            return value
        if os.environ.get("BACKUP_CONTENT_PROOF", "false") not in {"true", "false"}:
            raise BackupError("BACKUP_CONTENT_PROOF must be true or false")
        return cls(host=need("PGHOST"), password_file=need("PGPASSWORD_FILE"),
                   endpoint=need("R2_ENDPOINT"), access_key_file=need("R2_ACCESS_KEY_FILE"),
                   secret_key_file=need("R2_SECRET_KEY_FILE"), recipient_file=need("AGE_RECIPIENT_FILE"),
                   database=os.environ.get("PGDATABASE", "pawbridge"),
                   user=os.environ.get("PGUSER", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
                   workdir=os.environ.get("BACKUP_WORKDIR", "/work"),
                   retention_days=int(os.environ.get("BACKUP_RETENTION_DAYS", "7")),
                   timeout=int(os.environ.get("BACKUP_TIMEOUT_SECONDS", "2400")),
                   content_proof=os.environ.get("BACKUP_CONTENT_PROOF", "false") == "true")

    def pg_env(self):
        # 자식 프로세스에만 전달한다. 명령행·로그·덤프 metadata에 넣지 않는다.
        return dict(os.environ, PGHOST=self.host, PGPORT=str(self.port), PGDATABASE=self.database,
                    PGUSER=self.user, PGPASSWORD=secret(self.password_file), PGCONNECT_TIMEOUT="10",
                    PGOPTIONS="-c statement_timeout=600000 -c timezone=UTC -c datestyle=ISO,MDY")

    def connect(self, **extra):
        import psycopg
        return psycopg.connect(host=self.host, port=self.port, dbname=self.database, user=self.user,
                               password=secret(self.password_file), connect_timeout=10,
                               options="-c statement_timeout=600000 -c timezone=UTC -c datestyle=ISO,MDY",
                               **extra)

    def client(self):
        return storage_client(self.endpoint, self.access_key_file, self.secret_key_file)


def storage_client(endpoint, access_key_file, secret_key_file):
    if not re.fullmatch(r"https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com", endpoint):
        raise BackupError("only the reviewed R2 HTTPS endpoint is allowed")
    import boto3
    from botocore.config import Config as ClientConfig
    return boto3.client("s3", endpoint_url=endpoint, region_name="auto",
                        aws_access_key_id=secret(access_key_file),
                        aws_secret_access_key=secret(secret_key_file),
                        config=ClientConfig(connect_timeout=10, read_timeout=30,
                                            retries={"mode": "standard", "total_max_attempts": 3},
                                            request_checksum_calculation="when_required",
                                            response_checksum_validation="when_required"))


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def encrypt_command(command, recipient, output, env, timeout):
    """평문은 파이프 안에만 둔다. 생산자와 암호화기 둘 다 성공해야 한다."""
    processes = []
    timed_out = threading.Event()

    def terminate():
        timed_out.set()
        for process in processes:
            if process.poll() is None:
                process.kill()

    timer = threading.Timer(timeout, terminate)
    timer.daemon = True
    plain_hash = hashlib.sha256()
    with Path(output).open("xb") as encrypted, tempfile.TemporaryFile() as producer_error, tempfile.TemporaryFile() as age_error:
        try:
            producer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=producer_error, env=env)
            processes.append(producer)
            age = subprocess.Popen(["age", "--encrypt", "--recipient", recipient], stdin=subprocess.PIPE,
                                   stdout=encrypted, stderr=age_error)
            processes.append(age)
            timer.start()
            while chunk := producer.stdout.read(64 * 1024):
                plain_hash.update(chunk)
                age.stdin.write(chunk)
            producer.stdout.close()
            age.stdin.close()
            producer.wait(timeout=timeout)
            age.wait(timeout=timeout)
            if timed_out.is_set():
                raise BackupError("dump/encryption deadline exceeded")
            if producer.returncode or age.returncode:
                raise BackupError("dump or encryption failed")
            encrypted.flush()
            os.fsync(encrypted.fileno())
        finally:
            timer.cancel()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
    if Path(output).stat().st_size == 0:
        raise BackupError("empty encrypted backup")
    return plain_hash.hexdigest()


def role_signature(cfg, exclude_roles=()):
    """역할·비밀번호 해시·멤버십 변경을 내부 해시로 감지한다. 결과는 외부 로그에 남기지 않는다."""
    hash_ = hashlib.sha256()
    with cfg.connect(autocommit=True) as connection:
        queries = [
            "SELECT (to_jsonb(r)-'oid')::text FROM pg_authid r WHERE NOT (rolname=ANY(%s)) ORDER BY rolname",
            """SELECT (to_jsonb(m)-'oid'-'roleid'-'member'-'grantor' ||
                jsonb_build_object('role',r.rolname,'member',u.rolname,'grantor',g.rolname))::text
                FROM pg_auth_members m JOIN pg_authid r ON r.oid=m.roleid
                JOIN pg_authid u ON u.oid=m.member JOIN pg_authid g ON g.oid=m.grantor
                WHERE NOT (r.rolname=ANY(%s)) AND NOT (u.rolname=ANY(%s)) AND NOT (g.rolname=ANY(%s))
                ORDER BY r.rolname,u.rolname,g.rolname"""]
        for index, query in enumerate(queries):
            params = (list(exclude_roles),) * (1 if index == 0 else 3)
            for row in connection.execute(query, params):
                hash_.update(row[0].encode())
                hash_.update(b"\n")
    return hash_.hexdigest()


def database_proof(connection, content=False):
    """동일 snapshot의 테이블 수·행 수. 첫 복원 시험에는 전행 content proof를 추가한다."""
    from psycopg import sql
    tables = connection.execute("""SELECT n.nspname,c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind='r' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
        ORDER BY n.nspname,c.relname""").fetchall()
    proof = {"tables": {}, "extensions": dict(connection.execute(
        "SELECT extname,extversion FROM pg_extension ORDER BY extname").fetchall())}
    for schema, table in tables:
        name = schema + "." + table
        qualified = sql.Identifier(schema, table)
        if content:
            count, total = 0, 0
            with connection.cursor() as cursor:
                with cursor.copy(sql.SQL("COPY (SELECT to_jsonb(t)::text FROM ONLY {} t) TO STDOUT").format(qualified)) as copy:
                    copy.set_types(["text"])
                    for row in copy.rows():
                        count += 1
                        total = (total + int.from_bytes(hashlib.sha256(row[0].encode()).digest(), "big")) % (1 << 256)
            proof["tables"][name] = {"rows": count, "content_sum_sha256": f"{total:064x}"}
        else:
            count = connection.execute(sql.SQL("SELECT count(*) FROM ONLY {}").format(qualified)).fetchone()[0]
            proof["tables"][name] = {"rows": count}
    # 앱이 원래 조회 가능한 테이블만 복원 시험 대상으로 삼는다. 관리자 전용 테이블 접근을 요구하지 않는다.
    proof["application_reads"] = {}
    for service in ("animal", "user", "community", "store", "payment"):
        schema = "pawbridge_" + service
        role = schema + "_app"
        if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
            continue
        rows = connection.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relkind='r' AND has_schema_privilege(%s,n.oid,'USAGE')
            AND has_table_privilege(%s,c.oid,'SELECT') ORDER BY c.relname""", (schema, role, role)).fetchall()
        proof["application_reads"][role] = [row[0] for row in rows]
    return proof


def capture(cfg, recipient, directory, run):
    import psycopg
    with cfg.connect() as connection:
        if connection.execute("SELECT rolname FROM pg_authid WHERE oid=10").fetchone() != ("postgres",):
            raise BackupError("the source bootstrap role must be postgres")
    before_roles = role_signature(cfg)
    emit("backup_phase", phase="roles_export")
    encrypt_command(["pg_dumpall", "--roles-only", "--no-password"], recipient,
                    directory / "roles.sql.age", cfg.pg_env(), cfg.timeout)
    with cfg.connect() as connection:
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        connection.read_only = True
        server_version, server_time = connection.execute(
            "SELECT current_setting('server_version_num')::int, extract(epoch from clock_timestamp())").fetchone()
        if server_version // 10000 != 17:
            raise BackupError("PostgreSQL 17 is required by this recovery contract")
        if abs(float(server_time) - time.time()) > 300:
            raise BackupError("database and backup runner clocks differ by more than five minutes")
        snapshot = connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
        emit("backup_phase", phase="database_export", content_proof=cfg.content_proof)
        encrypt_command(["pg_dump", "--format=custom", "--compress=gzip:6", "--no-password",
                         "--lock-wait-timeout=30s", "--snapshot=" + snapshot,
                         "--dbname=" + cfg.database], recipient, directory / "database.dump.age",
                        cfg.pg_env(), cfg.timeout)
        proof = database_proof(connection, cfg.content_proof)
    if before_roles != role_signature(cfg):
        raise BackupError("roles changed during backup; nothing will be published")
    manifest = {"format": 1, "run": run, "database": cfg.database, "server_version": server_version,
                "content_proof": cfg.content_proof, "proof": proof,
                "roles_signature": before_roles,
                "recipient_sha256": hashlib.sha256(recipient.encode()).hexdigest()}
    # 작은 매니페스트도 표준 입력에서 암호화하고 평문 파일을 만들지 않는다.
    process = subprocess.run(["age", "--encrypt", "--recipient", recipient],
                             input=json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if process.returncode:
        raise BackupError("manifest encryption failed")
    with (directory / "manifest.json.age").open("xb") as out:
        out.write(process.stdout)


def read_object(client, bucket, key, max_bytes=16384):
    obj = client.get_object(Bucket=bucket, Key=key)
    with obj["Body"] as body:
        data = body.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BackupError("completion marker too large")
    return data


def verify_object(client, bucket, key, size, expected_hash):
    obj = client.get_object(Bucket=bucket, Key=key)
    hash_ = hashlib.sha256()
    seen = 0
    with obj["Body"] as body:
        while chunk := body.read(1024 * 1024):
            seen += len(chunk)
            if seen > size:
                raise BackupError("uploaded object exceeds expected size")
            hash_.update(chunk)
    if seen != size or hash_.hexdigest() != expected_hash:
        raise BackupError("uploaded encrypted object checksum mismatch")


def parse_run(run):
    if not RUN_RE.fullmatch(run):
        raise BackupError("invalid backup run identifier")
    return dt.datetime.strptime(run.split("-")[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def validate_marker(key, data, prefix=PREFIX):
    marker = json.loads(data)
    run = marker.get("run", "")
    created = parse_run(run)
    run_prefix = prefix + run + "/"
    if key != run_prefix + "complete.json" or marker.get("format") != 1:
        raise BackupError("invalid completion marker")
    if marker.get("created_at") != created.isoformat():
        raise BackupError("marker timestamp does not match run identifier")
    files = marker.get("files", {})
    if set(files) != FILES:
        raise BackupError("incomplete backup manifest")
    for name, entry in files.items():
        if entry.get("key") != run_prefix + name or type(entry.get("size")) is not int or entry["size"] <= 0:
            raise BackupError("invalid backup object reference")
        if not re.fullmatch(r"[a-f0-9]{64}", entry.get("sha256", "")):
            raise BackupError("invalid backup checksum")
    return marker, created


def list_keys(client, cfg):
    keys = []
    token = None
    for _ in range(10):
        kwargs = dict(Bucket=cfg.bucket, Prefix=cfg.prefix, MaxKeys=1000)
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        keys.extend(entry["Key"] for entry in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")
        if not token:
            raise BackupError("invalid object listing pagination")
    raise BackupError("backup prefix listing limit exceeded; manual investigation required")


def prune(client, cfg, current_run, now):
    """현재 성공을 확인한 뒤에만 실행. 전용 prefix 밖·최신 완료 백업은 지우지 않는다."""
    # PC·VM 시간이 함께 어긋나도 외부 보관 서버 시각과 대조하기 전에는 삭제하지 않는다.
    response = client.head_object(Bucket=cfg.bucket, Key=cfg.prefix + current_run + "/complete.json")
    date = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date", "")
    try:
        storage_time = email.utils.parsedate_to_datetime(date)
        if storage_time.tzinfo is None or abs((storage_time - now).total_seconds()) > 300:
            raise ValueError("clock drift")
    except (TypeError, ValueError, OverflowError) as exc:
        raise BackupError("storage and runner clocks are unverified or differ; retention refused") from exc
    keys = list_keys(client, cfg)
    completed = []
    for key in keys:
        if key.endswith("/complete.json"):
            marker, created = validate_marker(key, read_object(client, cfg.bucket, key), cfg.prefix)
            completed.append((created, marker["run"], key, marker))
    if not any(run == current_run for _, run, _, _ in completed):
        raise BackupError("current completion marker missing; retention refused")
    if any(created > now + dt.timedelta(minutes=5) for created, _, _, _ in completed):
        raise BackupError("future-dated backup detected; retention refused")
    newest = max(completed, key=lambda item: (item[0], item[1]))[1]
    protected = {current_run, newest}
    cutoff = now - dt.timedelta(days=cfg.retention_days)
    expired = set()
    for created, run, key, marker in completed:
        if created < cutoff and run not in protected:
            # 완료 표시를 먼저 지워 정리 도중 실패해도 불완전한 복원 후보를 남기지 않는다.
            client.delete_object(Bucket=cfg.bucket, Key=key)
            expired.add(run)
    for key in keys:
        relative = key.removeprefix(cfg.prefix)
        parts = relative.split("/")
        if not key.startswith(cfg.prefix) or len(parts) != 2 or parts[1] not in FILES:
            continue
        try:
            created = parse_run(parts[0])
        except (BackupError, ValueError):
            continue
        if created < cutoff and parts[0] not in protected:
            # 완료 표시가 없는 오래된 중단 백업에도 같은 보관 기간을 적용한다.
            client.delete_object(Bucket=cfg.bucket, Key=key)
    return len(expired)


def run_backup(cfg, client=None, capture_fn=capture, now=None):
    os.umask(0o077)
    recipient = cfg.validate()
    client = client or cfg.client()
    now = (now or dt.datetime.now(UTC)).replace(microsecond=0)
    run = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
    emit("backup_started", run=run)
    with tempfile.TemporaryDirectory(prefix=run + "-", dir=cfg.workdir) as name:
        directory = Path(name)
        capture_fn(cfg, recipient, directory, run)
        files = {}
        emit("backup_phase", phase="encrypted_upload", run=run)
        for name in sorted(FILES):
            path = directory / name
            size, hash_ = path.stat().st_size, digest(path)
            if not size:
                raise BackupError("empty backup artifact")
            key = cfg.prefix + run + "/" + name
            # UUID별 신규 키를 사용한다. 병렬 전송을 제한해 메모리 부하를 줄인다.
            from boto3.s3.transfer import TransferConfig
            client.upload_file(str(path), cfg.bucket, key,
                               ExtraArgs={"Metadata": {"sha256": hash_}, "ContentType": "application/octet-stream"},
                               Config=TransferConfig(multipart_threshold=16 * 1024 * 1024,
                                                     multipart_chunksize=16 * 1024 * 1024,
                                                     max_concurrency=1, use_threads=False))
            verify_object(client, cfg.bucket, key, size, hash_)
            files[name] = {"key": key, "size": size, "sha256": hash_}
        marker = {"format": 1, "run": run, "created_at": now.isoformat(), "files": files}
        marker_key = cfg.prefix + run + "/complete.json"
        payload = json.dumps(marker, sort_keys=True).encode()
        client.put_object(Bucket=cfg.bucket, Key=marker_key, Body=payload, ContentType="application/json")
        if read_object(client, cfg.bucket, marker_key) != payload:
            raise BackupError("completion marker verification failed")
        emit("backup_phase", phase="retention", run=run)
        deleted = prune(client, cfg, run, now)
        emit("backup_completed", run=run, encrypted_bytes=sum(f["size"] for f in files.values()),
             expired_runs=deleted, content_proof=cfg.content_proof)
        return marker


def main():
    try:
        cfg = Config.from_env()
        def deadline(signum, frame):
            raise BackupError("overall backup deadline exceeded")
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(cfg.timeout)
        run_backup(cfg)
    except Exception as exc:
        emit("backup_failed", error_type=type(exc).__name__,
             reason=str(exc) if isinstance(exc, BackupError) else "operation failed; inspect phase and runtime status")
        return 1
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
