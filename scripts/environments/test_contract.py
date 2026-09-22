import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml
from promote_image import promote
from local_dev import compose, prepare, app_env, ROOT

class EnvironmentTest(unittest.TestCase):
    def test_compose_has_only_local_ports_and_isolated_storage(self):
        c=compose()
        self.assertEqual('pawbridge-dev',c['name'])
        self.assertTrue(c['networks']['dev']['internal'])
        for name,s in c['services'].items():
            if name=='kafka-volume-init':
                self.assertEqual('none',s['network_mode']);continue
            self.assertEqual(['dev','access'] if name=='local-access' else ['dev'],s['networks'])
            self.assertIn('@sha256:',s['image'])
            self.assertIn('mem_limit',s)
            self.assertNotIn('network_mode',s)
            self.assertNotIn('privileged',s)
            self.assertTrue(all(p.startswith('127.0.0.1:') for p in s.get('ports',[])))
        self.assertFalse(any(v.get('external') for v in c['volumes'].values()))
        self.assertFalse(any(s.get('restart')=='always' for s in c['services'].values()))

    def test_external_collection_and_production_endpoints_are_not_used(self):
        for name in ('api-gateway','animal-service','user-service','community-service','store-service','payment-service'):
            for host in (True,False):
                e=app_env(name,host)
                for key in ('APMS_PHOTO_ARCHIVE_ENABLED','TOURAPI_ENABLED','TOURAPI_SCHEDULE_ENABLED','SHELTER_DIRECTORY_SCHEDULE_ENABLED','LOST_GALLERY_FEED_ENABLED','SPRING_BATCH_JOB_ENABLED'):
                    self.assertEqual('false',e[key])
                self.assertNotIn('api.pawbridge.kr',json.dumps(e))
                self.assertNotIn('.svc.',json.dumps(e))
                if name!='api-gateway':self.assertTrue(e[name.split('-')[0].upper()+'_POSTGRESQL_USERNAME'].startswith('pawbridge_dev_'))
        self.assertIn('127.0.0.1:19092',app_env('animal-service',True)['SPRING_KAFKA_BOOTSTRAP_SERVERS'])
        self.assertEqual('kafka:9092',app_env('animal-service')['SPRING_KAFKA_BOOTSTRAP_SERVERS'])

    def test_preparation_preserves_passwords_and_refuses_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'dev';prepare(state);before=(state/'.env').read_bytes()
            prepare(state);self.assertEqual(before,(state/'.env').read_bytes())
            self.assertEqual(0o700,state.stat().st_mode & 0o777)
            self.assertEqual(0o600,(state/'.env').stat().st_mode & 0o777)
            self.assertNotIn('${',(state/'animal-service.env').read_text())
            other=Path(tmp)/'other';other.mkdir();(other/'mine').write_text('keep')
            with self.assertRaises(ValueError):prepare(other)
            self.assertEqual('keep',(other/'mine').read_text())

    def test_cdc_targets_only_local_data_and_does_not_copy_secrets(self):
        docs=json.loads((ROOT/'environments/dev/compose/connectors.json').read_text())
        self.assertEqual(5,len(docs))
        for name,c in docs.items():
            self.assertEqual('postgresql',c['database.hostname'])
            self.assertEqual('pawbridge_dev_'+name+'_cdc',c['database.user'])
            self.assertEqual('${file:/run/secrets/cdc.properties:'+name+'.password}',c['database.password'])
            self.assertEqual('disabled',c['publication.autocreate.mode'])
            self.assertEqual('no_data',c['snapshot.mode'])

    def test_promotion_changes_image_only_and_requires_exact_tested_digest(self):
        digest='sha256:'+'a'*64; revision='b'*40
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); (root/'environments').mkdir()
            (root/'environments/environment-contract.json').write_text(json.dumps({'services':{'photo-service':{'devValues':'dev.yaml','prodValues':'prod.yaml'}}}))
            prod={'image':{'repository':'test/image','digest':'sha256:'+'c'*64},'replicaCount':1,'secretRef':'prod-secret','env':{'DB':'unchanged'}}
            (root/'prod.yaml').write_text(yaml.safe_dump(prod)); evidence=root/'evidence.json'
            evidence.write_text(json.dumps({'environment':'dev','result':'passed','revision':revision,'images':{'photo-service':digest},'report':'local-e2e-report'}))
            with patch('promote_image.subprocess.check_output', return_value=yaml.safe_dump({'image':{'repository':'test/image','digest':digest},'secretRef':'dev-secret'})):
                path,content=promote(root,'photo-service',revision,digest,evidence)
                after=yaml.safe_load(content)
                self.assertEqual('prod-secret',after['secretRef']); self.assertEqual(prod['env'],after['env'])
                self.assertEqual(digest,after['image']['digest']); self.assertEqual(prod,yaml.safe_load(path.read_text()))
                with self.assertRaises(ValueError): promote(root,'photo-service',revision,'sha256:'+'d'*64,evidence)
