"""Visitor production rendering and least-privilege secret contracts."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]
VSO = ROOT / 'gitops/security/community-visits-vso'

class DailyVisitorsRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resources = [yaml.safe_load((VSO / p).read_text()) for p in
                         yaml.safe_load((VSO / 'kustomization.yaml').read_text())['resources']]
        cls.by_kind = {r['kind']:r for r in cls.resources}
        cls.helm = shlex.split(os.getenv('HELM_COMMAND','helm'))

    def render(self, *extra):
        return subprocess.run([*self.helm,'template','community-service',str(ROOT/'charts/community-service'),
            '--namespace','pawbridge','--values',str(ROOT/'environments/prod/values/community-service.yaml'),
            *extra],capture_output=True,text=True,timeout=30)

    def test_production_secret_is_required_and_never_inline(self):
        result=self.render(); self.assertEqual(0,result.returncode,result.stderr)
        resources=list(yaml.safe_load_all(result.stdout))
        self.assertNotIn('Job',{r['kind'] for r in resources if r})
        deployment=next(r for r in resources if r and r['kind']=='Deployment')
        env={e['name']:e for e in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual('true',env['DAILY_VISITORS_ENABLED']['value'])
        self.assertEqual('https://www.pawbridge.kr',env['DAILY_VISITORS_ALLOWED_ORIGINS']['value'])
        self.assertEqual({'secretKeyRef':{'name':'community-visits-auth','key':'DAILY_VISITORS_SIGNING_KEY',
                         'optional':False}},env['DAILY_VISITORS_SIGNING_KEY']['valueFrom'])
        for args in [('--set','visitsSecretRef='),('--set','postgresqlSecretRef='),
                     ('--set','env.DAILY_VISITORS_SIGNING_KEY=must-not-appear'),
                     ('--set','env.DAILY_VISITORS_ALLOWED_ORIGINS=https://not-approved.invalid')]:
            with self.subTest(args=args): self.assertNotEqual(0,self.render(*args).returncode)

    def test_scoped_tls_auth_and_single_field_destination(self):
        self.assertEqual({'ServiceAccount','VaultConnection','VaultAuth','VaultStaticSecret'},set(self.by_kind))
        self.assertTrue(all(r['metadata']['namespace']=='pawbridge' for r in self.resources))
        self.assertFalse(self.by_kind['ServiceAccount']['automountServiceAccountToken'])
        connection=self.by_kind['VaultConnection']['spec']
        self.assertEqual('https://vault.vault.svc.cluster.local:8200',connection['address'])
        self.assertEqual('vault-internal-ca',connection['caCertSecretRef'])
        self.assertFalse(connection['skipTLSVerify'])
        auth=self.by_kind['VaultAuth']['spec']
        self.assertEqual('community-visits-vault-connection',auth['vaultConnectionRef'])
        self.assertEqual({'role':'community-visits-read','serviceAccount':'community-visits-vault-auth',
                         'audiences':['vault'],'tokenExpirationSeconds':600},auth['kubernetes'])
        secret=self.by_kind['VaultStaticSecret']['spec']
        self.assertEqual('pawbridge/prod/community/visits',secret['path'])
        self.assertEqual('kv-v2',secret['type'])
        self.assertEqual({'create':True,'overwrite':False,'name':'community-visits-auth',
            'transformation':{'excludeRaw':True,'includes':['^DAILY_VISITORS_SIGNING_KEY$']}},secret['destination'])
        self.assertEqual([{'kind':'Deployment','name':'community-service'}],secret['rolloutRestartTargets'])

    def test_vault_exact_paths_and_self_token_permissions(self):
        role=json.loads((ROOT/'infra/vault/roles/community-visits-read.json').read_text())
        self.assertEqual(['community-visits-vault-auth'],role['bound_service_account_names'])
        self.assertEqual(['pawbridge'],role['bound_service_account_namespaces'])
        self.assertEqual(['community-visits-read'],role['token_policies'])
        self.assertIs(True,role['token_no_default_policy'])
        self.assertEqual(('10m','10m','vault'),(role['token_ttl'],role['token_max_ttl'],role['audience']))
        text=re.sub(r'#.*','',(ROOT/'infra/vault/policies/community-visits-read.hcl').read_text())
        actual={path:re.findall(r'"(.*?)"',body) for path,body in
                re.findall(r'path\s+"([^"]+)"\s*\{([^}]+)\}',text)}
        self.assertEqual({'secret/data/pawbridge/prod/community/visits':['read'],
            'secret/metadata/pawbridge/prod/community/visits':['read'],
            'auth/token/lookup-self':['read'],'auth/token/renew-self':['update'],
            'auth/token/revoke-self':['update']},actual)

    def test_image_pair_matches_verified_source(self):
        community=yaml.safe_load((ROOT/'environments/prod/values/community-service.yaml').read_text())
        migration=community['schemaMigration']
        self.assertFalse(migration['enabled'])
        self.assertEqual('501d513b76a7340c6a03fcb34ebb55f0d1984650',migration['sourceRevision'])
        self.assertEqual(community['image']['digest'],migration['apiImageDigest'])
        self.assertEqual('sha-'+migration['sourceRevision'],community['image']['tag'])
        self.assertEqual('sha256:2756a9cf510977987aa7e1797f36cb43fade1ef19c9b66ee16fea96c43dba326',community['image']['digest'])
        gateway=yaml.safe_load((ROOT/'environments/prod/values/api-gateway.yaml').read_text())
        self.assertEqual('sha256:972e5685fb37d49792347173d342843b692344cd004afa43ddcb13219e394104',gateway['image']['digest'])

    def test_supply_files_are_checked_by_ci(self):
        workflow=yaml.safe_load((ROOT/'.github/workflows/environment-contract.yml').read_text())
        paths=workflow.get('on',workflow.get(True))['pull_request']['paths']
        self.assertIn('gitops/security/community-visits-vso/**',paths)
        self.assertIn('infra/vault/policies/community-visits-read.hcl',paths)
        self.assertIn('infra/vault/roles/community-visits-read.json',paths)
