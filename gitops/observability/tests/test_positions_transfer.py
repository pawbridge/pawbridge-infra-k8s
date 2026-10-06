"""위치 백업의 훼손·오래된 파일·잘못된 전환을 거절한다."""
import datetime
import copy
import importlib.util
import json
import pathlib
import tempfile
import unittest


PATH = pathlib.Path(__file__).resolve().parents[1] / 'logs/positions_transfer.py'
SPEC = importlib.util.spec_from_file_location('positions_transfer', PATH)
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)
NOW = datetime.datetime(2026, 10, 6, 4, 0, tzinfo=datetime.timezone.utc)
CHECKPOINT = b'''positions:
  ? path: "cursor:fixture"
    labels: '{}'
  : "1791258900000000"
'''


class PositionsTransferTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.snapshot = pathlib.Path(self.directory.name) / 'snapshot'

    def archive(self, payload=CHECKPOINT):
        return transfer.create_snapshot(self.snapshot, payload, 'fixture-context', 'fixture-pod', 'fixture-uid', NOW)

    def test_private_snapshot_round_trip_preserves_exact_bytes(self):
        original = self.archive()
        manifest, content = transfer.load_snapshot(self.snapshot, 'fixture-context', NOW)
        self.assertEqual(CHECKPOINT, content)
        self.assertEqual(original, manifest)
        self.assertEqual(0o700, self.snapshot.stat().st_mode & 0o777)
        for name in ('manifest.json', 'positions.yml'):
            self.assertEqual(0o600, (self.snapshot / name).stat().st_mode & 0o777)

    def test_previous_backup_is_never_overwritten(self):
        self.archive()
        with self.assertRaises(FileExistsError):
            self.archive()
        self.assertEqual(CHECKPOINT, (self.snapshot / 'positions.yml').read_bytes())

    def test_changed_checkpoint_fails_checksum(self):
        self.archive()
        (self.snapshot / 'positions.yml').write_bytes(CHECKPOINT.replace(b'0000000', b'0000001'))
        with self.assertRaisesRegex(ValueError, '불일치'):
            transfer.load_snapshot(self.snapshot, 'fixture-context', NOW)

    def test_stale_and_future_backup_refused(self):
        self.archive()
        for delta in (61, -1):
            with self.subTest(seconds=delta), self.assertRaises(ValueError):
                transfer.load_snapshot(self.snapshot, 'fixture-context', NOW + datetime.timedelta(seconds=delta))

    def test_wrong_context_or_destination_refused(self):
        self.archive()
        with self.assertRaisesRegex(ValueError, '절대 경로'):
            transfer.load_snapshot(pathlib.Path('relative-backup'), 'fixture-context', NOW)
        with self.assertRaises(ValueError):
            transfer.load_snapshot(self.snapshot, 'other-context', NOW)
        path = self.snapshot / 'manifest.json'
        manifest = json.loads(path.read_text())
        manifest['relative_path'] = '../../another-service'
        path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            transfer.load_snapshot(self.snapshot, 'fixture-context', NOW)

    def test_symlink_backup_refused(self):
        self.archive()
        link = pathlib.Path(self.directory.name) / 'link'
        link.symlink_to(self.snapshot)
        with self.assertRaises(ValueError):
            transfer.load_snapshot(link, 'fixture-context', NOW)

    def test_empty_malformed_or_invalid_offsets_refused(self):
        for content in (b'', b'positions: {}', b'wrong: {}', b'positions: [1]',
                        CHECKPOINT.replace(b'1791258900000000', b'-1'),
                        b'positions: &positions\n  *positions: "1"', b'positions: [broken'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                transfer.validate_checkpoint(content)


class SeedTargetTest(unittest.TestCase):
    def setUp(self):
        self.source = {'spec': {'nodeName': 'fixture-node'}}
        self.target = transfer.yaml.safe_load((PATH.parent / 'positions-transfer-pod.yaml').read_text())
        self.target['spec']['nodeName'] = 'fixture-node'
        self.target['status'] = {'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}
        workloads = list(transfer.yaml.safe_load_all((PATH.parent / 'workloads.yaml').read_text()))
        self.claim = next(obj for obj in workloads if obj['kind'] == 'PersistentVolumeClaim' and obj['metadata']['name'] == transfer.CLAIM_NAME)
        self.claim['metadata']['uid'] = 'fixture-pvc-uid'
        self.claim['spec']['volumeName'] = 'fixture-pv'
        self.claim['status'] = {'phase': 'Bound'}
        self.volume = {'metadata': {'name': 'fixture-pv'}, 'spec': {
            'persistentVolumeReclaimPolicy': 'Retain', 'claimRef': {
                'uid': 'fixture-pvc-uid', 'namespace': 'monitoring', 'name': transfer.CLAIM_NAME}}}

    def validate(self):
        transfer.validate_seed_target(self.source, self.target, self.claim, self.volume)

    def test_declared_helper_and_retained_claim_allowed(self):
        self.validate()
        self.assertEqual('32Mi', self.target['spec']['containers'][0]['resources']['limits']['memory'])

    def test_wrong_purpose_node_mount_and_extra_container_refused(self):
        original = copy.deepcopy(self.target)
        mutations = [lambda: self.target['metadata']['labels'].update({'pawbridge.io/purpose': 'other'}),
                     lambda: self.target['spec'].update(nodeName='other-node'),
                     lambda: self.target['spec']['volumes'][0]['persistentVolumeClaim'].update(claimName='pawbridge-loki-data'),
                     lambda: self.target['spec']['containers'][0]['volumeMounts'][0].update(mountPath='/another'),
                     lambda: self.target['spec']['containers'].append(copy.deepcopy(self.target['spec']['containers'][0]))]
        for mutation in mutations:
            self.target = copy.deepcopy(original)
            mutation()
            with self.assertRaises(ValueError):
                self.validate()

    def test_unready_unsafe_or_wrong_image_refused(self):
        original = copy.deepcopy(self.target)
        mutations = [lambda: self.target['status'].update(phase='Pending'),
                     lambda: self.target['spec'].update(automountServiceAccountToken=True),
                     lambda: self.target['spec']['securityContext'].update(runAsUser=0),
                     lambda: self.target['spec']['containers'][0].update(image='grafana/alloy:latest'),
                     lambda: self.target['spec']['containers'][0]['securityContext'].update(readOnlyRootFilesystem=False)]
        for mutation in mutations:
            self.target = copy.deepcopy(original)
            mutation()
            with self.assertRaises(ValueError):
                self.validate()

    def test_unbound_unprotected_and_oversized_claim_refused(self):
        original = copy.deepcopy(self.claim)
        mutations = [lambda: self.claim['status'].update(phase='Pending'),
                     lambda: self.claim['metadata'].update(annotations={}),
                     lambda: self.claim['spec']['resources']['requests'].update(storage='4Gi')]
        for mutation in mutations:
            self.claim = copy.deepcopy(original)
            mutation()
            with self.assertRaises(ValueError):
                self.validate()

    def test_wrong_claim_binding_and_destructive_reclaim_refused(self):
        original = copy.deepcopy(self.volume)
        for mutation in [lambda: self.volume['spec']['claimRef'].update(uid='other-pvc'),
                         lambda: self.volume['spec'].update(persistentVolumeReclaimPolicy='Delete')]:
            self.volume = copy.deepcopy(original)
            mutation()
            with self.assertRaises(ValueError):
                self.validate()


if __name__ == '__main__':
    unittest.main()
