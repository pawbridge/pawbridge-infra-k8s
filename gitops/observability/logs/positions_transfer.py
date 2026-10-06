"""승인된 Alloy 저장소 전환에서 읽기 위치를 보존한다.

snapshot은 읽기 조회와 비공개 로컬 백업만 수행한다. seed는 기본적으로
사전 점검만 하며 --apply가 있어야 전용 전환 Pod의 새 PVC에 기록한다.
컨텍스트·리소스 UID·백업 해시·유효 시간·대상 파일 부재를 확인한다.
운영 배포나 Pod/PVC 생성·삭제는 수행하지 않는다.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import subprocess

import yaml


RELATIVE_PATH = 'loki.source.kubernetes.apps/positions.yml'
STORAGE_PATH = '/var/lib/alloy/' + RELATIVE_PATH
CLAIM_NAME = 'pawbridge-alloy-positions'
IMAGE_DIGEST = 'sha256:0f4434c92b3e6cdac38bb129b344e1790c246f7b6e2eaffcc16a5fa363240e33'
MAX_BYTES = 1024 * 1024

# 이미 있는 파일을 덮어쓰지 않는다. 같은 파일시스템의 hard link로 최종 파일을
# 원자적으로 공개하고, 실패해도 원래 파일은 유지한다. 입력은 stdin으로만 받는다.
SEED_COMMAND = '''set -eu
base=/var/lib/alloy
directory="$base/loki.source.kubernetes.apps"
target="$directory/positions.yml"
test ! -L "$base"
test ! -L "$directory"
test ! -e "$target"
test ! -L "$target"
umask 077
mkdir -p "$directory"
temporary=$(mktemp "$directory/.positions-seed.XXXXXXXX")
trap 'rm -f -- "$temporary"' EXIT
cat > "$temporary"
test -s "$temporary"
ln "$temporary" "$target"
'''


def validate_checkpoint(content):
    if not 0 < len(content) <= MAX_BYTES:
        raise ValueError('위치 파일 크기가 허용 범위를 벗어남')
    try:
        tokens = list(yaml.scan(content, Loader=yaml.SafeLoader))
        root = yaml.compose(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        raise ValueError('위치 파일 YAML 해석 실패') from None
    if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in tokens):
        raise ValueError('위치 파일에 허용하지 않는 YAML 참조가 있음')
    if not isinstance(root, yaml.MappingNode) or len(root.value) != 1:
        raise ValueError('위치 파일 최상위 형식이 다름')
    name, positions = root.value[0]
    if not isinstance(name, yaml.ScalarNode) or name.value != 'positions':
        raise ValueError('위치 파일 positions 필드 없음')
    if not isinstance(positions, yaml.MappingNode) or not positions.value:
        raise ValueError('보존할 읽기 위치가 없음')
    seen = set()
    for entry, offset in positions.value:
        if not isinstance(entry, yaml.MappingNode) or len(entry.value) != 2:
            raise ValueError('위치 파일 키 형식이 다름')
        fields = {}
        for key, value in entry.value:
            if not isinstance(key, yaml.ScalarNode) or not isinstance(value, yaml.ScalarNode):
                raise ValueError('위치 파일 키가 문자열이 아님')
            if key.value in fields:
                raise ValueError('위치 파일 키 필드 중복')
            fields[key.value] = value.value
        if set(fields) != {'path', 'labels'}:
            raise ValueError('위치 파일 키 필드가 다름')
        if not isinstance(offset, yaml.ScalarNode) or not offset.value.isdecimal() or not 0 < int(offset.value) < 2**63:
            raise ValueError('위치 파일 offset이 유효한 양의 정수가 아님')
        identity = fields['path'], fields['labels']
        if identity in seen:
            raise ValueError('읽기 위치 중복')
        seen.add(identity)
    return len(seen)


def create_snapshot(directory, content, context, source_pod, source_uid, now=None):
    entries = validate_checkpoint(content)
    if not directory.is_absolute():
        raise ValueError('백업 경로는 절대 경로여야 함')
    captured = now or datetime.datetime.now(datetime.timezone.utc)
    manifest = {'version': 1, 'context': context, 'namespace': 'monitoring',
                'source_pod': source_pod, 'source_uid': source_uid,
                'relative_path': RELATIVE_PATH, 'captured_at_utc': captured.isoformat(),
                'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content), 'entries': entries}
    directory.mkdir(mode=0o700)  # 기존 백업 경로의 재사용·덮어쓰기 금지.
    for name, payload in (('positions.yml', content), ('manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())):
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    return manifest


def load_snapshot(directory, context, now=None):
    if not directory.is_absolute():
        raise ValueError('백업 경로는 절대 경로여야 함')
    if directory.is_symlink() or (directory / 'positions.yml').is_symlink() or (directory / 'manifest.json').is_symlink():
        raise ValueError('백업 경로의 심볼릭 링크 금지')
    manifest = json.loads((directory / 'manifest.json').read_text())
    content = (directory / 'positions.yml').read_bytes()
    entries = validate_checkpoint(content)
    if manifest.get('version') != 1 or manifest.get('namespace') != 'monitoring' or manifest.get('relative_path') != RELATIVE_PATH:
        raise ValueError('백업 대상 계약이 다름')
    if manifest.get('context') != context:
        raise ValueError('백업과 전환 컨텍스트가 다름')
    if manifest.get('sha256') != hashlib.sha256(content).hexdigest() or manifest.get('bytes') != len(content) or manifest.get('entries') != entries:
        raise ValueError('백업 해시·크기·위치 수 불일치')
    captured = datetime.datetime.fromisoformat(manifest['captured_at_utc'])
    if captured.tzinfo is None:
        raise ValueError('백업 시각에 시간대 없음')
    age = ((now or datetime.datetime.now(datetime.timezone.utc)) - captured).total_seconds()
    if not 0 <= age <= 60:
        raise ValueError('전환 백업이 60초보다 오래됐거나 미래 시각임')
    return manifest, content


def validate_seed_target(source, target, claim, volume):
    spec = target['spec']
    if target['metadata'].get('labels', {}).get('pawbridge.io/purpose') != 'alloy-position-transfer':
        raise ValueError('전용 전환 Pod가 아님')
    if target['status'].get('phase') != 'Running' or not any(
            item['type'] == 'Ready' and item['status'] == 'True' for item in target['status'].get('conditions', [])):
        raise ValueError('전환 Pod가 준비되지 않음')
    if len(spec['containers']) != 1 or spec.get('initContainers') or spec.get('ephemeralContainers'):
        raise ValueError('전환 Pod의 추가 컨테이너 금지')
    container = spec['containers'][0]
    if not container['image'].endswith('@' + IMAGE_DIGEST) or container.get('command') != ['/bin/sh', '-c', 'sleep 3600']:
        raise ValueError('전환 Pod의 이미지·단독 대기 명령이 다름')
    if spec.get('automountServiceAccountToken') is not False:
        raise ValueError('전환 Pod의 Kubernetes 토큰 자동 마운트 금지')
    if spec.get('volumes') != [{'name': 'data', 'persistentVolumeClaim': {'claimName': CLAIM_NAME}}]:
        raise ValueError('전환 Pod는 새 PVC만 연결해야 함')
    if spec['nodeName'] != source['spec']['nodeName']:
        raise ValueError('전환 노드가 원본과 다름')
    if container.get('volumeMounts') != [{'name': 'data', 'mountPath': '/var/lib/alloy'}]:
        raise ValueError('전환 마운트 위치가 다름')
    security = spec.get('securityContext', {})
    if any(security.get(key) != 473 for key in ('runAsUser', 'runAsGroup', 'fsGroup')) or security.get('runAsNonRoot') is not True:
        raise ValueError('전환 쓰기 사용자·그룹이 다름')
    container_security = container.get('securityContext', {})
    if container_security.get('allowPrivilegeEscalation') is not False or container_security.get('readOnlyRootFilesystem') is not True or container_security.get('capabilities', {}).get('drop') != ['ALL']:
        raise ValueError('전환 컨테이너 보안 계약이 다름')
    claim_spec = claim['spec']
    protection = set(claim['metadata'].get('annotations', {}).get('argocd.argoproj.io/sync-options', '').split(','))
    if claim['status']['phase'] != 'Bound' or claim_spec.get('storageClassName') != 'local-path' or claim_spec.get('accessModes') != ['ReadWriteOnce'] or claim_spec.get('resources', {}).get('requests', {}).get('storage') != '64Mi' or not {'Prune=false', 'Delete=false'} <= protection:
        raise ValueError('PVC 연결·용량·보존 계약이 다름')
    reference = volume['spec'].get('claimRef', {})
    if volume['metadata']['name'] != claim_spec['volumeName'] or reference.get('uid') != claim['metadata']['uid'] or reference.get('namespace') != 'monitoring' or reference.get('name') != CLAIM_NAME or volume['spec'].get('persistentVolumeReclaimPolicy') != 'Retain':
        raise ValueError('영구 볼륨의 PVC 연결 또는 회수 보존 정책이 다름')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['snapshot', 'seed'])
    parser.add_argument('--kubeconfig', required=True, type=pathlib.Path)
    parser.add_argument('--context', required=True)
    parser.add_argument('--application-uid', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--source-pod', required=True)
    parser.add_argument('--source-uid', required=True)
    parser.add_argument('--snapshot-dir', required=True, type=pathlib.Path)
    parser.add_argument('--target-pod')
    parser.add_argument('--target-uid')
    parser.add_argument('--pvc-uid')
    parser.add_argument('--sudo-kubectl', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.kubeconfig.is_absolute() or not args.kubeconfig.is_file():
        parser.error('명시한 kubeconfig 절대 경로가 없음')
    command = (['sudo'] if args.sudo_kubectl else []) + [
        'kubectl', '--kubeconfig', str(args.kubeconfig), '--context', args.context, '--request-timeout=15s']

    def kubectl(*arguments, content=None):
        result = subprocess.run(command + list(arguments), input=content, capture_output=True, timeout=25)
        if result.returncode:
            raise RuntimeError('제한된 kubectl 작업 실패; 대상 상태를 확인해야 함')
        return result.stdout

    def resource(namespace, kind, name, uid):
        obj = json.loads(kubectl('-n', namespace, 'get', kind, name, '-o', 'json'))
        if obj['metadata']['uid'] != uid:
            raise ValueError('승인한 리소스 UID와 다름')
        return obj

    def application():
        obj = resource('argocd', 'application', 'observability-baseline', args.application_uid)
        revisions = [item.get('targetRevision') for item in obj['spec']['sources'] if item.get('ref') == 'values']
        if revisions != [args.source_revision] or obj.get('operation'):
            raise ValueError('승인한 운영 Git 기준과 다르거나 동기화 작업이 진행 중임')
        return obj

    application()
    source = resource('monitoring', 'pod', args.source_pod, args.source_uid)
    if source['metadata'].get('labels', {}).get('app.kubernetes.io/name') != 'pawbridge-alloy' or source['status'].get('phase') != 'Running' or not all(item.get('ready') for item in source['status'].get('containerStatuses', [])) or not source['status'].get('containerStatuses'):
        raise ValueError('원본이 준비된 Alloy Pod가 아님')
    if args.action == 'snapshot':
        if args.apply:
            parser.error('snapshot에는 --apply를 사용하지 않음')
        content = kubectl('-n', 'monitoring', 'exec', args.source_pod, '--', 'cat', STORAGE_PATH)
        resource('monitoring', 'pod', args.source_pod, args.source_uid)
        print(json.dumps(create_snapshot(args.snapshot_dir, content, args.context, args.source_pod, args.source_uid)))
        return
    if not all((args.target_pod, args.target_uid, args.pvc_uid)):
        parser.error('seed는 전환 Pod와 PVC의 이름·UID가 필요함')
    manifest, content = load_snapshot(args.snapshot_dir, args.context)
    if manifest['source_pod'] != args.source_pod or manifest['source_uid'] != args.source_uid:
        raise ValueError('백업 원본 Pod와 승인한 원본이 다름')
    target = resource('monitoring', 'pod', args.target_pod, args.target_uid)
    claim = resource('monitoring', 'pvc', CLAIM_NAME, args.pvc_uid)
    volume = json.loads(kubectl('get', 'pv', claim['spec']['volumeName'], '-o', 'json'))
    validate_seed_target(source, target, claim, volume)
    kubectl('-n', 'monitoring', 'exec', args.target_pod, '--', '/bin/sh', '-c',
            'test ! -e ' + STORAGE_PATH + ' && test ! -L ' + STORAGE_PATH)
    if args.apply:
        application()
        source = resource('monitoring', 'pod', args.source_pod, args.source_uid)
        target = resource('monitoring', 'pod', args.target_pod, args.target_uid)
        claim = resource('monitoring', 'pvc', CLAIM_NAME, args.pvc_uid)
        volume = json.loads(kubectl('get', 'pv', claim['spec']['volumeName'], '-o', 'json'))
        validate_seed_target(source, target, claim, volume)
        manifest, content = load_snapshot(args.snapshot_dir, args.context)
        kubectl('-n', 'monitoring', 'exec', '-i', args.target_pod, '--', '/bin/sh', '-c', SEED_COMMAND, content=content)
        resource('monitoring', 'pod', args.target_pod, args.target_uid)
        restored = kubectl('-n', 'monitoring', 'exec', args.target_pod, '--', 'cat', STORAGE_PATH)
        if hashlib.sha256(restored).hexdigest() != manifest['sha256']:
            raise ValueError('복사 후 읽기 검수 해시 불일치')
    print(json.dumps({'action': 'seed', 'applied': args.apply, 'target_uid': args.target_uid,
                      'pvc_uid': args.pvc_uid, 'sha256': manifest['sha256'], 'entries': manifest['entries']}))


if __name__ == '__main__':
    main()
