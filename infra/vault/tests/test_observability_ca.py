"""Run the CA helper against a synthetic kubectl, without cluster access."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "sync-vault-internal-ca.sh"
STUB = r"""#!/usr/bin/env python3
import json, os, pathlib, sys
args = [a for a in sys.argv[1:] if not a.startswith('--context=') and not a.startswith('--request-timeout=')]
with open(os.environ['CALLS'], 'a') as out:
    out.write(json.dumps(sys.argv[1:]) + '\n')
case = os.environ.get('CASE', '')
if args == ['config', 'current-context']:
    print('other-context' if case == 'wrong-context' else 'pawbridge-vbox-k136')
elif args[:2] == ['get', 'namespace']:
    sys.exit(1 if case == 'missing-namespace' and args[2] == 'monitoring' else 0)
elif args[:2] == ['-n', 'vault']:
    print(os.environ['CA'])
elif 'get' in args and 'secret' in args:
    if case == 'denied':
        sys.stderr.write('permission denied'); sys.exit(1)
    if case == 'matching' or pathlib.Path(os.environ['APPLIED']).exists():
        print(os.environ['CA'])
elif 'create' in args:
    sys.stdin.read()
    print('apiVersion: v1\nkind: Secret')
elif 'apply' in args:
    sys.stdin.read()
    pathlib.Path(os.environ['APPLIED']).touch()
else:
    sys.exit(2)
"""


class CaSyncTests(unittest.TestCase):
    def run_case(self, case, *arguments):
        with tempfile.TemporaryDirectory(prefix="pawbridge-ca-test-") as directory:
            root = Path(directory)
            stub = root / "kubectl"
            stub.write_text(STUB); stub.chmod(0o700)
            encoded = base64.b64encode(b"synthetic public certificate fixture").decode()
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'], CASE=case,
                       CA=encoded, CALLS=str(root / "calls"), APPLIED=str(root / "applied"))
            result = subprocess.run(['bash', str(SCRIPT), *arguments], env=env,
                                    capture_output=True, text=True, timeout=15)
            calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()] if (root / 'calls').exists() else []
            self.assertNotIn(encoded, result.stdout + result.stderr)
            return result, calls

    def test_monitoring_apply_only_touches_monitoring_and_pins_context(self):
        result, calls = self.run_case('missing', 'apply', 'monitoring')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(any('apply' in c for c in calls))
        self.assertTrue(all('--context=pawbridge-vbox-k136' in c for c in calls))
        for args in calls:
            if '-n' in args:
                self.assertIn(args[args.index('-n') + 1], ('vault', 'monitoring'))

    def test_existing_default_namespace_scope_is_preserved(self):
        result, calls = self.run_case('matching', 'check')
        self.assertEqual(0, result.returncode, result.stderr)
        targets = {c[c.index('-n') + 1] for c in calls if '-n' in c}
        self.assertEqual({'vault', 'databases', 'pawbridge', 'kafka'}, targets)

    def test_matching_ca_is_not_written(self):
        result, calls = self.run_case('matching', 'apply', 'monitoring')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(any('apply' in c or 'create' in c for c in calls))

    def test_check_never_repairs_a_missing_ca(self):
        result, calls = self.run_case('missing', 'check', 'monitoring')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(any('apply' in c or 'create' in c for c in calls))

    def test_denied_or_wrong_context_or_missing_namespace_stops_before_any_write(self):
        for case in ('denied', 'wrong-context', 'missing-namespace'):
            with self.subTest(case=case):
                result, calls = self.run_case(case, 'apply', 'monitoring')
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(any('apply' in c or 'create' in c for c in calls))

    def test_unknown_scope_is_rejected_before_cluster_access(self):
        result, calls = self.run_case('', 'apply', 'all')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], calls)


if __name__ == '__main__':
    unittest.main()
