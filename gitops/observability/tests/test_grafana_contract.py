"""Negative checks against offline renders; no live Grafana or Secret access."""
import copy
import pathlib
import sys

import yaml

from check_render import ChartLoader, validate_grafana


def main():
    objects = []
    for path in sys.argv[1:]:
        objects.extend(o for o in yaml.load_all(pathlib.Path(path).read_text(), Loader=ChartLoader) if o)
    validate_grafana(objects)
    def grafana(items):
        return next(o for o in items if o['kind'] == 'Deployment'
                    and o['metadata'].get('labels', {}).get('app.kubernetes.io/name') == 'grafana')
    def pod(items):
        return grafana(items)['spec']['template']['spec']
    mutations = {
        'unexpected image': lambda items: pod(items)['containers'][0].update(image='grafana/grafana:latest'),
        'API token mounting': lambda items: pod(items).update(automountServiceAccountToken=True),
        'plain admin password': lambda items: next(e for e in pod(items)['containers'][0]['env']
            if e['name'] == 'GF_SECURITY_ADMIN_PASSWORD').update(value='fixture-only-not-a-secret'),
        'missing dashboard': lambda items: items.remove(next(o for o in items if o['kind'] == 'ConfigMap'
            and o['metadata']['name'] == 'pawbridge-observability-dashboards')),
        'PVC rolling overlap': lambda items: grafana(items)['spec'].update(strategy={'type': 'RollingUpdate'}),
    }
    for name, mutate in mutations.items():
        altered = copy.deepcopy(objects)
        mutate(altered)
        try:
            validate_grafana(altered)
        except (AssertionError, StopIteration):
            print('Rejected:', name)
        else:
            raise AssertionError('Unsafe render accepted: ' + name)
    print('Grafana offline positive and 5 negative checks passed')


if __name__ == '__main__':
    main()
