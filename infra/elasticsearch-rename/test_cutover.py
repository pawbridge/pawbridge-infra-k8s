import copy
import importlib.util
from pathlib import Path
import unittest
import yaml
from test_source import ROOT, render

spec = importlib.util.spec_from_file_location('trust', Path(__file__).with_name('update-trust-bundle.py'))
trust = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trust)

class CutoverContract(unittest.TestCase):
    def test_production_change_preserves_auth_storage_version_and_resources(self):
        before = next(d for d in render('infra/elasticsearch-rename/original') if d['kind'] == 'Elasticsearch')
        after = copy.deepcopy(next(d for d in render('gitops/stateful/elasticsearch') if d['kind'] == 'Elasticsearch'))
        self.assertEqual(after['metadata']['name'], 'pawbridge-elasticsearch')
        after['metadata']['name'] = before['metadata']['name']
        after['metadata']['labels'] = before['metadata']['labels']
        node = after['spec']['nodeSets'][0]
        original = before['spec']['nodeSets'][0]
        self.assertEqual(node['name'], 'worker1')
        self.assertNotEqual(node['name'], original['name'])
        node['name'] = original['name']
        self.assertEqual(node['podTemplate']['spec']['nodeSelector'], {'kubernetes.io/hostname': 'pawbridge-k136-w1'})
        node['podTemplate']['spec']['nodeSelector'] = original['podTemplate']['spec']['nodeSelector']
        node['podTemplate']['metadata']['labels'] = original['podTemplate']['metadata']['labels']
        node['volumeClaimTemplates'][0]['metadata']['labels'] = original['volumeClaimTemplates'][0]['metadata']['labels']
        self.assertEqual(after, before)

    def test_all_four_app_urls_use_new_tls_endpoint(self):
        for service in ('store-service', 'animal-service', 'community-service', 'python-ai-service'):
            data = yaml.safe_load((ROOT / 'environments/dev/values' / (service + '.yaml')).read_text())
            key = 'ES_URL' if service == 'python-ai-service' else 'SPRING_ELASTICSEARCH_URIS'
            self.assertEqual(data['env'][key], 'https://pawbridge-elasticsearch-es-http.databases.svc:9200')

    def test_kafka_endpoint_keeps_tls_verification(self):
        data = yaml.safe_load((ROOT / 'gitops/stateful/store-search-sink/connector.yaml').read_text())['spec']['config']
        self.assertEqual(data['connection.url'], 'https://pawbridge-elasticsearch-es-http.databases.svc:9200')
        self.assertEqual(data['elastic.https.ssl.endpoint.identification.algorithm'], 'https')
        self.assertEqual(data['topics'], 'store.product-sku.events')
        self.assertEqual(data['topic.to.external.resource.mapping'], 'store.product-sku.events:store-products-write')

    def test_dual_ca_bundle_preserves_source_and_deduplicates(self):
        old = '-----BEGIN CERTIFICATE-----\nOLD\n-----END CERTIFICATE-----\n'
        new = '-----BEGIN CERTIFICATE-----\nNEW\n-----END CERTIFICATE-----\n'
        self.assertEqual(trust.bundle(old, new), old + new)
        self.assertEqual(trust.bundle(old + new, new), old + new)
        self.assertEqual(trust.bundle(old, old), old)

    def test_ca_parser_rejects_private_key_or_other_content(self):
        for value in ('', 'password', '-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----'):
            with self.assertRaises(ValueError):
                trust.certificates(value)

    def test_only_four_public_trust_paths_are_in_scope(self):
        self.assertEqual(trust.PATHS, tuple('pawbridge/dev/' + s + '/elasticsearch/trust'
                                          for s in ('store', 'animal', 'community', 'python')))

    def test_final_restore_close_permission_is_limited_to_named_indices(self):
        roles = yaml.safe_load((ROOT / 'infra/elasticsearch-rename/target/roles.yml').read_text())
        rule = roles['pawbridge_snapshot_restore']['indices'][0]
        self.assertIn('indices:admin/close', rule['privileges'])
        self.assertEqual(set(rule['names']), {'animals-recovered-v001', 'store-products-v001', 'posts',
                                             'animals', 'store-products-read', 'store-products-write'})
        self.assertNotIn('all', rule['privileges'])

if __name__ == '__main__':
    unittest.main()
