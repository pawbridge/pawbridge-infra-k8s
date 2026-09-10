"""Check actual offline Kustomize renders and reject unsafe secret-sync changes."""
import copy
from pathlib import Path
import sys
import yaml


def validate(bundles):
    expected_kinds = {'ServiceAccount', 'VaultConnection', 'VaultAuth', 'VaultStaticSecret'}
    common = bundles['grafana']
    connection = next(o for o in common if o['kind'] == 'VaultConnection')
    assert connection['metadata']['name'] == 'observability-vault-connection'
    assert connection['spec'] == {
        'address': 'https://vault.vault.svc.cluster.local:8200', 'caCertSecretRef': 'vault-internal-ca',
        'skipTLSVerify': False, 'tlsServerName': 'vault.vault.svc.cluster.local', 'timeout': '10s'}
    assert len(common) == 4 and len(bundles['slack']) == 3
    for target in ('grafana', 'slack'):
        objects = bundles[target]
        assert all(o['metadata']['namespace'] == 'monitoring' and o['kind'] in expected_kinds for o in objects)
        auth_name = 'observability-' + target + '-vault-auth'
        account = next(o for o in objects if o['kind'] == 'ServiceAccount')
        assert account['metadata']['name'] == auth_name and account['automountServiceAccountToken'] is False
        auth = next(o for o in objects if o['kind'] == 'VaultAuth')
        assert auth['metadata']['name'] == auth_name
        assert auth['spec'] == {'vaultConnectionRef': 'observability-vault-connection', 'method': 'kubernetes',
                                'mount': 'kubernetes', 'kubernetes': {'role': 'observability-' + target + '-read',
                                'serviceAccount': auth_name, 'audiences': ['vault'], 'tokenExpirationSeconds': 600}}
        obj = next(o for o in objects if o['kind'] == 'VaultStaticSecret')
        secret = 'monitoring-grafana-admin' if target == 'grafana' else 'monitoring-slack-webhook'
        assert obj['metadata']['name'] == secret
        assert obj['metadata']['annotations']['argocd.argoproj.io/sync-options'] == 'Prune=false,Delete=false'
        spec = obj['spec']
        assert spec['path'] == 'pawbridge/dev/observability/' + target
        assert spec['vaultAuthRef'] == auth_name
        assert spec['mount'] == 'secret' and spec['type'] == 'kv-v2'
        assert spec['refreshAfter'] == '1m' and spec['hmacSecretData'] is True
        assert not spec.get('rolloutRestartTargets')
        assert spec['destination'] == {'create': True, 'overwrite': False, 'name': secret,
            'transformation': {'excludeRaw': True, 'includes': (
                ['^admin-user$', '^admin-password$'] if target == 'grafana' else ['^url$'])}}
    assert len(bundles['argocd']) == 2 and len(bundles['argocd-slack']) == 1
    project = next(o for o in bundles['argocd'] if o['kind'] == 'AppProject')
    assert project['metadata']['name'] == 'pawbridge-observability-secrets'
    assert project['spec']['sourceRepos'] == ['https://github.com/pawbridge/pawbridge-infra-k8s.git']
    assert project['spec']['destinations'] == [{'server': 'https://kubernetes.default.svc', 'namespace': 'monitoring'}]
    assert not project['spec'].get('clusterResourceWhitelist')
    assert {(r['group'], r['kind']) for r in project['spec']['namespaceResourceWhitelist']} == {
        ('', 'ServiceAccount'), *{('secrets.hashicorp.com', k) for k in expected_kinds - {'ServiceAccount'}}}
    for target, bundle in [('grafana', 'argocd'), ('slack', 'argocd-slack')]:
        app = next(o for o in bundles[bundle] if o['kind'] == 'Application')
        assert app['metadata']['name'] == 'observability-' + target + '-vso'
        assert app['spec']['project'] == project['metadata']['name']
        assert app['spec']['source'] == {'repoURL': 'https://github.com/pawbridge/pawbridge-infra-k8s.git',
            'targetRevision': 'dev', 'path': 'gitops/security/observability-vso' + ('/slack' if target == 'slack' else '')}
        assert app['spec']['destination'] == project['spec']['destinations'][0]
        assert app['spec']['syncPolicy'] == {'syncOptions': ['Prune=false', 'FailOnSharedResource=true']}


def main():
    directory = Path(sys.argv[1])
    bundles = {name: [o for o in yaml.safe_load_all((directory / (name + '.yaml')).read_text()) if o]
               for name in ('grafana', 'slack', 'argocd', 'argocd-slack')}
    validate(bundles)
    def one(data, bundle, kind):
        return next(o for o in data[bundle] if o['kind'] == kind)
    mutations = {
        'TLS bypass': lambda d: one(d, 'grafana', 'VaultConnection')['spec'].update(skipTLSVerify=True),
        'cross-secret auth': lambda d: one(d, 'slack', 'VaultStaticSecret')['spec'].update(vaultAuthRef='observability-grafana-vault-auth'),
        'secret overwrite': lambda d: one(d, 'grafana', 'VaultStaticSecret')['spec']['destination'].update(overwrite=True),
        'raw secret leakage': lambda d: one(d, 'slack', 'VaultStaticSecret')['spec']['destination']['transformation'].update(excludeRaw=False),
        'unrequested restart': lambda d: one(d, 'grafana', 'VaultStaticSecret')['spec'].update(rolloutRestartTargets=[{'kind': 'Deployment', 'name': 'user-service'}]),
        'automatic sync': lambda d: one(d, 'argocd', 'Application')['spec']['syncPolicy'].update(automated={'prune': True}),
        'namespace expansion': lambda d: one(d, 'argocd', 'AppProject')['spec']['destinations'][0].update(namespace='*'),
    }
    for name, change in mutations.items():
        data = copy.deepcopy(bundles); change(data)
        try:
            validate(data)
        except (AssertionError, StopIteration):
            print('Rejected:', name)
        else:
            raise AssertionError('Unsafe secret sync accepted: ' + name)
    print('Actual renders: positive and 7 negative contract checks passed')


if __name__ == '__main__':
    main()
