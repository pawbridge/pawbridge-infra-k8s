#!/usr/bin/env python3
"""Run interactively on the control plane; changes only four public CA entries."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

CONTEXT = 'pawbridge-vbox-k136'
PATHS = tuple('pawbridge/dev/' + service + '/elasticsearch/trust'
              for service in ('store', 'animal', 'community', 'python'))
K = ['kubectl', '--context', CONTEXT, '--request-timeout=30s']
CONSUMERS = {'store': 'store-service', 'animal': 'animal-service',
             'community': 'community-service', 'python': 'python-ai-service'}

def run(args, data=None, timeout=60):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed; raw output suppressed: ' + args[0])
    return result.stdout

def certificates(pem):
    blocks = re.findall(r'-----BEGIN CERTIFICATE-----\s+[^-]+-----END CERTIFICATE-----', pem)
    if not blocks or re.sub(r'-----BEGIN CERTIFICATE-----\s+[^-]+-----END CERTIFICATE-----', '', pem).strip():
        raise ValueError('Invalid certificate-only PEM')
    return [block.strip() + '\n' for block in blocks]

def bundle(old, new):
    return ''.join(dict.fromkeys(certificates(old) + certificates(new)))

def deployment(service):
    return json.loads(run(K + ['-n', 'pawbridge', 'get', 'deployment', CONSUMERS[service], '-o', 'json']))

def wait_consumer(service, pem, previous_template):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        # This field contains public CA certificates, never a private key.
        encoded = run(K + ['-n', 'pawbridge', 'get', 'secret', service + '-search-http-ca',
                           '-o', 'jsonpath={.data.ca\\.crt}']).decode()
        current = deployment(service)
        desired = current['spec'].get('replicas', 1)
        status = current.get('status', {})
        if (base64.b64decode(encoded).decode() == pem
                and current['spec']['template'] != previous_template
                and status.get('observedGeneration', 0) >= current['metadata']['generation']
                and status.get('updatedReplicas', 0) == desired
                and status.get('availableReplicas', 0) == desired
                and status.get('replicas', 0) == desired):
            print('VSO certificate and rollout verified:', CONSUMERS[service], flush=True)
            return
        time.sleep(3)
    raise RuntimeError('Consumer did not complete certificate rollout: ' + service)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    if not os.isatty(0):
        raise RuntimeError('Interactive terminal required')
    if run(['hostname']).decode().strip() != 'pawbridge-k136-cp1':
        raise RuntimeError('Wrong host')
    for name in ('store-search', 'pawbridge-elasticsearch'):
        es = json.loads(run(K + ['-n', 'databases', 'get', 'elasticsearch', name, '-o', 'json']))
        if es['status'].get('health') != 'green':
            raise RuntimeError('Elasticsearch must be green: ' + name)
    ca = []
    for pod in ('store-search-es-default-0', 'pawbridge-elasticsearch-es-default-0'):
        pem = run(K + ['-n', 'databases', 'exec', pod, '-c', 'elasticsearch', '--',
                       'cat', '/usr/share/elasticsearch/config/http-certs/ca.crt']).decode()
        for block in certificates(pem):
            run(['openssl', 'x509', '-noout', '-checkend', '86400'], block.encode())
        ca.append(pem)
    pem = bundle(*ca)
    old_certificates = set(certificates(ca[0]))
    allowed_certificates = set(certificates(pem))
    os.umask(0o077)
    recovery = Path(tempfile.mkdtemp(prefix='pawbridge-es-trust-', dir='/tmp'))
    session = run(K + ['-n', 'vault', 'exec', 'vault-0', '--', 'mktemp', '-d',
                       '/tmp/pawbridge-es-trust.XXXXXX']).decode().strip()
    if not re.fullmatch(r'/tmp/pawbridge-es-trust\.[A-Za-z0-9]+', session):
        raise RuntimeError('Unexpected session path')
    prefix = K + ['-n', 'vault', 'exec', '-i', 'vault-0', '--', 'env', 'HOME=' + session, 'vault']
    def vault(args, data=None):
        return run(prefix + args, data)
    try:
        username = input('Vault admin username: ').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]+', username):
            raise ValueError('Invalid username')
        login = K + ['-n', 'vault', 'exec', '-it', 'vault-0', '--', 'sh', '-c',
                     'HOME="$1" vault login -method=userpass username="$2" >/dev/null',
                     'sh', session, username]
        if subprocess.run(login, timeout=180).returncode:
            raise RuntimeError('Vault login failed')
        before = {}
        for path in PATHS:
            entry = json.loads(vault(['kv', 'get', '-format=json', '-mount=secret', path]))['data']
            if set(entry['data']) != {'ca.crt', 'ca-sha256'}:
                raise RuntimeError('Unexpected trust fields: ' + path)
            present = set(certificates(entry['data']['ca.crt']))
            if not old_certificates.issubset(present) or not present.issubset(allowed_certificates):
                raise RuntimeError('Trust differs from approved source CA: ' + path)
            before[path] = entry
        (recovery / 'trust-before.json').write_text(json.dumps(before))
        snapshot = session + '/vault-before-trust.snapshot'
        vault(['operator', 'raft', 'snapshot', 'save', snapshot])
        raw = run(K + ['-n', 'vault', 'exec', 'vault-0', '--', 'cat', snapshot])
        (recovery / 'vault-before-trust.snapshot').write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        ready = {'snapshot': str(recovery / 'vault-before-trust.snapshot'), 'sha256': digest,
                 'receipt': str(recovery / 'off-vm-verified.json')}
        (recovery / 'ready.json').write_text(json.dumps(ready))
        print('BACKUP_READY ' + json.dumps(ready), flush=True)
        print('Waiting up to 10 minutes for Codex to verify the off-VM backup. No trust change yet.', flush=True)
        deadline = time.monotonic() + 600
        receipt = recovery / 'off-vm-verified.json'
        while not receipt.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError('Off-VM backup confirmation timed out; no trust change')
            time.sleep(1)
        if json.loads(receipt.read_text()).get('sha256') != digest:
            raise RuntimeError('Off-VM backup checksum does not match')
        for path, entry in before.items():
            if entry['data']['ca.crt'] == pem:
                print('Already matches:', path, flush=True)
                continue
            # CAS refuses to overwrite a concurrent edit. Earlier successful paths
            # retain both CAs, so a later failure does not revoke source trust.
            version = entry['metadata']['version']
            service = path.split('/')[2]
            previous_template = deployment(service)['spec']['template']
            payload = {'ca.crt': pem, 'ca-sha256': hashlib.sha256(pem.encode()).hexdigest()}
            vault(['kv', 'put', '-cas=' + str(version), '-mount=secret', path,
                   'ca.crt=-', 'ca-sha256=' + payload['ca-sha256']], pem.encode())
            after = json.loads(vault(['kv', 'get', '-format=json', '-mount=secret', path]))['data']['data']
            if after != payload:
                raise RuntimeError('Trust read-back differs: ' + path)
            print('Verified dual-CA trust:', path, flush=True)
            wait_consumer(service, pem, previous_template)
        print('SUCCESS: four Vault trust entries verified. VSO may restart consuming apps. No endpoint cutover.', flush=True)
    finally:
        try:
            vault(['token', 'revoke', '-self'])
        except Exception:
            print('Session revocation unverified; recovery files retained.', flush=True)
        else:
            # Delete only this invocation's known token and snapshot files.
            run(K + ['-n', 'vault', 'exec', 'vault-0', '--', 'rm', '-f',
                     session + '/.vault-token', session + '/vault-before-trust.snapshot'])
            # Vault may create a CLI cache beneath this invocation's private HOME.
            run(K + ['-n', 'vault', 'exec', 'vault-0', '--', 'rm', '-rf', '--', session + '/.cache'])
            run(K + ['-n', 'vault', 'exec', 'vault-0', '--', 'rmdir', session])
            print('Temporary Vault session revoked and removed.', flush=True)
        print('Recovery directory:', recovery, flush=True)

if __name__ == '__main__':
    main()
