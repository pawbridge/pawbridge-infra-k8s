"""Keep the reviewed release pair and the PostgreSQL deployment boundary explicit."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[3]
FILE = 'environments/prod/values/animal-service.yaml'
REVISION = '66332461b644f6513c6ce2eff4de47fb0dea061b'
API = 'sha256:3c39f016d5b1e45d911c2b66f22cc621cb62dfffac7b062c15d81d2fc74408a0'
MIGRATION = 'sha256:305b804883633b6cf37c2dc7c7b23186d7520bc00d3d5102087c583682e9e460'

class ShelterReleaseTest(unittest.TestCase):
    def test_reviewed_pair_does_not_enable_mysql_migration(self):
        after = yaml.safe_load((ROOT / FILE).read_text())
        self.assertEqual('sha-' + REVISION, after['image']['tag'])
        self.assertEqual(API, after['image']['digest'])
        self.assertEqual(API, after['schemaMigration']['apiImageDigest'])
        self.assertEqual(REVISION, after['schemaMigration']['sourceRevision'])
        self.assertEqual('dorosiya/pawbridge-animal-service@' + MIGRATION, after['schemaMigration']['image'])
        self.assertFalse(after['schemaMigration']['enabled'])
        self.assertEqual('animal-postgresql-auth', after['postgresqlSecretRef'])
        self.assertEqual('', after['mysqlSecretRef'])
        self.assertEqual('validate', after['env']['SPRING_JPA_HIBERNATE_DDL_AUTO'])
        self.assertEqual('postgresql', after['env']['PAWBRIDGE_ANIMALQUERY_BACKEND'])

if __name__ == '__main__':
    unittest.main()
