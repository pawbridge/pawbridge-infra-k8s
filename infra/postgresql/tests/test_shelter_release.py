"""Keep the reviewed release pair and the PostgreSQL deployment boundary explicit."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[3]
FILE = 'environments/prod/values/animal-service.yaml'
REVISION = '03d7a9e5d58b783848be647f79f29d1b98a33abf'
API = 'sha256:277d47960e999067c31906b070eca0d3393000a349ff54eea57f95cb4f08a22c'
MIGRATION = 'sha256:fd522e8146655239570fbcf89312785581ddc6e4a7e183b297ace3849df77c6f'

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
