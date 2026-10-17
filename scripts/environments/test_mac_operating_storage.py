import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import mac_operating_storage as storage


class StorageContract(unittest.TestCase):
    def test_candidate_replaces_data_mounts_and_preserves_backup_boundary(self):
        assets = Path(__file__).resolve().parents[2] / 'environments/mac/operating/background-jobs-20261016'
        doc = json.loads((assets / 'compose.json').read_text())
        storage.validate(doc)
        result = storage.configure(doc, 'sha256:' + '1' * 64)
        storage.validate(result)
        self.assertEqual(result['networks'], doc['networks'])
        for key in doc['services']:
            if key not in (*storage.DATA, 'storage-init'):
                self.assertEqual(result['services'][key], doc['services'][key])
        init = result['services']['storage-init']
        self.assertEqual(init['network_mode'], 'none')
        self.assertEqual(init['profiles'], ['bootstrap'])
        self.assertNotIn('env_file', init)
        self.assertEqual(len(init['volumes']), 4)
        bad = copy.deepcopy(result)
        bad['services']['postgres']['volumes'][0]['type'] = 'bind'
        with self.assertRaises(ValueError): storage.validate(bad)

    def test_new_operating_generator_cannot_reintroduce_shared_data_binds(self):
        import mac_compose_operating_bundle as bundle
        image_keys = ('POSTGRES', 'REDIS', 'EVENTS', 'ACCESS', 'API_GATEWAY',
                      'USER_SERVICE', 'ANIMAL_SERVICE', 'COMMUNITY_SERVICE',
                      'STORE_SERVICE', 'PAYMENT_SERVICE', 'PHOTO_SERVICE', 'PYTHON_AI_SERVICE')
        images = {'MAC_IMAGE_' + key: 'sha256:' + format(i, '064x')
                       for i, key in enumerate(image_keys, 1)}
        result = bundle.operating_compose(images)
        storage.validate(result)
        self.assertEqual(result['services']['storage-init']['network_mode'], 'none')

    def test_missing_readonly_and_symlink_mounts_refuse_before_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            real = Path(root) / 'real'; real.mkdir()
            link = Path(root) / 'link'; link.symlink_to(real)
            cases = [(real, {}), (real, {str(real): ('ext4', {'ro'})}),
                     (link, {str(link): ('ext4', {'rw'})})]
            for path, mounts in cases:
                with self.subTest(path=path, mounts=mounts), patch.object(storage.os, 'chown') as change:
                    with self.assertRaises(ValueError): storage.initialize_empty([path], 501, 20, mounts)
                    change.assert_not_called()

    def test_populated_or_shared_mount_refuses_before_mutating_any_volume(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / k for k in storage.DATA]
            for path in paths: path.mkdir()
            mounts = {str(p): ('ext4', {'rw'}) for p in paths}
            with patch.object(storage.os, 'chown') as change:
                (paths[-1] / 'existing').write_text('preserve')
                with self.assertRaises(ValueError): storage.initialize_empty(paths, 501, 20, mounts)
                change.assert_not_called()
                (paths[-1] / 'existing').unlink()
                mounts[str(paths[-1])] = ('fuseblk', {'rw'})
                with self.assertRaises(ValueError): storage.initialize_empty(paths, 501, 20, mounts)
                change.assert_not_called()
                with self.assertRaises(ValueError): storage.initialize_empty(paths, 0, 20, mounts)
                change.assert_not_called()

    def test_empty_linux_mounts_receive_private_owner_and_mode(self):
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root) / k for k in storage.DATA]
            for path in paths: path.mkdir()
            with patch.object(storage.os, 'chown') as change:
                storage.initialize_empty(paths, os.getuid(), os.getgid(), {str(p): ('ext4', {'rw'}) for p in paths})
            self.assertEqual(change.call_count, 3)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o700 for p in paths))

if __name__ == '__main__': unittest.main()
