"""고정 Alloy의 Kubernetes 소스로 컨테이너 교체 후 읽기 위치를 검증한다.

Kubernetes API와 로그 수신 성공 응답은 모의한다. 실제 클러스터·Loki 저장,
전원 장애 또는 정확히 한 번 전달의 검증이 아니다. 외부 통신과 이미지 다운로드는 없다.
--resources-render로 변경 전 emptyDir 렌더를 주면 위치 유실을 재현한다.
"""
import argparse
import datetime
import importlib.util
import tempfile
import json
import pathlib
import time
import uuid

import yaml

from test_logs_runtime import ALLOY, PYTHON, ROOT, docker


HTTP_PROBE = '''import sys,urllib.request
request=urllib.request.Request(sys.argv[1],data=b'' if len(sys.argv)>2 else None)
with urllib.request.urlopen(request,timeout=5) as response:
 print(response.read().decode())
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resources-render', required=True, type=pathlib.Path)
    parser.add_argument('--migration', action='store_true', help='새 볼륨으로 기존 위치 백업·이관도 검증')
    args = parser.parse_args()
    objects = [obj for obj in yaml.safe_load_all(args.resources_render.read_text()) if obj]
    deployment = next(obj for obj in objects if obj['kind'] == 'Deployment' and obj['metadata']['name'] == 'pawbridge-alloy')
    data_volume = next(volume for volume in deployment['spec']['template']['spec']['volumes'] if volume['name'] == 'data')
    persistent = 'persistentVolumeClaim' in data_volume
    if args.migration and not persistent:
        parser.error('이관 검사는 PVC 후보 렌더가 필요함')
    spec = importlib.util.spec_from_file_location('positions_transfer', ROOT / 'logs/positions_transfer.py')
    transfer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transfer)
    source = '''logging { level = "warn" }
