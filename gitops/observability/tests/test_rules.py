"""Generate promtool fixtures from the real rule file; no live API or secrets.

Run: python3 test_rules.py /absolute/path/to/promtool
Requires PyYAML and an existing promtool binary. Does not install tools.
"""
import pathlib
import subprocess
import sys
import tempfile

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
RULES = yaml.safe_load((ROOT / 'rules.yaml').read_text())['spec']
BY_NAME = {r['alert']: r for g in RULES['groups'] for r in g['rules']}


def scenario(name, series, checks):
    rule = BY_NAME[name]
    return {
        'name': name,
        'interval': '1m',
        'input_series': [{'series': key, 'values': value} for key, value in series],
        'alert_rule_test': [
            {'eval_time': time, 'alertname': name, 'exp_alerts': [
                {'exp_labels': dict(labels, **rule['labels']),
                 'exp_annotations': rule['annotations']} for labels in expected
            ]} for time, expected in checks
        ],
    }


NODE = {'node': 'worker-test', 'condition': 'Ready', 'status': 'true'}
POD = {'namespace': 'pawbridge', 'pod': 'test-pod', 'uid': 'fixture', 'condition': 'true'}
CONTAINER = {'namespace': 'pawbridge', 'pod': 'test-pod', 'uid': 'fixture', 'container': 'app'}
CASES = [
    scenario('PawBridgeTargetDown', [('up{job="fixture",instance="test"}', '0+0x5 1+0x3')],
             [('4m', []), ('5m', [{'job': 'fixture', 'instance': 'test'}]), ('6m', [])]),
    scenario('PawBridgeMonitoringMissing', [], [('4m', []), ('5m', [{}])]),
    scenario('PawBridgeMonitoringMissing', [
        ('up{job="node-exporter",instance="test"}', '1+0x7'),
        ('kube_node_info{node="test"}', '1+0x7'),
    ], [('6m', [])]),
    scenario('PawBridgeMonitoringMissing', [
        ('up{job="node-exporter",instance="test"}', '1+0x7'),
        ('kube_node_info{node="test"}', '1+0x7'),
        ('kube_node_info{node="missing"}', '1+0x7'),
    ], [('4m', []), ('5m', [{}])]),
    scenario('PawBridgeNodeNotReady', [
        ('kube_node_status_condition{node="worker-test",condition="Ready",status="true"}', '0+0x5 1+0x2')
    ], [('4m', []), ('5m', [NODE]), ('6m', [])]),
    scenario('PawBridgeNodePressure', [
        ('kube_node_status_condition{node="worker-test",condition="MemoryPressure",status="true"}', '1+0x5 0+0x2')
    ], [('4m', []), ('5m', [dict(NODE, condition='MemoryPressure')]), ('6m', [])]),
    scenario('PawBridgeNodeMemoryLow', [
        ('node_memory_MemAvailable_bytes{instance="test"}', '5+0x10 20+0x2'),
        ('node_memory_MemTotal_bytes{instance="test"}', '100+0x13'),
    ], [('9m', []), ('10m', [{'instance': 'test'}]), ('11m', [])]),
    scenario('PawBridgeNodeMemoryLow', [
        ('node_memory_MemAvailable_bytes{instance="test"}', '10+0x12'),
        ('node_memory_MemTotal_bytes{instance="test"}', '100+0x12'),
    ], [('11m', [])]),
    scenario('PawBridgeNodeDiskLow', [
        ('node_filesystem_avail_bytes{instance="test",device="sda1",mountpoint="/",fstype="ext4"}', '10+0x10 30+0x2'),
        ('node_filesystem_size_bytes{instance="test",device="sda1",mountpoint="/",fstype="ext4"}', '100+0x13'),
        ('node_filesystem_readonly{instance="test",device="sda1",mountpoint="/",fstype="ext4"}', '0+0x13'),
    ], [('9m', []), ('10m', [{'instance': 'test', 'device': 'sda1', 'mountpoint': '/', 'fstype': 'ext4'}]), ('11m', [])]),
    scenario('PawBridgePodNotReady', [
        ('kube_pod_status_ready{namespace="pawbridge",pod="test-pod",uid="fixture",condition="true"}', '0+0x10 1+0x2'),
        ('kube_pod_status_phase{namespace="pawbridge",pod="test-pod",uid="fixture",phase="Running"}', '1+0x13'),
    ], [('9m', []), ('10m', [POD]), ('11m', [])]),
    scenario('PawBridgePodNotReady', [
        ('kube_pod_status_ready{namespace="pawbridge",pod="test-pod",uid="fixture",condition="true"}', '0+0x12'),
        ('kube_pod_status_phase{namespace="pawbridge",pod="test-pod",uid="fixture",phase="Succeeded"}', '1+0x12'),
        ('kube_pod_status_phase{namespace="pawbridge",pod="test-pod",uid="fixture",phase="Running"}', '0+0x12'),
    ], [('11m', [])]),
    scenario('PawBridgeContainerRestarting', [
        ('kube_pod_container_status_restarts_total{namespace="pawbridge",pod="test-pod",uid="fixture",container="app"}', '0+1x10 10+0x20'),
    ], [('2m', []), ('10m', [CONTAINER]), ('30m', [])]),
    scenario('PawBridgeContainerOOM', [
        ('kube_pod_container_status_restarts_total{namespace="pawbridge",pod="test-pod",uid="fixture",container="app"}', '0 1+0x20'),
        ('kube_pod_container_status_last_terminated_reason{namespace="pawbridge",pod="test-pod",uid="fixture",container="app",reason="OOMKilled"}', '1+0x22'),
    ], [('0m', []), ('2m', [CONTAINER]), ('20m', [])]),
    scenario('PawBridgeContainerOOM', [
        ('kube_pod_container_status_restarts_total{namespace="pawbridge",pod="test-pod",uid="fixture",container="app"}', '1+0x20'),
        ('kube_pod_container_status_last_terminated_reason{namespace="pawbridge",pod="test-pod",uid="fixture",container="app",reason="OOMKilled"}', '1+0x20'),
    ], [('15m', [])]),
    scenario('PawBridgeRuleEvaluationFailed', [('prometheus_rule_evaluation_failures_total{instance="test"}', '0+1x10 10+0x10')],
             [('0m', []), ('8m', [{'instance': 'test'}]), ('20m', [])]),
    scenario('PawBridgeAlertDeliveryFailed', [('alertmanager_notifications_failed_total{instance="test",integration="slack"}', '0+1x10 10+0x20')],
             [('0m', []), ('8m', [{'instance': 'test', 'integration': 'slack'}]), ('30m', [])]),
]


