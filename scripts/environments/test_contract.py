import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import yaml
from validate import validate_dev_documents
from promote_image import promote

ROOT = Path(__file__).resolve().parents[2]

class EnvironmentTest(unittest.TestCase):
    def test_no_cross_namespace_or_production_endpoint(self):
        for env in ['jdbc:postgresql://pawbridge-postgresql.databases.svc.cluster.local/pawbridge', 'http://animal-service.pawbridge.svc:8081', 'https://api.pawbridge.kr', 'pawbridge-animal-originals']:
            with self.subTest(env=env), self.assertRaises(ValueError):
                validate_dev_documents([{'kind':'ConfigMap','data':{'endpoint':env}}])
        with self.assertRaises(ValueError): validate_dev_documents([{'metadata':{'namespace':'pawbridge'}}])
        with self.assertRaises(ValueError): validate_dev_documents([{'kind':'Service','spec':{'type':'NodePort'}}])

    def test_argo_targets_and_application_identities_are_separate(self):
        dev = list((ROOT/'gitops/argocd/environments/dev').glob('*service.yaml'))
        self.assertGreaterEqual(len(dev), 7)
        for p in dev:
            a = yaml.safe_load(p.read_text())
            self.assertEqual('dev', a['spec']['source']['targetRevision'])
            self.assertEqual('pawbridge-dev', a['spec']['destination']['namespace'])
            self.assertNotIn('automated', a['spec']['syncPolicy'])
            prod = yaml.safe_load((ROOT/'gitops/argocd/environments/prod'/p.name).read_text())
            self.assertEqual('main', prod['spec']['source']['targetRevision'])
            self.assertNotEqual(a['metadata']['name'], prod['metadata']['name'])

    def test_platform_is_namespaced_and_connect_uses_its_own_broker_and_secrets(self):
        root = ROOT/'environments/dev/platform'
        for p in root.glob('*.yaml'):
            for d in yaml.safe_load_all(p.read_text()):
                if d['kind'] in ('Kustomization', 'Namespace'): continue
                self.assertEqual('pawbridge-dev', d['metadata']['namespace'], p.name)
        c = yaml.safe_load((root/'connect.yaml').read_text())['spec']
        self.assertEqual('pawbridge-dev-kafka-bootstrap:9092', c['bootstrapServers'])
        self.assertEqual('pawbridge-dev-connect', c['groupId'])
        self.assertEqual('dev-connect-offsets', c['offsetStorageTopic'])
        for service in ('animal','user','community','store','payment'):
            c = yaml.safe_load((root/('cdc-'+service+'.yaml')).read_text())['spec']
            self.assertEqual('stopped', c['state'])
            self.assertIn('.pawbridge-dev.svc.', c['config']['database.hostname'])
            self.assertIn('secrets:pawbridge-dev/dev-', c['config']['database.password'])

    def test_promotion_changes_image_only_and_requires_exact_tested_digest(self):
        digest='sha256:'+'a'*64; revision='b'*40
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'environments').mkdir()
            (root/'environments/environment-contract.json').write_text(json.dumps({'services':{'photo-service':{'devValues':'dev.yaml','prodValues':'prod.yaml'}}}))
            prod={'image':{'repository':'test/image','digest':'sha256:'+'c'*64},'replicaCount':1,'secretRef':'prod-secret','env':{'DB':'unchanged'}}
            (root/'prod.yaml').write_text(yaml.safe_dump(prod)); evidence=root/'evidence.json'
            evidence.write_text(json.dumps({'environment':'dev','result':'passed','revision':revision,'images':{'photo-service':digest},'report':'local-e2e-report'}))
            with patch('promote_image.subprocess.check_output', return_value=yaml.safe_dump({'image':{'repository':'test/image','digest':digest},'secretRef':'dev-secret'})):
                path,content=promote(root,'photo-service',revision,digest,evidence)
                after=yaml.safe_load(content)
                self.assertEqual('prod-secret',after['secretRef']); self.assertEqual(prod['env'],after['env'])
                self.assertEqual(digest,after['image']['digest']); self.assertEqual(prod,yaml.safe_load(path.read_text()))
                with self.assertRaises(ValueError): promote(root,'photo-service',revision,'sha256:'+'d'*64,evidence)
