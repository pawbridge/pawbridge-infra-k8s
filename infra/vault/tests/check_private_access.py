"""Validate the private Vault endpoint and unchanged existing Helm resources."""
import argparse
import copy
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
LABELS = {'app.kubernetes.io/name': 'vault', 'app.kubernetes.io/instance': 'vault', 'component': 'server'}
UI = {'enabled': True, 'serviceType': 'NodePort', 'serviceNodePort': 30820,
      'externalPort': 8200, 'targetPort': 8200, 'externalTrafficPolicy': 'Local',
      'publishNotReadyAddresses': True, 'activeVaultPodOnly': False}
PORTS = [{'protocol': 'TCP', 'port': 8200}]
RULES = [
    {'from': [{'namespaceSelector': {'matchLabels': {
        'kubernetes.io/metadata.name': 'vault-secrets-operator-system'}}}], 'ports': PORTS},
    {'from': [{'podSelector': {}}], 'ports': PORTS + [{'protocol': 'TCP', 'port': 8201}]},
    {'from': [{'ipBlock': {'cidr': '192.168.57.1/32'}}], 'ports': PORTS},
]


def read_yaml(path):
    return yaml.safe_load(path.read_text(encoding='utf-8'))


def source_contract(bundle):
    values, policy, kustomization, project, application = bundle
    assert values['ui'] == UI, 'Private UI service contract differs'
    assert values['server']['service'] == {'enabled': True, 'type': 'ClusterIP'}
    assert values['global']['tlsDisable'] is False
    assert values['server']['ingress']['enabled'] is False
    assert policy['metadata']['name'] == 'vault-private-access'
    assert policy['metadata']['namespace'] == 'vault'
    assert policy['metadata']['annotations']['argocd.argoproj.io/sync-wave'] == '-1'
    assert policy['spec'] == {'podSelector': {'matchLabels': LABELS}, 'policyTypes': ['Ingress'], 'ingress': RULES}
    assert kustomization['resources'] == ['namespace.yaml', 'network-policy.yaml']
    assert project['spec']['destinations'] == [{'server': 'https://kubernetes.default.svc', 'namespace': 'vault'}]
    assert {'group': 'networking.k8s.io', 'kind': 'NetworkPolicy'} in project['spec']['namespaceResourceWhitelist']
    assert all(x['kind'] != '*' and x['group'] != '*' for x in project['spec']['namespaceResourceWhitelist'])
    assert application['spec']['sources'][0]['chart'] == 'vault'
    assert application['spec']['sources'][0]['targetRevision'] == '0.34.1'
    assert application['spec']['syncPolicy'] == {'syncOptions': ['Prune=false', 'FailOnSharedResource=true']}


def objects(path):
    result = {}
    for item in yaml.safe_load_all(path.read_text(encoding='utf-8')):
        if item:
            key = (item['kind'], item['metadata'].get('namespace', ''), item['metadata']['name'])
            assert key not in result, 'Duplicate rendered resource'
            result[key] = item
    return result


def render_contract(before, after, security):
    key = ('Service', 'vault', 'vault-ui')
    assert set(after) == set(before) | {key}, 'Only the UI Service may be added to Helm output'
    assert key not in before
    for existing in before:
        assert before[existing] == after[existing], 'Existing resource changed: ' + str(existing)
    service = after[key]['spec']
    assert service['type'] == 'NodePort'
    assert service['externalTrafficPolicy'] == 'Local'
    assert service['publishNotReadyAddresses'] is True
    assert service['selector'] == LABELS, 'The unseal endpoint must not select only active pods'
    assert service['ports'] == [{'name': 'https', 'port': 8200, 'targetPort': 8200, 'nodePort': 30820}]
    assert set(security) == {('Namespace', '', 'vault'), ('NetworkPolicy', 'vault', 'vault-private-access')}
    assert security[('NetworkPolicy', 'vault', 'vault-private-access')]['spec']['ingress'] == RULES


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--before', type=Path)
    parser.add_argument('--after', type=Path)
    parser.add_argument('--security', type=Path)
    args = parser.parse_args()
    paths = ['gitops/security/vault/values.yaml', 'gitops/security/vault/network-policy.yaml',
             'gitops/security/vault/kustomization.yaml', 'gitops/argocd/vault-baseline-pilot/project.yaml',
             'gitops/argocd/vault-baseline-pilot/application.yaml']
    bundle = [read_yaml(ROOT / p) for p in paths]
    source_contract(bundle)
    mutations = {
        'source IP hidden by SNAT': lambda b: b[0]['ui'].update(externalTrafficPolicy='Cluster'),
        'sealed Vault unreachable': lambda b: b[0]['ui'].update(publishNotReadyAddresses=False),
        'unseal excluded by active selector': lambda b: b[0]['ui'].update(activeVaultPodOnly=True),
        'Raft port exposed': lambda b: b[0]['ui'].update(targetPort=8201),
        'internal service exposed': lambda b: b[0]['server']['service'].update(type='NodePort'),
        'TLS disabled': lambda b: b[0]['global'].update(tlsDisable=True),
        'public ingress enabled': lambda b: b[0]['server']['ingress'].update(enabled=True),
        'Internet source allowed': lambda b: b[1]['spec']['ingress'][2]['from'][0]['ipBlock'].update(cidr='0.0.0.0/0'),
        'all namespaces allowed': lambda b: b[1]['spec']['ingress'][0]['from'][0].update(namespaceSelector={}),
        'VSO access removed': lambda b: b[1]['spec']['ingress'].pop(0),
        'policy selects no Vault': lambda b: b[1]['spec']['podSelector']['matchLabels'].update(component='client'),
        'policy applied too late': lambda b: b[1]['metadata']['annotations'].update({'argocd.argoproj.io/sync-wave': '1'}),
        'policy omitted from deployment': lambda b: b[2]['resources'].remove('network-policy.yaml'),
        'policy forbidden by Argo project': lambda b: b[3]['spec']['namespaceResourceWhitelist'].remove({'group': 'networking.k8s.io', 'kind': 'NetworkPolicy'}),
        'automatic prune enabled': lambda b: b[4]['spec']['syncPolicy'].update(automated={'prune': True}),
    }
    for name, mutate in mutations.items():
        changed = copy.deepcopy(bundle)
        mutate(changed)
        try:
            source_contract(changed)
        except AssertionError:
            print('Rejected:', name)
        else:
            raise AssertionError('Unsafe change accepted: ' + name)
    print('Source contract and 15 negative cases passed')
    if any((args.before, args.after, args.security)):
        assert all((args.before, args.after, args.security)), 'Supply all three render paths'
        before, after, security = map(objects, (args.before, args.after, args.security))
        render_contract(before, after, security)
        changed = copy.deepcopy(after)
        stateful = next(k for k in changed if k[0] == 'StatefulSet')
        metadata = changed[stateful]['spec']['template']['metadata']
        metadata['annotations'] = {**(metadata.get('annotations') or {}), 'test-unrequested-restart': 'true'}
        try:
            render_contract(before, changed, security)
        except AssertionError:
            print('Rejected: unrequested StatefulSet change')
        else:
            raise AssertionError('Existing resource mutation accepted')
        print('Pinned Helm renders passed; all existing resources are unchanged')


if __name__ == '__main__':
    main()
