"""Offline checks for the bulk activation delta on the image-update PR.

Render the HEAD values and edited values with the same animal-service chart,
release animal-service and namespace pawbridge. Render paused values with
TOURAPI_ENABLED=false and TOURAPI_SCHEDULE_ENABLED=false.
Build the HEAD and edited animal-python-runtime-vso Kustomizations separately.

Usage: python3 -B test_travel_bulk_runtime.py before.yaml after.yaml paused.yaml vso-before.yaml vso-after.yaml
No cluster, Vault or provider access; inputs must contain manifests, not secrets.
"""
import copy
from pathlib import Path
import sys
import unittest

import yaml


BULK_ENV = {'TOURAPI_BULKPETENABLED': 'true', 'TOURAPI_MAXBULKPAGESPERRUN': '10'}


def objects(path):
    docs = [doc for doc in yaml.safe_load_all(Path(path).read_text()) if doc]
    indexed = {(doc['kind'], doc['metadata']['name']): doc for doc in docs}
    if len(indexed) != len(docs):
        raise ValueError('Duplicate manifest identity')
    return indexed


def container(manifests):
    return manifests[('Deployment', 'animal-service')]['spec']['template']['spec']['containers'][0]


class TravelBulkRuntimeTests(unittest.TestCase):
    def test_only_bulk_settings_change_in_application_render(self):
        after = copy.deepcopy(AFTER)
        env = container(after)['env']
        added = [entry for entry in env if entry['name'] in BULK_ENV]
        self.assertEqual(len(BULK_ENV), len(added))
        self.assertEqual(BULK_ENV, {entry['name']: entry['value'] for entry in added})
        env[:] = [entry for entry in env if entry['name'] not in BULK_ENV]
        # APMS CronJob, migration Job, resource limits and existing quotas are unchanged.
        self.assertEqual(BEFORE, after)

    def test_pause_disables_collection_without_changing_other_resources(self):
        paused = copy.deepcopy(PAUSED)
        flags = {'TOURAPI_ENABLED', 'TOURAPI_SCHEDULE_ENABLED'}
        entries = [entry for entry in container(paused)['env'] if entry['name'] in flags]
        self.assertEqual(len(flags), len(entries))
        for entry in entries:
            self.assertEqual('false', entry['value'])
            entry['value'] = 'true'
        self.assertEqual(AFTER, paused)

    def test_only_new_key_is_added_to_existing_runtime_secret_allowlist(self):
        after = copy.deepcopy(VSO_AFTER)
        secret = after[('VaultStaticSecret', 'animal-runtime-auth')]['spec']
        self.assertEqual('pawbridge/dev/animal/runtime', secret['path'])
        self.assertEqual('animal-runtime-auth', secret['destination']['name'])
        transformation = secret['destination']['transformation']
        self.assertIs(True, transformation['excludeRaw'])
        self.assertEqual(1, transformation['includes'].count('^TOURAPI_BULKSERVICEKEY$'))
        transformation['includes'].remove('^TOURAPI_BULKSERVICEKEY$')
        self.assertEqual(VSO_BEFORE, after)
        self.assertIn({'secretRef': {'name': 'animal-runtime-auth', 'optional': False}},
                      container(AFTER)['envFrom'])
        self.assertNotIn('TOURAPI_BULKSERVICEKEY', [entry['name'] for entry in container(AFTER)['env']])

    def test_migration_runs_before_api_sync_with_no_automatic_retry(self):
        job = AFTER[('Job', 'animal-service-schema-migrate')]
        annotations = job['metadata']['annotations']
        self.assertEqual('PreSync', annotations['argocd.argoproj.io/hook'])
        self.assertEqual('10', annotations['argocd.argoproj.io/sync-wave'])
        self.assertEqual('HookSucceeded', annotations['argocd.argoproj.io/hook-delete-policy'])
        self.assertTrue(annotations['pawbridge.kr/recovery-reference'])
        self.assertEqual(0, job['spec']['backoffLimit'])
        self.assertEqual('Never', job['spec']['template']['spec']['restartPolicy'])


if __name__ == '__main__':
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    BEFORE, AFTER, PAUSED, VSO_BEFORE, VSO_AFTER = map(objects, sys.argv[1:])
    unittest.main(argv=[sys.argv[0]])
