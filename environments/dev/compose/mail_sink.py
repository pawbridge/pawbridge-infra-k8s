#!/usr/bin/env python3
"""Dev-only SMTP sink; cached Python 3.11, no relay and no disk storage.

smtpd belongs to the pinned 3.11 standard library (removed in 3.12).
This bounded test receiver must never be published or used for real email.
"""
import asyncore
from collections import deque
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import smtpd
import threading

MESSAGES = deque(maxlen=50)
LOCK = threading.Lock()


class Sink(smtpd.SMTPServer):
    def process_message(self, peer, mailfrom, rcpttos, data, **kwargs):
        if not rcpttos or any(not addr.lower().endswith('@example.invalid') for addr in rcpttos):
            return '550 Only @example.invalid development recipients are accepted'
        message = BytesParser(policy=policy.default).parsebytes(data)
        bodies = [part.get_content() for part in message.walk()
                  if part.get_content_type() in ('text/plain', 'text/html')]
        with LOCK:
            MESSAGES.append({'to': rcpttos, 'subject': str(message.get('Subject', '')),
                             'body': '\n'.join(bodies)})
        return None


class Inbox(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/', '/messages', '/health'):
            self.send_error(404); return
        with LOCK:
            content = {'status': 'UP'} if self.path == '/health' else list(MESSAGES)
            body = json.dumps(content, ensure_ascii=False, indent=2).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # Never log message contents, recipients or verification codes.


if __name__ == '__main__':
    Sink(('0.0.0.0', 1025), None, data_size_limit=65536, decode_data=False)
    threading.Thread(target=asyncore.loop, daemon=True).start()
    HTTPServer(('0.0.0.0', 8025), Inbox).serve_forever()
