"""Compare Helm renders: before-default after-default before-dev after-dev.

Render dev with the same Argo parameters: autoscaling.enabled=true,
autoscaling.minReplicas=1, autoscaling.maxReplicas=1, serviceMonitor.enabled=false.
Only manifest fixtures are read; no cluster connection or Secret lookup.
"""
import copy
from pathlib import Path
import sys
import unittest
import yaml


def objects(path):
    docs = [d for d in yaml.safe_load_all(Path(path).read_text()) if d]
    result = {(d['kind'], d['metadata']['name']): d for d in docs}
    assert len(result) == len(docs), 'Duplicate manifest identity'
    return result


class PlacementTests(unittest.TestCase):
    def test_default_render_unchanged(self):
        self.assertEqual(BEFORE_DEFAULT, AFTER_DEFAULT)

    def test_only_deployment_placement_changes(self):
        before = copy.deepcopy(BEFORE_DEV)
        after = copy.deepcopy(AFTER_DEV)
        old_spec = before[('Deployment', 'store-service')]['spec']['template']['spec']
        new_spec = after[('Deployment', 'store-service')]['spec']['template']['spec']
        self.assertEqual(old_spec.pop('nodeSelector'),
                         {'kubernetes.io/hostname': 'pawbridge-k136-w2'})
        self.assertNotIn('nodeSelector', new_spec)
        self.assertNotIn('affinity', old_spec)
        self.assertIn('affinity', new_spec)
        new_spec.pop('affinity')
        # Includes image digest, probes, env, Secret refs, limits, HPA and PreSync Job.
        self.assertEqual(before, after)

    def test_only_workers_allowed_with_w1_preferred(self):
        spec = AFTER_DEV[('Deployment', 'store-service')]['spec']['template']['spec']
        self.assertEqual(spec['affinity'], {'nodeAffinity': {
            'requiredDuringSchedulingIgnoredDuringExecution': {'nodeSelectorTerms': [
                {'matchExpressions': [{'key': 'kubernetes.io/hostname', 'operator': 'In',
                                       'values': ['pawbridge-k136-w1', 'pawbridge-k136-w2']}]}]},
            'preferredDuringSchedulingIgnoredDuringExecution': [
                {'weight': 100, 'preference': {'matchExpressions': [
                    {'key': 'kubernetes.io/hostname', 'operator': 'In',
                     'values': ['pawbridge-k136-w1']}]}}]}})


if __name__ == '__main__':
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    BEFORE_DEFAULT, AFTER_DEFAULT, BEFORE_DEV, AFTER_DEV = map(objects, sys.argv[1:])
    unittest.main(argv=[sys.argv[0]])
