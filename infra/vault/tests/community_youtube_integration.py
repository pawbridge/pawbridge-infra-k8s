"""Opt-in real Vault tests. Uses only task-owned, ephemeral Docker resources."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


ROOT = Path(__file__).resolve().parents[3]
POLICY_NAME = 'community-youtube-read'
SECRET_PATH = 'pawbridge/dev/community/youtube'
VAULT_IMAGE = 'hashicorp/vault@sha256:5be49781ecf78bfe775c5309c6a4d9f4e9e040b6c885c99eb2b12fb69855e1a2'
LABEL = 'pawbridge.test.home-video-vso'


class CheckFailed(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailed(message)


def command(args, body=None, timeout=30):
    result = subprocess.run(args, input=body, capture_output=True, timeout=timeout)
    require(result.returncode == 0, 'Command failed; credentials and raw output suppressed')
    return result.stdout


class IsolatedVault:
    def __init__(self):
        self.identifier = uuid.uuid4().hex
        self.name = 'pawbridge-video-vault-test-' + self.identifier[:12]
        self.docker = ['docker', '--context', 'default']
        self.root_token = 'isolated-fixture-' + uuid.uuid4().hex
        self.created = False
        self.policy = (ROOT / 'infra/vault/policies/community-youtube-read.hcl').read_text()
        self.checks = []

    def check(self, name):
        self.checks.append(name)
        print(json.dumps({'check': name, 'result': 'passed', 'valuesPrinted': False}), flush=True)

    def start(self):
        context = json.loads(command(self.docker + ['context', 'inspect', 'default']))[0]
        require(context['Endpoints']['docker']['Host'].startswith(('unix://', 'npipe://')),
                'Only the local default Docker context is permitted')
        image = json.loads(command(self.docker + ['image', 'inspect', VAULT_IMAGE]))[0]
        require(image['Config']['Labels']['version'] == '2.0.4', 'Vault image version differs')
        env = os.environ.copy()
        env['VAULT_DEV_ROOT_TOKEN_ID'] = self.root_token
        args = self.docker + [
            'run', '-d', '--pull=never', '--name', self.name,
            '--label', LABEL + '=' + self.identifier, '--network', 'none',
            '--memory', '384m', '--memory-swap', '384m', '--cpus', '0.5',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', '100:100', '--tmpfs', '/tmp:rw,size=32m,uid=100,gid=100',
            '--tmpfs', '/vault/file:rw,size=32m,uid=100,gid=100',
            '--tmpfs', '/vault/logs:rw,size=16m,uid=100,gid=100',
            '--env', 'SKIP_SETCAP=true', '--env', 'VAULT_DEV_ROOT_TOKEN_ID',
            VAULT_IMAGE, 'server', '-dev', '-dev-listen-address=127.0.0.1:8200',
        ]
        result = subprocess.run(args, env=env, capture_output=True, timeout=30)
        require(result.returncode == 0, 'Isolated Vault start failed; raw output suppressed')
        self.created = True
        state = json.loads(command(self.docker + ['inspect', self.name]))[0]
        require(state['HostConfig']['NetworkMode'] == 'none'
                and not state['HostConfig']['PortBindings']
                and not state['HostConfig']['Privileged']
                and not any(m['Type'] in ('bind', 'volume') for m in state['Mounts']),
                'Vault isolation contract differs')
        for _ in range(40):
            result = self.call(['status', '-format=json'], allow_failure=True)
            if result.returncode == 0:
                require(json.loads(result.stdout)['version'] == '2.0.4', 'Actual Vault version differs')
                break
            time.sleep(0.25)
        else:
            raise CheckFailed('Isolated Vault readiness timeout; raw logs suppressed')
        mounts = self.json(['secrets', 'list', '-format=json'])
        require(mounts['secret/']['options']['version'] == '2', 'Expected KV-v2 mount missing')
        self.check('real_vault_2_0_4_network_none_no_ports_no_operating_data')

    def call(self, args, body=b'', token=None, allow_failure=False):
        # Token travels over stdin, never an argument or printed response.
        script = ('VAULT_TOKEN=; IFS= read -r VAULT_TOKEN || test -n "$VAULT_TOKEN" || exit 99; '
                  'export VAULT_TOKEN; export VAULT_ADDR=http://127.0.0.1:8200; '
                  'exec vault "$@"')
        result = subprocess.run(self.docker + ['exec', '-i', self.name, 'sh', '-c', script, 'sh', *args],
                                input=((token or self.root_token) + '\n').encode() + body,
                                capture_output=True, timeout=20)
        if not allow_failure:
            status = re.search(rb'Code: (\d{3})', result.stderr)
            classification = ('HTTP ' + status.group(1).decode()) if status else 'CLI error'
            require(result.returncode == 0,
                    'Vault ' + args[0] + ' failed (' + classification + '); sensitive output suppressed')
        return result

    def json(self, args, body=b'', token=None):
        return json.loads(self.call(args, body, token).stdout)

    def write_policy(self, name, text):
        self.call(['policy', 'write', name, '-'], text.encode())

    def create_token(self, policy, ttl='10m'):
        auth = self.json(['token', 'create', '-format=json', '-no-default-policy',
                          '-policy=' + policy, '-ttl=' + ttl, '-explicit-max-ttl=' + ttl])['auth']
        require(auth['policies'] == [policy] and auth['renewable'], 'Token policy/renewability differs')
        return auth['client_token']

    def denied(self, args, token, body=b''):
        result = self.call(args, body, token, allow_failure=True)
        require(result.returncode != 0 and b'Code: 403' in result.stderr,
                'Expected Vault HTTP 403 was not observed; raw output suppressed')

    def put_fixture(self):
        self.call(['write', 'secret/data/' + SECRET_PATH, '-'],
                  json.dumps({'data': {'YOUTUBE_DATA_API_KEY': 'synthetic-youtube-key',
                                       'UNRELATED_FIELD': 'synthetic-unrelated-value'}}).encode())

    def token_tests(self):
        self.put_fixture()
        blocks = re.findall(r'path\s+"([^"]+)"\s*\{[^}]+\}', self.policy)
        legacy = '\n'.join(match.group() for match in re.finditer(r'path\s+"([^"]+)"\s*\{[^}]+\}',
                                                                  self.policy)
                           if match.group(1).startswith('secret/'))
        require(len(blocks) >= 2 and legacy.count('capabilities') == 2, 'Legacy fixture differs')
        self.write_policy('legacy-video-read', legacy)
        legacy_token = self.create_token('legacy-video-read')
        require(self.json(['kv', 'get', '-format=json', '-mount=secret', SECRET_PATH],
                          token=legacy_token)['data']['data']['YOUTUBE_DATA_API_KEY'] == 'synthetic-youtube-key',
                'Legacy token could not read its permitted fixture')
        for args in [['token', 'lookup', '-format=json'],
                     ['token', 'renew', '-format=json'], ['token', 'revoke', '-self']]:
            self.denied(args, legacy_token)
        self.check('legacy_kv_reads_work_but_three_self_endpoints_return_real_403')

        self.write_policy(POLICY_NAME, self.policy)
        token = self.create_token(POLICY_NAME)
        lookup = self.json(['token', 'lookup', '-format=json'], token=token)['data']
        require(lookup['policies'] == [POLICY_NAME] and 0 < lookup['ttl'] <= 600,
                'Self lookup policy or TTL differs')
        renewed = self.json(['token', 'renew', '-format=json', '-increment=1h'], token=token)['auth']
        require(0 < renewed['lease_duration'] <= 600 and renewed['policies'] == [POLICY_NAME],
                'Renewal exceeded the max TTL or changed policies')
        data = self.json(['kv', 'get', '-format=json', '-mount=secret', SECRET_PATH], token=token)
        require(data['data']['data']['YOUTUBE_DATA_API_KEY'] == 'synthetic-youtube-key', 'Fixture read differs')
        self.json(['kv', 'metadata', 'get', '-format=json', '-mount=secret', SECRET_PATH], token=token)
        self.check('fixed_policy_self_lookup_and_renew_keep_exact_policy_and_600s_max')

        for path in [SECRET_PATH + '-backup', 'pawbridge/dev/animal/private',
                     'pawbridge/dev/community/r2', 'pawbridge/dev/community/database']:
            self.denied(['read', '-format=json', 'secret/data/' + path], token)
        self.denied(['list', '-format=json', 'secret/metadata/pawbridge/dev/community'], token)
        self.denied(['write', 'secret/data/' + SECRET_PATH, '-'], token,
                    json.dumps({'data': {'YOUTUBE_DATA_API_KEY': 'must-not-be-written'}}).encode())
        self.denied(['policy', 'write', 'must-not-be-created', '-'], token, legacy.encode())
        self.denied(['token', 'create', '-format=json'], token)
        other_token = self.create_token(POLICY_NAME)
        for endpoint in ['lookup', 'renew', 'revoke']:
            self.denied(['write', '-format=json', 'auth/token/' + endpoint, '-'], token,
                        json.dumps({'token': other_token}).encode())
        self.call(['token', 'revoke', '-self'], token=other_token)
        self.check('other_secrets_list_write_policy_and_other_token_management_denied')

        self.call(['token', 'revoke', '-self'], token=token)
        self.denied(['token', 'lookup', '-format=json'], token)
        self.denied(['read', '-format=json', 'secret/data/' + SECRET_PATH], token)
        require(self.json(['kv', 'get', '-format=json', '-mount=secret', SECRET_PATH])
                ['data']['data']['YOUTUBE_DATA_API_KEY'] == 'synthetic-youtube-key',
                'Self revocation removed or changed the stored fixture')
        self.check('self_revocation_invalidates_token_without_removing_secret')

        short_token = self.create_token(POLICY_NAME, ttl='3s')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = self.call(['token', 'lookup', '-format=json'], token=short_token, allow_failure=True)
            if result.returncode:
                require(b'Code: 403' in result.stderr, 'Expiration failure was not HTTP 403')
                break
            time.sleep(0.5)
        else:
            raise CheckFailed('Expired token remained usable')
        self.denied(['read', '-format=json', 'secret/data/' + SECRET_PATH], short_token)
        self.check('accelerated_3s_token_expiration_denies_subsequent_secret_read')

    def close(self):
        if self.created:
            state = json.loads(command(self.docker + ['inspect', self.name]))[0]
            require(state['Config']['Labels'].get(LABEL) == self.identifier,
                    'Cleanup ownership mismatch; container retained')
            command(self.docker + ['rm', '-f', '-v', self.name])
            self.created = False
            print(json.dumps({'taskOwnedVaultRemoved': True, 'operatingResourcesModified': False}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-isolated', action='store_true',
                        help='Explicitly permit the documented local ephemeral Docker fixture')
    args = parser.parse_args()
    require(args.run_isolated, 'No test started: --run-isolated is required')
    fixture = IsolatedVault()
    try:
        fixture.start()
        fixture.token_tests()
        print(json.dumps({'result': 'passed', 'checks': fixture.checks,
                          'scope': 'real_vault_acl_and_token_lifecycle_only',
                          'vsoControllerTested': False, 'valuesPrinted': False}), flush=True)
    finally:
        fixture.close()


if __name__ == '__main__':
    try:
        main()
    except (CheckFailed, subprocess.TimeoutExpired) as error:
        print(json.dumps({'result': 'failed', 'reason': str(error) if isinstance(error, CheckFailed)
                          else 'Subprocess timeout; raw output suppressed', 'valuesPrinted': False}), flush=True)
        raise SystemExit(1)
