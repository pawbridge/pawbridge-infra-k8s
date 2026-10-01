"""원본 DB·업무 프로세스 없이 신규 PostgreSQL에 복원하고 재시작·5개 앱 역할 조회를 검사한다."""
import argparse
import dataclasses
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess

from backup import BackupError, Config, database_proof, emit
from restore import restore_bundle, verify_restored

SERVICES = ('animal', 'user', 'community', 'store', 'payment')


def command(args, timeout=2400, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, **kwargs)
    if result.returncode:
        raise BackupError('isolated drill command failed: ' + args[0])
    return result.stdout


def run_drill(bundle, identity, workdir, passwords, report):
    os.umask(0o077)
    root, passwords, report = Path(workdir), Path(passwords), Path(report)
    if root.exists() or report.exists():
        raise BackupError('drill requires a new work directory and report')
    for service in SERVICES:
        if not (passwords / ('pawbridge_' + service + '_app')).is_file():
            raise BackupError('all five application credential files are required')
    root.mkdir(mode=0o700)
    socket = root / 'socket'
    if len(str(socket).encode()) > 80:
        raise BackupError('choose a shorter isolated work directory for the Unix socket')
    socket.mkdir()
    data = root / 'data'
    password = root / 'bootstrap-password'
    password.write_text(secrets.token_urlsafe(32))
    target = Config(host=str(socket.resolve()), password_file=str(password), endpoint='', access_key_file='',
                    secret_key_file='', recipient_file='', workdir=str(root))
    command(['initdb','-D',str(data),'--username=postgres','--encoding=UTF8','--locale=C.UTF-8',
             '--auth-local=peer','--auth-host=reject','--pwfile='+str(password)], timeout=90)
    # OS postgres만 관리자 peer 접속; 나머지 앱 역할은 원본 비밀번호로 SCRAM 인증을 검사한다.
    (data/'pg_hba.conf').write_text('local all postgres peer\nlocal all all scram-sha-256\nhost all all 0.0.0.0/0 reject\n')
    control = ['pg_ctl','-D',str(data),'-l',str(root/'postgres.log'),'-w']
    started = False
    try:
        command(control+['-o',"-k "+shlex.quote(str(socket.resolve()))+" -c listen_addresses='' -c shared_buffers=64MB -c max_connections=20",'start'],timeout=90)
        started = True
        evidence = restore_bundle(bundle, identity, target, root/'restore-stage.json')
        command(control+['-m','fast','restart'],timeout=90)
        manifest = json.loads(command(['age','--decrypt','--identity',str(identity),str(Path(bundle)/'manifest.json.age')],timeout=90))
        verify_restored(target, manifest)
        applications = {}
        for service in SERVICES:
            schema = 'pawbridge_' + service
            app = dataclasses.replace(target,user=schema+'_app',password_file=str(passwords/(schema+'_app')))
            from psycopg import sql
            with app.connect() as connection:
                tables = manifest['proof']['application_reads'].get(schema+'_app', [])
                if not tables:
                    raise BackupError('original application has no verified readable tables')
                for table in tables:
                    connection.execute(sql.SQL('SELECT 1 FROM ONLY {} LIMIT 1').format(sql.Identifier(schema,table))).fetchall()
                applications[service] = {'readable_tables':len(tables)}
        with target.connect() as connection:
            columns = connection.execute("""SELECT n.nspname,c.relname,a.attname FROM pg_attribute a
                JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_type t ON t.oid=a.atttypid WHERE c.relkind='r' AND t.typname='vector'
                AND a.attnum>0 AND NOT a.attisdropped ORDER BY n.nspname,c.relname,a.attname""").fetchall()
            vector_checks = 0
            for schema,table,column in columns:
                qualified,identifier = sql.Identifier(schema,table),sql.Identifier(column)
                row = connection.execute(sql.SQL('SELECT {}::text FROM {} WHERE {} IS NOT NULL LIMIT 1').format(identifier,qualified,identifier)).fetchone()
                if row:
                    connection.execute(sql.SQL('SELECT {} FROM {} ORDER BY {} <=> %s::vector LIMIT 1').format(identifier,qualified,identifier),(row[0],)).fetchone()
                    vector_checks += 1
        evidence.update(restart_verified=True,application_role_reads_verified=True,
                        application_schemas=applications,vector_columns_queried=vector_checks)

    finally:
        if started:
            command(['pg_ctl','-D',str(data),'-m','fast','-w','stop'],timeout=90)
    with report.open('x') as out:
        json.dump(evidence,out,indent=2)
    emit('isolated_drill_completed',run=evidence['run'],verified_tables=evidence['verified_tables'],
         applications_verified=len(applications),vector_columns_queried=vector_checks)
    return evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ['directory','identity','workdir','app-password-directory','report']:
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--allow-isolated-restore',action='store_true')
    args=parser.parse_args()
    if not args.allow_isolated_restore:
        parser.error('explicit isolated restore acknowledgement required')
    try:
        run_drill(args.directory,args.identity,args.workdir,args.app_password_directory,args.report)
    except Exception as exc:
        emit('isolated_drill_failed',error_type=type(exc).__name__,
             reason=str(exc) if isinstance(exc,BackupError) else 'isolated drill failed; preserve private evidence')
        raise SystemExit(1)
