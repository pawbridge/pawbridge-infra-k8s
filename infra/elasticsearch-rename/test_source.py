"""Offline source overlay contract; run python3 infra/elasticsearch-rename/test_source.py."""
import copy
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[2]

def render(path):
    return [d for d in yaml.safe_load_all(subprocess.check_output(
        ['kubectl', 'kustomize', str(ROOT / path)], text=True)) if d]

class SourceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = render('infra/elasticsearch-rename/original')
        cls.prepared = render('infra/elasticsearch-rename/source')

    def test_only_expected_additions(self):
        before = next(d for d in self.base if d['kind'] == 'Elasticsearch')
        after = copy.deepcopy(next(d for d in self.prepared if d['kind'] == 'Elasticsearch'))
        self.assertEqual(after['spec']['auth']['fileRealm'].pop(), {'secretName': 'pawbridge-es-migration-auth'})
        self.assertEqual(after['spec']['auth']['roles'].pop(), {'secretName': 'pawbridge-es-migration-roles'})
        node = after['spec']['nodeSets'][0]
        self.assertEqual(node['config'].pop('path.repo'), ['/mnt/pawbridge-snapshots'])
        pod = node['podTemplate']['spec']
        self.assertEqual(pod.pop('volumes'), [{'name': 'migration-snapshots', 'persistentVolumeClaim': {'claimName': 'pawbridge-es-source-snapshots'}}])
        self.assertEqual(pod['containers'][0].pop('volumeMounts'), [{'name': 'migration-snapshots', 'mountPath': '/mnt/pawbridge-snapshots'}])
        self.assertEqual(before, after)

    def test_distinct_snapshot_volume_without_credentials(self):
        pvc = next(d for d in self.prepared if d['kind'] == 'PersistentVolumeClaim')
        self.assertEqual(pvc['metadata']['name'], 'pawbridge-es-source-snapshots')
        self.assertEqual(pvc['metadata']['annotations']['argocd.argoproj.io/sync-options'], 'Prune=false')
        self.assertFalse(any(d['metadata']['name'] == 'pawbridge-es-migration-auth' for d in self.prepared))

    def test_no_new_target_or_application_changes(self):
        self.assertEqual(sorted(d['kind'] for d in self.prepared), ['Elasticsearch', 'PersistentVolumeClaim', 'Secret', 'Secret'])
        self.assertEqual(next(d for d in self.prepared if d['kind'] == 'Elasticsearch')['metadata']['name'], 'store-search')

if __name__ == '__main__':
    unittest.main()
