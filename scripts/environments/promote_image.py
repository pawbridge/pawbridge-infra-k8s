#!/usr/bin/env python3
"""Prepare a local prod image update; never applies, publishes or changes secrets."""
import argparse
import json
import re
import subprocess
from pathlib import Path
import yaml


def promote(root, service, revision, digest, evidence):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('full tested dev revision required')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('immutable tested digest required')
    contract = json.loads((root/'environments/environment-contract.json').read_text())
    cfg = contract['services'][service]
    test = json.loads(evidence.read_text())
    if not (test.get('environment') == 'dev' and test.get('result') == 'passed'
            and test.get('revision') == revision and test.get('images', {}).get(service) == digest
            and test.get('report')):
        raise ValueError('matching dev E2E result and report required')
    source = yaml.safe_load(subprocess.check_output(['git', '-C', str(root), 'show', revision+':'+cfg['devValues']], text=True))
    if source['image'].get('digest') != digest:
        raise ValueError('tested digest does not match the selected dev revision')
    target = root/cfg['prodValues']
    prod = yaml.safe_load(target.read_text())
    if source['image'].get('repository') != prod['image'].get('repository', source['image']['repository']):
        raise ValueError('image repository changed; requires separate review')
    prod['image'] = {**prod['image'], **{k: v for k, v in source['image'].items() if k in ('repository','tag','digest')}}
    if 'schemaMigration' in source:
        migration = source['schemaMigration']
        if migration.get('apiImageDigest') != digest or not re.fullmatch(r'.+@sha256:[0-9a-f]{64}', migration.get('image', '')):
            raise ValueError('matched migration/API image pair required')
        if source['image'].get('tag') != 'sha-'+migration.get('sourceRevision',''):
            raise ValueError('migration/API source mismatch')
        for k in ('image','apiImageDigest','sourceRevision'):
            prod.setdefault('schemaMigration', {})[k] = migration[k]
    return target, yaml.safe_dump(prod, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    for key in ('service','tested-dev-revision','expected-dev-digest'):
        parser.add_argument('--'+key, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--write', action='store_true', help='write the selected prod values locally; default is preview')
    args = parser.parse_args()
    target, content = promote(args.root, args.service, args.tested_dev_revision, args.expected_dev_digest, args.evidence)
    if args.write:
        target.write_text(content)
        print('Prepared local change:', target)
    else:
        print(content)

if __name__ == '__main__': main()
