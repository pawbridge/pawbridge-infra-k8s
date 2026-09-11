import unittest
from test_source import render

class TargetContract(unittest.TestCase):
    def test_target_is_isolated_and_version_pinned(self):
        docs = render('infra/elasticsearch-rename/target')
        self.assertEqual(sorted(d['kind'] for d in docs), ['Elasticsearch', 'PersistentVolumeClaim', 'Secret'])
        es = next(d for d in docs if d['kind'] == 'Elasticsearch')
        base = next(d for d in render('infra/elasticsearch-rename/original') if d['kind'] == 'Elasticsearch')
        self.assertEqual(es['metadata']['name'], 'pawbridge-elasticsearch')
        self.assertEqual(es['spec']['image'], base['spec']['image'])
        self.assertEqual(es['spec']['auth']['fileRealm'], [{'secretName': 'pawbridge-es-restore-auth'}])
        node = es['spec']['nodeSets'][0]
        pod = node['podTemplate']['spec']
        self.assertEqual(pod['nodeSelector']['kubernetes.io/hostname'], 'pawbridge-k136-w2')
        self.assertEqual(pod['containers'][0]['resources']['limits']['memory'], '1536Mi')
        self.assertEqual(pod['volumes'][0]['persistentVolumeClaim']['claimName'], 'pawbridge-es-target-snapshots')
        self.assertEqual(pod['initContainers'], base['spec']['nodeSets'][0]['podTemplate']['spec']['initContainers'])
        self.assertEqual(node['volumeClaimTemplates'][0]['spec'], base['spec']['nodeSets'][0]['volumeClaimTemplates'][0]['spec'])
        self.assertEqual(es['spec']['volumeClaimDeletePolicy'], 'DeleteOnScaledownOnly')
        self.assertFalse(any(d['metadata']['name'].startswith('store-search') for d in docs))

if __name__ == '__main__':
    unittest.main()
