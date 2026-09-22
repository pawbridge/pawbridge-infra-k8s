#!/usr/bin/env python3
"""Render both environments and reject cross-environment dev destinations."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import yaml


def validate_dev_documents(documents):
    for doc in documents:
        if not doc: continue
        if doc.get('metadata', {}).get('namespace', 'pawbridge-dev') != 'pawbridge-dev':
            raise ValueError('dev resource points outside its namespace')
        raw = json.dumps(doc)
        if re.search(r'\.(pawbridge|databases|kafka)\.svc(?:\.|["/:])', raw):
            raise ValueError('dev references a production service')
        if re.search(r'https?://(?:api|www|images)\.pawbridge\.kr(?:["/:]|$)', raw):
            raise ValueError('dev references a production endpoint')
        if 'pawbridge-animal-originals' in raw or 'pawbridge-public-images' in raw:
            raise ValueError('dev references a production file bucket')
        if doc.get('kind') == 'Service' and doc.get('spec', {}).get('type', 'ClusterIP') != 'ClusterIP':
            raise ValueError('initial dev services must be ClusterIP')
        if doc.get('kind') == 'Deployment':
            pod = doc['spec']['template']['spec']
            if doc['spec'].get('replicas') != 0:
                raise ValueError('initial dev apps must remain stopped')
            for volume in pod.get('volumes', []):
                if 'hostPath' in volume and not volume['hostPath']['path'].endswith('/pawbridge-gpu-dev'):
                    raise ValueError('dev may not mount a production host directory')
            for container in pod['containers']:
                if '@sha256:' not in container['image']:
                    raise ValueError('app image must be immutable')
                if not container.get('resources', {}).get('limits', {}).get('memory'):
                    raise ValueError('memory limit required')
                for env in container.get('env', []):
                    ref = env.get('valueFrom', {}).get('secretKeyRef')
                    if ref and not (ref['name'].startswith('dev-') or ref['name']=='photo-service-internal-auth'):
                        raise ValueError('dedicated dev secret name required')
                for item in container.get('envFrom', []):
                    if 'secretRef' in item and not item['secretRef']['name'].startswith('dev-'):
                        raise ValueError('dev envFrom must be dedicated')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd()
    contract = json.loads((root/'environments/environment-contract.json').read_text())
    helm = shlex.split(os.getenv('HELM_COMMAND', 'helm'))
    args.output.mkdir(parents=True, exist_ok=True)
    count = 0
    for name, cfg in contract['services'].items():
        for env, ns in [('dev', 'pawbridge-dev'), ('prod', 'pawbridge')]:
            rendered = subprocess.check_output([*helm, 'template', name, cfg['chart'], '--namespace', ns, '--values', cfg[env+'Values']], text=True)
            docs = list(yaml.safe_load_all(rendered))
            if env == 'dev': validate_dev_documents(docs)
            (args.output/(env+'-'+name+'.yaml')).write_text(rendered)
            count += 1
    print(json.dumps({'rendered': count, 'devIsolation': 'passed', 'liveApplied': False}))

if __name__ == '__main__': main()
