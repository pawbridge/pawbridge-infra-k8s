"""운영 DB 변경·예약 조기 활성화·키 공개를 방지하는 배포 계약 검사."""
from pathlib import Path
import unittest
import yaml
from render import render

ROOT = Path(__file__).resolve().parents[3]
PUBLIC='age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p'
IMAGE='dorosiya/pawbridge-postgresql-backup@sha256:'+'a'*64
ENDPOINT='https://'+'a'*32+'.r2.cloudflarestorage.com'


class DeploymentContracts(unittest.TestCase):
    def test_candidate_stays_suspended_and_writes_no_database_or_volume_resource(self):
        objects=list(yaml.safe_load_all(render(IMAGE,ENDPOINT,PUBLIC)))
        self.assertFalse({'StatefulSet','PersistentVolume','PersistentVolumeClaim','Secret'} & {o['kind'] for o in objects})
        cron=next(o for o in objects if o['kind']=='CronJob')
        self.assertTrue(cron['spec']['suspend'])
        self.assertEqual('Asia/Seoul',cron['spec']['timeZone'])
        self.assertEqual('10 3,15 * * *',cron['spec']['schedule'])
        self.assertEqual('Forbid',cron['spec']['concurrencyPolicy'])
        pod=cron['spec']['jobTemplate']['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertFalse(any('hostPath' in volume or 'persistentVolumeClaim' in volume for volume in pod['volumes']))
        container=pod['containers'][0]
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertEqual(['ALL'],container['securityContext']['capabilities']['drop'])
        self.assertTrue(all('value' in e and ('PASSWORD' not in e['name'] or e['name'].endswith('_FILE')) for e in container['env']))

    def test_activation_requires_an_explicit_flag_after_inputs_are_valid(self):
        objects=list(yaml.safe_load_all(render(IMAGE,ENDPOINT,PUBLIC,activate=True)))
        self.assertFalse(next(o for o in objects if o['kind']=='CronJob')['spec']['suspend'])

    def test_mutable_image_wrong_storage_and_private_key_are_rejected(self):
        for image,endpoint,recipient in [(IMAGE.replace('@sha256:',':'),ENDPOINT,PUBLIC),
            (IMAGE,'http://example.invalid',PUBLIC),(IMAGE,ENDPOINT,'AGE-SECRET-KEY-NOT-A-REAL-KEY')]:
            with self.subTest(inputs=(image,endpoint,recipient)),self.assertRaises(ValueError):
                render(image,endpoint,recipient)

    def test_vault_sync_reads_only_backup_credentials_without_a_restart_target(self):
        objects=list(yaml.safe_load_all((ROOT/'gitops/security/postgresql-backup-vso/postgresql-backup.yaml').read_text()))
        static=next(o for o in objects if o['kind']=='VaultStaticSecret')['spec']
        self.assertEqual('pawbridge/dev/postgresql/backup-r2',static['path'])
        self.assertNotIn('rolloutRestartTargets',static)
        self.assertFalse(static['destination']['overwrite'])
        self.assertEqual(['^access-key-id$','^secret-access-key$'],static['destination']['transformation']['includes'])
        policy=(Path(__file__).parent/'vault-policy.hcl').read_text()
        self.assertIn('capabilities = ["read"]',policy)
        self.assertNotIn('*',policy)
        self.assertNotIn('AGE-SECRET-KEY-',(ROOT/'gitops/stateful/postgresql-backup/cronjob.yaml').read_text())


if __name__=='__main__':
    unittest.main()
