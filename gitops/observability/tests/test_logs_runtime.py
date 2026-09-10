# Isolated Docker smoke using synthetic logs; no host ports or live credentials.
# Usage: PYTHONDONTWRITEBYTECODE=1 python3 test_logs_runtime.py <render-base.yaml>
# Requires the pinned images below and existing python:3.11-slim. No image pulls.
# Does not prove live Kubernetes discovery/RBAC, traffic capacity or 72h expiry.
import configparser
import io
import json
import pathlib
import secrets
import subprocess
import sys
import uuid

import yaml

from check_render import ChartLoader

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOKI = 'grafana/loki@sha256:79f3fbf2a3eca97dd735e2aa0871a2fc15365a81c68dea67f639b092c301f454'
ALLOY = 'grafana/alloy@sha256:0f4434c92b3e6cdac38bb129b344e1790c246f7b6e2eaffcc16a5fa363240e33'
GRAFANA = 'grafana/grafana@sha256:c132a683b2430fff9115a29b2a79c8ab97540cdcc90846e3c81878c778ca3596'
PYTHON = 'python:3.11-slim'


def docker(*args, data=None, check=True, timeout=90):
    r = subprocess.run(['docker', *args], input=data, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise RuntimeError('Local Docker failure: ' + r.stderr[-1800:])
    return r


def main():
    prefix = 'pawbridge-logs-test-' + uuid.uuid4().hex[:10]
    volumes, containers = [], []
    for image in [LOKI, ALLOY, GRAFANA, PYTHON]:
        docker('image', 'inspect', image)
    objects = [o for o in yaml.load_all(pathlib.Path(sys.argv[1]).read_text(), Loader=ChartLoader) if o]
    grafana = next(o['data'] for o in objects if o['kind'] == 'ConfigMap' and 'grafana.ini' in o.get('data', {}))
    password = secrets.token_urlsafe(24)
    sources = yaml.safe_load(grafana['datasources.yaml'])
    sources['datasources'][1]['url'] = 'http://127.0.0.1:3100'
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(grafana['grafana.ini'])
    ini['paths'] = {'data': '/var/lib/grafana', 'logs': '/tmp', 'plugins': '/var/lib/grafana/plugins',
                    'provisioning': '/etc/validation/provisioning'}
    ini_buffer = io.StringIO()
    ini.write(ini_buffer)
    production = (ROOT / 'logs/config.alloy').read_text()
    # Use the identical processing/writer, replacing only the Kubernetes input.
    fixture = '''logging { level = "warn" }
loki.source.api "fixture" {
  http {
    listen_address = "127.0.0.1"
    listen_port = 9999
  }
  forward_to = [loki.process.apps.receiver]
}
''' + production[production.index('// Defense in depth'):]
    fixture = fixture.replace('http://pawbridge-loki.monitoring.svc.cluster.local:3100', 'http://127.0.0.1:3100')
    files = {'loki.yaml': (ROOT / 'logs/loki.yaml').read_text(), 'production.alloy': production,
             'fixture.alloy': fixture, 'grafana.ini': ini_buffer.getvalue(),
             'provisioning/datasources/datasources.yaml': yaml.safe_dump(sources),
             'provisioning/dashboards/providers.yaml': grafana['dashboardproviders.yaml']}
    try:
        for suffix in ['config', 'loki-data', 'grafana-data']:
            name = prefix + '-' + suffix
            docker('volume', 'create', '--label', 'pawbridge.observability.test=' + prefix, name)
            volumes.append(name)
        setup = '''import json,sys,pathlib,os
d=json.load(sys.stdin)
for name,text in d['files'].items():
 p=pathlib.Path('/config')/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);p.chmod(0o644)
for name in ['/loki','/grafana']: os.chmod(name,0o777)
p=pathlib.Path('/grafana/dashboards/pawbridge');p.mkdir(parents=True)
(p/'pawbridge-overview.json').write_text(d['dashboard'])
'''
        docker('run', '--rm', '-i', '--pull=never', '--network=none', '--cap-drop=ALL',
               '--security-opt=no-new-privileges', '--memory=96m', '--cpus=1',
               '-v', volumes[0] + ':/config', '-v', volumes[1] + ':/loki', '-v', volumes[2] + ':/grafana',
               PYTHON, 'python', '-c', setup, data=json.dumps({'files': files,
                   'dashboard': (ROOT / 'dashboards/pawbridge-overview.json').read_text()}))
        common = ['--pull=never', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                  '--cpus=1', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777',
                  '-v', volumes[0] + ':/etc/validation:ro']
        for image, uid, args in [
            (LOKI, '10001', ['-config.file=/etc/validation/loki.yaml', '-verify-config=true']),
            (ALLOY, '473', ['validate', '--stability.level=generally-available', '/etc/validation/production.alloy'])]:
            docker('run', '--rm', '--network=none', '--memory=768m', '--user', uid, *common, image, *args)
            print('Exact-image config validation passed:', image.split('@')[0], flush=True)
        loki = prefix + '-loki'
        containers.append(loki)
        docker('run', '-d', '--name', loki, '--network=none', '--user=10001', '--memory=768m',
               '-e', 'GOMEMLIMIT=640MiB', '-v', volumes[1] + ':/var/lib/loki',
               *common, LOKI, '-config.file=/etc/validation/loki.yaml', '-target=all')

        def probe(code, payload=None):
            return docker('run', '--rm', '-i', '--pull=never', '--network=container:' + loki,
                          '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                          '--memory=96m', '--cpus=1', PYTHON, 'python', '-c', code,
                          data=json.dumps(payload or {})).stdout

        wait_code = '''import urllib.request,time,sys,json
url=json.load(sys.stdin)['url']
for i in range(70):
 try:
  with urllib.request.urlopen(url,timeout=2) as r:
   if r.status==200: break
 except Exception: pass
 time.sleep(1)
else: raise RuntimeError('Readiness timeout: '+url)
'''
        probe(wait_code, {'url': 'http://127.0.0.1:3100/ready'})
        alloy = prefix + '-alloy'
        containers.append(alloy)
        docker('run', '-d', '--name', alloy, '--network=container:' + loki,
               '--user=473', '--memory=256m', '-e', 'GOMEMLIMIT=200MiB',
               '--tmpfs', '/var/lib/alloy:rw,nosuid,nodev,size=64m,uid=473,gid=473',
               *common, ALLOY, 'run', '--stability.level=generally-available', '--disable-reporting',
               '--server.http.listen-addr=127.0.0.1:12345', '--storage.path=/var/lib/alloy', '/etc/validation/fixture.alloy')
        probe(wait_code, {'url': 'http://127.0.0.1:12345/-/ready'})
        sentinel = 'pawbridge-local-sentinel-' + uuid.uuid4().hex
        test_code = '''import urllib.request,urllib.parse,json,sys,time
d=json.load(sys.stdin); now=time.time_ns()
lines=[d['sentinel'], 'Authorization: Bearer fixture-token-not-real', 'password=fixture-secret-not-real', 'x'*17000]
body={'streams':[{'stream':{'cluster':'pawbridge-k136','namespace':'pawbridge','app':'animal-service','pod':'fixture','container':'app','user_id':'must-not-index'},'values':[[str(now+i),line] for i,line in enumerate(lines)]}]}
request=urllib.request.Request('http://127.0.0.1:9999/loki/api/v1/push',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=5) as r: assert r.status==204
query='http://127.0.0.1:3100/loki/api/v1/query_range?'+urllib.parse.urlencode({'query':'{app="animal-service"}','start':str(now-1000000000),'limit':1000})
for i in range(20):
 with urllib.request.urlopen(query,timeout=5) as r: result=json.load(r)['data']['result']
 if result: break
 time.sleep(1)
assert result, 'No synthetic log arrived'
assert [v[1] for s in result for v in s['values']]==[d['sentinel']], 'Filtering failed; content suppressed'
# Query results also expose structured metadata (e.g. detected_level); /series
# is the index contract. Do not confuse result fields with indexed labels.
series_url='http://127.0.0.1:3100/loki/api/v1/series?'+urllib.parse.urlencode({'match[]':'{app="animal-service"}'})
with urllib.request.urlopen(series_url,timeout=5) as r: series=json.load(r)['data']
assert series and all(set(s)=={'cluster','namespace','app','pod','container','service_name'} for s in series), 'Unexpected indexed label keys'
assert all('user_id' not in s['stream'] for s in result)
assert all(s['stream']['service_name']==s['stream']['app'] for s in result), 'Unexpected service identity'
print('Synthetic pipeline passed: allow line, filter 2 sensitive/1 oversized lines, restrict labels')
'''
        print(probe(test_code, {'sentinel': sentinel}), end='', flush=True)
        # Kill only this synthetic Loki to exercise WAL rather than graceful flush.
        docker('kill', '--signal=KILL', loki)
        docker('start', loki)
        probe(wait_code, {'url': 'http://127.0.0.1:3100/ready'})
        query_code = '''import urllib.request,urllib.parse,json,sys
d=json.load(sys.stdin)
url='http://127.0.0.1:3100/loki/api/v1/query_range?'+urllib.parse.urlencode({'query':'{app="animal-service"}','limit':1000})
with urllib.request.urlopen(url,timeout=10) as r: result=json.load(r)['data']['result']
assert any(v[1]==d['sentinel'] for s in result for v in s['values']), 'Accepted log missing after restart'
print('Loki abrupt process restart: accepted sentinel preserved')
'''
        print(probe(query_code, {'sentinel': sentinel}), end='', flush=True)
        docker('restart', '--time=30', alloy)
        probe(wait_code, {'url': 'http://127.0.0.1:12345/-/ready'})
        sentinel += '-after-alloy-restart'
        print(probe(test_code, {'sentinel': sentinel}), end='', flush=True)
        print('Alloy restart: new synthetic log delivery recovered (not a no-loss guarantee)', flush=True)
        grafana_name = prefix + '-grafana'
        containers.append(grafana_name)
        docker('run', '-d', '--name', grafana_name, '--network=container:' + loki, '--user=472',
               '--memory=256m', '-e', 'GOMEMLIMIT=230MiB', '-e', 'GF_SECURITY_ADMIN_USER=local-fixture-admin',
               '-e', 'GF_SECURITY_ADMIN_PASSWORD=' + password, '-e', 'GF_PATHS_CONFIG=/etc/validation/grafana.ini',
               '-e', 'GF_PATHS_PROVISIONING=/etc/validation/provisioning',
               '--tmpfs', '/var/lib/grafana-search:rw,nosuid,nodev,size=64m,uid=472,gid=472',
               '-v', volumes[2] + ':/var/lib/grafana', *common, GRAFANA)
        probe(wait_code, {'url': 'http://127.0.0.1:3000/api/health'})
        grafana_code = '''import urllib.request,urllib.parse,json,sys,base64
d=json.load(sys.stdin); auth='Basic '+base64.b64encode(('local-fixture-admin:'+d['password']).encode()).decode()
def get(path):
 with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:3000'+path,headers={'Authorization':auth}),timeout=10) as r: return json.load(r)
assert get('/api/datasources/uid/pawbridge-loki')['type']=='loki'
assert len(get('/api/dashboards/uid/pawbridge-overview')['dashboard']['panels'])==6
result=get('/api/datasources/proxy/uid/pawbridge-loki/loki/api/v1/query_range?'+urllib.parse.urlencode({'query':'{app="animal-service"}','limit':1000}))['data']['result']
assert any(v[1]==d['sentinel'] for s in result for v in s['values'])
print('Grafana: authenticated dashboard and Loki datasource proxy query passed')
'''
        print(probe(grafana_code, {'password': password, 'sentinel': sentinel}), end='', flush=True)
        docker('restart', '--time=15', grafana_name)
        probe(wait_code, {'url': 'http://127.0.0.1:3000/api/health'})
        print(probe(grafana_code, {'password': password, 'sentinel': sentinel}), end='', flush=True)
        print('Grafana restart with same data volume: login and provisioned dashboard/query passed', flush=True)
        print(docker('stats', '--no-stream', '--format', '{{.Name}} {{.MemUsage}}', *containers).stdout, end='')
        assert all(not json.loads(docker('inspect', c).stdout)[0]['State']['OOMKilled'] for c in containers)
        print('PASS: isolated smoke only; live discovery, load and retention expiry still unverified.')
    except Exception:
        for name in containers:
            print(name, docker('inspect', '--format', '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}', name, check=False).stdout.strip())
            logs = docker('logs', '--tail=12', name, check=False)
            print((logs.stdout + logs.stderr)[-2400:])
        raise
    finally:
        for name in reversed(containers):
            docker('rm', '-f', name, check=False)
        for name in reversed(volumes):
            docker('volume', 'rm', name, check=False)
        print('Removed only synthetic test containers and volumes:', prefix, flush=True)


if __name__ == '__main__':
    main()
