import tempfile
import unittest
import copy
from pathlib import Path
import mac_compose_secrets as s


class ServiceSecretContractTest(unittest.TestCase):
    def setUp(self):
        self.inputs = {group: {k: 'synthetic_' + group + '_' + k for k in keys}
                       for group, keys in s.SCHEMA.items()}
        self.base = {'DB_PASSWORD': 'internal-unchanged', 'R2_ACCESS_KEY_ID': 'old',
                     'TOSS_SECRET_KEY': 'old', 'GOOGLE_SECRET_KEY': 'old', 'LLM_PROVIDER': 'stub'}

    def test_every_service_receives_only_declared_provider_fields(self):
        for name, groups in s.SERVICE_INPUTS.items():
            with self.subTest(service=name):
                out = s.service_values(name, self.base, self.inputs)
                expected = {k for group in groups for k in s.SCHEMA[group] if not k.startswith('VITE_')}
                self.assertEqual(expected, {k for k in out if s.is_external(k)})
                self.assertEqual('internal-unchanged', out['DB_PASSWORD'])
                self.assertNotIn('VITE_TOSS_CLIENT_KEY', out)
                self.assertNotIn(self.inputs['r2-backup']['R2_SECRET_ACCESS_KEY'], out.values())
                self.assertNotIn(self.inputs['r2-originals']['R2_SECRET_ACCESS_KEY'], out.values())

    def test_infrastructure_rejects_provider_keys(self):
        with self.assertRaises(ValueError): s.service_values('postgres', self.base, self.inputs)

    def test_duplicate_invalid_and_multiline_fields_rejected_without_values(self):
        for text in ['KEY=private\nKEY=private', 'KEY=private\r', 'KEY=private\ncontinued', 'KEY= private']:
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as ctx: s.parse_env(text, 'synthetic')
                self.assertNotIn('private', str(ctx.exception))

    def test_raw_format_preserves_dollars_quotes_hash_and_equals(self):
        self.assertEqual({'KEY': 'a$b"c\'d#e=f'}, s.parse_env('KEY=a$b"c\'d#e=f\n', 'synthetic'))

    def test_existing_container_image_environment_can_have_lowercase_names(self):
        self.assertEqual({'container': 'oci'}, s.parse_env('container=oci\n', 'kafka'))

    def test_app_overrides_keep_collectors_disabled_and_enable_real_chat(self):
        a = s.service_values('animal-service', self.base, self.inputs)
        for k in ('APMS_PHOTO_ARCHIVE_ENABLED', 'TOURAPI_ENABLED', 'TOURAPI_SCHEDULE_ENABLED', 'SHELTER_DIRECTORY_SCHEDULE_ENABLED'):
            self.assertEqual('false', a[k])
        self.assertEqual('gemini', s.service_values('python-ai-service', self.base, self.inputs)['LLM_PROVIDER'])

    def test_private_writer_refuses_overwrite_and_sets_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'value.env'
            s.write_private(p, 'KEY=synthetic\n')
            self.assertEqual(0o600, p.stat().st_mode & 0o777)
            with self.assertRaises(FileExistsError): s.write_private(p, 'other')
            self.assertEqual('KEY=synthetic\n', p.read_text())

    def test_symlink_and_world_readable_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'input.env'; p.write_text('KEY=synthetic'); p.chmod(0o644)
            with self.assertRaises(ValueError): s.private(p)
            p.chmod(0o600); link = Path(tmp) / 'link'; link.symlink_to(p)
            with self.assertRaises(ValueError): s.private(link)

    def test_relay_has_only_fixed_destinations_and_rejects_unknown_sni(self):
        self.inputs['r2-public'].update(R2_ENDPOINT='https://' + 'a' * 32 + '.r2.cloudflarestorage.com', R2_BUCKET_NAME='synthetic-bucket')
        hosts, text = s.relay_config(self.inputs)
        self.assertIn('default "";', text)
        self.assertIn('map_hash_bucket_size 256;', text)
        self.assertNotIn('$ssl_preread_server_name:', text)
        self.assertNotIn('host.docker.internal', text)
        self.assertIn('smtp.gmail.com', hosts)

    def input_directory(self, root, change=None):
        values = copy.deepcopy(self.inputs)
        values['toss-test'].update(TOSS_SECRET_KEY='test_sk_synthetic', VITE_TOSS_CLIENT_KEY='test_ck_synthetic')
        values['public-data']['APMS_API_BASE_URL']='https://apis.data.go.kr/synthetic'
        values['r2-public'].update(R2_ENDPOINT='https://'+'a'*32+'.r2.cloudflarestorage.com', R2_BUCKET_NAME='synthetic-bucket')
        if change: change(values)
        root.chmod(0o700)
        for group, fields in values.items():
            s.write_private(root/(group+'.env'), ''.join(k+'='+value+'\n' for k,value in fields.items()))

    def test_complete_input_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.input_directory(Path(tmp))
            self.assertEqual(set(s.SCHEMA), set(s.load_inputs(Path(tmp))))

    def test_live_toss_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.input_directory(Path(tmp), lambda v:v['toss-test'].update(TOSS_SECRET_KEY='live_sk_synthetic'))
            with self.assertRaises(ValueError):s.load_inputs(Path(tmp))

    def test_private_or_credential_bearing_provider_endpoint_is_rejected(self):
        for endpoint in ['http://apis.data.go.kr/path','https://localhost/path','https://user@apis.data.go.kr/path','https://apis.data.go.kr/path?key=synthetic']:
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as tmp:
                self.input_directory(Path(tmp),lambda v:v['public-data'].update(APMS_API_BASE_URL=endpoint))
                with self.assertRaises(ValueError):s.load_inputs(Path(tmp))

    def test_missing_and_unexpected_fields_are_rejected(self):
        for mutate in [lambda v:v['gemini'].update(GEMINI_API_KEY=''),lambda v:v['gmail'].update(UNEXPECTED_KEY='synthetic')]:
            with tempfile.TemporaryDirectory() as tmp:
                self.input_directory(Path(tmp),mutate)
                with self.assertRaises(ValueError):s.load_inputs(Path(tmp))


if __name__ == '__main__': unittest.main()
