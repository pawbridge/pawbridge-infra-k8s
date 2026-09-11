"""Runtime/capacity PromQL contracts; synthetic data only, no cluster changes."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import yaml
from test_dashboard_queries import dashboards, expand

ROOT = Path(__file__).resolve().parents[1]


def main():
    docs = dashboards()
    comparisons = [p for doc in docs.values() for p in doc['panels'] if p['type'] == 'bargauge']
    assert len(comparisons) == 4
    for panel in comparisons:
        assert panel['options']['orientation'] == 'horizontal'
        assert len(panel['targets']) == 1
        target = panel['targets'][0]
        assert target['instant'] is True and target['range'] is False
        assert target['expr'].startswith('sort_desc(') and target['expr'].endswith(')')
    for name, panel_id in [('pods', 25), ('service', 16)]:
        panel = next(p for p in docs[name]['panels'] if p['id'] == panel_id)
        assert panel['title'] == '$pod · 배치 VM / 파드 준비 상태'
        assert 'kube_pod_status_ready' in panel['targets'][0]['expr']
        assert panel['targets'][0]['legendFormat'] == '{{node}}'
        values = panel['fieldConfig']['defaults']['mappings'][0]['options']
        assert values['0']['text'] == '파드 준비 안 됨'
        assert values['1']['text'] == '파드 준비됨'
        assert panel['fieldConfig']['defaults']['noValue'] == '수집값 없음'
    objects = list(yaml.safe_load_all((ROOT / 'spring-runtime.yaml').read_text()))
    monitor, policy = objects
    assert monitor['metadata']['namespace'] == 'monitoring'
    assert monitor['metadata']['labels'] == {'release': 'pawbridge-observability'}
    spec = monitor['spec']
    assert spec['namespaceSelector'] == {'matchNames': ['pawbridge']}
    assert spec['selector']['matchExpressions'] == [{
        'key': 'app', 'operator': 'In', 'values': [
            'api-gateway', 'user-service', 'animal-service', 'community-service',
            'store-service', 'payment-service']}]
    assert spec['sampleLimit'] == 3000 and spec['targetLimit'] == 10
    endpoint = spec['endpoints'][0]
    assert endpoint['path'] == '/actuator/prometheus' and endpoint['port'] == 'http'
    assert endpoint['interval'] == '30s' and endpoint['scrapeTimeout'] == '10s'
    assert endpoint['honorLabels'] is False
    assert endpoint['relabelings'][0] == {'targetLabel': 'job', 'replacement': 'pawbridge-spring-runtime'}
    keep = endpoint['metricRelabelings'][0]['regex']
    assert not re.fullmatch(keep, 'unbounded_custom_metric')
    for doc in docs.values():
        for p in doc['panels']:
            for t in p.get('targets', []):
                for metric in re.findall(r'\b(?:jvm|http_server)_\w+', t['expr']):
                    assert re.fullmatch(keep, metric), metric
    assert policy['metadata']['namespace'] == 'pawbridge'
    assert policy['spec'] == {
        'podSelector': {'matchLabels': {'app': 'api-gateway'}}, 'policyTypes': ['Ingress'],
        'ingress': [{'from': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'monitoring'}},
                             'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'prometheus',
                                                            'prometheus': 'pawbridge-observability-prometheus'}}}],
                     'ports': [{'protocol': 'TCP', 'port': 8080}]}]}
    # Explicitly reject panel overlap, including the existing repeated pod row.
    for d in docs.values():
        panels = d['panels']
        for i, a in enumerate(panels):
            a = a['gridPos']
            for b in panels[i + 1:]:
                b = b['gridPos']
                assert not (a['x'] < b['x'] + b['w'] and b['x'] < a['x'] + a['w']
                            and a['y'] < b['y'] + b['h'] and b['y'] < a['y'] + a['h'])
    def expr(doc, panel, index=0):
        return expand(next(p for p in docs[doc]['panels'] if p['id'] == panel)['targets'][index]['expr'])
    cases = []
    def case(query, series, value, labels='{}'):
        cases.append({'interval': '1m', 'input_series': [
            {'series': metric, 'values': values} for metric, values in series],
            'promql_expr_test': [{'expr': query, 'eval_time': '5m',
                                 'exp_samples': [] if value is None else [{'labels': labels, 'value': value}]}]})
    node = '{job="node-exporter",node="worker-2"}'
    vm = [('node_memory_MemTotal_bytes' + node, '1000+0x5'),
          ('node_memory_MemAvailable_bytes' + node, '80+0x5'),
          ('node_memory_MemTotal_bytes{job="node-exporter",node="other"}', '9000+0x5')]
    for index, expected in enumerate([1000, 80, 8]):
        case(expr('pods', 26, index), vm, expected)
        case(expr('pods', 26, index), [], None)
    tags = '{namespace="pawbridge",pod="animal-a",container="app"}'
    info = [('kube_pod_container_info' + tags, '1+0x5')]
    limit = [('kube_pod_container_resource_limits{namespace="pawbridge",pod="animal-a",container="app",resource="memory"}', '500+0x5')]
    case(expr('pods', 27, 2), info + limit, 500)
    case(expr('pods', 27, 2), info, -1)
    case(expr('pods', 27, 2), info + limit + [
        ('kube_pod_container_info{namespace="pawbridge",pod="animal-a",container="sidecar"}', '1+0x5')], -1)
    case(expr('pods', 27, 2), [], None)
    base = 'job="pawbridge-spring-runtime",namespace="pawbridge",service="animal-service"'
    count = 'http_server_requests_seconds_count{' + base + ',status="200",uri="/animals"}'
    total = 'http_server_requests_seconds_sum{' + base + ',status="200",uri="/animals"}'
    success = [(count, '0+60x5'), (total, '0+12x5')]
    # Monitoring traffic and other services do not affect the business result.
    unrelated = [(count.replace('/animals', '/actuator/prometheus'), '0+600x5'),
                 (count.replace('animal-service', 'store-service'), '0+600x5')]
    case(expr('service', 31), success + unrelated, 1)
    case(expr('service', 32), success + unrelated, 0.2)
    case(expr('service', 33), success, 0)
    failure = [(count.replace('200', '500'), '0+60x5')]
    case(expr('service', 33), success + failure, 50)
    for panel in [31, 32, 33]:
        case(expr('service', panel), [], None)
    for panel in [32, 33]:
        case(expr('service', panel), [(count, '0+0x5'), (total, '0+0x5')], None)
    case(expr('service', 28), [
        ('jvm_memory_used_bytes{' + base + ',pod="animal-a",area="heap",id="young"}', '20+0x5'),
        ('jvm_memory_used_bytes{' + base + ',pod="animal-a",area="heap",id="old"}', '30+0x5'),
        ('jvm_memory_used_bytes{' + base + ',pod="animal-a",area="nonheap",id="code"}', '100+0x5')],
        50, '{pod="animal-a"}')
    with tempfile.TemporaryDirectory(prefix='pawbridge-runtime-tests-') as tmp:
        path = Path(tmp) / 'tests.yaml'
        path.write_text(yaml.safe_dump({'evaluation_interval': '1m', 'tests': cases}))
        subprocess.run([sys.argv[1], 'test', 'rules', str(path)], check=True)
    print(f'Runtime collection/policy/layout contracts and {len(cases)} PromQL cases passed')


if __name__ == '__main__':
    main()
