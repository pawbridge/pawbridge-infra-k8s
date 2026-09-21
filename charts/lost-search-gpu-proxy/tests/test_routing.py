"""Exercise the pinned, already-cached NGINX image; never download or contact production."""
import concurrent.futures
import http.server
import json
from pathlib import Path
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import yaml

CHART = Path(__file__).resolve().parents[1]


class UnixHttpServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        values = yaml.safe_load((CHART / 'values.yaml').read_text())
        cls.image = values['image']['repository'] + '@' + values['image']['digest']
        subprocess.run(['docker', 'image', 'inspect', cls.image], check=True, capture_output=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix='pawbridge-proxy-contract-')
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        root.chmod(0o755)
        cls.release = threading.Event()
        cls.condition = threading.Condition()
        cls.started = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.do_GET()

            def do_GET(self):
                status = 200 if self.headers.get('X-Internal-Api-Key') == 'test-key' else 401
                if status == 200 and '/9999/similar' in self.path:
                    with cls.condition:
                        cls.started += 1
                        cls.condition.notify_all()
                    cls.release.wait(5)
                body = json.dumps({'path': self.path, 'key': self.headers.get('X-Internal-Api-Key'),
                    'authorization': self.headers.get('Authorization'), 'cookie': self.headers.get('Cookie'),
                    'user': self.headers.get('X-User-Id')}).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.server = UnixHttpServer(str(root / 'search.sock'), Handler)
        (root / 'search.sock').chmod(0o666)
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        def stop_upstream():
            cls.release.set()
            cls.server.shutdown()
            cls.server.server_close()
            thread.join(timeout=2)
        cls.addClassCleanup(stop_upstream)
        cls.container = 'pawbridge-proxy-contract-' + uuid.uuid4().hex[:10]
        def remove_container():
            subprocess.run(['docker', 'rm', '-f', cls.container], capture_output=True, check=False)
        cls.addClassCleanup(remove_container)
        args = ['docker', 'run', '-d', '--pull=never', '--name', cls.container,
            '--label', 'pawbridge.test=proxy-routing', '--memory=64m', '--memory-swap=64m',
            '--cpus=0.5', '--pids-limit=64', '--user=1000:1000', '--read-only',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--tmpfs', '/tmp:size=16m,mode=1777',
            '-p', '127.0.0.1::8000', '-v', str(root) + ':/run/pawbridge-gpu:ro',
            '-v', str(CHART / 'nginx.conf') + ':/etc/pawbridge/nginx.conf:ro',
            '--entrypoint', 'nginx', cls.image, '-c', '/etc/pawbridge/nginx.conf', '-g', 'daemon off;']
        subprocess.run(args, check=True, capture_output=True)
        inspect = json.loads(subprocess.check_output(['docker', 'inspect', cls.container], text=True))[0]
        port = inspect['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
        cls.base = 'http://127.0.0.1:' + port
        for _ in range(40):
            try:
                if cls.request('/livez')[0] == 200:
                    return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.1)
        raise RuntimeError('Test NGINX did not start: ' + subprocess.check_output(['docker', 'logs', cls.container], text=True))

    @classmethod
    def request(cls, path, key='test-key', method='GET', headers=None):
        supplied = {'X-Internal-Api-Key': key} if key is not None else {}
        supplied.update(headers or {})
        request = urllib.request.Request(cls.base + path, method=method, headers=supplied)
        try:
            response = urllib.request.urlopen(request, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            data = json.loads(raw) if response.headers.get_content_type() == 'application/json' else None
            return response.status, data, dict(response.headers)

    def test_recommendation_forwards_internal_auth_and_query_but_strips_user_headers(self):
        path = '/internal/animals/123/similar?species=CAT'
        status, data, _ = self.request(path, headers={'Authorization': 'Bearer dummy', 'Cookie': 'session=dummy', 'X-User-Id': '123'})
        self.assertEqual(200, status)
        self.assertEqual(path, data['path'])
        self.assertEqual('test-key', data['key'])
        self.assertIsNone(data['authorization'])
        self.assertIsNone(data['cookie'])
        self.assertIsNone(data['user'])
        for key in (None, 'wrong-key'):
            with self.subTest(key=key):
                self.assertEqual(401, self.request(path, key=key)[0])

    def test_only_positive_id_get_route_is_forwarded(self):
        for path in ('/internal/animals/0/similar', '/internal/animals/-1/similar',
                     '/internal/animals/foo/similar', '/internal/animals/1/similar/extra', '/private'):
            with self.subTest(path=path):
                self.assertEqual(404, self.request(path)[0])
        for method in ('POST', 'PUT', 'DELETE', 'HEAD'):
            with self.subTest(method=method):
                status, _, headers = self.request('/internal/animals/1/similar', method=method)
                self.assertEqual(405, status)
                self.assertEqual('GET', headers['Allow'])

    def test_recommendation_admission_is_bounded_and_independent_of_photo_search(self):
        type(self).started = 0
        self.release.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.request, '/internal/animals/9999/similar?species=DOG') for _ in range(2)]
            try:
                with self.condition:
                    self.assertTrue(self.condition.wait_for(lambda: self.started == 2, timeout=3))
                self.assertEqual(503, self.request('/internal/animals/1/similar?species=DOG')[0])
                self.assertEqual(200, self.request('/internal/animals/lost-candidates', method='POST')[0])
            finally:
                self.release.set()
            self.assertEqual([200, 200], [future.result()[0] for future in futures])
        self.assertEqual(200, self.request('/internal/animals/1/similar')[0])

    def test_existing_photo_route_and_liveness_are_preserved(self):
        self.assertEqual(200, self.request('/livez', key=None)[0])
        self.assertEqual(200, self.request('/internal/animals/lost-candidates', method='POST')[0])
        self.assertEqual(403, self.request('/internal/animals/lost-candidates')[0])


if __name__ == '__main__':
    unittest.main()
