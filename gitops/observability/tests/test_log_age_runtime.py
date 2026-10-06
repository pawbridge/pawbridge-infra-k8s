"""합성 로그의 원본 시각으로 보관 범위 제외와 정상 전송을 검증한다.

운영과 같은 고정 이미지만 사용한다. 다운로드·호스트 포트·외부 통신은 없다.
실제 Kubernetes tail이나 72시간 경과 후 저장 데이터 삭제 검증은 아니다.
--config-root로 수정 전 로그 설정을 지정하면 회귀 재현에도 사용할 수 있다.
"""
import argparse
import json
import pathlib
import uuid

from test_logs_runtime import ALLOY, LOKI, PYTHON, ROOT, docker


PROBE = r'''
import json, sys, time, urllib.error, urllib.parse, urllib.request
d = json.load(sys.stdin)
def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.read().decode()
for base in ['http://127.0.0.1:3100/ready', 'http://127.0.0.1:12345/-/ready']:
    for _ in range(60):
        try:
            get(base)
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    else:
        raise AssertionError('Synthetic service readiness timeout')
now = time.time_ns()
hour = 3600 * 10**9
minute = 60 * 10**9
cases = [
    ('fresh', now, 'fresh-synthetic-line'),
    ('within-window', now - 72*hour + minute, 'near-boundary-synthetic-line'),
    ('outside-window', now - 72*hour - minute, 'expired-boundary-synthetic-line'),
    ('old-reread', now - 96*hour, 'expired-repeated-synthetic-line'),
    ('sensitive-one', now, 'Authorization: Bearer synthetic-not-a-secret'),
    ('sensitive-two', now, 'password=synthetic-not-a-secret'),
    ('oversized', now, 'x'*17000),
]
def push(cases):
    body = {'streams': [
        {'stream': {'cluster': 'pawbridge-k136', 'namespace': 'pawbridge',
                    'app': 'animal-service', 'container': 'app', 'pod': d['prefix']+'-'+pod},
         'values': [[str(timestamp), line]]}
        for pod, timestamp, line in cases]}
    request = urllib.request.Request('http://127.0.0.1:9999/loki/api/v1/push',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=5) as r:
        assert r.status == 204
push(cases)
# Reproduce an inclusive tail reconnect: same original timestamp and line again.
push([cases[3]])
def counter(metrics, name, reason=None):
    return sum(float(line.rsplit(' ', 1)[1]) for line in metrics.splitlines()
               if (line.startswith(name+'{') or line.startswith(name+' '))
               and (reason is None or 'reason="'+reason+'"' in line))
for _ in range(30):
    metrics = get('http://127.0.0.1:12345/metrics')
    # Let the writer flush before checking exclusions, including a regression run.
    if counter(metrics, 'loki_write_sent_entries_total') >= 2:
        break
    time.sleep(1)
assert counter(metrics, 'loki_process_dropped_lines_total', 'outside_retention') == 3, \
    'Expired rereads were not classified separately before delivery'
assert counter(metrics, 'loki_process_dropped_lines_total', 'sensitive_line') == 2
assert counter(metrics, 'loki_process_dropped_lines_total', 'line_too_long') == 1
assert counter(metrics, 'loki_write_dropped_entries_total') == 0, 'Unexpected delivery loss'
assert counter(metrics, 'loki_write_sent_entries_total') == 2
loki_metrics = get('http://127.0.0.1:3100/metrics')
assert counter(loki_metrics, 'loki_discarded_samples_total') == 0, 'Expired input reached Loki'
query_end = time.time_ns()
# The writer's successful send count above proves both eligible entries were
# accepted. Historical query visibility is separate from this ingestion test;
# query only the recent sentinel to check immediate delivery and its timestamp.
query = 'http://127.0.0.1:3100/loki/api/v1/query_range?' + urllib.parse.urlencode({
    'query': '{app="animal-service"}', 'start': str(query_end-2*minute),
    'end': str(query_end), 'limit': 1000})
with urllib.request.urlopen(query, timeout=10) as r:
    results = json.load(r)['data']['result']
received = {(s['stream']['pod'], int(t), line) for s in results for t, line in s['values']}
expected = {(d['prefix']+'-'+pod, t, line) for pod, t, line in cases[:1]}
assert received == expected, ('Original timestamps or accepted-window logs changed: '
    + json.dumps({'received': sorted((p,t) for p,t,_ in received),
                  'expected': sorted((p,t) for p,t,_ in expected)}))
print('PASS: fresh and 71h59m logs accepted; fresh timestamp/query preserved; '
      '72h1m/96h/repeated logs excluded; sensitive/size filters preserved; '
      'no writer drop or Loki rejection', flush=True)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-root', type=pathlib.Path, default=ROOT)
    args = parser.parse_args()
    production = (args.config_root / 'logs/config.alloy').read_text()
    # The API fixture defaults to replacing timestamps: explicitly preserve them.
    fixture = '''logging { level = "warn" }
