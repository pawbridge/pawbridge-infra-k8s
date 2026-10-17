"""Host periodic jobs: durable outcomes, one owner, bounded children, local health."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid
from datetime import datetime


def atomic(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('Invalid status file')
    return json.loads(path.read_text())


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Private owned directory required')
    return path


def validate(config):
    if set(config) != {'name', 'command', 'cwd', 'state', 'interval', 'timeout', 'stale'}:
        raise ValueError('Invalid job configuration')
    if not isinstance(config['name'], str) or not config['name'].replace('-', '').isalnum():
        raise ValueError('Invalid job name')
    command = config['command']
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or '\x00' in x for x in command):
        raise ValueError('Argument array required')
    if not Path(command[0]).is_absolute() or not Path(config['cwd']).is_dir() or not Path(config['state']).is_absolute():
        raise ValueError('Absolute executable/state and existing working directory required')
    for key in ('interval', 'timeout', 'stale'):
        if type(config[key]) not in (int, float) or not math.isfinite(config[key]) or config[key] <= 0:
            raise ValueError('Positive finite timing required')
    if not config['timeout'] < config['interval'] < config['stale']:
        raise ValueError('Require timeout < interval < stale')


def run_due(config, now=None):
    validate(config)
    root = private_directory(config['state'])
    status_path = root / 'status.json'
    fd = os.open(root / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 'busy'
        now = time.time() if now is None else now
        status = read(status_path) if status_path.exists() else {}
        if status and status.get('name') != config['name']:
            raise ValueError('Job state owner mismatch')
        previous = status.get('lastAttempt')
        if previous is not None and now < previous:
            raise ValueError('Clock moved backwards')
        if previous is not None and now - previous < config['interval']:
            return 'not-due'
        attempt = uuid.uuid4().hex
        status.update(name=config['name'], state='running', lastAttempt=now, attempt=attempt, errorType=None)
        atomic(status_path, status)
        process = None
        try:
            with (root / (attempt + '.log')).open('xb') as output:
                os.chmod(output.name, 0o600)
                process = subprocess.Popen(config['command'], cwd=config['cwd'], stdout=output,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                code = process.wait(timeout=config['timeout'])
            if code:
                raise subprocess.CalledProcessError(code, config['command'])
            status.update(state='success', lastSuccess=time.time(), exitCode=0)
        except BaseException as error:
            if process is not None and process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            status.update(state='failed', errorType=type(error).__name__,
                          exitCode=process.returncode if process else None)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            status['finished'] = time.time()
            atomic(status_path, status)
        return status['state']


def job_health(config, now=None):
    validate(config)
    now = time.time() if now is None else now
    try:
        status = read(Path(config['state']) / 'status.json')
        if status.get('name') != config['name']:
            return 'invalid'
        attempt = float(status['lastAttempt'])
        success = status.get('lastSuccess')
        if not math.isfinite(attempt) or attempt > now or (success is not None and (not math.isfinite(float(success)) or success > now)):
            return 'clock-invalid'
        if status['state'] == 'failed':
            return 'failed'
        if status['state'] == 'running' and now - attempt > config['timeout'] + 10:
            return 'overdue'
        if success is None:
            return 'initializing' if status['state'] == 'running' else 'missing-success'
        if now - success > config['stale']:
            return 'stale'
        return 'ok' if status['state'] in ('success', 'running') else 'invalid'
    except (OSError, ValueError, TypeError, KeyError):
        return 'missing-or-invalid'


def gallery_health(path, max_age=3600, now=None):
    now = time.time() if now is None else now
    try:
        status = read(path)
        if status['state'] in ('failed', 'stopped'):
            return status['state']
        if status['state'] not in ('idle', 'building', 'fetching', 'waiting'):
            return 'invalid'
        timestamps = [datetime.fromisoformat(status[k]).timestamp() for k in ('lastSuccess', 'lastChecked') if status.get(k)]
        if not timestamps:
            return 'missing-success'
        if max(timestamps) > now:
            return 'clock-invalid'
        return 'stale' if now - max(timestamps) > max_age else 'ok'
    except (OSError, ValueError, TypeError, KeyError):
        return 'missing-or-invalid'


def monitor(config):
    """Local only. A successful monitor process does not imply healthy jobs."""
    result = {'checkedAt': time.time(), 'jobs': {}}
    for name, specification in config['jobs'].items():
        result['jobs'][name] = job_health(specification)
    for name, specification in config.get('galleries', {}).items():
        result['jobs'][name] = gallery_health(specification['path'], specification['maxAge'])
    result['healthy'] = bool(result['jobs']) and all(v == 'ok' for v in result['jobs'].values())
    output = Path(config['output'])
    private_directory(output.parent)
    atomic(output, result)
    return result


def monitor_health(path, max_age=180, now=None):
    now = time.time() if now is None else now
    try:
        status = read(path)
        checked = float(status['checkedAt'])
        if not math.isfinite(checked) or not 0 <= now - checked <= max_age:
            return 'monitor-stale'
        return 'ok' if status['healthy'] is True else 'unhealthy'
    except (OSError, ValueError, TypeError, KeyError):
        return 'missing-or-invalid'


def main():
    def interrupted(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('run', 'health', 'loop', 'monitor', 'monitor-health'))
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    config = read(args.config)
    if args.action == 'monitor-health':
        result = monitor_health(config['output'])
        print(json.dumps({'monitor': result}))
        return 0 if result == 'ok' else 1
    if args.action == 'monitor':
        result = monitor(config)
        print(json.dumps(result))
        return 0 if result['healthy'] else 1
    if args.action == 'health':
        result = job_health(config)
        print(json.dumps({'job': config['name'], 'health': result}))
        return 0 if result == 'ok' else 1
    if args.action == 'run':
        result = run_due(config)
        return 1 if result == 'failed' else 0
    while True:
        run_due(config)
        time.sleep(min(30, config['interval']))


if __name__ == '__main__':
    os.umask(0o077)
    raise SystemExit(main())
