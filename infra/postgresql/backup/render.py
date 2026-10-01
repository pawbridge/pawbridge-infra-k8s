"""비밀값 없이 배포 후보를 렌더링한다. kubectl/Vault/R2 상태는 변경하지 않는다."""
import argparse
from pathlib import Path
import re
import yaml


def render(image, endpoint, recipient, activate=False):
    if not re.fullmatch(r'dorosiya/pawbridge-postgresql-backup@sha256:[a-f0-9]{64}', image):
        raise ValueError('published immutable backup image digest required')
    if not re.fullmatch(r'https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com', endpoint):
        raise ValueError('R2 HTTPS account endpoint required')
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise ValueError('age public recipient required; never pass a private identity')
    root = Path(__file__).resolve().parents[3] / 'gitops/stateful/postgresql-backup'
    objects = list(yaml.safe_load_all((root / 'cronjob.yaml').read_text()))
    for obj in objects:
        if obj['kind'] == 'ConfigMap':
            obj['data']['recipient'] = recipient
        if obj['kind'] == 'CronJob':
            obj['spec']['suspend'] = not activate
            container = obj['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]
            container['image'] = image
            next(e for e in container['env'] if e['name'] == 'R2_ENDPOINT')['value'] = endpoint
    for file in ['network-policy.yaml', 'rules.yaml']:
        objects.extend(yaml.safe_load_all((root / file).read_text()))
    return yaml.safe_dump_all(objects, sort_keys=False, allow_unicode=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--recipient-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    content = render(args.image, args.endpoint, Path(args.recipient_file).read_text().strip(), args.activate)
    with Path(args.output).open('x') as out:
        out.write(content)
