"""Offline release contracts; no Kubernetes, Redis or database connections."""
import copy
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
COMMUNITY_REVISION = '99282fcb6391b0f56ea847b600a0d1808d991d66'
COMMUNITY_DIGEST = 'sha256:391045fbcf029a326edab1f082d2d26c2d88e5468ae6413722d17cbb537251bf'
MIGRATION_DIGEST = 'sha256:2c4e24279d5427fc132e8247651959c5bc6198c3e125e2c139247c6006b7638a'
GATEWAY_REVISION = '6e498eb0118ee8114193f643da0971e2091c32a2'
GATEWAY_DIGEST = 'sha256:9ae73f83e3fb7ef63a81d140a97b4b1812c32f21309c8f8530bb186019d7affe'


def read_yaml(path):
    return yaml.safe_load(path.read_text())


def render(values=None):
    helm = shlex.split(os.environ.get('HELM_COMMAND', 'helm'))
    command = [*helm, 'template', 'community-service',
               str(ROOT / 'charts/community-service'), '--namespace', 'pawbridge']
    with tempfile.TemporaryDirectory() as directory:
        if values is not None:
            file = Path(directory) / 'values.yaml'
            file.write_text(yaml.safe_dump(values))
            command.extend(['--values', str(file)])
        return subprocess.run(command, capture_output=True, text=True, timeout=30)


class MemberChatRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.values = read_yaml(ROOT / 'environments/prod/values/community-service.yaml')
        result = render(cls.values)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        deployment = next(item for item in cls.resources if item['kind'] == 'Deployment')
        cls.pod = deployment['spec']['template']['spec']
        cls.container = cls.pod['containers'][0]
        cls.env = {item['name']: item for item in cls.container['env']}

    def test_production_chat_uses_distinct_namespace_and_exact_https_origins(self):
        self.assertEqual('true', self.env['MEMBER_CHAT_ENABLED']['value'])
        self.assertEqual('pawbridge-prod', self.env['MEMBER_CHAT_NAMESPACE']['value'])
        self.assertEqual({'https://www.pawbridge.kr', 'https://pawbridge.kr'},
                         set(self.env['MEMBER_CHAT_ALLOWED_ORIGINS']['value'].split(',')))

    def test_redis_credentials_are_secret_backed_and_not_overridden_by_url(self):
        self.assertEqual({'secretKeyRef': {'name': 'redis-auth', 'key': 'redis-password'}},
                         self.env['SPRING_DATA_REDIS_PASSWORD']['valueFrom'])
        self.assertNotIn('value', self.env['SPRING_DATA_REDIS_PASSWORD'])
        self.assertNotIn('SPRING_DATA_REDIS_URL', self.env)
        self.assertEqual('redis-master.databases.svc.cluster.local',
                         self.env['SPRING_DATA_REDIS_HOST']['value'])
        self.assertEqual('6379', self.env['SPRING_DATA_REDIS_PORT']['value'])

    def test_community_and_migration_are_the_approved_same_source_pair(self):
        self.assertEqual('sha-' + COMMUNITY_REVISION, self.values['image']['tag'])
        self.assertEqual('dorosiya/pawbridge-community-service@' + COMMUNITY_DIGEST,
                         self.container['image'])
        migration = self.values['schemaMigration']
        self.assertEqual(COMMUNITY_REVISION, migration['sourceRevision'])
        self.assertEqual(COMMUNITY_DIGEST, migration['apiImageDigest'])
        self.assertEqual('dorosiya/pawbridge-community-service@' + MIGRATION_DIGEST,
                         migration['image'])
        self.assertIs(False, migration['enabled'])
        self.assertIs(False, migration['existingSchemaVerified'])
        self.assertEqual('', migration['recoveryReference'])
        self.assertFalse(any(item['kind'] == 'Job' for item in self.resources))

    def test_gateway_uses_the_approved_websocket_capable_image(self):
        values = read_yaml(ROOT / 'environments/prod/values/api-gateway.yaml')
        self.assertEqual('sha-' + GATEWAY_REVISION, values['image']['tag'])
        self.assertEqual(GATEWAY_DIGEST, values['image']['digest'])
        self.assertEqual('http://community-service.pawbridge.svc.cluster.local:8082',
                         values['env']['COMMUNITY_SERVICE_URL'])

    def test_shared_redis_supply_keeps_permissions_and_existing_consumers(self):
        secret = read_yaml(ROOT / 'gitops/security/redis-auth-vso/client-vault-static-secret.yaml')
        spec = secret['spec']
        self.assertEqual('pawbridge', secret['metadata']['namespace'])
        self.assertEqual('secret', spec['mount'])
        self.assertEqual('pawbridge/dev/redis/auth', spec['path'])
        self.assertEqual('redis-client-vault-auth', spec['vaultAuthRef'])
        self.assertEqual({
            'create': True, 'overwrite': False, 'name': 'redis-auth',
            'transformation': {'excludeRaw': True, 'includes': ['^redis-password$']},
        }, spec['destination'])
        self.assertEqual([
            {'kind': 'Deployment', 'name': name}
            for name in ('animal-service', 'store-service', 'user-service', 'community-service')
        ], spec['rolloutRestartTargets'])

    def test_default_chart_does_not_enable_chat_or_inject_redis_credentials(self):
        result = render()
        self.assertEqual(0, result.returncode, result.stderr)
        deployment = next(item for item in yaml.safe_load_all(result.stdout)
                          if item and item['kind'] == 'Deployment')
        names = {item['name'] for item in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertNotIn('MEMBER_CHAT_ENABLED', names)
        self.assertNotIn('SPRING_DATA_REDIS_PASSWORD', names)

    def test_enabled_chat_rejects_missing_secret_host_namespace_or_origins(self):
        cases = [
            ('redisSecretRef', '', 'Redis authentication Secret'),
            ('SPRING_DATA_REDIS_HOST', '', 'explicit Redis host'),
            ('MEMBER_CHAT_NAMESPACE', '', 'explicit Redis namespace'),
            ('MEMBER_CHAT_NAMESPACE', 'local/dev', 'explicit Redis namespace'),
            ('MEMBER_CHAT_ALLOWED_ORIGINS', '', 'explicit HTTP origins'),
            ('MEMBER_CHAT_ALLOWED_ORIGINS', 'https://*.pawbridge.kr', 'explicit HTTP origins'),
            ('MEMBER_CHAT_ALLOWED_ORIGINS', 'https://www.pawbridge.kr/path', 'explicit HTTP origins'),
        ]
        for key, value, expected in cases:
            with self.subTest(key=key, value=value):
                values = copy.deepcopy(self.values)
                target = values if key == 'redisSecretRef' else values['env']
                target[key] = value
                result = render(values)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(expected, result.stderr)

    def test_enabled_chat_rejects_plaintext_password_or_credential_overriding_url(self):
        for key, value, expected in (
            ('SPRING_DATA_REDIS_PASSWORD', 'synthetic-not-a-secret', 'only from the selected Secret'),
            ('SPRING_REDIS_PASSWORD', 'synthetic-not-a-secret', 'only from the selected Secret'),
            ('SPRING_DATA_REDIS_URL', 'redis://redis-master:6379', 'not a URL'),
        ):
            with self.subTest(key=key):
                values = copy.deepcopy(self.values)
                values['env'][key] = value
                result = render(values)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(expected, result.stderr)


if __name__ == '__main__':
    unittest.main()
