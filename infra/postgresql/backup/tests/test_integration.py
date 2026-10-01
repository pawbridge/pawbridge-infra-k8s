"""운영·인터넷과 분리된 컨테이너에서 실제 PostgreSQL/age 백업·복원을 검증한다."""
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import uuid
import tempfile
import unittest

from backup import BackupError, Config, PREFIX, run_backup, database_proof
from restore import restore_bundle, verify_inputs, verify_restored
from tests.fakes import MemoryS3


@unittest.skipUnless(os.environ.get('PAWBRIDGE_BACKUP_TEST_CONTAINER') == 'true', 'explicit isolated container only')
class PostgreSQLRoundtripTests(unittest.TestCase):
    @classmethod
    def command(cls, args, **kwargs):
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, **kwargs)
        if p.returncode:
            raise AssertionError('isolated fixture command failed: ' + args[0] + '\n' + p.stderr.decode()[-2500:])
        return p.stdout

    @classmethod
    def cluster(cls, base, user, password_file, restore=False):
        base.mkdir()
        socket = base / 'socket'
        socket.mkdir()
        data = base / 'data'
        cls.command(['initdb', '-D', str(data), '--username=' + user, '--encoding=UTF8', '--locale=C.UTF-8',
                     '--auth-local=scram-sha-256', '--auth-host=reject', '--pwfile=' + str(password_file)])
        if restore:
            # bootstrap은 컨테이너의 postgres OS 사용자로만 peer 접속한다. 앱 역할은 원본 SCRAM을 검증한다.
            (data / 'pg_hba.conf').write_text('local all postgres peer\nlocal all all scram-sha-256\nhost all all 0.0.0.0/0 reject\n')
        options = "-k " + str(socket) + " -c listen_addresses='' -c shared_buffers=32MB -c max_connections=20"
        try:
            cls.command(['pg_ctl', '-D', str(data), '-l', str(base / 'postgres.log'), '-o', options, '-w', 'start'])
        except AssertionError as exc:
            raise AssertionError(str(exc) + '\n' + (base / 'postgres.log').read_text()[-2000:]) from exc
        return socket, data

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(dir='/work', prefix='roundtrip-')
        cls.root = Path(cls.temp.name)
        cls.addClassCleanup(cls.temp.cleanup)
        for name, value in [('password', 'synthetic-postgres-password'), ('access', 'synthetic-access'),
                            ('secret', 'synthetic-secret'), ('bootstrap-password', 'synthetic-bootstrap-password')]:
            (cls.root / name).write_text(value)
        cls.command(['age-keygen', '-o', str(cls.root / 'identity')])
        recipient = cls.command(['age-keygen', '-y', str(cls.root / 'identity')]).decode().strip()
        (cls.root / 'recipient').write_text(recipient)
        socket, cls.source_data = cls.cluster(cls.root / 'source', 'postgres', cls.root / 'password')
        cls.addClassCleanup(cls.command, ['pg_ctl', '-D', str(cls.source_data), '-m', 'fast', '-w', 'stop'])
        cls.cfg = Config(host=str(socket), database='postgres', password_file=str(cls.root / 'password'),
                         endpoint='https://' + 'a' * 32 + '.r2.cloudflarestorage.com',
                         access_key_file=str(cls.root / 'access'), secret_key_file=str(cls.root / 'secret'),
                         recipient_file=str(cls.root / 'recipient'), workdir=str(cls.root), content_proof=True)
        with cls.cfg.connect(autocommit=True) as c:
            c.execute('CREATE DATABASE pawbridge')
        cls.cfg = dataclasses.replace(cls.cfg, database='pawbridge')
        with cls.cfg.connect(autocommit=True) as c:
            c.execute('CREATE EXTENSION vector')
            for service in ['animal', 'user', 'community', 'store', 'payment']:
                service = 'pawbridge_' + service
                c.execute('CREATE SCHEMA "' + service + '"')
                c.execute('CREATE ROLE ' + service + "_app LOGIN PASSWORD 'synthetic-app-password'")
                c.execute('GRANT USAGE ON SCHEMA "' + service + '" TO ' + service + '_app')
                c.execute('CREATE TABLE "' + service + '".flyway_schema_history(version text, checksum int)')
                c.execute('INSERT INTO "' + service + '".flyway_schema_history VALUES (%s,%s)', ('V1', 12345))
            c.execute('CREATE TABLE pawbridge_animal.animals(id bigint PRIMARY KEY, color text, weight numeric, happened date, note text, embedding vector(3))')
            c.execute("INSERT INTO pawbridge_animal.animals VALUES (1,'흰색',2.3,'2026-10-01',E'여러 줄\\n기록','[1,0,0]'),(2,'갈색',NULL,NULL,NULL,'[0,1,0]')")
            c.execute('CREATE TABLE pawbridge_animal.references_table(id bigint PRIMARY KEY, animal_id bigint REFERENCES pawbridge_animal.animals(id))')
            c.execute('INSERT INTO pawbridge_animal.references_table VALUES (1,1)')
            c.execute('CREATE INDEX animal_vector_idx ON pawbridge_animal.animals USING hnsw(embedding vector_cosine_ops)')
            c.execute('CREATE TABLE pawbridge_store.products(id bigint PRIMARY KEY, name text)')
            c.execute("INSERT INTO pawbridge_store.products VALUES (1,'상품 테스트')")
            c.execute('GRANT SELECT ON pawbridge_animal.animals TO pawbridge_animal_app')
            c.execute('GRANT SELECT ON pawbridge_store.products TO pawbridge_store_app')
            c.execute('CREATE ROLE animal_reader NOLOGIN')
            c.execute('GRANT animal_reader TO pawbridge_animal_app')
        with cls.cfg.connect(autocommit=True) as c:
            for service in ['animal','user','community','store','payment']:
                schema = 'pawbridge_' + service
                c.execute('GRANT SELECT ON ALL TABLES IN SCHEMA '+schema+' TO '+schema+'_app')
        with cls.cfg.connect(autocommit=True) as c:
            c.execute('CREATE TABLE pawbridge_store.admin_only(id bigint PRIMARY KEY)')
        cls.client = MemoryS3()
        with contextlib.redirect_stdout(io.StringIO()):
            cls.marker = run_backup(cls.cfg, cls.client)
        cls.bundle = cls.root / 'bundle'
        cls.bundle.mkdir()
        for name in cls.marker['files']:
            (cls.bundle / name).write_bytes(cls.client.objects[PREFIX + cls.marker['run'] + '/' + name])
        (cls.bundle / 'complete.json').write_bytes(cls.client.objects[PREFIX + cls.marker['run'] + '/complete.json'])

    def setUp(self):
        self.target_dir = self.root / ('t-' + uuid.uuid4().hex[:8])
        self.addCleanup(shutil.rmtree, self.target_dir, True)
        socket, self.target_data = self.cluster(self.target_dir, 'postgres', self.root / 'bootstrap-password', restore=True)
        self.target = dataclasses.replace(self.cfg, host=str(socket), user='postgres',
                                          password_file=str(self.root / 'bootstrap-password'))
        self.addCleanup(self.command, ['pg_ctl', '-D', str(self.target_data), '-m', 'fast', '-w', 'stop'])

    def restore(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return restore_bundle(self.bundle, self.root / 'identity', self.target, self.target_dir / 'report.json')

    def test_real_compressed_encrypted_backup_restores_data_roles_constraints_vectors_and_restart(self):
        report = self.restore()
        self.assertTrue(report['content_proof'])
        self.assertTrue(report['roles_and_memberships_match'])
        self.assertEqual(9, report['verified_tables'])
        import psycopg
        app = dataclasses.replace(self.target, user='pawbridge_animal_app', password_file=str(self.root / 'app-password'))
        (self.root / 'app-password').write_text('synthetic-app-password')
        with app.connect(autocommit=True) as c:
            self.assertEqual(2, c.execute('SELECT count(*) FROM pawbridge_animal.animals').fetchone()[0])
            self.assertEqual(1, c.execute("SELECT id FROM pawbridge_animal.animals ORDER BY embedding <=> '[1,0,0]' LIMIT 1").fetchone()[0])
            self.assertEqual('흰색', c.execute('SELECT color FROM pawbridge_animal.animals WHERE id=1').fetchone()[0])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                c.execute('SELECT * FROM pawbridge_store.products')
        with self.target.connect(autocommit=True) as c:
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                c.execute('INSERT INTO pawbridge_animal.references_table VALUES (2,999)')
        self.command(['pg_ctl', '-D', str(self.target_data), '-l', str(self.target_dir / 'postgres.log'), '-m', 'fast', '-w', 'restart'])
        with app.connect() as c:
            self.assertEqual(2, c.execute('SELECT count(*) FROM pawbridge_animal.animals').fetchone()[0])
        (self.root / 'wrong-app-password').write_text('wrong-synthetic-password')
        with self.assertRaises(psycopg.OperationalError):
            dataclasses.replace(app, password_file=str(self.root / 'wrong-app-password')).connect()
        report.update(restart_verified=True, application_role_reads_verified=True)
        (self.target_dir / 'drill-final.json').write_text(json.dumps(report))
        print(json.dumps({'isolated_roundtrip': report}))

    def test_full_drill_verifies_all_five_application_roles_after_restart(self):
        from drill import run_drill
        passwords = self.target_dir / 'app-passwords'
        passwords.mkdir()
        for service in ['animal','user','community','store','payment']:
            (passwords / ('pawbridge_' + service + '_app')).write_text('synthetic-app-password')
        with contextlib.redirect_stdout(io.StringIO()):
            evidence = run_drill(self.bundle,self.root/'identity',self.target_dir/'drill space',passwords,self.target_dir/'drill.json')
        self.assertTrue(evidence['restart_verified'])
        self.assertEqual(5,len(evidence['application_schemas']))
        self.assertEqual(1,evidence['vector_columns_queried'])

    def test_drill_auth_failure_stops_the_db_without_publishing_a_final_success_report(self):
        import psycopg
        from drill import run_drill
        passwords = self.target_dir / 'app-passwords'
        passwords.mkdir()
        for service in ['animal','user','community','store','payment']:
            (passwords / ('pawbridge_' + service + '_app')).write_text(
                'wrong-synthetic-password' if service=='payment' else 'synthetic-app-password')
        drill_root = self.target_dir / 'drill'
        report = self.target_dir / 'drill.json'
        with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(psycopg.OperationalError):
            run_drill(self.bundle,self.root/'identity',drill_root,passwords,report)
        self.assertFalse(report.exists())
        self.assertFalse(json.loads((drill_root/'restore-stage.json').read_text())['application_role_reads_verified'])
        status = subprocess.run(['pg_ctl','-D',str(drill_root/'data'),'status'],capture_output=True,timeout=10)
        self.assertEqual(3,status.returncode)

    def test_wrong_identity_leaves_the_target_without_the_recovered_database(self):
        other = self.root / 'wrong-identity'
        self.command(['age-keygen', '-o', str(other)])
        with self.assertRaisesRegex(BackupError, 'decryption failed'):
            restore_bundle(self.bundle, other, self.target, self.target_dir / 'report.json')
        with dataclasses.replace(self.target, database='postgres').connect() as c:
            self.assertEqual(0, c.execute("SELECT count(*) FROM pg_database WHERE datname='pawbridge'").fetchone()[0])

    def test_existing_database_is_not_overwritten(self):
        with dataclasses.replace(self.target, database='postgres').connect(autocommit=True) as c:
            c.execute('CREATE DATABASE pawbridge')
        with self.assertRaisesRegex(BackupError, 'already exists'):
            self.restore()

    def test_changed_application_read_permissions_fail_restore_verification(self):
        self.restore()
        manifest = json.loads(self.command(['age','--decrypt','--identity',str(self.root/'identity'),str(self.bundle/'manifest.json.age')]))
        with self.target.connect(autocommit=True) as c:
            c.execute('REVOKE SELECT ON pawbridge_store.products FROM pawbridge_store_app')
        with self.assertRaisesRegex(BackupError,'permissions'):
            verify_restored(self.target,manifest)

    def test_same_row_count_with_changed_data_fails_content_verification(self):
        self.restore()
        result = self.command(['age', '--decrypt', '--identity', str(self.root / 'identity'), str(self.bundle / 'manifest.json.age')])
        manifest = json.loads(result)
        with self.target.connect(autocommit=True) as c:
            c.execute("UPDATE pawbridge_animal.animals SET color='changed' WHERE id=1")
        with self.assertRaisesRegex(BackupError, 'content'):
            verify_restored(self.target, manifest)

    def test_corrupted_ciphertext_is_rejected_before_restore(self):
        bundle = self.target_dir / 'corrupt-bundle'
        bundle.mkdir()
        for p in self.bundle.iterdir():
            (bundle / p.name).write_bytes(p.read_bytes())
        with (bundle / 'database.dump.age').open('ab') as out:
            out.write(b'corrupted')
        with self.assertRaisesRegex(BackupError, 'checksum'):
            restore_bundle(bundle, self.root / 'identity', self.target, self.target_dir / 'report.json')


if __name__ == '__main__':
    unittest.main()