CASES += [
    scenario('PawBridgeLogPipelineMissing', [], [('4m', []), ('5m', [{}])]),
    scenario('PawBridgeLogPipelineMissing', [
        ('up{service="pawbridge-loki"}', '1+0x7'),
        ('up{service="pawbridge-alloy"}', '1+0x7'),
    ], [('6m', [])]),
    scenario('PawBridgeLogDataLoss', [
        ('loki_write_dropped_entries_total{component_id="fixture"}', '0+1x5 5+0x20'),
    ], [('0m', []), ('4m', [{}]), ('20m', [])]),
    scenario('PawBridgeLogDataLoss', [
        ('loki_ingester_wal_disk_full_failures_total{instance="fixture"}', '0+1x5 5+0x20'),
    ], [('0m', []), ('4m', [{}]), ('20m', [])]),
]


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Usage: test_rules.py /path/to/promtool')
    assert set(BY_NAME) == {c['name'] for c in CASES}, 'Every rule needs a scenario'
    with tempfile.TemporaryDirectory(prefix='pawbridge-rules-') as temp:
        path = pathlib.Path(temp)
        (path / 'rules.yaml').write_text(yaml.safe_dump(RULES, allow_unicode=True))
        (path / 'tests.yaml').write_text(yaml.safe_dump({
            'rule_files': ['rules.yaml'], 'evaluation_interval': '1m', 'tests': CASES,
        }, allow_unicode=True))
        subprocess.run([sys.argv[1], 'check', 'rules', 'rules.yaml'], cwd=path, check=True)
        subprocess.run([sys.argv[1], 'test', 'rules', 'tests.yaml'], cwd=path, check=True)
    print(f'{len(BY_NAME)} rules, {len(CASES)} scenarios passed; no live request.')


if __name__ == '__main__':
    main()
