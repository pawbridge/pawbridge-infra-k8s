"""Offline contracts for Community-only YouTube key supply and video images."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
VSO = ROOT / 'gitops/security/community-youtube-vso'
COMMUNITY_REVISION = '82a9cfa648ebfb122fad1f44c16d843985eb611b'
COMMUNITY_DIGEST = 'sha256:13c3fcd3ea69dc0cfc087f509d226dbec0cc2b42efae68b7fa0c2bf3f494ae18'
MIGRATION_DIGEST = 'sha256:c7f2104e5e1b543f710c99f8c3eb9f7ba63a9ddb476c79953cfc843680fdd3b2'
GATEWAY_REVISION = '6ae772456bcea1b5e149585193188d50a7d5d83b'
GATEWAY_DIGEST = 'sha256:9e5b31a721dd510800d636a6e88bb1a12fe8e82153bce395284a89e4b905656d'


def read_yaml(path):
    return yaml.safe_load(path.read_text())


class HomeVideoRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        resources = read_yaml(VSO / 'kustomization.yaml')['resources']
        cls.resources = [read_yaml(VSO / name) for name in resources]
        cls.by_kind = {resource['kind']: resource for resource in cls.resources}
        cls.rendered = {}
        cls.values = {}
        helm = shlex.split(os.environ.get('HELM_COMMAND', 'helm'))
        for service in ('community-service', 'api-gateway'):
            values = ROOT / f'environments/prod/values/{service}.yaml'
            cls.values[service] = read_yaml(values)
            rendered = subprocess.check_output(
                [*helm, 'template', service, str(ROOT / f'charts/{service}'),
                 '--namespace', 'pawbridge', '--values', str(values)],
                text=True, timeout=30,
            )
            cls.rendered[service] = list(yaml.safe_load_all(rendered))

    def deployment(self, service):
        return next(resource for resource in self.rendered[service]
                    if resource and resource['kind'] == 'Deployment')

    def test_authentication_chain_is_namespace_scoped_and_tls_verified(self):
        self.assertEqual(4, len(self.resources))
        self.assertEqual({'ServiceAccount', 'VaultConnection', 'VaultAuth', 'VaultStaticSecret'},
                         set(self.by_kind))
        self.assertTrue(all(r['metadata']['namespace'] == 'pawbridge' for r in self.resources))
        account = self.by_kind['ServiceAccount']
        self.assertFalse(account['automountServiceAccountToken'])
        auth = self.by_kind['VaultAuth']['spec']
        connection = self.by_kind['VaultConnection']
        self.assertEqual(connection['metadata']['name'], auth['vaultConnectionRef'])
        self.assertEqual('kubernetes', auth['method'])
        self.assertEqual('kubernetes', auth['mount'])
        self.assertEqual({
            'role': 'community-youtube-read',
            'serviceAccount': account['metadata']['name'],
            'audiences': ['vault'],
            'tokenExpirationSeconds': 600,
        }, auth['kubernetes'])
        self.assertEqual('https://vault.vault.svc.cluster.local:8200', connection['spec']['address'])
        self.assertEqual('vault.vault.svc.cluster.local', connection['spec']['tlsServerName'])
        self.assertEqual('vault-internal-ca', connection['spec']['caCertSecretRef'])
        self.assertIs(False, connection['spec']['skipTLSVerify'])
        self.assertEqual('10s', connection['spec']['timeout'])
        self.assertEqual(self.by_kind['VaultAuth']['metadata']['name'],
                         self.by_kind['VaultStaticSecret']['spec']['vaultAuthRef'])

    def test_vault_role_and_policy_allow_only_the_video_path(self):
        role = json.loads((ROOT / 'infra/vault/roles/community-youtube-read.json').read_text())
        self.assertEqual({
            'bound_service_account_names': ['community-youtube-vault-auth'],
            'bound_service_account_namespaces': ['pawbridge'],
            'audience': 'vault',
            'token_policies': ['community-youtube-read'],
            'token_no_default_policy': True,
            'token_ttl': '10m',
            'token_max_ttl': '10m',
        }, role)
        policy = (ROOT / 'infra/vault/policies/community-youtube-read.hcl').read_text()
        policy = re.sub(r'#.*', '', policy).strip()
        blocks = re.findall(r'path\s+"([^"]+)"\s*\{([^}]+)\}', policy)
        self.assertEqual({
            'secret/data/pawbridge/dev/community/youtube',
            'secret/metadata/pawbridge/dev/community/youtube',
        }, {path for path, _ in blocks})
        self.assertEqual(2, len(blocks))
        for _, body in blocks:
            self.assertRegex(body.strip(), r'^capabilities\s*=\s*\["read"\]$')
        self.assertEqual('', re.sub(r'path\s+"[^"]+"\s*\{[^}]+\}', '', policy).strip())

    def test_destination_whitelist_excludes_raw_and_unrelated_keys(self):
        spec = self.by_kind['VaultStaticSecret']['spec']
        self.assertEqual('secret', spec['mount'])
        self.assertEqual('kv-v2', spec['type'])
        self.assertEqual('pawbridge/dev/community/youtube', spec['path'])
        self.assertEqual({
            'create': True, 'overwrite': False, 'name': 'community-youtube-auth',
            'transformation': {'excludeRaw': True, 'includes': ['^YOUTUBE_DATA_API_KEY$']},
        }, spec['destination'])
        patterns = spec['destination']['transformation']['includes']
        source_keys = {'YOUTUBE_DATA_API_KEY', '_raw', 'R2_ACCESS_KEY_ID',
                       'R2_SECRET_ACCESS_KEY', 'postgres-password', 'JWT_SECRET',
                       'YOUTUBE_DATA_API_KEY_BACKUP', 'PREFIX_YOUTUBE_DATA_API_KEY'}
        selected = {key for key in source_keys if any(re.search(p, key) for p in patterns)}
        self.assertEqual({'YOUTUBE_DATA_API_KEY'}, selected)

    def test_key_rotation_targets_only_community(self):
        spec = self.by_kind['VaultStaticSecret']['spec']
        self.assertEqual('1m', spec['refreshAfter'])
        self.assertIs(True, spec['hmacSecretData'])
        self.assertEqual([{'kind': 'Deployment', 'name': 'community-service'}],
                         spec['rolloutRestartTargets'])

    def test_community_render_requires_video_secret_and_preserves_db_and_r2(self):
        deployment = self.deployment('community-service')
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        container = pod['containers'][0]
        self.assertEqual('dorosiya/pawbridge-community-service@' + COMMUNITY_DIGEST,
                         container['image'])
        self.assertEqual([
            {'secretRef': {'name': 'community-youtube-auth', 'optional': False}},
            {'secretRef': {'name': 'community-service-r2-secrets-vso', 'optional': False}},
        ], container['envFrom'])
        env = {item['name']: item for item in container['env']}
        self.assertNotIn('YOUTUBE_DATA_API_KEY', env)
        self.assertEqual({'secretKeyRef': {'name': 'community-postgresql-auth',
                                          'key': 'postgres-password'}},
                         env['SPRING_DATASOURCE_PASSWORD']['valueFrom'])
        self.assertEqual('jdbc:postgresql://pawbridge-postgresql.databases.svc.cluster.local:5432/pawbridge',
                         env['SPRING_DATASOURCE_URL']['value'])
        self.assertEqual('pawbridge_community_app', env['SPRING_DATASOURCE_USERNAME']['value'])
        self.assertEqual('pawbridge_community', env['SPRING_DATASOURCE_HIKARI_SCHEMA']['value'])
        self.assertEqual('validate', env['SPRING_JPA_HIBERNATE_DDL_AUTO']['value'])
        self.assertEqual('never', env['SPRING_SQL_INIT_MODE']['value'])

    def test_gateway_render_changes_image_without_receiving_video_key(self):
        container = self.deployment('api-gateway')['spec']['template']['spec']['containers'][0]
        self.assertEqual('dorosiya/pawbridge-api-gateway@' + GATEWAY_DIGEST, container['image'])
        self.assertEqual([{'secretRef': {'name': 'api-gateway-secrets-vso', 'optional': False}}],
                         container['envFrom'])
        self.assertNotIn('YOUTUBE_DATA_API_KEY', {item['name'] for item in container['env']})
        self.assertEqual('sha-' + GATEWAY_REVISION, self.values['api-gateway']['image']['tag'])

    def test_migration_metadata_matches_source_but_no_job_is_enabled(self):
        values = self.values['community-service']
        self.assertEqual('sha-' + COMMUNITY_REVISION, values['image']['tag'])
        migration = values['schemaMigration']
        self.assertEqual(COMMUNITY_REVISION, migration['sourceRevision'])
        self.assertEqual(COMMUNITY_DIGEST, migration['apiImageDigest'])
        self.assertEqual('dorosiya/pawbridge-community-service@' + MIGRATION_DIGEST,
                         migration['image'])
        self.assertIs(False, migration['enabled'])
        for resources in self.rendered.values():
            self.assertFalse(any(r and r['kind'] == 'Job' for r in resources))


if __name__ == '__main__':
    unittest.main()
