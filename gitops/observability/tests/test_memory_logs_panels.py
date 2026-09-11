"""Dashboard display contracts; --fixture emits synthetic PromQL tests for promtool."""
import itertools
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def docs():
    return {name: json.loads((ROOT / 'dashboards' / f'pawbridge-{name}.json').read_text())
            for name in ['overview', 'pods', 'service']}


def panel(name, identifier):
    return next(p for p in docs()[name]['panels'] if p['id'] == identifier)


class DisplayContracts(unittest.TestCase):
    def test_panels_do_not_overlap_and_ids_are_unique(self):
        for name, doc in docs().items():
            self.assertEqual(len(doc['panels']), len({p['id'] for p in doc['panels']}))
            for a, b in itertools.combinations(doc['panels'], 2):
                x, y = a['gridPos'], b['gridPos']
                overlap = (x['x'] < y['x'] + y['w'] and y['x'] < x['x'] + x['w']
                           and x['y'] < y['y'] + y['h'] and y['y'] < x['y'] + x['h'])
                self.assertFalse(overlap, (name, a['id'], b['id']))

    def test_capacity_panels_are_non_stacked_range_queries(self):
        for name, identifier in [('overview', 34), ('pods', 35)]:
            p = panel(name, identifier)
            self.assertEqual(p['type'], 'timeseries')
            self.assertEqual(p['fieldConfig']['defaults']['unit'], 'bytes')
            self.assertFalse(p['fieldConfig']['defaults']['custom']['spanNulls'])
            self.assertEqual(p['fieldConfig']['defaults']['custom']['stacking']['mode'], 'none')
            self.assertEqual(len(p['targets']), 3)
            for target in p['targets']:
                self.assertFalse(target['instant'])
                self.assertTrue(target['range'])
                self.assertNotIn('vector(0)', target['expr'])

    def test_vm_capacity_is_independent_of_pod_and_namespace(self):
        for t in panel('pods', 35)['targets']:
            self.assertIn('node="$node"', t['expr'])
            self.assertNotIn('$pod', t['expr'])
            self.assertNotIn('$namespace', t['expr'])

    def test_log_panels_fetch_recent_lines_and_display_oldest_first(self):
        for name, identifier in [('service', 36), ('pods', 37)]:
            p = panel(name, identifier)
            self.assertEqual(p['datasource']['uid'], 'pawbridge-loki')
            self.assertEqual(p['type'], 'logs')
            self.assertEqual(p['options']['sortOrder'], 'Ascending')
            self.assertEqual(p['options']['dedupStrategy'], 'none')
            self.assertTrue(p['options']['showTime'])
            self.assertTrue(p['options']['wrapLogMessage'])
            self.assertEqual(p['targets'][0]['maxLines'], 1000)
            # Fetch latest 1000, then display in ascending order; forward would fetch oldest.
            self.assertEqual(p['targets'][0]['direction'], 'backward')

    def test_log_filters_use_real_alloy_labels_and_scope(self):
        self.assertEqual(panel('service', 36)['targets'][0]['expr'],
                         '{namespace="$namespace",app="$service"}')
        self.assertEqual(panel('pods', 37)['targets'][0]['expr'],
                         '{namespace=~"$namespace",pod="$pod"}')
        row = next(p for p in docs()['service']['panels'] if p['type'] == 'row')
        self.assertLess(panel('service', 36)['gridPos']['y'], row['gridPos']['y'])
        self.assertIn('DB·모니터링 파드는 대상이 아닙니다', panel('pods', 37)['description'])


def fixture():
    queries = [t['expr'].replace('$node', 'worker-2')
               for name, identifier in [('overview', 34), ('pods', 35)]
               for t in panel(name, identifier)['targets']]
    tests = []
    scenarios = [
        (None, [3000, 2100, 900, 2000, 1600, 400]),
        ('available', [3000, None, None, 2000, None, None]),
        ('total', [None, None, 900, None, None, 400]),
        ('all', [None] * 6),
    ]
    for missing, expected in scenarios:
        series = []
        for node, total, available in [('worker-1', 1000, 500), ('worker-2', 2000, 400)]:
            series.append({'series': f'kube_node_info{{node="{node}"}}', 'values': '1+0x5'})
            for metric, value, category in [('MemTotal', total, 'total'), ('MemAvailable', available, 'available')]:
                if missing == 'all' or (node == 'worker-2' and missing == category):
                    continue
                # Duplicate scrape identities must not double-count node memory.
                for instance in ['primary', 'duplicate']:
                    series.append({'series': f'node_memory_{metric}_bytes{{job="node-exporter",node="{node}",instance="{instance}"}}',
                                   'values': f'{value}+0x5'})
        tests.append({'interval': '1m', 'input_series': series, 'promql_expr_test': [
            {'expr': expr, 'eval_time': '5m', 'exp_samples': [] if value is None else [{'labels': '{}', 'value': value}]}
            for expr, value in zip(queries, expected)]})
    return {'evaluation_interval': '1m', 'tests': tests}


if __name__ == '__main__':
    if '--fixture' in sys.argv:
        import yaml
        print(yaml.safe_dump(fixture(), allow_unicode=True))
    else:
        unittest.main()
