"""Verify the rendered APMS shutdown and deadline contract.

Usage: python3 -B test_batch_shutdown_contract.py rendered.yaml
"""
from pathlib import Path
import sys
import unittest

import yaml


def resources(path):
    documents = [document for document in yaml.safe_load_all(Path(path).read_text()) if document]
    return {(document['kind'], document['metadata']['name']): document for document in documents}


class BatchShutdownContractTest(unittest.TestCase):
    def test_pod_grace_exceeds_spring_shutdown_deadline(self):
        deployment = RENDERED[('Deployment', 'animal-service')]
        pod_spec = deployment['spec']['template']['spec']
        self.assertEqual(360, pod_spec['terminationGracePeriodSeconds'])

        env = {entry['name']: entry['value']
               for entry in pod_spec['containers'][0]['env'] if 'value' in entry}
        self.assertEqual('330s', env['SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE'])
        self.assertEqual('10m', env['APMS_BATCH_STALE_EXECUTION_THRESHOLD'])
        self.assertEqual('4m', env['APMS_BATCH_ELASTICSEARCH_INDEX_TIMEOUT'])
        self.assertEqual('35s', env['APMS_BATCH_ELASTICSEARCH_CANCELLATION_WAIT'])
        self.assertEqual('4', env['APMS_BATCH_ELASTICSEARCH_PARALLELISM'])

    def test_trigger_deadline_covers_backend_index_deadline(self):
        cronjob = RENDERED[('CronJob', 'animal-service-batch')]
        job_spec = cronjob['spec']['jobTemplate']['spec']
        self.assertEqual(600, job_spec['activeDeadlineSeconds'])
        args = job_spec['template']['spec']['containers'][0]['args']
        max_time = args[args.index('--max-time') + 1]
        self.assertEqual('300', max_time)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    RENDERED = resources(sys.argv[1])
    unittest.main(argv=[sys.argv[0]])
