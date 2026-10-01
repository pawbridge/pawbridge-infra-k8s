import contextlib
import datetime as dt
import io
from pathlib import Path
import tempfile
import unittest
from backup import BackupError, UTC, PREFIX
from download import download_bundle
from tests.fakes import MemoryS3
from tests.test_backup import completed
from restore import verify_inputs


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = MemoryS3()
        self.run, self.key = completed(self.client, dt.datetime(2026, 10, 1, tzinfo=UTC))

    def test_download_needs_only_storage_and_publishes_verified_ciphertext_bundle(self):
        destination = self.root / 'new'
        with contextlib.redirect_stdout(io.StringIO()):
            marker = download_bundle(self.client, self.run, destination)
        self.assertEqual(marker, verify_inputs(destination))
        self.assertEqual([], self.client.deleted)

    def test_existing_directory_is_never_overwritten(self):
        destination = self.root / 'existing'
        destination.mkdir()
        (destination / 'preserve').write_bytes(b'keep')
        with self.assertRaisesRegex(BackupError, 'existing'):
            download_bundle(self.client, self.run, destination)
        self.assertEqual(['preserve'], [p.name for p in destination.iterdir()])

    def test_corrupt_download_has_no_completion_marker(self):
        self.client.objects[PREFIX + self.run + '/database.dump.age'] += b'corrupted'
        destination = self.root / 'new'
        with self.assertRaisesRegex(BackupError, 'size|checksum'):
            download_bundle(self.client, self.run, destination)
        self.assertFalse((destination / 'complete.json').exists())

    def test_invalid_run_is_rejected_before_storage_access(self):
        with self.assertRaisesRegex(BackupError, 'identifier'):
            download_bundle(self.client, '../escape', self.root / 'new')
        self.assertFalse((self.root / 'new').exists())
