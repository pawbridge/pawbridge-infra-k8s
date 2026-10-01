import contextlib
import dataclasses
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from backup import BackupError, Config, FILES, PREFIX, UTC, encrypt_command, list_keys, prune, run_backup, validate_marker
from restore import restore_bundle
from tests.fakes import MemoryS3

PUBLIC = 'age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p'
NOW = dt.datetime(2026, 10, 1, 3, 10, tzinfo=UTC)


def fixture_capture(cfg, recipient, directory, run):
    for name in FILES:
        (directory / name).write_bytes(b'unit fixture, not a real encrypted dump: ' + name.encode())


def completed(client, at, salt='a'):
    run = at.strftime('%Y%m%dT%H%M%SZ') + '-' + salt * 32
    files = {}
    for name in FILES:
        key = PREFIX + run + '/' + name
        client.objects[key] = b'old fixture'
        files[name] = {'key': key, 'size': len(b'old fixture'), 'sha256': hashlib.sha256(b'old fixture').hexdigest()}
    marker = {'format': 1, 'run': run, 'created_at': at.isoformat(), 'files': files}
    key = PREFIX + run + '/complete.json'
    client.objects[key] = json.dumps(marker).encode()
    return run, key


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('password', 'fixture-password'), ('access', 'fixture-access'), ('secret', 'fixture-secret'), ('recipient', PUBLIC)]:
            (self.root / name).write_text(value)
        self.cfg = Config(host='/fixture/socket', password_file=str(self.root / 'password'),
                          endpoint='https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
                          access_key_file=str(self.root / 'access'), secret_key_file=str(self.root / 'secret'),
                          recipient_file=str(self.root / 'recipient'), workdir=str(self.root))
        self.client = MemoryS3(NOW)

    def run_backup(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_backup(self.cfg, self.client, fixture_capture, NOW)

    def test_publish_only_after_all_three_ciphertexts_are_read_back_and_checked(self):
        marker = self.run_backup()
        self.assertEqual(4, len(self.client.objects))
        self.assertEqual(FILES, set(marker['files']))
        self.assertEqual({'password', 'access', 'secret', 'recipient'}, {p.name for p in self.root.iterdir()})

    def test_incomplete_dump_never_uploads_or_deletes_the_old_backup(self):
        old, _ = completed(self.client, NOW - dt.timedelta(days=8))
        def failed(*args):
            raise BackupError('dump failed')
        before = dict(self.client.objects)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(BackupError):
            run_backup(self.cfg, self.client, failed, NOW)
        self.assertEqual(before, self.client.objects)
        self.assertEqual([], self.client.deleted)

    def test_corrupt_remote_ciphertext_never_publishes_a_marker_or_prunes(self):
        completed(self.client, NOW - dt.timedelta(days=8))
        self.client.corrupt_upload = True
        with self.assertRaisesRegex(BackupError, 'checksum|size'):
            self.run_backup()
        self.assertEqual(1, sum(key.endswith('/complete.json') for key in self.client.objects))
        self.assertEqual([], self.client.deleted)

    def test_seven_day_boundary_and_existing_unrelated_objects_are_preserved(self):
        expired, _ = completed(self.client, NOW - dt.timedelta(days=7, seconds=1), 'a')
        boundary, _ = completed(self.client, NOW - dt.timedelta(days=7), 'b')
        completed(self.client, NOW - dt.timedelta(days=1), 'c')
        self.client.objects['unrelated/manual-20260924.dump'] = b'preserve'
        vault_key = 'vault/v1/' + expired + '/database.dump.age'
        self.client.objects[vault_key] = b'preserve operational backup'
        marker = self.run_backup()
        self.assertEqual(4, len(self.client.deleted))
        self.assertTrue(all(key.startswith(PREFIX + expired) for key in self.client.deleted))
        self.assertTrue(any(key.startswith(PREFIX + boundary) for key in self.client.objects))
        self.assertIn('unrelated/manual-20260924.dump', self.client.objects)
        self.assertEqual(b'preserve operational backup', self.client.objects[vault_key])
        self.assertIn(PREFIX + marker['run'] + '/complete.json', self.client.objects)

    def test_retention_removes_completion_marker_before_its_archives(self):
        _, key = completed(self.client, NOW - dt.timedelta(days=9))
        self.run_backup()
        self.assertEqual(key, self.client.deleted[0])

    def test_old_interrupted_artifacts_are_removed_only_after_a_new_success(self):
        run = (NOW - dt.timedelta(days=8)).strftime('%Y%m%dT%H%M%SZ') + '-' + 'a' * 32
        old = PREFIX + run + '/database.dump.age'
        unknown = PREFIX + run + '/manual-evidence.txt'
        self.client.objects.update({old: b'incomplete', unknown: b'preserve'})
        self.run_backup()
        self.assertIn(old, self.client.deleted)
        self.assertIn(unknown, self.client.objects)

    def test_last_completed_backup_is_kept_even_when_every_backup_is_old(self):
        old_run, _ = completed(self.client, NOW - dt.timedelta(days=9), 'a')
        newest_run, newest_key = completed(self.client, NOW - dt.timedelta(days=8), 'b')
        prune(self.client, self.cfg, newest_run, NOW)
        self.assertIn(newest_key, self.client.objects)
        self.assertFalse(any(key.startswith(PREFIX + old_run) for key in self.client.objects))

    def test_invalid_marker_reference_aborts_retention_before_any_deletion(self):
        _, old_key = completed(self.client, NOW - dt.timedelta(days=8))
        current, _ = completed(self.client, NOW, 'b')
        marker = json.loads(self.client.objects[old_key])
        marker['files']['roles.sql.age']['key'] = 'unrelated/manual-secret.sql'
        self.client.objects[old_key] = json.dumps(marker).encode()
        with self.assertRaises(BackupError):
            prune(self.client, self.cfg, current, NOW)
        self.assertEqual([], self.client.deleted)

    def test_future_clock_marker_aborts_retention(self):
        current, _ = completed(self.client, NOW)
        completed(self.client, NOW + dt.timedelta(days=1), 'b')
        with self.assertRaisesRegex(BackupError, 'future'):
            prune(self.client, self.cfg, current, NOW)
        self.assertEqual([], self.client.deleted)

    def test_runner_clock_drift_from_storage_prevents_all_retention_deletion(self):
        old,_ = completed(self.client,NOW-dt.timedelta(days=8))
        current,_ = completed(self.client,NOW,'b')
        with self.assertRaisesRegex(BackupError,'clocks'):
            prune(self.client,self.cfg,current,NOW+dt.timedelta(days=10))
        self.assertEqual([],self.client.deleted)

    def test_missing_current_marker_aborts_retention(self):
        completed(self.client, NOW - dt.timedelta(days=8))
        with self.assertRaises(BackupError):
            prune(self.client, self.cfg, 'absent', NOW)
        self.assertEqual([], self.client.deleted)

    def test_reviewed_operational_backup_bucket_is_accepted(self):
        recipient = dataclasses.replace(self.cfg, bucket='pawbridge-backups').validate()
        self.assertEqual(PUBLIC, recipient)

    def test_other_bucket_prefix_and_non_r2_endpoint_are_rejected_before_transfer(self):
        for changes in [{'bucket': 'pawbridge-public-images'}, {'bucket': 'pawbridge-animal-originals'},
                        {'bucket': 'pawbridge-postgresql-backups'}, {'endpoint': 'http://example.invalid'},
                        {'prefix': ''}, {'prefix': 'vault/v1/'}]:
            with self.subTest(changes=changes), self.assertRaises(BackupError):
                dataclasses.replace(self.cfg, **changes).validate()

    def test_unconfigured_public_key_prevents_execution(self):
        (self.root / 'recipient').write_text('CONFIGURE_BEFORE_EXECUTION')
        with self.assertRaises(BackupError):
            self.run_backup()
        self.assertEqual({}, self.client.objects)

    def test_listing_is_bounded_when_pagination_never_ends(self):
        with patch.object(self.client, 'list_objects_v2', return_value={'IsTruncated': True, 'NextContinuationToken': 'repeat'}):
            with self.assertRaisesRegex(BackupError, 'listing limit'):
                list_keys(self.client, self.cfg)

    def test_tcp_restore_target_is_rejected_before_decryption_or_db_access(self):
        target = dataclasses.replace(self.cfg, host='pawbridge-postgresql.databases.svc.cluster.local', user='pawbridge_restore_bootstrap')
        with self.assertRaisesRegex(BackupError, 'isolated socket'):
            restore_bundle(self.root, 'absent-key', target, self.root / 'report.json')

    def test_producer_failure_cannot_be_hidden_by_successful_encryption(self):
        with self.assertRaises(BackupError):
            encrypt_command([sys.executable, '-c', 'import sys;print("partial");sys.exit(7)'],
                            PUBLIC, self.root / 'partial.age', None, 5)

    def test_dump_deadline_terminates_the_producer_and_encryptor(self):
        with self.assertRaises(BackupError):
            encrypt_command([sys.executable, '-c', 'import time;time.sleep(10)'],
                            PUBLIC, self.root / 'timeout.age', None, 0.2)


if __name__ == '__main__':
    unittest.main()
