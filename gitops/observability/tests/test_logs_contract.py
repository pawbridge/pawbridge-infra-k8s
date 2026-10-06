"""Check the actual Kustomize result and reject dangerous log pipeline mutations."""
import copy
import pathlib
import re
import sys

import yaml


def validate(objects):
    def one(kind, name):
        return next(o for o in objects if o['kind'] == kind and o['metadata']['name'] == name)
    loki = one('Deployment', 'pawbridge-loki')
    alloy = one('Deployment', 'pawbridge-alloy')
    for deployment in [loki, alloy]:
        assert deployment['metadata']['namespace'] == 'monitoring'
        assert deployment['spec']['replicas'] == 1 and deployment['spec']['strategy'] == {'type': 'Recreate'}
        pod = deployment['spec']['template']['spec']
        assert pod['nodeSelector']['kubernetes.io/hostname'] == 'pawbridge-k136-cp1'
        assert not pod.get('hostNetwork') and not pod.get('initContainers')
        assert pod['securityContext']['runAsNonRoot'] is True
        assert not any('hostPath' in v for v in pod['volumes'])
        container = pod['containers'][0]
        assert len(pod['containers']) == 1 and '@sha256:' in container['image'] and ':latest' not in container['image']
        assert container['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                                'capabilities': {'drop': ['ALL']}}
        ref = next(v['configMap']['name'] for v in pod['volumes'] if v['name'] == 'config')
        base = deployment['metadata']['name'] + '-config'
        assert re.fullmatch(re.escape(base) + r'-[a-z0-9]{10}', ref), 'Config must change pod template on edit'
        one('ConfigMap', ref)
    lp, ap = [o['spec']['template']['spec'] for o in [loki, alloy]]
    assert lp['automountServiceAccountToken'] is False
    assert ap['automountServiceAccountToken'] is True and ap['serviceAccountName'] == 'pawbridge-alloy'
    assert lp['containers'][0]['resources']['limits']['memory'] == '768Mi'
    assert ap['containers'][0]['resources']['limits']['memory'] == '256Mi'
    assert '--stability.level=generally-available' in ap['containers'][0]['args']
    assert ap['securityContext']['fsGroup'] == 473
    alloy_data = next(v for v in ap['volumes'] if v['name'] == 'data')
    assert alloy_data == {'name': 'data', 'persistentVolumeClaim': {'claimName': 'pawbridge-alloy-positions'}}
    assert {'name': 'data', 'mountPath': '/var/lib/alloy'} in ap['containers'][0]['volumeMounts']
    assert '--storage.path=/var/lib/alloy' in ap['containers'][0]['args']
    alloy_claim = one('PersistentVolumeClaim', 'pawbridge-alloy-positions')
    assert alloy_claim['metadata']['namespace'] == 'monitoring'
    assert alloy_claim['spec']['storageClassName'] == 'local-path'
    assert alloy_claim['spec']['accessModes'] == ['ReadWriteOnce']
    assert alloy_claim['spec']['resources']['requests']['storage'] == '64Mi'
    assert alloy_claim['metadata']['annotations']['argocd.argoproj.io/sync-options'] == 'Prune=false,Delete=false'
    role = one('Role', 'pawbridge-observability-pod-logs')
    assert role['metadata']['namespace'] == 'pawbridge'
    assert role['rules'] == [{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']},
                             {'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']}]
    binding = one('RoleBinding', 'pawbridge-observability-pod-logs')
    assert binding['roleRef']['kind'] == 'Role' and binding['roleRef']['name'] == role['metadata']['name']
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': 'pawbridge-alloy', 'namespace': 'monitoring'}]
    # The baseline also owns the existing Gateway metrics ingress policy.
    assert all(o['kind'] in ['Role', 'RoleBinding'] or
               (o['kind'] == 'NetworkPolicy' and o['metadata']['name'] == 'prometheus-gateway-metrics')
               for o in objects if o['metadata'].get('namespace') == 'pawbridge')
    pvc = one('PersistentVolumeClaim', 'pawbridge-loki-data')
    assert pvc['spec']['storageClassName'] == 'local-path' and pvc['spec']['accessModes'] == ['ReadWriteOnce']
    assert pvc['metadata']['annotations']['argocd.argoproj.io/sync-options'] == 'Prune=false,Delete=false'
    configs = [o for o in objects if o['kind'] == 'ConfigMap']
    config = yaml.safe_load(next(o['data']['loki.yaml'] for o in configs if 'loki.yaml' in o['data']))
    assert config['auth_enabled'] is False  # Compensating private network policy checked below.
    assert config['limits_config']['retention_period'] == '72h'
    assert config['limits_config']['reject_old_samples'] is True
    assert config['limits_config']['reject_old_samples_max_age'] == '72h'
    assert config['limits_config']['ingestion_rate_mb'] == 0.01
    assert config['compactor']['retention_enabled'] is True
    assert config['schema_config']['configs'][0]['index']['period'] == '24h'
    assert config['ingester']['wal']['enabled'] is True
    for path in [config['common']['path_prefix'], config['ingester']['wal']['dir'],
                 config['compactor']['working_directory'], config['storage_config']['tsdb_shipper']['active_index_directory']]:
        assert path.startswith('/var/lib/loki')
    text = next(o['data']['config.alloy'] for o in configs if 'config.alloy' in o['data'])
    assert 'names = ["pawbridge"]' in text and 'own_namespace = false' in text
    assert 'queue_config {' not in text and not re.search(r'\bwal\s*\{', text)
    assert 'http://pawbridge-loki.monitoring.svc.cluster.local:3100/loki/api/v1/push' in text
    age_stages = [stage for stage in re.findall(r'stage\.drop\s*\{([^{}]*)\}', text)
                  if re.search(r'\bolder_than\s*=', stage)]
    assert len(age_stages) == 1, 'Expired rereads must be excluded before delivery'
    assert re.search(r'older_than\s*=\s*"72h"', age_stages[0]), 'Keep the full Loki acceptance window'
    assert re.search(r'drop_counter_reason\s*=\s*"outside_retention"', age_stages[0])
    monitor = one('ServiceMonitor', 'pawbridge-logs')
    keep = monitor['spec']['endpoints'][0]['metricRelabelings'][0]
    assert keep['action'] == 'keep' and re.fullmatch(keep['regex'], 'loki_process_dropped_lines_total')
    policies = [o for o in objects if o['kind'] == 'NetworkPolicy']
    general = one('NetworkPolicy', 'observability-internal')['spec']
    assert general['podSelector']['matchExpressions'] == [{'key': 'app.kubernetes.io/name', 'operator': 'NotIn',
                                                         'values': ['pawbridge-loki', 'pawbridge-alloy']}]
    logs = one('NetworkPolicy', 'observability-logs')['spec']
    assert logs['ingress'][0]['from'] == [{'podSelector': {'matchExpressions': [
        {'key': 'app.kubernetes.io/name', 'operator': 'In', 'values': ['pawbridge-alloy', 'grafana', 'prometheus']} ]}}]
    assert {p['metadata']['name'] for p in policies} == {
        'observability-internal', 'observability-logs', 'grafana-windows-access',
        'prometheus-gateway-metrics'}


def main():
    objects = [o for o in yaml.safe_load_all(pathlib.Path(sys.argv[1]).read_text()) if o]
    validate(objects)
    def obj(items, kind, name):
        return next(o for o in items if o['kind'] == kind and o['metadata']['name'] == name)
    def change_alloy(items, old, new):
        config = next(o for o in items if o['kind'] == 'ConfigMap' and 'config.alloy' in o.get('data', {}))
        assert old in config['data']['config.alloy']
        config['data']['config.alloy'] = config['data']['config.alloy'].replace(old, new)
    mutations = {
        'lost read positions': lambda x: next(v for v in obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['volumes'] if v['name'] == 'data').update(persistentVolumeClaim={}, emptyDir={'sizeLimit': '64Mi'}),
        'wrong positions claim': lambda x: next(v for v in obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['volumes'] if v['name'] == 'data')['persistentVolumeClaim'].update(claimName='pawbridge-loki-data'),
        'positions deletion': lambda x: obj(x, 'PersistentVolumeClaim', 'pawbridge-alloy-positions')['metadata']['annotations'].clear(),
        'unwritable positions': lambda x: obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['securityContext'].update(fsGroup=10001),
        'broad RBAC': lambda x: obj(x, 'Role', 'pawbridge-observability-pod-logs')['rules'][0].update(resources=['*']),
        'PVC deletion': lambda x: obj(x, 'PersistentVolumeClaim', 'pawbridge-loki-data')['metadata']['annotations'].clear(),
        'unrestricted ingress': lambda x: obj(x, 'NetworkPolicy', 'observability-internal')['spec'].update(podSelector={}),
        'rolling single disk': lambda x: obj(x, 'Deployment', 'pawbridge-loki')['spec'].update(strategy={'type': 'RollingUpdate'}),
        'root collector': lambda x: obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['securityContext'].update(runAsNonRoot=False),
        'experimental mode': lambda x: obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['containers'][0].update(args=['run', '--stability.level=experimental']),
        'stale configuration': lambda x: next(v for v in obj(x, 'Deployment', 'pawbridge-loki')['spec']['template']['spec']['volumes'] if v['name']=='config').update(configMap={'name': 'pawbridge-loki-config'}),
        'missing expiry filter': lambda x: change_alloy(x, 'older_than = "72h"', 'expression = "never-matches-fixture"'),
        'premature expiry': lambda x: change_alloy(x, 'older_than = "72h"', 'older_than = "71h"'),
        'late expiry': lambda x: change_alloy(x, 'older_than = "72h"', 'older_than = "73h"'),
        'ambiguous expiry metric': lambda x: change_alloy(x, 'drop_counter_reason = "outside_retention"', 'drop_counter_reason = "sensitive_line"'),
        'unobserved exclusions': lambda x: obj(x, 'ServiceMonitor', 'pawbridge-logs')['spec']['endpoints'][0]['metricRelabelings'][0].update(regex='loki_write_sent_entries_total'),
    }
    for name, mutate in mutations.items():
        changed = copy.deepcopy(objects)
        mutate(changed)
        try:
            validate(changed)
        except (AssertionError, StopIteration, KeyError):
            print('Rejected:', name)
        else:
            raise AssertionError('Unsafe logs configuration accepted: ' + name)
    print(f'Logs render positive and {len(mutations)} negative checks passed')


if __name__ == '__main__':
    main()
