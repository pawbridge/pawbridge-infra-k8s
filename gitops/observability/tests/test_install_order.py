"""Check rendered install ordering, not a live Argo installation.

Usage: python3 test_install_order.py BASE_RENDER RESOURCES BASE_APP SLACK_APP
"""
import copy
import pathlib
import sys
import unittest

import yaml

sys.dont_write_bytecode = True
from check_render import ChartLoader


def read(path):
    return [o for o in yaml.load_all(pathlib.Path(path).read_text(), Loader=ChartLoader) if o]


def one(objects, kind, suffix):
    matches = [o for o in objects if o['kind'] == kind and o['metadata']['name'].endswith(suffix)]
    assert len(matches) == 1, (kind, suffix, len(matches))
    return matches[0]


def wave(obj):
    return int((obj['metadata'].get('annotations') or {}).get('argocd.argoproj.io/sync-wave', '0'))


def validate(objects):
    create = one(objects, 'Job', '-admission-create')
    patch = one(objects, 'Job', '-admission-patch')
    operator = one(objects, 'Deployment', '-operator')
    assert create['metadata']['annotations']['helm.sh/hook'] == 'pre-install,pre-upgrade'
    assert 'argocd.argoproj.io/hook' not in create['metadata']['annotations']
    a = patch['metadata']['annotations']
    assert a.get('argocd.argoproj.io/hook') == 'Sync', 'CA patch must precede rule admission'
    assert set(a.get('argocd.argoproj.io/hook-delete-policy', '').split(',')) == {
        'BeforeHookCreation', 'HookSucceeded'}, 'Patch must run on the next full sync'
    assert wave(operator) < wave(patch)
    assert '--patch-failure-policy=Fail' in patch['spec']['template']['spec']['containers'][0]['args']
    for kind in ['MutatingWebhookConfiguration', 'ValidatingWebhookConfiguration']:
        webhook = one(objects, kind, '-admission')
        assert wave(webhook) < wave(patch)
        assert all(w['failurePolicy'] == 'Fail' for w in webhook['webhooks'])
    guarded = [o for o in objects if o['kind'] in ['PrometheusRule', 'AlertmanagerConfig']]
    assert guarded, 'PawBridge rules must be included'
    for obj in guarded:
        assert 'argocd.argoproj.io/hook' not in obj['metadata'].get('annotations', {}), 'Rules are not disposable hooks'
        assert wave(patch) < wave(obj), 'CA patch must finish before every guarded resource'
    # Keep chart 89.2.4 passive RBAC shared across PreSync/PostSync.
    # Argo cleans successful hooks up at operation completion, not each wave.
    sa = patch['spec']['template']['spec']['serviceAccountName']
    for kind in ['ServiceAccount', 'Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding']:
        obj = one(objects, kind, '-admission')
        assert obj['metadata']['name'] == sa
        a = obj['metadata']['annotations']
        assert set(a['helm.sh/hook'].split(',')) == {
            'pre-install', 'pre-upgrade', 'post-install', 'post-upgrade'}
        assert 'argocd.argoproj.io/hook' not in a
    for kind in ['RoleBinding', 'ClusterRoleBinding']:
        binding = one(objects, kind, '-admission')
        assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': sa, 'namespace': 'monitoring'}]
        assert binding['roleRef']['name'] == sa


class InstallOrderTest(unittest.TestCase):
    def setUp(self):
        self.objects = copy.deepcopy(OBJECTS)

    def test_rendered_bootstrap_contract(self):
        validate(self.objects)

    def test_previous_post_sync_patch_is_rejected(self):
        one(self.objects, 'Job', '-admission-patch')['metadata']['annotations'].pop('argocd.argoproj.io/hook', None)
        with self.assertRaisesRegex(AssertionError, 'CA patch must precede'):
            validate(self.objects)

    def test_rule_before_ca_patch_is_rejected(self):
        one(self.objects, 'PrometheusRule', 'pawbridge-baseline')['metadata']['annotations'] = {}
        with self.assertRaisesRegex(AssertionError, 'CA patch must finish'):
            validate(self.objects)

    def test_ignoring_certificate_failure_is_rejected(self):
        one(self.objects, 'ValidatingWebhookConfiguration', '-admission')['webhooks'][0]['failurePolicy'] = 'Ignore'
        with self.assertRaises(AssertionError):
            validate(self.objects)

    def test_patch_account_without_pre_sync_rbac_is_rejected(self):
        one(self.objects, 'RoleBinding', '-admission')['metadata']['annotations']['helm.sh/hook'] = 'post-install'
        with self.assertRaises(AssertionError):
            validate(self.objects)

    def test_patch_repeatability_is_required(self):
        one(self.objects, 'Job', '-admission-patch')['metadata']['annotations']['argocd.argoproj.io/hook-delete-policy'] = 'HookFailed'
        with self.assertRaisesRegex(AssertionError, 'next full sync'):
            validate(self.objects)

    def test_slack_overlay_changes_only_opt_in_values(self):
        expected = copy.deepcopy(BASE_APP)
        app = one(expected, 'Application', 'observability-baseline')
        files = app['spec']['sources'][0]['helm']['valueFiles']
        self.assertEqual(files, ['$values/gitops/observability/values.yaml'])
        files.append('$values/gitops/observability/slack-values.yaml')
        self.assertEqual(expected, SLACK_APP)
        self.assertNotIn('automated', app['spec']['syncPolicy'])
        self.assertIn('Prune=false', app['spec']['syncPolicy']['syncOptions'])


if __name__ == '__main__':
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    OBJECTS = read(sys.argv[1]) + read(sys.argv[2])
    BASE_APP, SLACK_APP = read(sys.argv[3]), read(sys.argv[4])
    unittest.main(argv=[sys.argv[0]], verbosity=2)