loki.source.kubernetes "apps" {
  targets = [{
    __meta_kubernetes_namespace = "pawbridge",
    __meta_kubernetes_pod_name = "position-fixture",
    __meta_kubernetes_pod_uid = "fixture-uid",
    __meta_kubernetes_pod_container_name = "app",
    cluster = "pawbridge-k136",
    namespace = "pawbridge",
    app = "animal-service",
    pod = "position-fixture",
    container = "app",
  }]
  client { api_server = "http://127.0.0.1:18080" }
  forward_to = [loki.process.apps.receiver]
}
'''
    production = (ROOT / 'logs/config.alloy').read_text()
    fixture = source + production[production.index('// Defense in depth'):]
    fixture = fixture.replace('http://pawbridge-loki.monitoring.svc.cluster.local:3100', 'http://127.0.0.1:18080')
    prefix = 'pawbridge-log-positions-' + uuid.uuid4().hex[:10]
    volumes, containers = [], []
    api = prefix + '-api'

    def http(path, post=False):
        arguments = [HTTP_PROBE, 'http://127.0.0.1:' + path]
        if post:
            arguments.append('POST')
        return docker('exec', api, 'python', '-c', *arguments, timeout=10).stdout.strip()

    def sent_count():
        metrics = http('12345/metrics')
        return sum(float(line.rsplit(' ', 1)[1]) for line in metrics.splitlines()
                   if line.startswith('loki_write_sent_entries_total{'))

    def wait_for_delivery(count):
        for _ in range(30):
            try:
                if sent_count() >= count:
                    return
            except RuntimeError as error:
                if 'ConnectionRefusedError' not in str(error):
                    raise  # 접속 준비 외의 Docker/HTTP 오류는 숨기지 않는다.
            time.sleep(1)
        raise AssertionError('Fixture delivery did not become ready within 30 seconds')

    try:
        for image in (ALLOY, PYTHON):
            docker('image', 'inspect', image)
        for suffix in ('config', 'positions', 'positions-target'):
            name = prefix + '-' + suffix
            docker('volume', 'create', '--label', 'pawbridge.observability.test=' + prefix, name)
            volumes.append(name)
        files = {'fixture.alloy': fixture, 'api.py': (ROOT / 'tests/fixtures/kubernetes_log_api.py').read_text()}
        docker('run', '--rm', '-i', '--pull=never', '--network=none', '--cap-drop=ALL',
               '--cap-add=CHOWN', '--security-opt=no-new-privileges', '--memory=96m', '--cpus=1',
               '-v', volumes[0] + ':/config', '-v', volumes[1] + ':/positions',
               '-v', volumes[2] + ':/positions-target',
               PYTHON, 'python', '-c', 'import json,pathlib,sys,os; '
               '[(pathlib.Path("/config")/name).write_text(value) for name,value in json.load(sys.stdin).items()]; '
               '[os.chown(path,473,473) for path in ("/positions","/positions-target")]', data=json.dumps(files))
        common = ['--pull=never', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                  '--cpus=1', '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m,mode=1777',
                  '-v', volumes[0] + ':/etc/validation:ro']
        containers.append(api)
        docker('run', '-d', '--name', api, '--network=none', '--memory=96m', '--user=65534',
               *common, PYTHON, 'python', '/etc/validation/api.py')
        for generation in (1, 2):
            if generation == 2:
                http('18080/fixture/next', post=True)
            alloy = prefix + '-alloy-' + str(generation)
            containers.append(alloy)
            position_volume = volumes[2] if args.migration and generation == 2 else volumes[1]
            mount = ['-v', position_volume + ':/var/lib/alloy'] if persistent else [
                '--tmpfs', '/var/lib/alloy:rw,nosuid,nodev,size=64m,uid=473,gid=473']
            docker('run', '-d', '--name', alloy, '--network=container:' + api,
                   '--memory=256m', '--user=473', '-e', 'GOMEMLIMIT=200MiB', *common, *mount,
                   ALLOY, 'run', '--stability.level=generally-available', '--disable-reporting',
                   '--server.http.listen-addr=127.0.0.1:12345', '--storage.path=/var/lib/alloy',
                   '/etc/validation/fixture.alloy')
            wait_for_delivery(2)
            state = json.loads(http('18080/fixture/state'))
            if generation == 1:
                assert state['requests'][0]['since_time'] is None
                assert state['requests'][0]['served'] == ['older-fixture', 'checkpoint-fixture']

                if args.migration:
                    for _ in range(20):
                        checkpoint = docker('exec', alloy, 'cat', transfer.STORAGE_PATH, check=False)
                        if checkpoint.returncode == 0:
                            break
                        time.sleep(1)
                    else:
                        raise AssertionError('Original live Alloy did not save a checkpoint')
                    with tempfile.TemporaryDirectory() as temporary:
                        backup = pathlib.Path(temporary) / 'private-snapshot'
                        manifest = transfer.create_snapshot(backup, checkpoint.stdout.encode(), 'fixture-context', alloy, 'fixture-uid')
                        _, content = transfer.load_snapshot(backup, 'fixture-context')
                        seed_args = ['run', '--rm', '-i', '--pull=never', '--network=none', '--read-only',
                                     '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=32m',
                                     '--cpus=1', '--user=473:473', '-v', volumes[2] + ':/var/lib/alloy',
                                     '--entrypoint=/bin/sh', ALLOY, '-c']
                        docker(*seed_args, transfer.SEED_COMMAND, data=content.decode())
                        restored = docker(*seed_args, 'cat ' + transfer.STORAGE_PATH).stdout.encode()
                        assert restored == content, 'Seed did not preserve exact checkpoint bytes'
                        result = docker(*seed_args, transfer.SEED_COMMAND, data=content.decode(), check=False)
                        assert result.returncode != 0, 'Seed overwrote existing checkpoint'
                        assert docker(*seed_args, 'cat ' + transfer.STORAGE_PATH).stdout.encode() == restored
                        print('PASS: live checkpoint backed up privately; copied as UID 473 into a new volume; '
                              'exact bytes verified; existing checkpoint overwrite refused; entries=' + str(manifest['entries']), flush=True)

            else:
                resumed = state['requests'][-1]
                assert resumed['since_time'] is not None, 'Pod replacement lost the persisted read position'
                actual = datetime.datetime.fromisoformat(resumed['since_time'].replace('Z', '+00:00')).timestamp()
                assert actual == state['checkpoint'], 'Did not resume at the last recorded original timestamp'
                assert resumed['served'] == ['checkpoint-fixture', 'new-fixture'], 'Old history replayed or new logs skipped'
                metrics = http('12345/metrics')
                assert not any(float(line.rsplit(' ', 1)[1]) > 0 for line in metrics.splitlines()
                               if line.startswith('loki_write_dropped_entries_total{'))
            assert not json.loads(docker('inspect', alloy).stdout)[0]['State']['OOMKilled']
            docker('stop', '--time=15', alloy, timeout=20)
            docker('rm', alloy)
            containers.remove(alloy)
        print('PASS: recreated Alloy resumed from stored timestamp; old history excluded; '
              'equal-timestamp boundary and new input preserved; delivery sink mocked', flush=True)
    finally:
        failed = []
        for name in reversed(containers):
            if docker('rm', '-f', name, check=False).returncode:
                failed.append(name)
        for name in reversed(volumes):
            if docker('volume', 'rm', name, check=False).returncode:
                failed.append(name)
        if failed:
            raise RuntimeError('Owned fixture cleanup failed: ' + ', '.join(failed))
        print('Removed only owned positions fixture resources:', prefix, flush=True)


if __name__ == '__main__':
    main()
