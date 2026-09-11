"""Check offline chart configs and Korean notifications with no network access.

Usage: python3 test_notifications.py /path/to/render-base.yaml /path/to/render-slack.yaml
Requires the already downloaded, digest-pinned Alertmanager image. Never pass live Secrets.
"""
import base64
import io
import json
import pathlib
import subprocess
import sys
import tarfile

import yaml

sys.dont_write_bytecode = True
from check_render import ChartLoader

IMAGE = 'quay.io/prometheus/alertmanager@sha256:690c7b525f4367aa91f73e2f91c632206d32e97c6384bdbf2fb7a861b420340d'


def config(path):
    objects = list(yaml.load_all(pathlib.Path(path).read_text(), Loader=ChartLoader))
    matches = [o for o in objects if o and o.get('kind') == 'Secret'
               and 'alertmanager.yaml' in o.get('data', {})]
    assert len(matches) == 1
    text = base64.b64decode(matches[0]['data']['alertmanager.yaml']).decode()
    assert 'hooks.slack.com' not in text, 'Only offline placeholder-free renders are allowed'
    return yaml.safe_load(text)


def main():
    base, slack = [config(path) for path in sys.argv[1:]]
    receiver = next(r for r in slack['receivers'] if r['name'] == 'slack-ko')['slack_configs'][0]
    assert receiver['send_resolved'] is True
    assert receiver['api_url_file'] == '/etc/alertmanager/secrets/monitoring-slack-webhook/url'
    # Change only the fixture file location; never provide an actual webhook.
    receiver['api_url_file'] = '/tmp/validation/webhook'
    files = {
        'base.yaml': yaml.safe_dump(base), 'slack.yaml': yaml.safe_dump(slack),
        'webhook': 'https://example.invalid/not-a-webhook',
        'notification.tmpl': '{{ define "title" }}' + receiver['title'] + '{{ end }}\n'
                             + '{{ define "body" }}' + receiver['text'] + '{{ end }}',
    }
    cases = [(status, target, labels) for status in ['firing', 'resolved']
             for target, labels in [('node', {'node': 'fixture-node', 'namespace': 'monitoring',
                                             'pod': 'fixture-exporter', 'instance': '192.0.2.1:9100'}),
                                    ('csr', {'certificatesigningrequest': 'fixture-csr'})]]
    for status, target, labels in cases:
        files[status + '-' + target + '.json'] = json.dumps({
            'Status': status, 'Receiver': 'slack-ko',
            'Alerts': [{'Status': status, 'Labels': labels,
                        'Annotations': {'summary': '테스트 경보', 'description': '테스트 확인 안내'},
                        'StartsAt': '2026-09-10T00:00:00Z', 'EndsAt': '2026-09-10T00:10:00Z'}],
        })
    commands = ['set -eu', 'mkdir -p /tmp/validation', 'tar -xf - -C /tmp/validation',
                '/bin/amtool check-config /tmp/validation/base.yaml /tmp/validation/slack.yaml']
    for name in ['PawBridgeKubeletCertificateExpiring', 'PawBridgeKubeletCertificateCritical',
                 'PawBridgeKubeletCertificateMetricsMissing', 'PawBridgeKubeletCSRPending',
                 'PawBridgeCSRCollectionUnhealthy']:
        commands.append('/bin/amtool config routes test --config.file=/tmp/validation/slack.yaml '
                        '--verify.receivers=slack-ko alertname=' + name)
        commands.append('/bin/amtool config routes test --config.file=/tmp/validation/base.yaml '
                        '--verify.receivers=disabled alertname=' + name)
    for status, target, _ in cases:
        for template in ['title', 'body']:
            commands.append('/bin/amtool template render --template.glob=/tmp/validation/notification.tmpl '
                            + '--template.data=/tmp/validation/' + status + '-' + target + '.json '
                            + "--template.text='{{ template \"" + template + "\" . }}'")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w') as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(data))
    result = subprocess.run([
        'docker', 'run', '--rm', '-i', '--pull=never', '--network=none', '--read-only',
        '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=256m', '--cpus=1',
        '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=8m', '--entrypoint=/bin/sh', IMAGE,
        '-c', '\n'.join(commands),
    ], input=payload.getvalue(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    assert result.returncode == 0, result.stderr.decode()
    output = result.stdout.decode()
    for expected in ['포우브릿지 장애 알림', '포우브릿지 복구 알림', '발생 중', '복구됨',
                     'fixture-node', '인증서 요청 fixture-csr', '테스트 경보', '테스트 확인 안내',
                     '2026-09-10 09:00:00 KST', '2026-09-10 09:10:00 KST']:
        assert expected in output, expected
    assert '<no value>' not in output
    assert '*대상:* fixture-node' in output
    assert 'monitoring/fixture-exporter' not in output, 'VM name must take precedence over exporter pod'
    print('Alertmanager 0.34.0: two configs and node/CSR firing/resolved Korean templates passed; network disabled.')


if __name__ == '__main__':
    main()
