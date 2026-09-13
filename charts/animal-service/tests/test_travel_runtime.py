"""Compare before/after Helm renders for travel activation.

Usage: python3 -B test_travel_runtime.py before.yaml after.yaml paused.yaml
Run with the same animal-service chart, namespace and Argo parameters.
Reads rendered files only; does not call Kubernetes or TourAPI.
"""
import copy
from pathlib import Path
import sys
import unittest

import yaml


TRAVEL_ENV = {
    'TOURAPI_ENABLED': 'true',
    'TOURAPI_SCHEDULE_ENABLED': 'true',
    'TOURAPI_COLLECTION_CRON': '0 */30 * * * *',
    'TOURAPI_MAXPAGESPERRUN': '10',
    'TOURAPI_MAXDETAILSPERRUN': '18',
    'TOURAPI_DAILYREQUESTLIMIT': '900',
    'TOURAPI_DETAILREFRESHDAYS': '14',
}


def objects(path):
    docs = [d for d in yaml.safe_load_all(Path(path).read_text()) if d]
    result = {(d['kind'], d['metadata']['name']): d for d in docs}
    assert len(docs) == len(result), 'Duplicate manifest identity'
    return result


def env_entry(manifests):
    return manifests[('Deployment', 'animal-service')]['spec']['template']['spec']['containers'][0]['env']


class TravelRuntimeTests(unittest.TestCase):
    def test_only_collection_settings_change(self):
        after = copy.deepcopy(AFTER)
        env = env_entry(after)
        added = [entry for entry in env if entry['name'] in TRAVEL_ENV]
        self.assertEqual(len(added), len(TRAVEL_ENV))
        self.assertEqual({entry['name']: entry['value'] for entry in added}, TRAVEL_ENV)
        env[:] = [entry for entry in env if entry['name'] not in TRAVEL_ENV]
        # Includes APMS CronJob, image, replicas, resources, Secret refs and migration Job.
        self.assertEqual(BEFORE, after)

    def test_pause_changes_only_activation_flags(self):
        paused = copy.deepcopy(PAUSED)
        flags = {'TOURAPI_ENABLED', 'TOURAPI_SCHEDULE_ENABLED'}
        found = set()
        for entry in env_entry(paused):
            if entry['name'] in flags:
                self.assertEqual(entry['value'], 'false')
                entry['value'] = 'true'
                found.add(entry['name'])
        self.assertEqual(found, flags)
        self.assertEqual(AFTER, paused)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    BEFORE, AFTER, PAUSED = map(objects, sys.argv[1:])
    unittest.main(argv=[sys.argv[0]])
