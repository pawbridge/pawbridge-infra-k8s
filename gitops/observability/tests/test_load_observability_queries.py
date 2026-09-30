"""Load-analysis metric contracts, using synthetic PromQL data only."""
import copy
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import yaml
from test_dashboard_queries import dashboards, expand
from test_memory_logs_panels import fixture as memory_fixture
from check_render import validate_dashboards

ROOT = Path(__file__).resolve().parents[1]

def main():
    docs = dashboards()
    values = yaml.safe_load((ROOT / 'values.yaml').read_text())
    source = values['grafana']['datasources']['datasources.yaml']['datasources'][0]
    assert source['jsonData']['timeInterval'] == values['prometheus']['prometheusSpec']['scrapeInterval'] == '30s'
    variables = {v['name']: v for v in docs['pods']['templating']['list']}
    assert variables['namespace']['multi'] is False and variables['namespace']['includeAll'] is False
    assert variables['namespace']['current']['value'] == 'pawbridge'
    # Use the shared validator against real dashboard sources without a Helm fixture.
    objects = [{'kind': 'ConfigMap', 'metadata': {'name': 'pawbridge-observability-dashboards', 'namespace': 'monitoring'},
                'data': {f'pawbridge-{name}.json': json.dumps(doc) for name, doc in docs.items()}}]
    validate_dashboards(objects)
    for name, pid, uid in [('pods', 37, 'pawbridge-prometheus'), ('service', 42, 'pawbridge-loki')]:
        altered = copy.deepcopy(objects)
        key = f'pawbridge-{name}.json'
        doc = json.loads(altered[0]['data'][key])
        next(p for p in doc['panels'] if p['id'] == pid)['datasource']['uid'] = uid
        altered[0]['data'][key] = json.dumps(doc)
        try:
            validate_dashboards(altered)
        except AssertionError:
            pass
        else:
            raise AssertionError('Wrong panel datasource accepted')
    objects = list(yaml.safe_load_all((ROOT / "spring-runtime.yaml").read_text()))
    keep = objects[0]["spec"]["endpoints"][0]["metricRelabelings"][0]["regex"]
    for name in ["active", "idle", "max", "pending", "timeout_total",
                 "acquire_seconds_count", "acquire_seconds_sum"]:
        assert re.fullmatch(keep, "hikaricp_connections_" + name)
    assert not re.fullmatch(keep, "hikaricp_unbounded_custom")
    for doc in docs.values():
        panels = doc["panels"]
        assert len({p["id"] for p in panels}) == len(panels)
        for i, a in enumerate(panels):
            a = a["gridPos"]
            for b in panels[i + 1:]:
                b = b["gridPos"]
                assert not (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                            and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])
    cases = []
    def query(doc, pid, index=0):
        p = next(p for p in docs[doc]["panels"] if p["id"] == pid)
        return expand(p["targets"][index]["expr"])
    def case(expr, series, expected):
        cases.append({"interval": "1m", "input_series": [
            {"series": name, "values": values} for name, values in series],
            "promql_expr_test": [{"expr": expr, "eval_time": "5m", "exp_samples": expected}]})
    pool = '{job="pawbridge-spring-runtime",namespace="pawbridge",service="animal-service",pod="animal-a",pool="normal"}'
    labels = '{pod="animal-a",pool="normal"}'
    sample = lambda value: [{"labels": labels, "value": value}]
    for pid, metric, value, index in [(38, "active", 10, 0), (38, "idle", 5, 1), (38, "max", 20, 2), (39, "pending", 83, 0)]:
        series = [("hikaricp_connections_" + metric + pool, str(value) + "+0x5"),
                  ("hikaricp_connections_" + metric + pool.replace("animal-service", "store-service"), "900+0x5")]
        case(query("service", pid, index), series, sample(value))
        case(query("service", pid, index), [], [])
    count = "hikaricp_connections_acquire_seconds_count" + pool
    total = "hikaricp_connections_acquire_seconds_sum" + pool
    case(query("service", 40), [(count, "0+60x5"), (total, "0+3x5")], sample(50))
    case(query("service", 40), [(count, "0+0x5"), (total, "0+0x5")], [])
    case(query("service", 40), [], [])
    timeout = "hikaricp_connections_timeout_total" + pool
    case(query("service", 41), [(timeout, "0+60x5")], sample(1))
    case(query("service", 41), [(timeout, "0+0x5")], sample(0))
    case(query("service", 41), [], [])
    tags = '{job="kubelet",metrics_path="/metrics/cadvisor",namespace="pawbridge",pod="animal-a",container="app"}'
    periods = "container_cpu_cfs_periods_total" + tags
    throttled = "container_cpu_cfs_throttled_periods_total" + tags
    cpu_labels = '{job="kubelet",metrics_path="/metrics/cadvisor",namespace="pawbridge",pod="animal-a",container="app"}'
    case(query("pods", 38), [(periods, "0+240x5"), (throttled, "0+60x5")],
         [{"labels": cpu_labels, "value": 25}])
    case(query("pods", 38), [(periods, "0+0x5"), (throttled, "0+0x5")], [])
    case(query("pods", 38), [], [])
    # Different APIs/statuses must remain distinct, and unrelated traffic stays excluded.
    http = 'job="pawbridge-spring-runtime",namespace="pawbridge",service="animal-service"'
    def timer(metric, uri, method, status):
        return 'http_server_requests_seconds_' + metric + '{' + http + ',uri="' + uri + '",method="' + method + '",status="' + status + '"}'
    traffic = [(timer('count', '/animals', 'GET', '200'), '0+60x5'),
               (timer('sum', '/animals', 'GET', '200'), '0+12x5'),
               (timer('count', '/slow', 'POST', '200'), '0+120x5'),
               (timer('sum', '/slow', 'POST', '200'), '0+120x5'),
               (timer('count', '/animals', 'GET', '429'), '0+30x5'),
               (timer('sum', '/animals', 'GET', '429'), '0+6x5'),
               (timer('count', '/actuator/prometheus', 'GET', '200'), '0+600x5'),
               (timer('count', '/animals', 'GET', '200').replace('animal-service', 'store-service'), '0+600x5')]
    case(query('service', 42), traffic, [
        {'labels': '{method="GET",uri="/animals"}', 'value': 1.5},
        {'labels': '{method="POST",uri="/slow"}', 'value': 2}])
    case(query('service', 43), traffic, [
        {'labels': '{method="GET",uri="/animals"}', 'value': 0.2},
        {'labels': '{method="POST",uri="/slow"}', 'value': 1}])
    case(query('service', 44), traffic, [
        {'labels': '{status="200"}', 'value': 3}, {'labels': '{status="429"}', 'value': 0.5}])
    for pid in [42, 43, 44, 45]:
        case(query('service', pid), [], [])
    case(query('service', 43), [(timer('count', '/animals', 'GET', '200'), '0+0x5'),
                               (timer('sum', '/animals', 'GET', '200'), '0+0x5')], [])
    case(query('service', 45), [
        ('up{' + http + ',pod="animal-a",instance="a"}', '1+0x5'),
        ('up{' + http + ',pod="animal-b",instance="b"}', '0+0x5'),
        ('up{' + http.replace('animal-service', 'store-service') + ',pod="store-a"}', '1+0x5')],
        [{'labels': '{pod="animal-a"}', 'value': 1}, {'labels': '{pod="animal-b"}', 'value': 0}])
    # Equal metric counts with different node identities must not pass completeness.
    nodes = [('kube_node_info{node="worker-1"}', '1+0x5'),
             ('kube_node_info{node="worker-2"}', '1+0x5')]
    def memory(node, total, available):
        return [('node_memory_MemTotal_bytes{job="node-exporter",node="' + node + '"}', str(total) + '+0x5'),
                ('node_memory_MemAvailable_bytes{job="node-exporter",node="' + node + '"}', str(available) + '+0x5')]
    wrong_identity = nodes + memory('worker-1', 1000, 500) + memory('retired', 9000, 8000)
    extra_identity = wrong_identity + memory('worker-2', 2000, 400)
    for i, expected in enumerate([3000, 2100, 900]):
        case(query('overview', 34, i), wrong_identity, [])
        case(query('overview', 34, i), extra_identity, [{'labels': '{}', 'value': expected}])
    # Retain all original full/missing/duplicate-memory checks (24 expression checks).
    cases.extend(memory_fixture()['tests'])
    expression_count = sum(len(c['promql_expr_test']) for c in cases)
    if len(sys.argv) == 2 and sys.argv[1] == "--static-only":
        print(f"Static metric/layout checks passed; {expression_count} PromQL cases prepared, not executed")
        return
    if len(sys.argv) != 2:
        raise SystemExit("Pass an existing promtool path or --static-only")
    with tempfile.TemporaryDirectory(prefix="pawbridge-load-queries-") as tmp:
        path = Path(tmp) / "cases.yaml"
        path.write_text(yaml.safe_dump({"evaluation_interval": "1m", "fuzzy_compare": True, "tests": cases}))
        subprocess.run([sys.argv[1], "test", "rules", str(path)], check=True)
    print(f"Load-analysis collection/layout and {expression_count} PromQL cases passed")

if __name__ == "__main__":
    main()
