"""Dashboard isolation and aggregation contracts against real PromQL via promtool.

Run: python3 test_dashboard_queries.py /absolute/path/to/promtool
No live resources, credentials, or network are used.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


def dashboards():
    return {name: json.loads((ROOT / 'dashboards' / f'pawbridge-{name}.json').read_text())
            for name in ['overview', 'service', 'pods']}


def expand(expr, **overrides):
    values = {'__rate_interval': '5m', '__range': '5m', 'namespace': 'pawbridge',
              'service': 'animal-service', 'node': 'worker-2', 'pod': 'animal-a'}
    values.update(overrides)
    for name, value in values.items():
        expr = expr.replace('$' + name, value)
    assert '$' not in expr, expr
    return expr


def main():
    docs = dashboards()
    for doc in docs.values():
        panels = doc['panels']
        assert len({p['id'] for p in panels}) == len(panels)
        assert doc['refresh'] == '30s' and doc['editable'] is False
        assert len(doc['links']) == 3
        for panel in panels:
            for t in panel.get('targets', []):
                assert ' or vector(0)' not in t['expr'], 'Missing collection must not look healthy'
                assert '{{instance}}' not in t['legendFormat'], 'VMs need human-readable names'
                if 'container_network_' in t['expr']:
                    assert 'container!=' not in t['expr'], 'Network series need not have a container label'
    variables = {v['name']: v for v in docs['service']['templating']['list']}
    assert not variables['service']['multi'] and not variables['service']['includeAll']
    assert variables['pod']['multi'] and variables['pod']['includeAll']
    repeated = next(p for p in docs['service']['panels'] if p['type'] == 'row')
    assert repeated['repeat'] == 'pod' and repeated['collapsed'] is False
    assert all('$pod' in t['expr'] for p in docs['service']['panels']
               if p['title'].startswith('$pod') for t in p.get('targets', []))
    assert all('$pod' not in t['expr'] for p in docs['service']['panels']
               if p['title'].startswith('$service') for t in p.get('targets', []))
    vm_variables = {v['name']: v for v in docs['pods']['templating']['list']}
    assert not vm_variables['pod']['multi'] and not vm_variables['pod']['includeAll']
    assert 'node="$node"' in vm_variables['pod']['query']['query']
    config = yaml.safe_load((ROOT / 'values.yaml').read_text())
    assert config['kube-state-metrics']['metricLabelsAllowlist'] == ['pods=[app]']

    def query(doc, title, index=0, **variables):
        panel = next(p for p in docs[doc]['panels'] if p['title'] == title)
        return expand(panel['targets'][index]['expr'], **variables)

    series = []
    def add(metric, labels, values):
        tags = ','.join(f'{key}="{value}"' for key, value in labels.items())
        series.append({'series': metric + '{' + tags + '}', 'values': values})

    for pod, app, ns, memory in [('animal-a', 'animal-service', 'pawbridge', 100),
                                 ('animal-b', 'animal-service', 'pawbridge', 200),
                                 ('store-a', 'store-service', 'pawbridge', 400),
                                 ('animal-a', 'animal-service', 'other', 800)]:
        base = {'namespace': ns, 'pod': pod}
        add('kube_pod_labels', dict(base, label_app=app), '1+0x5')
        add('kube_pod_info', dict(base, node='worker-2'), '1+0x5')
        cadvisor = dict(base, job='kubelet', metrics_path='/metrics/cadvisor')
        add('container_memory_working_set_bytes', dict(cadvisor, container='app'), f'{memory}+0x5')
        # Sandbox memory must not be counted a second time.
        add('container_memory_working_set_bytes', dict(cadvisor, container='POD'), '9000+0x5')
        add('container_cpu_usage_seconds_total', dict(cadvisor, container='app'), '0+60x5')
        # Network is collected at pod sandbox level, without container label.
        add('container_network_receive_bytes_total', dict(cadvisor, interface='eth0'), '0+120x5')

    cases = [
        ('service', '$service · 전체 메모리', {}, [{'labels': '{}', 'value': 300}]),
        ('service', '$service · 전체 CPU', {}, [{'labels': '{}', 'value': 2}]),
        ('service', '$service · 네트워크 속도', {}, [{'labels': '{}', 'value': 4}]),
        ('service', '$service · 선택 기간 누적 트래픽', {}, [{'labels': '{}', 'value': 1200}]),
        ('service', '$pod · 메모리', {}, [{'labels': '{namespace="pawbridge",pod="animal-a"}', 'value': 100}]),
        ('service', '$service · 전체 메모리', {'service': 'missing'}, []),
        ('overview', '서비스별 메모리 · 현재', {}, [
            {'labels': '{label_app="animal-service"}', 'value': 300},
            {'labels': '{label_app="store-service"}', 'value': 400}]),
        ('pods', '$node · 파드별 메모리 비교', {'node': 'missing'}, []),
    ]
    expressions = [{'expr': query(doc, title, **variables), 'eval_time': '5m', 'exp_samples': samples}
                   for doc, title, variables, samples in cases]
    # A pod that disappeared during the selected range still contributes traffic.
    historical = [dict(s) for s in series]
    for s in historical:
        if s['series'].startswith('kube_pod_labels{') and 'pod="animal-b"' in s['series']:
            s['values'] = '1 1 1 1 stale stale'
    fixture = {'evaluation_interval': '1m', 'tests': [
        {'interval': '1m', 'input_series': series, 'promql_expr_test': expressions},
        {'interval': '1m', 'input_series': historical, 'promql_expr_test': [expressions[3]]},
    ]}
    with tempfile.TemporaryDirectory(prefix='pawbridge-dashboard-promql-') as tmp:
        path = Path(tmp) / 'queries.yaml'
        path.write_text(yaml.safe_dump(fixture, allow_unicode=True))
        subprocess.run([sys.argv[1], 'test', 'rules', str(path)], check=True)
    print('Dashboard layout/filter contracts and 9 PromQL scenarios passed')


if __name__ == '__main__':
    main()
