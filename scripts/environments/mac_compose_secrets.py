#!/usr/bin/env python3
"""Render least-privilege external credentials for an isolated candidate clone.

Never prints values, edits inputs, starts containers or prepares production files.
Compose must read generated env_file entries with format: raw (2.30+).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

import mac_compose_candidate as candidate

SCHEMA = {
    'google-oauth': ('GOOGLE_CLIENT_ID', 'GOOGLE_SECRET_KEY'),
    'gmail': ('GOOGLE_EMAIL', 'GOOGLE_EMAIL_SECRET_KEY'),
    'gemini': ('GEMINI_API_KEY', 'GEMINI_MODEL'),
    'youtube': ('YOUTUBE_DATA_API_KEY',),
    'public-data': ('APMS_API_BASE_URL', 'APMS_API_SERVICE_KEY', 'SHELTER_DIRECTORY_SERVICE_KEY',
                    'TOURAPI_SERVICEKEY', 'TOURAPI_BULKSERVICEKEY'),
    'r2-public': ('R2_ENDPOINT', 'R2_REGION', 'R2_BUCKET_NAME', 'R2_PUBLIC_BASE_URL',
                  'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'),
    'r2-originals': ('R2_ENDPOINT', 'R2_REGION', 'R2_BUCKET_NAME', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'),
    'r2-backup': ('R2_ENDPOINT', 'R2_BUCKET_NAME', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY'),
    'toss-test': ('VITE_TOSS_CLIENT_KEY', 'TOSS_SECRET_KEY'),
}
SERVICE_INPUTS = {
    'api-gateway': (),
    'user-service': ('google-oauth', 'gmail'),
    'animal-service': ('r2-public', 'public-data'),
    'community-service': ('r2-public', 'youtube'),
    'store-service': ('r2-public',),
    'payment-service': ('toss-test',),
    'photo-service': (),
    'python-ai-service': ('gemini',),
}
EXTERNAL_PREFIXES = ('GOOGLE_', 'GEMINI_', 'R2_', 'TOSS_', 'VITE_', 'YOUTUBE_', 'APMS_API_',
                     'APMS_PHOTO_ARCHIVE_ACCESS_', 'APMS_PHOTO_ARCHIVE_SECRET_')
EXTERNAL_KEYS = {'TOURAPI_SERVICEKEY', 'TOURAPI_BULKSERVICEKEY', 'TOURAPI_KOREANSERVICEKEY',
                 'SHELTER_DIRECTORY_SERVICE_KEY'}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def private(path, directory=False):
    path = Path(path)
    require(not path.is_symlink(), 'Symlink input/output refused')
    s = path.stat()
    require(s.st_uid == os.getuid(), 'File owner differs')
    require(stat.S_IMODE(s.st_mode) == (0o700 if directory else 0o600), 'Private permissions required')
    require(stat.S_ISDIR(s.st_mode) if directory else stat.S_ISREG(s.st_mode), 'Wrong file type')
    return path


def parse_env(text, label):
    result = {}
    for number, line in enumerate(text.split('\n'), 1):
        if not line or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        require(sep and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key), label + ': invalid field at line ' + str(number))
        require(key not in result, label + ': duplicate field ' + key)
        require(not any(ord(c) < 32 or ord(c) == 127 for c in value), label + ': control character in ' + key)
        require(value == value.strip(), label + ': edge whitespace in ' + key)
        result[key] = value
    return result


def load_inputs(root):
    private(root, True)
    result = {}
    for name, keys in SCHEMA.items():
        path = private(root / (name + '.env'))
        values = parse_env(path.read_text(), name)
        require(set(values) == set(keys), name + ': unexpected or missing field names')
        for key in keys:
            require(values[key] and not values[key].startswith(('"', "'")), name + ': missing or quoted ' + key)
        result[name] = values
    require(result['toss-test']['TOSS_SECRET_KEY'].startswith('test_sk_'), 'Test-only Toss server key required')
    require(result['toss-test']['VITE_TOSS_CLIENT_KEY'].startswith('test_ck_'), 'Test-only Toss client key required')
    apms = urlsplit(result['public-data']['APMS_API_BASE_URL'])
    require(apms.scheme == 'https' and apms.hostname == 'apis.data.go.kr' and not apms.query
            and not apms.fragment and not apms.username and apms.port in (None, 443), 'Unexpected APMS endpoint')
    r2 = urlsplit(result['r2-public']['R2_ENDPOINT'])
    require(r2.scheme == 'https' and re.fullmatch(r'[a-f0-9]{32}\.r2\.cloudflarestorage\.com', r2.netloc)
            and r2.path in ('', '/') and not r2.query and not r2.fragment, 'Unexpected R2 endpoint')
    require(re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]', result['r2-public']['R2_BUCKET_NAME']), 'Invalid R2 bucket')
    return result


def is_external(key):
    return key.startswith(EXTERNAL_PREFIXES) or key in EXTERNAL_KEYS


def service_values(service, baseline, inputs):
    require(service in candidate.SERVICES, 'Unknown service')
    if service not in SERVICE_INPUTS:
        require(not any(is_external(k) for k in baseline), 'Infrastructure contains provider keys')
        return dict(baseline)
    values = {k: v for k, v in baseline.items() if not is_external(k)}
    for group in SERVICE_INPUTS[service]:
        values.update({k: v for k, v in inputs[group].items() if not k.startswith('VITE_')})
    if service == 'user-service':
        values.update(SPRING_MAIL_HOST='smtp.gmail.com', SPRING_MAIL_PORT='587',
                      SPRING_MAIL_PROPERTIES_MAIL_SMTP_AUTH='true',
                      SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_ENABLE='true',
                      SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_REQUIRED='true',
                      SPRING_MAIL_PROPERTIES_MAIL_SMTP_SSL_CHECKSERVERIDENTITY='true',
                      MANAGEMENT_HEALTH_MAIL_ENABLED='false')
    if service == 'python-ai-service':
        values.update(LLM_PROVIDER='gemini', LOST_GALLERY_SYNC_ENABLED='false')
    if service == 'animal-service':
        values.update(APMS_PHOTO_ARCHIVE_ENABLED='false', TOURAPI_ENABLED='false',
                      TOURAPI_SCHEDULE_ENABLED='false', SHELTER_DIRECTORY_SCHEDULE_ENABLED='false')
    expected = {k for group in SERVICE_INPUTS[service] for k in inputs[group] if not k.startswith('VITE_')}
    require({k for k in values if is_external(k)} == expected, 'Service provider allowlist differs')
    return values


def write_private(path, text):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)


def render(candidate_state, inputs_root, destination):
    state, _, _ = candidate.validate_files(candidate_state)
    inputs = load_inputs(inputs_root)
    require(destination.parent.name == 'test' and destination.parent.parent.name == 'runtime', 'Only runtime/test output is allowed')
    private(destination.parent, True)
    require(not destination.exists() and not destination.is_symlink(), 'Refusing to overwrite runtime inputs')
    result = {}
    for service in sorted(candidate.SERVICES):
        baseline = parse_env((candidate_state / 'secrets' / (service + '.env')).read_text(), service)
        result[service] = service_values(service, baseline, inputs)
    destination.mkdir(mode=0o700)
    for service, values in result.items():
        write_private(destination / (service + '.env'), ''.join(k + '=' + v + '\n' for k, v in sorted(values.items())))
    write_private(destination / 'frontend.env', 'VITE_TOSS_CLIENT_KEY=' + inputs['toss-test']['VITE_TOSS_CLIENT_KEY'] + '\n')
    receipt = {'version': 1, 'purpose': 'isolated-external-validation', 'source_candidate': str(candidate_state),
               'source_owner': state['owner'], 'source_state_sha256': candidate.digest(candidate_state / 'state.json'),
               'input_hashes': {n + '.env': candidate.digest(inputs_root / (n + '.env')) for n in SCHEMA},
               'runtime_hashes': {p.name: candidate.digest(p) for p in destination.glob('*.env')},
               'service_groups': SERVICE_INPUTS,
               'not_supplied': ['r2-originals', 'r2-backup'],
               'not_verified': ['OAuth login', 'mail delivery', 'payment confirmation', 'R2 writes', 'production']}
    write_private(destination / 'receipt.json', json.dumps(receipt, indent=2) + '\n')
    return receipt


def relay_config(inputs):
    r2host = urlsplit(inputs['r2-public']['R2_ENDPOINT']).hostname
    hosts = sorted({r2host, inputs['r2-public']['R2_BUCKET_NAME'] + '.' + r2host,
                    'generativelanguage.googleapis.com', 'api.tosspayments.com', 'www.googleapis.com',
                    'oauth2.googleapis.com', 'openidconnect.googleapis.com', 'apis.data.go.kr'})
    # Docker's resolver would resolve these aliases back to the relay. Resolve
    # upstream public records directly, while keeping app DNS inside Docker.
    conf = 'pid /tmp/nginx.pid;\nerror_log /dev/stderr warn;\nevents { worker_connections 64; }\nstream {\n'
    conf += ' map_hash_bucket_size 256;\n resolver 1.1.1.1 1.0.0.1 valid=60s ipv6=off;\n resolver_timeout 5s;\n'
    conf += ' map $ssl_preread_server_name $upstream {\n default "";\n'
    conf += ''.join(' "' + host + '" "' + host + ':443";\n' for host in hosts)
    conf += ' }\n server { listen 443; ssl_preread on; proxy_connect_timeout 5s; proxy_timeout 45s; proxy_pass $upstream; }\n'
    conf += ' server { listen 587; set $smtp smtp.gmail.com:587; proxy_connect_timeout 5s; proxy_timeout 15s; proxy_pass $smtp; }\n}\n'
    return hosts + ['smtp.gmail.com'], conf


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--inputs', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        receipt = render(args.candidate, args.inputs, args.output)
    except (ValueError, OSError, candidate.guard.GuardError) as error:
        raise SystemExit('Secret generation refused: ' + str(error))
    print('Rendered 14 service env files and one client-only frontend file. No containers started.')
