#!/usr/bin/env python3
"""Operate the approved member-chat test on the existing local dev runtime only."""
import argparse
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import uuid
import zipfile
from local_dev import cli, db_sql, docker, secret_files, write_private
from local_flows import HTTP, request

SERVICES = ('user-service', 'community-service', 'api-gateway', 'community-chat-peer', 'chat-peer-gateway')
ROOT = Path(__file__).resolve().parents[2]

def guard(state):
    if db_sql(state, 'SELECT name FROM public.pawbridge_local_environment;') != 'dev':
        raise ValueError('Local dev database marker required')

def jars(backend):
    result = {}
    for service in ('community-service', 'api-gateway', 'user-service'):
        path = (backend/service/'build/libs'/f'{service}-0.0.1-SNAPSHOT.jar').resolve(strict=True)
        if path.is_symlink() or not zipfile.is_zipfile(path): raise ValueError('Expected reviewed local JAR')
        result[service] = path
    return result

def chat_cli(state, backend, *arguments, **options):
    built = jars(backend)
    os.environ['MEMBER_CHAT_COMMUNITY_JAR'] = str(built['community-service'])
    os.environ['MEMBER_CHAT_GATEWAY_JAR'] = str(built['api-gateway'])
    os.environ['MEMBER_CHAT_USER_JAR'] = str(built['user-service'])
    return cli(state, '-f', str(ROOT/'environments/dev/compose/compose.chat-test.yaml'),
               '--profile', 'apps', '--profile', 'chat-test', *arguments, **options)

def migrate(state, backend):
    guard(state)
    libs = (backend/'community-service/build/migration/lib').resolve(strict=True)
    expected = {str(path.relative_to(backend/'community-service/src/migration/resources')):path.read_bytes()
                for path in (backend/'community-service/src/migration/resources/db/postgresql').glob('*.sql')}
    packaged = {}
    for jar in libs.glob('*.jar'):
        with zipfile.ZipFile(jar) as archive:
            packaged.update({name:archive.read(name) for name in archive.namelist()
                             if name.startswith('db/postgresql/') and name.endswith('.sql')})
    if packaged != expected or 'db/postgresql/V10__member_chat.sql' not in expected:
        raise ValueError('Build the exact reviewed Community migration distribution first')
    keys = dict(line.split('=',1) for line in secret_files(state)[0].read_text().splitlines() if line)
    images = dict(line.split('=',1) for line in (state/'images.env').read_text().splitlines() if line)
    java_image = images['DEV_IMAGE_COMMUNITY_SERVICE']
    if not re.fullmatch(r'[a-zA-Z0-9._:/-]+@sha256:[0-9a-f]{64}', java_image):
        raise ValueError('Immutable existing Community image required')
    url = 'jdbc:postgresql://127.0.0.1:15433/pawbridge'
    with tempfile.TemporaryDirectory(prefix='pawbridge-chat-migration-') as directory:
        env = Path(directory)/'runner.env'
        write_private(env, '\n'.join((
            'COMMUNITY_PG_MIGRATION_JDBC_URL='+url, 'COMMUNITY_PG_MIGRATION_CONFIRM_TARGET='+url,
            'COMMUNITY_PG_MIGRATION_USERNAME=pawbridge_dev_community_owner',
            'COMMUNITY_PG_MIGRATION_PASSWORD='+keys['DEV_COMMUNITY_OWNER_PASSWORD']))+'\n')
        # Reuses a locally available JDK image. Pulls and remote Docker contexts are forbidden.
        for command in ('migrate','validate'):
            subprocess.run([*docker(),'run','--pull=never','--rm','--network','host','--memory','256m',
                            '--env-file',str(env),'-v',str(libs)+':/migration:ro','--entrypoint','java',
                            java_image,'-Xmx128m','-cp','/migration/*',
                            'com.pawbridge.communityservice.migration.CommunityPostgresqlMigration',command],
                           check=True, timeout=120)
    print('Community V10 validated on guarded local dev; no other schema migrated.')

