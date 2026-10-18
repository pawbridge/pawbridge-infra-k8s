"""Encrypted backup without retention: create-only objects, verified before completion."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid
import shutil

# Installed beside the repository; alternatively BACKUP_LIBRARY points to its pinned copy.
LIBRARY = Path(os.environ.get('BACKUP_LIBRARY', str(Path(__file__).resolve().parents[2] / 'infra/postgresql/backup')))
sys.path.insert(0, str(LIBRARY))
from backup import Config, FILES, digest, capture, parse_run, validate_marker


def upload_new(client, cfg, run, directory, *, production_snapshot=False):
    """Never list, overwrite, prune, or delete. Verify ciphertext by streamed GET."""
    def publish(name):
        path = directory / name
        size = path.stat().st_size
        if path.is_symlink() or not 0 < size <= 5 * 1024 ** 3:
            raise ValueError('Invalid encrypted artifact size')
        sha = digest(path)
        key = cfg.prefix + run + '/' + name
        with path.open('rb') as data:
            client.put_object(Bucket=cfg.bucket, Key=key, Body=data, ContentLength=size,
                              IfNoneMatch='*', Metadata={'sha256': sha}, ContentType='application/octet-stream')
        remote = client.get_object(Bucket=cfg.bucket, Key=key)
        h = hashlib.sha256()
        seen = 0
        with remote['Body'] as stream:
            if remote['ContentLength'] != size:
                raise ValueError('Backup length mismatch')
            while chunk := stream.read(4 * 1024 ** 2):
                seen += len(chunk)
                if seen > size:
                    raise ValueError('Backup exceeds expected length')
                h.update(chunk)
        if seen != size or h.hexdigest() != sha:
            raise ValueError('Backup integrity mismatch')
        return {'key': key, 'size': size, 'sha256': sha}
    import re
    if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{32}', run):
        raise ValueError('Invalid run identity')
    files = {name: publish(name) for name in sorted(FILES)}
    marker = {'format': 1, 'run': run, 'created_at': parse_run(run).isoformat(),
              'files': files, 'productionSnapshot': production_snapshot,
              'purpose': 'scheduled-production' if production_snapshot else 'isolated-schedule-verification'}
    validate_marker(cfg.prefix + run + '/complete.json', json.dumps(marker).encode())
    with (directory / 'complete.json').open('x') as stream:
        json.dump(marker, stream, sort_keys=True)
    publish('complete.json')
    return marker


def run(cfg, client=None, capture_fn=capture, production_snapshot=False):
    os.umask(0o077)
    recipient = cfg.validate()
    if shutil.disk_usage(cfg.workdir).free < 5 * 1024 ** 3:
        raise ValueError('Less than 5 GiB backup working space remains')
    run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
    directory = Path(cfg.workdir) / run_id
    directory.mkdir(mode=0o700)
    capture_fn(cfg, recipient, directory, run_id)
    marker = upload_new(client or cfg.client(), cfg, run_id, directory, production_snapshot=production_snapshot)
    print(json.dumps({'run': run_id, 'verified': True, 'retention': False}), flush=True)
    return marker


if __name__ == '__main__':
    try:
        mode = os.environ.get('BACKUP_DATA_SCOPE')
        if mode not in ('isolated', 'production'):
            raise ValueError('Explicit backup data scope required')
        run(Config.from_env(), production_snapshot=mode == 'production')
    except Exception as error:
        print(json.dumps({'backup': 'failed', 'errorType': type(error).__name__}), flush=True)
        raise SystemExit(1)