loki.source.api "fixture" {
  http {
    listen_address = "127.0.0.1"
    listen_port = 9999
  }
  use_incoming_timestamp = true
  forward_to = [loki.process.apps.receiver]
}
''' + production[production.index('// Defense in depth'):]
    fixture = fixture.replace('http://pawbridge-loki.monitoring.svc.cluster.local:3100',
                              'http://127.0.0.1:3100')
    files = {'production.alloy': production, 'fixture.alloy': fixture,
             'loki.yaml': (args.config_root / 'logs/loki.yaml').read_text()}
    prefix = 'pawbridge-log-age-' + uuid.uuid4().hex[:10]
    volumes, containers = [], []
    for image in [ALLOY, LOKI, PYTHON]:
        docker('image', 'inspect', image)
    try:
        for suffix in ['config', 'loki-data']:
            name = prefix + '-' + suffix
            docker('volume', 'create', '--label', 'pawbridge.observability.test='+prefix, name)
            volumes.append(name)
        docker('run', '--rm', '-i', '--pull=never', '--network=none', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--memory=96m', '--cpus=1',
               '-v', volumes[0]+':/config', '-v', volumes[1]+':/data', PYTHON,
               'python', '-c', 'import json,sys,pathlib,os; '
               '[(pathlib.Path("/config")/name).write_text(text) '
               'for name,text in json.load(sys.stdin).items()]; os.chmod("/data",0o777)',
               data=json.dumps(files))
        common = ['--pull=never', '--read-only', '--cap-drop=ALL',
                  '--security-opt=no-new-privileges', '--cpus=1',
                  '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777',
                  '-v', volumes[0]+':/etc/validation:ro']
        docker('run', '--rm', '--network=none', '--memory=256m', '--user=473',
               *common, ALLOY, 'validate', '--stability.level=generally-available',
               '/etc/validation/production.alloy')
        print('Exact Alloy production configuration validation passed', flush=True)
        loki, alloy = prefix+'-loki', prefix+'-alloy'
        containers.append(loki)
        docker('run', '-d', '--name', loki, '--network=none', '--memory=768m',
               '--user=10001', '-e', 'GOMEMLIMIT=640MiB',
               '-v', volumes[1]+':/var/lib/loki', *common, LOKI,
               '-config.file=/etc/validation/loki.yaml', '-target=all')
        containers.append(alloy)
        docker('run', '-d', '--name', alloy, '--network=container:'+loki,
               '--memory=256m', '--user=473', '-e', 'GOMEMLIMIT=200MiB',
               '--tmpfs', '/var/lib/alloy:rw,nosuid,nodev,size=64m,uid=473,gid=473',
               *common, ALLOY, 'run', '--stability.level=generally-available',
               '--disable-reporting', '--server.http.listen-addr=127.0.0.1:12345',
               '--storage.path=/var/lib/alloy', '/etc/validation/fixture.alloy')
        result = docker('run', '--rm', '-i', '--pull=never', '--network=container:'+loki,
                        '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                        '--memory=96m', '--cpus=1', PYTHON, 'python', '-c', PROBE,
                        data=json.dumps({'prefix': prefix}), timeout=160)
        print(result.stdout, end='', flush=True)
        for name in containers:
            assert not json.loads(docker('inspect', name).stdout)[0]['State']['OOMKilled']
    finally:
        cleanup_errors = []
        for name in reversed(containers):
            if docker('rm', '-f', name, check=False).returncode:
                cleanup_errors.append(name)
        for name in reversed(volumes):
            if docker('volume', 'rm', name, check=False).returncode:
                cleanup_errors.append(name)
        if cleanup_errors:
            raise RuntimeError('Synthetic resource cleanup failed: ' + ', '.join(cleanup_errors))
        print('Removed only owned synthetic test resources:', prefix, flush=True)


if __name__ == '__main__':
    main()
