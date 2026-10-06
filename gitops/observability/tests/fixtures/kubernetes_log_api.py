"""가짜 Kubernetes 로그 API와 성공 응답만 주는 로그 수신기를 제공한다."""
import datetime
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LOCK = threading.Lock()
BASE = int(time.time()) - 600
ENTRIES = [(BASE, 'older-fixture'), (BASE + 300, 'checkpoint-fixture')]
REQUESTS = []


def timestamp(seconds):
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def reply(self, status, payload=None):
        body = b'' if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path == '/loki/api/v1/push':
            # 수신 성공 응답을 모의한다. 실제 Loki 저장 성공 시험은 아니다.
            self.reply(204)
        elif self.path == '/fixture/next':
            with LOCK:
                ENTRIES.append((BASE + 600, 'new-fixture'))
            self.reply(200)
        else:
            self.reply(404)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/version':
            self.reply(200, {'major': '1', 'minor': '36', 'gitVersion': 'v1.36.0'})
        elif parsed.path == '/fixture/state':
            with LOCK:
                state = {'base': BASE, 'checkpoint': BASE + 300, 'requests': list(REQUESTS)}
            self.reply(200, state)
        elif parsed.path.endswith('/log'):
            since = urllib.parse.parse_qs(parsed.query).get('sinceTime', [None])[0]
            threshold = datetime.datetime.fromisoformat(since.replace('Z', '+00:00')).timestamp() if since else 0
            with LOCK:
                entries = [(stamp, line) for stamp, line in ENTRIES if stamp >= threshold]
                REQUESTS.append({'since_time': since, 'served': [line for _, line in entries]})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            try:
                for stamp, line in entries:
                    chunk = (timestamp(stamp) + ' ' + line + '\n').encode()
                    self.wfile.write(('%x\r\n' % len(chunk)).encode() + chunk + b'\r\n')
                self.wfile.flush()
                # 실제 follow 연결처럼 열어 두되 테스트 종료 시 스레드는 함께 끝난다.
                time.sleep(180)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif parsed.path.startswith('/api/v1/namespaces/pawbridge/pods/'):
            self.reply(200, {'apiVersion': 'v1', 'kind': 'Pod',
                             'metadata': {'name': 'position-fixture', 'namespace': 'pawbridge', 'uid': 'fixture-uid'},
                             'status': {'phase': 'Running', 'containerStatuses': [{'name': 'app', 'state': {'running': {}}}]}})
        else:
            self.reply(404)


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 18080), Handler).serve_forever()
