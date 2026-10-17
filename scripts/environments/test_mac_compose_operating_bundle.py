import unittest,copy
import mac_compose_operating_bundle as b

class OperatingBundleContract(unittest.TestCase):
    def setUp(self):
        self.values=b.prep.fresh_values()
        self.inputs={g:{k:g+'-'+k for k in fields} for g,fields in b.prep.external.SCHEMA.items()}
        image_keys = ('POSTGRES', 'REDIS', 'EVENTS', 'ACCESS', 'API_GATEWAY',
                      'USER_SERVICE', 'ANIMAL_SERVICE', 'COMMUNITY_SERVICE',
                      'STORE_SERVICE', 'PAYMENT_SERVICE', 'PHOTO_SERVICE', 'PYTHON_AI_SERVICE')
        self.images = {'MAC_IMAGE_' + key: 'sha256:' + format(i, '064x')
                       for i, key in enumerate(image_keys, 1)}

    def test_animal_connection_capacity_covers_collection_overlap_in_every_mode(self):
        for mode in b.MODE_FLAGS:
            with self.subTest(mode=mode):
                env=b.environments(self.values,self.inputs,mode)
                animal=env['animal-service']
                for key in ('SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE',
                            'ANIMAL_POSTGRESQL_MAX_POOL_SIZE','ANIMAL_POSTGRESQL_POOL_MAX'):
                    self.assertEqual('10',animal[key])
                for key in ('SPRING_DATASOURCE_HIKARI_MINIMUMIDLE',
                            'ANIMAL_POSTGRESQL_MIN_IDLE','ANIMAL_POSTGRESQL_POOL_MIN'):
                    self.assertEqual('0',animal[key])
                for service in ('user','community','store','payment'):
                    settings=env[service+'-service']
                    self.assertEqual('3',settings['SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE'])
                    self.assertEqual('3',settings[service.upper()+'_POSTGRESQL_MAX_POOL_SIZE'])
                    self.assertEqual('0',settings[service.upper()+'_POSTGRESQL_MIN_IDLE'])

    def test_standby_disables_sources_and_cleanup_but_preserves_api_configuration(self):
        env=b.environments(self.values,self.inputs,'standby')
        for svc in b.APP_PORTS:
            if svc not in ('photo-service','python-ai-service'):
                self.assertEqual('false',env[svc]['PAWBRIDGE_JOBS_CLEANUP_ENABLED'])
        animal=env['animal-service']
        self.assertEqual('false',animal['APMS_BATCH_SCHEDULE_ENABLED'])
        self.assertEqual('false',animal['APMS_PHOTO_ARCHIVE_ENABLED'])
        self.assertEqual('false',animal['TOURAPI_SCHEDULE_ENABLED'])
        self.assertEqual('-',animal['PAWBRIDGE_SHELTER_OBSERVATIONS_CRON'])
        self.assertEqual('true',animal['TOURAPI_ENABLED'])

    def test_drain_keeps_cart_projection_and_consumers_without_new_collection(self):
        env=b.environments(self.values,self.inputs,'drain')
        self.assertEqual('true',env['store-service']['PAWBRIDGE_JOBS_CART_SYNC_ENABLED'])
        self.assertEqual('true',env['animal-service']['PAWBRIDGE_ANIMAL_QUERY_PROJECTION_SCHEDULE_ENABLED'])
        self.assertEqual('false',env['animal-service']['APMS_BATCH_SCHEDULE_ENABLED'])
        self.assertEqual('false',env['user-service']['PAWBRIDGE_JOBS_CLEANUP_ENABLED'])
        self.assertNotIn('SPRING_KAFKA_LISTENER_AUTO_STARTUP',env['animal-service'])

    def test_active_schedule_contract_matches_confirmed_source_intervals(self):
        env=b.environments(self.values,self.inputs,'active')
        self.assertEqual('true',env['animal-service']['APMS_BATCH_SCHEDULE_ENABLED'])
        self.assertEqual('0 */30 * * * *',env['animal-service']['APMS_BATCH_COLLECTION_CRON'])
        self.assertEqual('60000',env['animal-service']['APMS_PHOTO_ARCHIVE_INTERVAL_MS'])
        self.assertEqual('true',env['community-service']['PAWBRIDGE_JOBS_REFRESH_ENABLED'])

    def test_secret_distribution_keeps_admin_cdc_backup_and_frontend_out_of_apps(self):
        env=b.environments(self.values,self.inputs)
        denied=[self.values['roles']['postgres']]+[v for k,v in self.values['roles'].items() if k.endswith('_cdc')]+list(self.inputs['r2-backup'].values())
        for svc in b.APP_PORTS:
            self.assertFalse(set(denied)&set(env[svc].values()))
            self.assertFalse(any(k.startswith('VITE_') for k in env[svc]))
        for svc in b.prep.APPS:
            self.assertEqual('pawbridge_'+svc+'_app',env[svc+'-service'][svc.upper()+'_POSTGRESQL_USERNAME'])
        self.assertEqual(self.inputs['r2-originals']['R2_SECRET_ACCESS_KEY'],env['animal-service']['APMS_PHOTO_ARCHIVE_SECRET_ACCESS_KEY'])
        self.assertNotIn(self.inputs['r2-originals']['R2_SECRET_ACCESS_KEY'],env['photo-service'].values())
        self.assertEqual(self.inputs['r2-originals']['R2_SECRET_ACCESS_KEY'],env['animal-service']['LOST_GALLERY_FEED_STORAGE_SECRET_ACCESS_KEY'])

    def test_no_db_schema_migration_or_native_gallery_writer_implicitly_starts(self):
        env=b.environments(self.values,self.inputs,'active')
        for svc in b.prep.APPS:
            self.assertEqual('false',env[svc+'-service']['SPRING_FLYWAY_ENABLED'])
            self.assertEqual('validate',env[svc+'-service']['SPRING_JPA_HIBERNATE_DDL_AUTO'])
        self.assertEqual('false',env['native-ai']['LOST_GALLERY_SYNC_ENABLED'])
        self.assertEqual('false',env['python-ai-service']['LOST_GALLERY_SYNC_ENABLED'])

    def test_unknown_mode_and_reused_generated_credentials_refused(self):
        with self.assertRaises(ValueError):b.environments(self.values,self.inputs,'unknown')
        self.values['redis']=self.values['internal_api']
        with self.assertRaises(ValueError):b.environments(self.values,self.inputs)

    def test_required_auth_expiries_and_payment_store_url_are_resolved(self):
        env=b.environments(self.values,self.inputs)
        self.assertGreater(int(env['user-service']['JWT_ACCESS_TOKEN_EXPIRATION']),0)
        self.assertGreater(int(env['user-service']['JWT_REFRESH_TOKEN_EXPIRATION']),int(env['user-service']['JWT_ACCESS_TOKEN_EXPIRATION']))
        self.assertEqual('http://store-service:8083',env['payment-service']['STORE_SERVICE_URL'])
        self.assertEqual('/tmp/kafka-logs',env['kafka']['LOG_DIR'])

    def test_fresh_cdc_preserves_original_publications_and_uses_new_mac_slots(self):
        cfg=b.connector_configs()
        self.assertEqual(set(b.prep.APPS),set(cfg))
        for svc,c in cfg.items():
            self.assertEqual('no_data',c['snapshot.mode'])
            self.assertEqual('disabled',c['publication.autocreate.mode'])
            self.assertEqual('false',c['slot.drop.on.stop'])
            self.assertEqual('pawbridge_'+svc+'_outbox',c['publication.name'])
            self.assertEqual('pawbridge_mac_'+svc+'_outbox',c['slot.name'])
            self.assertEqual('pawbridge_'+svc+'_cdc',c['database.user'])
            self.assertIn('${file:',c['database.password'])

    def test_isolated_topology_cannot_reach_host_native_or_publish_ports(self):
        doc=b.compose(self.images,True);self.assertTrue(b.validate(doc,True))
        self.assertNotIn('host.docker.internal',b.configuration_files(True)['native-relay.conf'])
        self.assertIn('return 503',b.configuration_files(True)['native-relay.conf'])
        for changed in ('ports','networks','restart','privileged'):
            bad=copy.deepcopy(doc)
            bad['services']['postgres'][changed]={'ports':['5432:5432'],'networks':['private','egress'],'restart':'always','privileged':True}[changed]
            with self.subTest(changed=changed),self.assertRaises(ValueError):b.validate(bad,True)

    def test_unsafe_image_or_host_mount_is_refused(self):
        doc=b.compose(self.images,True)
        for changed in ('image','mount'):
            bad=copy.deepcopy(doc)
            if changed=='image':bad['services']['postgres']['image']='postgres:latest'
            else:bad['services']['postgres']['volumes'][0]['source']='/Users/shyu'
            with self.subTest(changed=changed),self.assertRaises(ValueError):b.validate(bad,True)

    def test_operating_private_infra_and_loopback_ingress(self):
        doc=b.compose(self.images,False);self.assertTrue(b.validate(doc,False))
        for svc in ('redis','kafka','connect'):self.assertEqual(['private'],doc['services'][svc]['networks'])
        for spec in doc['services'].values():
            for port in spec.get('ports',[]):self.assertTrue(port.startswith('127.0.0.1:'))
        self.assertEqual('400s',doc['services']['animal-service']['stop_grace_period'])
        self.assertNotIn('cloudflared',doc['services'])
        bad=copy.deepcopy(doc);bad['services']['postgres']['ports']=['0.0.0.0:5432:5432']
        with self.assertRaises(ValueError):b.validate(bad,False)
        bad=copy.deepcopy(doc);bad['services']['connect']['networks'].append('egress')
        with self.assertRaises(ValueError):b.validate(bad,False)

if __name__=='__main__':unittest.main()