def up(state, backend):
    guard(state)
    if db_sql(state, "SELECT count(*) FROM pawbridge_community.flyway_schema_history WHERE version='10' AND success;") != '1':
        raise ValueError('Community V10 required before enabling chat')
    chat_cli(state, backend, 'config', '--quiet')
    metadata = state/'member-chat-started.json'
    previous = json.loads(metadata.read_text()) if metadata.exists() else []
    running = set(cli(state, 'ps','--services','--status','running',capture_output=True,text=True).stdout.splitlines())
    if (set(SERVICES) & running) - set(previous):
        raise ValueError('Existing non-task app must not be recreated by this chat test')
    started = sorted(set(previous) | (set(SERVICES)-running))
    write_private(metadata, json.dumps(started))
    # A bind-mounted JAR changing does not change Compose configuration. Restart the reviewed builds.
    chat_cli(state, backend, 'up','-d','--pull','never','--no-deps','--force-recreate',*SERVICES)
    print('Two Community apps and two gateways started; unrelated apps not started.')

def accounts(state):
    guard(state)
    result = []
    run = uuid.uuid4().hex[:10]
    for n in range(3):
        email = f'note-chat-{run}-{n}@example.invalid'
        password = secrets.token_urlsafe(24)
        request('/api/v1/email/send', {'email':email})
        deadline = time.monotonic()+15; code = None
        while time.monotonic() < deadline:
            with HTTP.open('http://127.0.0.1:18025/messages',timeout=5) as response:
                for message in json.load(response):
                    if email in message['to']:
                        match = re.search(r'>\s*(\d{6})\s*</',message['body'])
                        if match: code = match.group(1)
            if code: break
            time.sleep(.25)
        if not code: raise ValueError('Local SMTP verification not delivered')
        request('/api/v1/email/verify', {'email':email,'code':code})
        user = request('/api/v1/users/signup', {'email':email,'name':f'채팅검증{n+1}',
                       'password':password,'rePassword':password,'role':'ROLE_USER'})['data']
        # Existing login refresh-token uniqueness uses a timestamp; avoid a same-second fixture collision.
        time.sleep(1.1)
        login = request('/api/v1/auth/login', {'email':email,'password':password})['data']
        result.append({'email':email,'password':password,'userId':int(user['userId']),'token':login['accessToken']})
    write_private(state/'member-chat-test-accounts.json', json.dumps(result,ensure_ascii=False))
    print('Three synthetic local accounts prepared. Credentials not printed.')

def stop(state, backend):
    metadata = state/'member-chat-started.json'
    if not metadata.exists(): raise ValueError('Task-owned start record required')
    started = json.loads(metadata.read_text())
    if not isinstance(started,list) or not set(started).issubset(SERVICES): raise ValueError('Invalid start record')
    if started: chat_cli(state, backend, 'stop',*started)
    # Remove test JAR mounts only from stopped, task-started base services. No volumes are removed.
    base = [service for service in started if service in ('api-gateway','community-service','user-service')]
    if base: cli(state,'--profile','apps','up','--no-start','--no-deps',*base)
    write_private(metadata, '[]')
    print('Task-started apps stopped; base JAR mounts restored; all volumes preserved.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('migrate','up','accounts','stop'))
    parser.add_argument('--state',type=Path,default=Path.home()/'.local/state/pawbridge/dev')
    parser.add_argument('--backend',type=Path,required=True)
    args = parser.parse_args()
    try:
        globals()[args.action](args.state.resolve(),args.backend.resolve(strict=True)) if args.action != 'accounts' else accounts(args.state.resolve())
    except Exception as error:
        # Subprocess or network exception strings may carry private data. Only report the class.
        print('Local member-chat test failed ('+type(error).__name__+'). No secrets printed.')
        raise SystemExit(1)
