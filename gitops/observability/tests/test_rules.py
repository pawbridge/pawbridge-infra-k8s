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


TTL = 'kubelet_certificate_manager_server_ttl_seconds{node="node-a",job="kubelet",metrics_path="/metrics"}'
NODE_A = {'node': 'node-a'}
NODE_INFO = ('kube_node_info{node="node-a"}', '1+0x25')
# Each threshold is tested on both sides. Critical replaces, not duplicates, warning.
for seconds, warning, critical in [
    (30 * 86400, False, False), (30 * 86400 - 1, True, False),
    (7 * 86400, True, False), (7 * 86400 - 1, False, True),
    (-1, False, True), ('+Inf', False, False), ('NaN', False, False),
]:
    for name, fires in [('PawBridgeKubeletCertificateExpiring', warning),
                        ('PawBridgeKubeletCertificateCritical', critical)]:
        CASES.append(scenario(name, [(TTL, f'{seconds}+0x7')],
                              [('4m', []), ('5m', [NODE_A] if fires else [])]))
for name, seconds in [('PawBridgeKubeletCertificateExpiring', 10 * 86400),
                      ('PawBridgeKubeletCertificateCritical', 86400)]:
    CASES.append(scenario(name, [(TTL, f'{seconds}+0x5 5184000+0x2')],
                          [('5m', [NODE_A]), ('6m', [])]))

for invalid in ['+Inf', 'NaN']:
    CASES.append(scenario('PawBridgeKubeletCertificateMetricsMissing',
                          [NODE_INFO, (TTL, f'{invalid}+0x5 1000000+0x2')],
                          [('4m', []), ('5m', [NODE_A]), ('6m', [])]))
CASES += [
    scenario('PawBridgeKubeletCertificateMetricsMissing', [NODE_INFO],
             [('4m', []), ('5m', [NODE_A])]),
    scenario('PawBridgeKubeletCertificateMetricsMissing', [NODE_INFO, (TTL, '1000000+0x20'),
             ('kube_node_info{node="node-b"}', '1+0x20')], [('5m', [{'node': 'node-b'}])]),
    scenario('PawBridgeKubeletCertificateMetricsMissing', [NODE_INFO, (TTL, '-1+0x20')],
             [('5m', [])]),  # Expired but readable belongs to Critical, not Missing.
    scenario('PawBridgeKubeletCertificateMetricsMissing', [NODE_INFO,
             (TTL.replace('job="kubelet"', 'job="unrelated"'), '1000000+0x20')],
             [('5m', [NODE_A])]),
    scenario('PawBridgeKubeletCertificateMetricsMissing', [NODE_INFO, (TTL, '1000000+0x5 stale')],
             [('10m', []), ('11m', [NODE_A])]),
]

CSR_LABELS = {'certificatesigningrequest': 'fixture-csr', 'signer_name': 'kubernetes.io/kubelet-serving'}
CSR = 'certificatesigningrequest="fixture-csr",signer_name="kubernetes.io/kubelet-serving"'
CREATED = ('kube_certificatesigningrequest_created{' + CSR + '}', '0+0x25')
LENGTH = ('kube_certificatesigningrequest_cert_length{' + CSR + '}', '0+0x25')


def condition(name, values='0+0x25'):
    return ('kube_certificatesigningrequest_condition{' + CSR + ',condition="' + name + '"}', values)


CASES.append(scenario('PawBridgeKubeletCSRPending', [CREATED, LENGTH,
                      condition('approved', '0+0x16 1+0x8'), condition('denied'), condition('failed')],
                      [('10m', []), ('15m', []), ('16m', [CSR_LABELS]), ('17m', [])]))
for excluded in ['approved', 'denied', 'failed']:
    CASES.append(scenario('PawBridgeKubeletCSRPending', [CREATED, LENGTH] + [
        condition(name, '1+0x25' if name == excluded else '0+0x25')
        for name in ['approved', 'denied', 'failed']], [('20m', [])]))
CASES += [
    scenario('PawBridgeKubeletCSRPending', [], [('20m', [])]),
    scenario('PawBridgeKubeletCSRPending', [CREATED, condition('approved'),
             (LENGTH[0], '100+0x25')], [('20m', [])]),
    scenario('PawBridgeKubeletCSRPending', [CREATED, LENGTH], [('20m', [])]),
    scenario('PawBridgeKubeletCSRPending', [
        (key.replace('kubernetes.io/kubelet-serving', 'kubernetes.io/kube-apiserver-client-kubelet'), value)
        for key, value in [CREATED, LENGTH, condition('approved')]], [('20m', [])]),
]

LIST_SUCCESS = 'kube_state_metrics_list_total{resource="*v1.CertificateSigningRequest",result="success"}'
CASES += [
    scenario('PawBridgeCSRCollectionUnhealthy', [], [('4m', []), ('5m', [{}])]),
    scenario('PawBridgeCSRCollectionUnhealthy', [(LIST_SUCCESS, '0+0x5 1+0x15')],
             [('4m', []), ('5m', [{}]), ('6m', [])]),
    # A successful empty list is healthy; absence of CSR object metrics is expected.
    scenario('PawBridgeCSRCollectionUnhealthy', [(LIST_SUCCESS, '1+0x25')], [('20m', [])]),
]
for operation in ['list', 'watch']:
    CASES.append(scenario('PawBridgeCSRCollectionUnhealthy', [(LIST_SUCCESS, '1+0x25'),
        ('kube_state_metrics_' + operation + '_total{resource="*v1.CertificateSigningRequest",result="error"}',
         '0+1x5 5+0x20')], [('4m', []), ('7m', [{}]), ('20m', [])]))


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
