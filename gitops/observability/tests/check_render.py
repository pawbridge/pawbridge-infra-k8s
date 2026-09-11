"""Validate offline Helm output, never live resources or runtime Secret values.

python3 check_render.py /path/to/render.yaml --resources /path/to/kustomize.yaml [--slack]
"""
import argparse
import base64
import collections
import configparser
import json
import pathlib
import re

import yaml


class ChartLoader(yaml.SafeLoader):
    pass


# PyYAML 5.x recognizes the CRD enum scalar '=' but lacks its constructor.
ChartLoader.add_constructor('tag:yaml.org,2002:value', ChartLoader.construct_scalar)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def validate_grafana_access(objects):
    services = [o for o in objects if o['kind'] == 'Service']
    grafana = [o for o in services if o['metadata']['name'] == 'pawbridge-observability-grafana']
    assert len(grafana) == 1
    service = grafana[0]['spec']
    assert service['type'] == 'NodePort'
    assert service['externalTrafficPolicy'] == 'Local', 'Preserve client IP for /32 ingress'
    assert service['ports'] == [{'name': 'http-web', 'port': 80, 'protocol': 'TCP',
                                 'targetPort': 'grafana', 'nodePort': 30300}]
    for obj in services:
        assert not obj['spec'].get('externalIPs')
        if obj is not grafana[0]:
            assert obj['spec'].get('type', 'ClusterIP') == 'ClusterIP'
    policies = [o for o in objects if o['kind'] == 'NetworkPolicy'
                and o['metadata']['name'] == 'grafana-windows-access']
    assert len(policies) == 1 and policies[0]['metadata']['namespace'] == 'monitoring'
    assert policies[0]['spec'] == {
        'podSelector': {'matchLabels': {'app.kubernetes.io/name': 'grafana',
                                       'app.kubernetes.io/instance': 'pawbridge-observability'}},
        'policyTypes': ['Ingress'],
        'ingress': [{'from': [{'ipBlock': {'cidr': '192.168.57.1/32'}}],
                     'ports': [{'protocol': 'TCP', 'port': 3000}]}],
    }


