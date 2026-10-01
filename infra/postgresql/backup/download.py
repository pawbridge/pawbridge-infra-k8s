"""DB·Vault 없이 R2의 암호화 백업만 내려받는 복구용 명령."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
from backup import BUCKET, PREFIX, BackupError, emit, parse_run, read_object, storage_client, validate_marker


def download_bundle(client, run, destination):
    parse_run(run)
    directory = Path(destination)
    if directory.exists():
        raise BackupError("refusing an existing recovery directory")
    key = PREFIX + run + "/complete.json"
    payload = read_object(client, BUCKET, key)
    marker, _ = validate_marker(key, payload)
    if sum(entry["size"] for entry in marker["files"].values()) > 16 * 1024 ** 3:
        raise BackupError("recovery bundle exceeds reviewed 16 GiB limit")
    directory.mkdir(mode=0o700)
    for name, entry in sorted(marker["files"].items()):
        hash_ = hashlib.sha256()
        size = 0
        obj = client.get_object(Bucket=BUCKET, Key=entry["key"])
        with obj["Body"] as source, (directory / name).open("xb") as out:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > entry["size"]:
                    raise BackupError("download exceeds expected encrypted size")
                hash_.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if size != entry["size"] or hash_.hexdigest() != entry["sha256"]:
            raise BackupError("downloaded encrypted object checksum mismatch")
    # 검증 완료 후에만 로컬 완료 표시를 쓴다. 중단된 폴더는 재사용하지 않는다.
    (directory / "complete.json").write_bytes(payload)
    emit("recovery_download_completed", run=run, encrypted_bytes=sum(f["size"] for f in marker["files"].values()))
    return marker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        client = storage_client(os.environ.get("R2_ENDPOINT", ""), os.environ["R2_ACCESS_KEY_FILE"],
                                os.environ["R2_SECRET_KEY_FILE"])
        download_bundle(client, args.run, args.destination)
    except Exception as exc:
        emit("recovery_download_failed", error_type=type(exc).__name__,
             reason=str(exc) if isinstance(exc, BackupError) else "download operation failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
