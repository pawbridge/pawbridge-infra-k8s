"""Verify rendered feed wiring without contacting Kubernetes, Vault, or R2.

Usage: python3 test_lost_gallery_runtime.py before.yaml disabled.yaml enabled.yaml vso-before.yaml vso-after.yaml
"""
import copy
from pathlib import Path
import sys
import unittest

import yaml

PREFIX = 'LOST_GALLERY_FEED_'


def objects(path):
    docs = [doc for doc in yaml.safe_load_all(Path(path).read_text()) if doc]
    result = {(doc['kind'], doc['metadata']['name']): doc for doc in docs}
    if len(result) != len(docs):
        raise ValueError('Duplicate resource identity')
    return result


def container(resources):
    return resources[('Deployment', 'animal-service')]['spec']['template']['spec']['containers'][0]


class LostGalleryRuntimeTests(unittest.TestCase):
    def test_disabled_feed_preserves_all_existing_resources(self):
        self.assertEqual(BEFORE, DISABLED)

    def test_activation_only_adds_feed_env_and_keeps_archive_overrides(self):
        stripped = copy.deepcopy(ENABLED)
        env = container(stripped)['env']
        added = [entry for entry in env if entry['name'].startswith(PREFIX)]
        self.assertEqual(5, len(added))
        self.assertEqual(5, len({entry['name'] for entry in added}))
        env[:] = [entry for entry in env if not entry['name'].startswith(PREFIX)]
        self.assertEqual(DISABLED, stripped)
        values = {entry['name']: entry.get('value') for entry in env}
        self.assertEqual('true', values['APMS_PHOTO_ARCHIVE_ENABLED'])
        self.assertEqual('100', values['APMS_PHOTO_ARCHIVE_MAX_PHOTOS'])
        self.assertEqual('60000', values['APMS_PHOTO_ARCHIVE_INTERVAL_MS'])

    def test_feed_uses_dedicated_key_and_vm_archive_credentials(self):
        env = {entry['name']: entry for entry in container(ENABLED)['env']}
        expected = {
            'INTERNAL_API_KEY': ('animal-runtime-auth', 'LOST_GALLERY_FEED_INTERNAL_API_KEY'),
            'STORAGE_ACCESS_KEY_ID': ('animal-photo-archive-r2-auth', 'R2_ACCESS_KEY_ID'),
            'STORAGE_SECRET_ACCESS_KEY': ('animal-photo-archive-r2-auth', 'R2_SECRET_ACCESS_KEY'),
        }
        for suffix, (name, key) in expected.items():
            entry = env[PREFIX + suffix]
            self.assertNotIn('value', entry)
            self.assertEqual({'secretKeyRef': {'name': name, 'key': key, 'optional': False}}, entry['valueFrom'])
        self.assertEqual('true', env[PREFIX + 'ENABLED']['value'])
        self.assertEqual('https://3e28b8ba8375b9010f6a426c1cb9755f.r2.cloudflarestorage.com',
                         env[PREFIX + 'STORAGE_ENDPOINT']['value'])

    def test_vault_only_allows_the_new_dedicated_key(self):
        after = copy.deepcopy(VSO_AFTER)
        spec = after[('VaultStaticSecret', 'animal-runtime-auth')]['spec']
        self.assertEqual('pawbridge/dev/animal/runtime', spec['path'])
        transform = spec['destination']['transformation']
        self.assertIs(True, transform['excludeRaw'])
        pattern = '^LOST_GALLERY_FEED_INTERNAL_API_KEY$'
        self.assertEqual(1, transform['includes'].count(pattern))
        transform['includes'].remove(pattern)
        self.assertEqual(VSO_BEFORE, after)


if __name__ == '__main__':
    if len(sys.argv) != 6:
        raise SystemExit(__doc__)
    BEFORE, DISABLED, ENABLED, VSO_BEFORE, VSO_AFTER = map(objects, sys.argv[1:])
    unittest.main(argv=[sys.argv[0]])