def validate_grafana(objects):
    matches = [o for o in objects if o['kind'] == 'Deployment'
               and o['metadata'].get('labels', {}).get('app.kubernetes.io/name') == 'grafana']
    assert len(matches) == 1, 'Exactly one Grafana deployment is required'
    deployment = matches[0]
    assert deployment['spec']['replicas'] == 1
    assert deployment['spec']['strategy'] == {'type': 'Recreate'}
    pod = deployment['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert pod['securityContext']['runAsNonRoot'] is True
    assert not pod.get('initContainers') and len(pod['containers']) == 1
    container = pod['containers'][0]
    assert container['image'] == ('docker.io/grafana/grafana:12.4.10@sha256:'
                                 'c132a683b2430fff9115a29b2a79c8ab97540cdcc90846e3c81878c778ca3596')
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    assert container['securityContext']['capabilities']['drop'] == ['ALL']
    env = {item['name']: item for item in container['env']}
    for name, key in [('GF_SECURITY_ADMIN_USER', 'admin-user'), ('GF_SECURITY_ADMIN_PASSWORD', 'admin-password')]:
        assert 'value' not in env[name]
        assert env[name]['valueFrom']['secretKeyRef'] == {'name': 'monitoring-grafana-admin', 'key': key}
    assert not any(o['kind'] == 'Secret' and o['metadata']['name'] == 'monitoring-grafana-admin' for o in objects)
    for o in objects:
        if o['kind'] in ['RoleBinding', 'ClusterRoleBinding']:
            assert not any(s.get('kind') == 'ServiceAccount' and s['name'] == pod['serviceAccountName']
                           for s in o.get('subjects', [])), 'Grafana must not receive Kubernetes API permissions'
    claims = [o for o in objects if o['kind'] == 'PersistentVolumeClaim'
              and o['metadata']['name'] == deployment['metadata']['name']]
    assert len(claims) == 1 and claims[0]['spec']['storageClassName'] == 'local-path'
    assert claims[0]['spec']['resources']['requests']['storage'] == '2Gi'
    config = next(o['data'] for o in objects if o['kind'] == 'ConfigMap'
                  and o['metadata']['name'] == deployment['metadata']['name'])
    ini = configparser.ConfigParser(interpolation=None)
    ini.read_string(config['grafana.ini'])
    for section, option in [('auth.anonymous', 'enabled'), ('users', 'allow_sign_up'),
                            ('analytics', 'reporting_enabled'), ('unified_alerting', 'enabled')]:
        assert not ini.getboolean(section, option)
    sources = yaml.safe_load(config['datasources.yaml'])['datasources']
    assert len(sources) == 2 and sources[0]['uid'] == 'pawbridge-prometheus'
    assert sources[0]['url'] == 'http://pawbridge-observability-prometheus.monitoring.svc.cluster.local:9090'
    assert sources[0]['access'] == 'proxy' and sources[0]['editable'] is False
    assert sources[1] == {'name': 'PawBridge Loki', 'uid': 'pawbridge-loki', 'type': 'loki',
                          'access': 'proxy', 'url': 'http://pawbridge-loki.monitoring.svc.cluster.local:3100',
                          'isDefault': False, 'editable': False, 'jsonData': {'maxLines': 1000}}
    providers = yaml.safe_load(config['dashboardproviders.yaml'])['providers']
    assert len(providers) == 1 and providers[0]['options']['path'] == '/var/lib/grafana/dashboards/pawbridge'
    dashboards = next(o for o in objects if o['kind'] == 'ConfigMap'
                      and o['metadata']['name'] == 'pawbridge-observability-dashboards')
    assert dashboards['metadata']['namespace'] == 'monitoring'
    expected = {'pawbridge-overview.json', 'pawbridge-service.json', 'pawbridge-pods.json'}
    assert set(dashboards['data']) == expected
    for name in expected:
        dashboard = json.loads(dashboards['data'][name])
        assert dashboard == json.loads((ROOT / 'dashboards' / name).read_text())
        assert dashboard['timezone'] == 'Asia/Seoul'
        assert dashboard['templating']['list'], 'Dashboard filters must be provisioned'
        assert all(panel['datasource']['uid'] == 'pawbridge-prometheus'
                   for panel in dashboard['panels'] if panel.get('targets'))


def validate(objects, slack):
    validate_grafana(objects)
    validate_grafana_access(objects)
    def one(kind):
        found = [o for o in objects if o['kind'] == kind]
        assert len(found) == 1, (kind, len(found))
        return found[0]

    app = yaml.safe_load((ROOT.parent / 'argocd/observability/application.yaml').read_text())
    project = yaml.safe_load((ROOT.parent / 'argocd/observability/project.yaml').read_text())['spec']
    assert 'automated' not in app['spec']['syncPolicy']
    assert app['spec']['sources'][0]['targetRevision'] == '89.2.4'
    assert all('slack-values' not in p for p in app['spec']['sources'][0]['helm']['valueFiles'])
    allowed_ns = {d['namespace'] for d in project['destinations']}
    for obj in objects:
        group = obj['apiVersion'].split('/')[0] if '/' in obj['apiVersion'] else ''
        namespace = obj['metadata'].get('namespace')
        whitelist = project['namespaceResourceWhitelist' if namespace else 'clusterResourceWhitelist']
        assert {'group': group, 'kind': obj['kind']} in whitelist, (group, obj['kind'])
        if namespace:
            assert namespace in allowed_ns
        assert obj['kind'] not in ['Ingress', 'HTTPRoute']
    for kind in ['Prometheus', 'Alertmanager']:
        spec = one(kind)['spec']
        assert spec['replicas'] == 1
        assert spec['nodeSelector']['kubernetes.io/hostname'] == 'pawbridge-k136-cp1'
        assert '@sha256:' in spec['image']
        assert spec['storage']['volumeClaimTemplate']['spec']['storageClassName'] == 'local-path'
        assert spec['securityContext']['runAsNonRoot']
        assert spec['resources']['requests'] and spec['resources']['limits']
    prom = one('Prometheus')['spec']
    rules = one('PrometheusRule')
    assert rules['metadata']['labels'] == prom['ruleSelector']['matchLabels']
    assert rules['metadata']['namespace'] == 'monitoring'
    assert one('Namespace')['metadata']['name'] == 'monitoring'
    policies = [o for o in objects if o['kind'] == 'NetworkPolicy']
    assert len(policies) == 3 and all(o['spec']['policyTypes'] == ['Ingress'] for o in policies)
    for rule in rules['spec']['groups'][0]['rules']:
        assert rule['alert'].startswith('PawBridge') and rule['for']
        assert rule['labels']['severity'] in ['warning', 'critical']
        assert all(re.search('[가-힣]', rule['annotations'][key]) for key in ['summary', 'description'])
    assert prom['version'] == 'v3.13.3'
    assert prom['retention'] == '7d' and prom['retentionSize'] == '4GB'
    assert prom['persistentVolumeClaimRetentionPolicy'] == {'whenDeleted': 'Retain', 'whenScaled': 'Retain'}
    assert not prom.get('enableAdminAPI') and not prom.get('enableRemoteWriteReceiver')
    assert prom['enforcedSampleLimit'] == 15000 and prom['enforcedTargetLimit'] == 10
    assert prom['serviceMonitorSelector'] == {'matchLabels': {'release': 'pawbridge-observability'}}
    assert prom['scrapeConfigSelector']['matchLabels']['pawbridge-monitoring'] == 'disabled'
    alertmanager = one('Alertmanager')['spec']
    assert alertmanager['alertmanagerConfigSelector']['matchLabels']['pawbridge-monitoring'] == 'disabled'
    assert alertmanager.get('secrets', []) == (['monitoring-slack-webhook'] if slack else [])
    ksm_name = 'pawbridge-observability-kube-state-metrics'
    ksm = next(o for o in objects if o['kind'] == 'Deployment' and o['metadata']['name'] == ksm_name)
    args = ksm['spec']['template']['spec']['containers'][0]['args']
    collectors = next(a.split('=', 1)[1].split(',') for a in args if a.startswith('--resources='))
    assert set(collectors) == {'nodes', 'pods', 'deployments', 'statefulsets', 'daemonsets',
                               'persistentvolumeclaims', 'certificatesigningrequests'}
    role = next(o for o in objects if o['kind'] == 'ClusterRole' and o['metadata']['name'] == ksm_name)
    csr_permissions = [r for r in role['rules'] if 'certificates.k8s.io' in r['apiGroups']]
    assert csr_permissions == [{'apiGroups': ['certificates.k8s.io'],
                               'resources': ['certificatesigningrequests'], 'verbs': ['list', 'watch']}]
    assert all(set(r['verbs']) <= {'get', 'list', 'watch'} for r in role['rules']), 'Read-only collector'
    monitor = next(o for o in objects if o['kind'] == 'ServiceMonitor' and o['metadata']['name'] == ksm_name)
    telemetry = next(e for e in monitor['spec']['endpoints'] if e['port'] == 'metrics')
    assert telemetry['interval'] == '30s'
    assert telemetry['metricRelabelings'] == [{
        'sourceLabels': ['__name__', 'resource'], 'action': 'keep',
        'regex': r'kube_state_metrics_(list|watch)_total;\*v1.CertificateSigningRequest'}]
    for obj in objects:
        if obj['kind'] in ['Deployment', 'Job', 'DaemonSet']:
            pod = obj['spec']['template']['spec']
            if obj['kind'] != 'DaemonSet':
                assert pod['nodeSelector']['kubernetes.io/hostname'] == 'pawbridge-k136-cp1'
            for container in pod['containers']:
                assert container['resources'].get('limits', {}).get('memory')
                assert container['resources'].get('requests', {}).get('memory')
                assert not re.search(r':latest(?:$|@)', container['image'])
        if obj['kind'] == 'ServiceMonitor' and obj['metadata']['name'].endswith('-kubelet'):
            endpoint = next(e for e in obj['spec']['endpoints'] if e.get('path', '/metrics') == '/metrics')
            keeps = [r['regex'] for r in endpoint['metricRelabelings'] if r['action'] == 'keep']
            assert keeps and all(re.fullmatch(r, 'kubelet_certificate_manager_server_ttl_seconds')
                                 for r in keeps), 'Serving certificate TTL must survive collection filters'
            # Operator discovery supplies the node label; it is not a chart relabeling.
            # The existing /metrics target was verified to carry each VM's node name.
            assert {'action': 'replace', 'sourceLabels': ['__metrics_path__'],
                    'targetLabel': 'metrics_path'} in endpoint['relabelings']
            for endpoint in obj['spec']['endpoints']:
                assert endpoint['scheme'] == 'https'
                assert endpoint['tlsConfig']['insecureSkipVerify'] is False
                assert endpoint['tlsConfig'].get('caFile')
                assert endpoint['interval'] == '30s'
    configs = [o for o in objects if o['kind'] == 'Secret' and 'alertmanager.yaml' in o.get('data', {})]
    assert len(configs) == 1
    # Offline chart-generated config only. Never pass kubectl Secret output here.
    content = base64.b64decode(configs[0]['data']['alertmanager.yaml']).decode()
    config = yaml.safe_load(content)
    assert 'hooks.slack.com' not in content
    assert config['route']['receiver'] == 'disabled'
    assert config['route']['repeat_interval'] == '12h'
    receivers = {r['name']: r for r in config['receivers']}
    if slack:
        receiver = receivers['slack-ko']['slack_configs'][0]
        assert receiver['api_url_file'] == '/etc/alertmanager/secrets/monitoring-slack-webhook/url'
        assert 'api_url' not in receiver and receiver['send_resolved'] is True
        assert '장애' in receiver['title'] and '복구' in receiver['title']
        assert 'Asia/Seoul' in receiver['text']
        assert '.Labels.certificatesigningrequest' in receiver['text']
        assert config['route']['routes'] == [{'receiver': 'slack-ko', 'matchers': ['alertname=~"PawBridge.*"']}]
    else:
        assert set(receivers) == {'disabled'} and config['route']['routes'] == []
    print('Offline render contract passed:', dict(collections.Counter(o['kind'] for o in objects)))
    print('Slack mode:', 'opt-in file reference (no credentials)' if slack else 'disabled')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('render')
    parser.add_argument('--resources', required=True, help='Actual Kustomize resource build output')
    parser.add_argument('--slack', action='store_true')
    args = parser.parse_args()
    with pathlib.Path(args.render).open() as source:
        objects = [o for o in yaml.load_all(source, Loader=ChartLoader) if o]
    with pathlib.Path(args.resources).open() as source:
        objects += [o for o in yaml.safe_load_all(source) if o]
    validate(objects, args.slack)
