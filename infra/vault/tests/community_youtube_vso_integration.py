"""Opt-in real VSO/Kubernetes/Vault integration, with no operating credentials."""
import argparse
import base64
from datetime import datetime
import io
import ipaddress
import json
import re
import subprocess
import tarfile
import time

import yaml

from community_youtube_integration import (
    CheckFailed, IsolatedVault, LABEL, POLICY_NAME, ROOT, SECRET_PATH,
    VAULT_IMAGE, command, require,
)


K3S_IMAGE = 'rancher/k3s@sha256:08fdebd14db9ab7d5ea821d5bfa95d02341a6ef886842fcc8d9dfd0e9fa9e0cd'
VSO_IMAGE = 'hashicorp/vault-secrets-operator@sha256:1314beb4df53650d1a8c0f70eab1de516e4362329a541156fef5acefbdc18cc8'
NAMESPACE = 'pawbridge'
ACCOUNT = 'community-youtube-vault-auth'
DESTINATION = 'community-youtube-auth'


def audit_time(entry):
    # Vault emits nanoseconds; Python 3.10 accepts at most microseconds here.
    stamp = re.sub(r'(\.\d{6})\d+', r'\1', entry['time']).replace('Z', '+00:00')
    return datetime.fromisoformat(stamp)


class IsolatedVso(IsolatedVault):
    def __init__(self):
        super().__init__()
        prefix = 'pawbridge-video-vso-' + self.identifier[:12]
        self.network = prefix + '-net'
        self.k3s = prefix + '-k3s'
        self.operator = prefix + '-operator'
        self.containers = []
        self.network_created = False
        self.kube_uid = None

    def create_container(self, name, image, args, memory, cpus, tmpfs=(), env=None):
        options = self.docker + [
            'create', '--pull=never', '--name', name, '--label', LABEL + '=' + self.identifier,
            '--network', self.network, '--memory', memory, '--memory-swap', memory,
            '--cpus', cpus, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        ]
        if name == self.k3s:
            options += ['--network-alias', 'k3s.test', '--ip', self.k3s_ip]
        if name == self.name:
            options += ['--network-alias', 'vault.test', '--user', '100:100', '--env', 'SKIP_SETCAP=true']
        for mount in tmpfs:
            options += ['--tmpfs', mount]
        process_env = None
        if env:
            process_env = dict(__import__('os').environ, **env)
            for key in env:
                options += ['--env', key]
        result = subprocess.run(options + [image, *args], env=process_env, capture_output=True, timeout=30)
        marker = 'explicit subnet required' if b'user configured subnets' in result.stderr else 'Docker CLI'
        require(result.returncode == 0, 'Fixture container creation failed (' + marker + '); raw output suppressed')
        self.containers.append(name)
        state = json.loads(command(self.docker + ['inspect', name]))[0]
        require(state['HostConfig']['NetworkMode'] == self.network
                and not state['HostConfig']['PortBindings']
                and not state['HostConfig']['Privileged']
                and not any(m['Type'] in ('bind', 'volume') for m in state['Mounts']),
                'Container isolation differs')

    def copy_files(self, container, files, uid):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            directory = tarfile.TarInfo('fixture')
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o755
            directory.uid = directory.gid = uid
            archive.addfile(directory)
            for filename, contents in files.items():
                require('/' not in filename and filename not in ('.', '..'), 'Unsafe fixture filename')
                entry = tarfile.TarInfo('fixture/' + filename)
                entry.uid = entry.gid = uid
                entry.mode = 0o600
                entry.size = len(contents)
                archive.addfile(entry, io.BytesIO(contents))
        command(self.docker + ['cp', '-a', '-', container + ':/'], stream.getvalue())

    def kube(self, *args, body=None, optional=False):
        result = subprocess.run(self.docker + [
            'exec', '-i', self.k3s, '/bin/kubectl',
            '--kubeconfig=/etc/rancher/k3s/k3s.yaml', '--request-timeout=10s', *args,
        ], input=body, capture_output=True, timeout=20)
        if optional and result.returncode:
            require(b'NotFound' in result.stderr, 'Kubernetes query failed for a reason other than absence')
            return None
        require(result.returncode == 0, 'Fixture Kubernetes command failed; raw output suppressed')
        return result.stdout

    def kube_json(self, *args, optional=False):
        output = self.kube(*args, '-o', 'json', optional=optional)
        return json.loads(output) if output is not None else None

    def apply(self, resources):
        require(self.kube_json('get', 'namespace', 'kube-system')['metadata']['uid'] == self.kube_uid,
                'Fixture Kubernetes identity changed')
        self.kube('apply', '-f', '-', body=json.dumps({
            'apiVersion': 'v1', 'kind': 'List', 'items': resources,
        }).encode())

    def call(self, args, body=b'', token=None, allow_failure=False):
        script = ('VAULT_TOKEN=; IFS= read -r VAULT_TOKEN || test -n "$VAULT_TOKEN" || exit 99; '
                  'export VAULT_TOKEN; export VAULT_ADDR=https://vault.test:8200; '
                  'export VAULT_CACERT=/vault/file/vault-ca.pem; export VAULT_TLS_SERVER_NAME=vault.test; '
                  'exec vault "$@"')
        result = subprocess.run(self.docker + ['exec', '-i', self.name, 'sh', '-c', script, 'sh', *args],
                                input=((token or self.root_token) + '\n').encode() + body,
                                capture_output=True, timeout=20)
        if not allow_failure:
            code = re.search(rb'Code: (\d{3})', result.stderr)
            require(result.returncode == 0, 'Fixture Vault command failed ('
                    + (code.group(1).decode() if code else 'CLI/TLS') + '); raw output suppressed')
        return result

    def poll(self, check, timeout, description):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for name in self.containers:
                state = json.loads(command(self.docker + ['inspect', name]))[0]['State']
                if state['Status'] == 'exited':
                    logs = subprocess.run(self.docker + ['logs', name], capture_output=True, timeout=10)
                    blob = (logs.stdout + logs.stderr).lower()
                    markers = [m for m in (b'address already in use', b'permission denied',
                                          b'no default routes', b'error initializing listener',
                                          b'no matches for kind') if m in blob]
                    role = 'k3s' if name == self.k3s else 'vso' if name == self.operator else 'vault'
                    raise CheckFailed('Fixture ' + role + ' exited with code ' + str(state['ExitCode'])
                                      + '; safe markers=' + ','.join(m.decode() for m in markers)
                                      + '; raw logs suppressed')
            if check():
                return
            time.sleep(1)
        raise CheckFailed(description + ' timed out; raw responses suppressed')

    def start(self):
        context = json.loads(command(self.docker + ['context', 'inspect', 'default']))[0]
        require(context['Endpoints']['docker']['Host'].startswith(('unix://', 'npipe://')),
                'Only the local default Docker context is permitted')
        for image in (K3S_IMAGE, VSO_IMAGE, VAULT_IMAGE):
            command(self.docker + ['image', 'inspect', image])
        ids = command(self.docker + ['network', 'ls', '--quiet']).decode().split()
        occupied = []
        for network in json.loads(command(self.docker + ['network', 'inspect', *ids])):
            occupied.extend(ipaddress.ip_network(c['Subnet']) for c in (network['IPAM']['Config'] or [])
                            if c.get('Subnet') and ':' not in c['Subnet'])
        candidates = [ipaddress.ip_network('198.18.' + str(n) + '.0/24') for n in range(128, 192)]
        available = next((c for c in candidates if not any(c.overlaps(o) for o in occupied)), None)
        require(available is not None, 'No non-overlapping isolated test subnet available')
        command(self.docker + ['network', 'create', '--internal', '--subnet', str(available),
                               '--label', LABEL + '=' + self.identifier, self.network])
        self.network_created = True
        net = json.loads(command(self.docker + ['network', 'inspect', self.network]))[0]
        require(net['Internal'] and net['Labels'].get(LABEL) == self.identifier, 'Network isolation differs')
        subnet = ipaddress.ip_network(net['IPAM']['Config'][0]['Subnet'])
        require(subnet.version == 4 and subnet.num_addresses > 16, 'Unexpected isolated Docker subnet')
        self.k3s_ip = str(subnet.network_address + 10)
        self.create_container(self.k3s, K3S_IMAGE, [
            'server', '--disable-agent', '--disable=traefik,servicelb,local-storage,metrics-server,coredns',
            '--disable-helm-controller', '--disable-cloud-controller', '--disable-network-policy',
            '--egress-selector-mode=disabled', '--tls-san=k3s.test', '--bind-address=0.0.0.0',
            '--node-ip=' + self.k3s_ip, '--advertise-address=' + self.k3s_ip,
        ], '1536m', '1', tmpfs=(
            '/var/lib/rancher/k3s:rw,size=384m', '/var/lib/kubelet:rw,size=16m',
            '/var/lib/cni:rw,size=16m', '/var/log:rw,size=16m', '/tmp:rw,size=32m',
        ))
        command(self.docker + ['start', self.k3s])

        def api_ready():
            result = subprocess.run(self.docker + [
                'exec', self.k3s, '/bin/kubectl', '--kubeconfig=/etc/rancher/k3s/k3s.yaml',
                '--request-timeout=3s', 'get', '--raw=/readyz',
            ], capture_output=True, timeout=8)
            return result.returncode == 0 and result.stdout.strip() == b'ok'

        self.poll(api_ready, 90, 'Real fixture Kubernetes readiness')
        self.kube_uid = self.kube_json('get', 'namespace', 'kube-system')['metadata']['uid']
        require(self.kube_json('version')['serverVersion']['gitVersion'].startswith('v1.36.1'),
                'Kubernetes version differs')
        config = yaml.safe_load(command(self.docker + ['exec', self.k3s, 'cat', '/etc/rancher/k3s/k3s.yaml']))
        config['clusters'][0]['cluster']['server'] = 'https://k3s.test:6443'
        self.kube_ca = base64.b64decode(config['clusters'][0]['cluster']['certificate-authority-data'])

        self.create_container(self.name, VAULT_IMAGE, [
            'server', '-dev', '-dev-tls', '-dev-tls-cert-dir=/vault/file',
            '-dev-tls-san=vault.test', '-dev-listen-address=0.0.0.0:8200',
        ],
                              '384m', '0.5', tmpfs=(
                                  '/tmp:rw,size=32m,uid=100,gid=100',
                                  '/vault/file:rw,size=32m,uid=100,gid=100',
                                  '/vault/logs:rw,size=32m,uid=100,gid=100',
                              ), env={'VAULT_DEV_ROOT_TOKEN_ID': self.root_token})
        command(self.docker + ['start', self.name])
        self.poll(lambda: self.call(['status', '-format=json'], allow_failure=True).returncode == 0,
                  30, 'Real TLS Vault readiness')
        require(self.json(['status', '-format=json'])['version'] == '2.0.4', 'Vault version differs')
        self.vault_ca = command(self.docker + ['exec', self.name, 'cat', '/vault/file/vault-ca.pem'])
        self.call(['audit', 'enable', 'file', 'file_path=/vault/logs/audit.json'])
        self.check('real_kubernetes_1_36_1_and_vault_2_0_4_tls_on_internal_network')

        self.create_container(self.operator, VSO_IMAGE, [
            '--leader-elect=false', '--metrics-bind-address=0', '--health-probe-bind-address=:8081',
            '--max-concurrent-reconciles=1', '--client-cache-size=10', '--client-cache-num-locks=2',
            '--backoff-initial-interval=1s', '--backoff-max-interval=3s',
        ], '512m', '0.5', tmpfs=('/tmp:rw,size=16m,uid=65532,gid=65532',), env={
            'KUBECONFIG': '/fixture/kubeconfig', 'OPERATOR_NAMESPACE': 'vault-secrets-operator-system',
        })
        self.copy_files(self.operator, {'kubeconfig': yaml.safe_dump(config).encode()}, 65532)
        blob = command(self.docker + ['cp', self.operator + ':/scripts/crds', '-'])
        with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
            crds = []
            for entry in archive.getmembers():
                if entry.isfile() and entry.name.endswith('.yaml'):
                    crds.extend(r for r in yaml.safe_load_all(archive.extractfile(entry).read()) if r)
        require(len(crds) >= 8 and all(r['kind'] == 'CustomResourceDefinition' for r in crds),
                'Image CRD bundle differs')
        self.apply(crds)
        self.kube('wait', '--for=condition=Established', 'crd', '--all', '--timeout=30s')
        self.setup_auth()

    def setup_auth(self):
        def account(name, namespace=NAMESPACE):
            return {'apiVersion': 'v1', 'kind': 'ServiceAccount',
                    'metadata': {'name': name, 'namespace': namespace}, 'automountServiceAccountToken': False}
        self.apply([
            *({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name}}
              for name in (NAMESPACE, 'vault-secrets-operator-system', 'wrong-video-namespace')),
            account('vault-test-reviewer'), account('wrong-video-account'),
            account(ACCOUNT, 'wrong-video-namespace'),
            {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'ClusterRoleBinding',
             'metadata': {'name': 'isolated-vault-reviewer'},
             'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole',
                         'name': 'system:auth-delegator'},
             'subjects': [{'kind': 'ServiceAccount', 'name': 'vault-test-reviewer', 'namespace': NAMESPACE}]},
            {'apiVersion': 'v1', 'kind': 'Secret',
             'metadata': {'name': 'vault-internal-ca', 'namespace': NAMESPACE},
             'data': {'ca.crt': base64.b64encode(self.vault_ca).decode()}},
            {'apiVersion': 'apps/v1', 'kind': 'Deployment',
             'metadata': {'name': 'community-service', 'namespace': NAMESPACE},
             'spec': {'replicas': 0, 'selector': {'matchLabels': {'app': 'isolated-community-fixture'}},
                      'template': {'metadata': {'labels': {'app': 'isolated-community-fixture'}},
                                   'spec': {'containers': [{'name': 'fixture',
                                                           'image': 'fixture.invalid/never-started:0'}]}}}},
        ])
        reviewer = self.kube('create', 'token', 'vault-test-reviewer', '-n', NAMESPACE, '--duration=20m').strip()
        self.call(['auth', 'enable', 'kubernetes'])
        self.call(['write', 'auth/kubernetes/config', '-'], json.dumps({
            'kubernetes_host': 'https://k3s.test:6443', 'kubernetes_ca_cert': self.kube_ca.decode(),
            'token_reviewer_jwt': reviewer.decode(), 'disable_local_ca_jwt': True,
            'disable_iss_validation': True,
        }).encode())
        self.role = json.loads((ROOT / 'infra/vault/roles/community-youtube-read.json').read_text())
        self.call(['write', 'auth/kubernetes/role/' + POLICY_NAME, '-'], json.dumps(self.role).encode())
        actual = self.json(['read', '-format=json', 'auth/kubernetes/role/' + POLICY_NAME])['data']
        require(actual['token_ttl'] == actual['token_max_ttl'] == 600
                and actual['token_no_default_policy'], 'Actual role lifetime/default policy differs')
        resources = []
        directory = ROOT / 'gitops/security/community-youtube-vso'
        for filename in yaml.safe_load((directory / 'kustomization.yaml').read_text())['resources']:
            resource = yaml.safe_load((directory / filename).read_text())
            if resource['kind'] == 'VaultConnection':
                resource['spec']['address'] = 'https://vault.test:8200'
                resource['spec']['tlsServerName'] = 'vault.test'
            resources.append(resource)
        self.apply(resources)
        self.put_fixture()
        legacy = '\n'.join(match.group() for match in re.finditer(r'path\s+"([^"]+)"\s*\{[^}]+\}',
                                                                  self.policy)
                           if match.group(1).startswith('secret/'))
        self.write_policy(POLICY_NAME, legacy)
        command(self.docker + ['start', self.operator])

    def audit(self):
        data = command(self.docker + ['exec', self.name, 'cat', '/vault/logs/audit.json'])
        return [json.loads(line) for line in data.splitlines() if line]

    def successful_logins(self):
        return [e for e in self.audit() if e.get('type') == 'response'
                and e.get('request', {}).get('path') == 'auth/kubernetes/login'
                and not e.get('error') and e.get('response', {}).get('auth', {}).get('policies') == [POLICY_NAME]]

    def ready_secret(self, expected='synthetic-youtube-key'):
        status = self.kube_json('get', 'vaultstaticsecret', 'community-youtube', '-n', NAMESPACE)
        conditions = {c['type']: c['status'] for c in status.get('status', {}).get('conditions', [])}
        if conditions.get('Ready') != 'True' or conditions.get('Healthy') != 'True':
            return False
        secret = self.kube_json('get', 'secret', DESTINATION, '-n', NAMESPACE, optional=True)
        if not secret:
            return False
        require(set(secret.get('data', {})) == {'YOUTUBE_DATA_API_KEY'}, 'Destination key whitelist differs')
        require(secret['metadata']['ownerReferences'] == [{
            'apiVersion': status['apiVersion'], 'kind': status['kind'],
            'name': status['metadata']['name'], 'uid': status['metadata']['uid'],
        }], 'Destination ownership differs')
        return base64.b64decode(secret['data']['YOUTUBE_DATA_API_KEY']).decode() == expected

    def controller_tests(self):
        def failed_renewal():
            status = self.kube_json('get', 'vaultstaticsecret', 'community-youtube', '-n', NAMESPACE)
            return any('renew-self' in c.get('message', '') and '403' in c.get('message', '')
                       for c in status.get('status', {}).get('conditions', []))
        self.poll(failed_renewal, 60, 'Original VSO renew-self HTTP 403')
        require(self.kube_json('get', 'secret', DESTINATION, '-n', NAMESPACE, optional=True) is None,
                'Original policy unexpectedly produced a destination Secret')
        self.check('actual_vso_1_4_1_legacy_renew_self_403_and_no_destination_secret')
        self.write_policy(POLICY_NAME, self.policy)
        self.poll(self.ready_secret, 90, 'VSO destination synchronization after policy fix')
        self.check('actual_vso_fixed_policy_creates_owned_whitelisted_secret_with_tls_verification')

        for account, namespace, audience in [
            ('wrong-video-account', NAMESPACE, 'vault'),
            (ACCOUNT, 'wrong-video-namespace', 'vault'),
            (ACCOUNT, NAMESPACE, 'wrong-video-audience'),
        ]:
            jwt = self.kube('create', 'token', account, '-n', namespace, '--audience=' + audience,
                            '--duration=10m').strip()
            denied = self.call(['write', '-format=json', 'auth/kubernetes/login', '-'],
                               json.dumps({'role': POLICY_NAME, 'jwt': jwt.decode()}).encode(),
                               allow_failure=True)
            require(denied.returncode != 0 and re.search(rb'Code: (400|403)', denied.stderr),
                    'Wrong account/namespace/audience was not rejected by real Vault Kubernetes auth')
        self.check('real_kubernetes_auth_rejects_wrong_account_namespace_and_audience')

        first = self.successful_logins()[-1]
        start = time.monotonic()
        original_auth_token = first['response']['auth']['client_token']  # Audit HMAC, not the token value.
        require(original_auth_token.startswith('hmac-sha256:'), 'Expected audit token HMAC missing')
        require(first['response']['auth'].get('token_ttl') == 600,
                'Audit token initial TTL differs from the unchanged 600s role')
        first_issued = audit_time(first)
        self.call(['write', 'secret/data/' + SECRET_PATH, '-'], json.dumps({
            'data': {'YOUTUBE_DATA_API_KEY': 'synthetic-rotated-key', 'UNRELATED_FIELD': 'excluded'},
        }).encode())
        self.poll(lambda: self.ready_secret('synthetic-rotated-key'), 90, 'Actual Secret value refresh')
        self.check('actual_vso_refreshes_rotated_synthetic_key_without_copying_unrelated_fields')
        next_progress = time.monotonic()
        deadline = start + 730
        reauthenticated = False
        renewed = False
        while time.monotonic() < deadline:
            events = self.audit()
            renewals = [e for e in events if e.get('type') == 'response'
                        and e.get('request', {}).get('path') == 'auth/token/renew-self'
                        and not e.get('error') and e.get('auth', {}).get('policies') == [POLICY_NAME]
                        and e.get('auth', {}).get('client_token') == original_auth_token
                        and (audit_time(e) - first_issued).total_seconds() >= 30]
            renewed = bool(renewals)  # Exclude the immediate renewal performed during initial login.
            logins = self.successful_logins()
            reauthenticated = any(
                e['response']['auth']['client_token'] != original_auth_token
                and any(audit_time(e) > audit_time(renewal) for renewal in renewals)
                and e['response']['auth'].get('token_ttl') == 600 for e in logins
            )
            if renewed and reauthenticated and time.monotonic() - start >= 601:
                break
            if time.monotonic() >= next_progress:
                print(json.dumps({'stage': 'observing_real_600s_role_lifetime',
                                  'elapsedSeconds': round(time.monotonic() - start),
                                  'successfulRenewalObserved': renewed,
                                  'newLoginObserved': reauthenticated, 'valuesPrinted': False}), flush=True)
                next_progress = time.monotonic() + 30
            time.sleep(3)
        require(renewed and reauthenticated and time.monotonic() - start >= 601,
                'VSO did not renew and reauthenticate across the real 600s lifetime')
        self.poll(lambda: self.ready_secret('synthetic-rotated-key'), 30, 'Secret after reauthentication')
        self.check('actual_vso_renewal_and_reauthentication_across_unchanged_600s_role_lifetime')
        self.call(['write', 'secret/data/' + SECRET_PATH, '-'], json.dumps({
            'data': {'YOUTUBE_DATA_API_KEY': 'synthetic-after-reauthentication', 'UNRELATED_FIELD': 'excluded'},
        }).encode())
        self.poll(lambda: self.ready_secret('synthetic-after-reauthentication'), 90,
                  'New Secret refresh after real role lifetime')
        self.check('actual_vso_reads_new_value_after_reauthentication_not_only_stale_secret_status')

    def close(self):
        failures = []
        for container in reversed(self.containers):
            try:
                state = json.loads(command(self.docker + ['inspect', container]))[0]
                require(state['Config']['Labels'].get(LABEL) == self.identifier, 'Cleanup ownership mismatch')
                command(self.docker + ['rm', '-f', '-v', container])
            except (CheckFailed, subprocess.TimeoutExpired):
                failures.append(container)
        if self.network_created and not failures:
            state = json.loads(command(self.docker + ['network', 'inspect', self.network]))[0]
            require(state['Labels'].get(LABEL) == self.identifier and not state.get('Containers'),
                    'Network cleanup ownership/use differs')
            command(self.docker + ['network', 'rm', self.network])
        require(not failures, 'Some task containers could not be cleaned up; unrelated resources untouched')
        print(json.dumps({'taskContainersAndInternalNetworkRemoved': True,
                          'operatingResourcesModified': False, 'valuesPrinted': False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated', action='store_true')
    args = parser.parse_args()
    require(args.run_isolated, 'No test started: --run-isolated is required')
    fixture = IsolatedVso()
    try:
        fixture.start()
        fixture.controller_tests()
    finally:
        fixture.close()
    print(json.dumps({'result': 'passed', 'checks': fixture.checks,
                      'scope': 'real_vso_kubernetes_auth_secret_sync_and_600s_token_lifecycle',
                      'operatingApplicationTested': False, 'valuesPrinted': False}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (CheckFailed, subprocess.TimeoutExpired) as error:
        print(json.dumps({'result': 'failed', 'reason': str(error) if isinstance(error, CheckFailed)
                          else 'Subprocess timeout; raw output suppressed', 'valuesPrinted': False}), flush=True)
        raise SystemExit(1)
