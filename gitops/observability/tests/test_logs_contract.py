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
    role = one('Role', 'pawbridge-observability-pod-logs')
    assert role['metadata']['namespace'] == 'pawbridge'
    assert role['rules'] == [{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']},
                             {'apiGroups': [''], 'resources': ['pods/log'], 'verbs': ['get']}]
    binding = one('RoleBinding', 'pawbridge-observability-pod-logs')
    assert binding['roleRef']['kind'] == 'Role' and binding['roleRef']['name'] == role['metadata']['name']
    assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': 'pawbridge-alloy', 'namespace': 'monitoring'}]
    assert all(o['kind'] in ['Role', 'RoleBinding'] for o in objects if o['metadata'].get('namespace') == 'pawbridge')
    pvc = one('PersistentVolumeClaim', 'pawbridge-loki-data')
    assert pvc['spec']['storageClassName'] == 'local-path' and pvc['spec']['accessModes'] == ['ReadWriteOnce']
    assert pvc['metadata']['annotations']['argocd.argoproj.io/sync-options'] == 'Prune=false,Delete=false'
    configs = [o for o in objects if o['kind'] == 'ConfigMap']
    config = yaml.safe_load(next(o['data']['loki.yaml'] for o in configs if 'loki.yaml' in o['data']))
    assert config['auth_enabled'] is False  # Compensating private network policy checked below.
    assert config['limits_config']['retention_period'] == '72h'
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
    policies = [o for o in objects if o['kind'] == 'NetworkPolicy']
    general = one('NetworkPolicy', 'observability-internal')['spec']
    assert general['podSelector']['matchExpressions'] == [{'key': 'app.kubernetes.io/name', 'operator': 'NotIn',
                                                         'values': ['pawbridge-loki', 'pawbridge-alloy']}]
    logs = one('NetworkPolicy', 'observability-logs')['spec']
    assert logs['ingress'][0]['from'] == [{'podSelector': {'matchExpressions': [
        {'key': 'app.kubernetes.io/name', 'operator': 'In', 'values': ['pawbridge-alloy', 'grafana', 'prometheus']} ]}}]
    assert {p['metadata']['name'] for p in policies} == {
        'observability-internal', 'observability-logs', 'grafana-windows-access'}


def main():
    objects = [o for o in yaml.safe_load_all(pathlib.Path(sys.argv[1]).read_text()) if o]
    validate(objects)
    def obj(items, kind, name):
        return next(o for o in items if o['kind'] == kind and o['metadata']['name'] == name)
    mutations = {
        'broad RBAC': lambda x: obj(x, 'Role', 'pawbridge-observability-pod-logs')['rules'][0].update(resources=['*']),
        'PVC deletion': lambda x: obj(x, 'PersistentVolumeClaim', 'pawbridge-loki-data')['metadata']['annotations'].clear(),
        'unrestricted ingress': lambda x: obj(x, 'NetworkPolicy', 'observability-internal')['spec'].update(podSelector={}),
        'rolling single disk': lambda x: obj(x, 'Deployment', 'pawbridge-loki')['spec'].update(strategy={'type': 'RollingUpdate'}),
        'root collector': lambda x: obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['securityContext'].update(runAsNonRoot=False),
        'experimental mode': lambda x: obj(x, 'Deployment', 'pawbridge-alloy')['spec']['template']['spec']['containers'][0].update(args=['run', '--stability.level=experimental']),
        'stale configuration': lambda x: next(v for v in obj(x, 'Deployment', 'pawbridge-loki')['spec']['template']['spec']['volumes'] if v['name']=='config').update(configMap={'name': 'pawbridge-loki-config'}),
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
    print('Logs render positive and 7 negative checks passed')


if __name__ == '__main__':
    main()
