import unittest
import mac_compose_operating as o

class OperatingPreparationTest(unittest.TestCase):
 def test_distinct_role_passwords_and_shared_consumers(self):
  v=o.fresh_values();o.validate_values(v);data=o.fragments(v)
  for svc in o.APPS:self.assertEqual(v['roles']['pawbridge_'+svc+'_app'],data[svc+'-service'][svc.upper()+'_POSTGRESQL_PASSWORD'])
  self.assertEqual(data['animal-service']['PYTHON_AI_SERVICE_INTERNAL_API_KEY'],data['python-ai-service']['INTERNAL_API_KEY'])
 def test_no_admin_or_cdc_password_in_app_fragments(self):
  v=o.fresh_values();data=o.fragments(v)
  blocked=[v['roles']['postgres']]+[v['roles']['pawbridge_'+s+'_cdc'] for s in o.APPS]
  self.assertFalse(any(secret in env.values() for env in data.values() for secret in blocked))
 def test_original_role_names_preserved_without_owner_passwords(self):
  v=o.fresh_values()
  self.assertEqual(12,len(v['roles']));self.assertFalse(any('_mac_' in k or k.endswith('_migration') for k in v['roles']))
 def test_duplicate_password_rejected(self):
  v=o.fresh_values();v['roles']['postgres']=v['redis']
  with self.assertRaises(ValueError):o.validate_values(v)
 def test_generation_has_no_trial_role_or_guard(self):
  data=o.fragments(o.fresh_values())
  self.assertFalse(any('trial_' in v or '/trial/' in v for env in data.values() for v in env.values()))
  self.assertEqual('false',data['animal-service']['APMS_BATCH_SCHEDULE_ENABLED'])
 def test_each_preparation_uses_new_values(self):
  a=o.fresh_values();b=o.fresh_values();self.assertTrue(set(a['roles'].values()).isdisjoint(b['roles'].values()))

if __name__=='__main__':unittest.main()
