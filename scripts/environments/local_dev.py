#!/usr/bin/env python3
"""Prepare and operate the isolated local Compose project; never talks to Kubernetes."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.request
import uuid
import zipfile
import yaml

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ('animal', 'user', 'community', 'store', 'payment')
PROJECT = 'pawbridge-dev'
PORTS = {'api-gateway': 28080, 'user-service': 28081, 'animal-service': 28082, 'community-service': 28083, 'store-service': 28084, 'payment-service': 28085, 'photo-service': 28086, 'python-ai-service': 28087}


def write_private(path, text):
    if path.is_symlink(): raise ValueError("Refusing a symlink in local state")
    if path.exists(): path.chmod(0o600)
    path.write_text(text)
    path.chmod(0o600)


def image(service):
    contract = json.loads((ROOT/'environments/environment-contract.json').read_text())
    doc = yaml.safe_load((ROOT/contract['services'][service]['devValues']).read_text())
    img = doc['image']
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', img.get('digest', '')):
        raise ValueError('Immutable image required: '+service)
    return img['repository']+'@'+img['digest']


def compose(google_oauth=False):
    """Read the versioned settings for validation; Docker Compose performs runtime merging."""
    folder=ROOT/'environments/dev/compose'
    config=yaml.safe_load((folder/'compose.yaml').read_text())
    if google_oauth:
        extra=yaml.safe_load((folder/'compose.google.yaml').read_text())['services']
        config['services']['google-oauth-egress']=extra['google-oauth-egress']
        for key in ('environment','depends_on'):
            config['services']['user-service'][key].update(extra['user-service'][key])
    return config


def app_env(name, host=False):
    values=compose()['services'][name]['environment'].copy()
    if host:
        overrides=yaml.safe_load((ROOT/'environments/dev/compose/ide-overrides.yaml').read_text())
        values.update(overrides[name])
    return values


def google_credentials(state):
    path=state/'google-oauth.env'
    if not path.exists(): return {}
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('google-oauth.env must be a private regular file (0600)')
    keys={}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'): continue
        key,sep,value=line.partition('=')
        if not sep or key in keys: raise ValueError('Invalid OAuth credential file')
        keys[key]=value
    if set(keys)!={'GOOGLE_CLIENT_ID','GOOGLE_SECRET_KEY'}:
        raise ValueError('OAuth file must contain only GOOGLE_CLIENT_ID and GOOGLE_SECRET_KEY')
    if not re.fullmatch(r'[A-Za-z0-9.-]+\.apps\.googleusercontent\.com',keys['GOOGLE_CLIENT_ID']):
        raise ValueError('Invalid Google client ID')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}',keys['GOOGLE_SECRET_KEY']):
        raise ValueError('Invalid Google client secret')
    return keys


def sql_init(keys):
    lines=["REVOKE CONNECT ON DATABASE pawbridge FROM PUBLIC;", "REVOKE CREATE ON SCHEMA public FROM PUBLIC;", "CREATE EXTENSION IF NOT EXISTS vector;", "CREATE EXTENSION IF NOT EXISTS pg_trgm;", "CREATE TABLE public.pawbridge_local_environment (name text PRIMARY KEY CHECK(name = 'dev'));", "INSERT INTO public.pawbridge_local_environment VALUES ('dev');"]
    for name in SERVICES:
        schema='pawbridge_'+name;owner='pawbridge_dev_'+name+'_owner';app='pawbridge_dev_'+name+'_app';cdc='pawbridge_dev_'+name+'_cdc'
        for role,kind in [(owner,'OWNER'),(app,'APP'),(cdc,'CDC')]:
            pw=keys['DEV_'+name.upper()+'_'+kind+'_PASSWORD']
            lines += [f"CREATE ROLE {role} LOGIN {'REPLICATION' if kind=='CDC' else 'NOREPLICATION'} PASSWORD '{pw}';",f'GRANT CONNECT ON DATABASE pawbridge TO {role};']
        lines += [f'CREATE SCHEMA {schema} AUTHORIZATION {owner};',f'GRANT USAGE ON SCHEMA {schema} TO {app}, {cdc};',f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app};',f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {app};',f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} GRANT SELECT ON TABLES TO {cdc};']
    lines += [f"CREATE ROLE pawbridge_dev_vector LOGIN PASSWORD '{keys['DEV_VECTOR_PASSWORD']}';",'GRANT CONNECT ON DATABASE pawbridge TO pawbridge_dev_vector;', 'GRANT USAGE ON SCHEMA pawbridge_animal TO pawbridge_dev_vector;', 'ALTER DEFAULT PRIVILEGES FOR ROLE pawbridge_dev_animal_owner IN SCHEMA pawbridge_animal GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO pawbridge_dev_vector;', 'ALTER DEFAULT PRIVILEGES FOR ROLE pawbridge_dev_animal_owner IN SCHEMA pawbridge_animal GRANT USAGE, SELECT ON SEQUENCES TO pawbridge_dev_vector;']
    return '\n'.join(lines)+'\n'


def prepare(state):
    state=state.resolve()
    if state.exists() and any(state.iterdir()) and not (state/'owner.json').exists():
        raise ValueError('Refusing to reuse an unrelated directory')
    state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    if (state/'owner.json').exists() and json.loads((state/'owner.json').read_text()) != {'project':PROJECT,'runtime':'local-compose'}:
        raise ValueError('Runtime owner mismatch')
    if (state/'.env').exists():
        keys=dict(line.split('=',1) for line in (state/'.env').read_text().splitlines())
    else:
        names=['DEV_POSTGRES_PASSWORD','DEV_REDIS_PASSWORD','DEV_INTERNAL_API_KEY','DEV_VECTOR_PASSWORD']+[f'DEV_{s.upper()}_{k}_PASSWORD' for s in SERVICES for k in ('APP','OWNER','CDC')]
        keys={name:secrets.token_hex(32) for name in names};keys['DEV_JWT_SECRET']=base64.b64encode(secrets.token_bytes(64)).decode()
        write_private(state/'.env',''.join(k+'='+v+'\n' for k,v in keys.items()))
    oauth=google_credentials(state)
    write_private(state/'owner.json',json.dumps({'project':PROJECT,'runtime':'local-compose'}))
    write_private(state/'init.sql',sql_init(keys));write_private(state/'redis.conf','appendonly yes\nmaxmemory 128mb\nmaxmemory-policy noeviction\nrequirepass '+keys['DEV_REDIS_PASSWORD']+'\n')
    write_private(state/'cdc.properties',''.join(s+'.password='+keys['DEV_'+s.upper()+'_CDC_PASSWORD']+'\n' for s in SERVICES))
    for name in ('api-gateway',*(s+'-service' for s in SERVICES)):
        values=app_env(name,host=True)
        if name=='user-service' and oauth: values.update(oauth)
        for k,v in values.items():
            values[k]=re.sub(r'\$\{([A-Z_]+)(?::-([^}]*))?\}',lambda m:keys.get(m[1],m[2]) if m[1] in keys or m[2] is not None else keys[m[1]],v)
        write_private(state/(name+'.env'),''.join(k+'='+v+'\n' for k,v in values.items()))
    write_private(state/'compose.env','PAWBRIDGE_DEV_STATE='+str(state)+'\n')
    images={name:image(name) for name in ('api-gateway',*(s+'-service' for s in SERVICES),'photo-service','python-ai-service')}
    write_private(state/'images.env',''.join('DEV_IMAGE_'+name.upper().replace('-','_')+'='+value+'\n' for name,value in images.items()))
    for name in ('init.sql','redis.conf','cdc.properties'):
        (state/name).chmod(0o444) # state directory stays 0700; only explicitly mounted files reach containers
    print('Prepared local configuration (credentials not printed):',state)


def docker():
    if os.getenv('DOCKER_HOST') or os.getenv('DOCKER_CONTEXT'):
        raise ValueError('Unset Docker overrides; local default context required')
    c=json.loads(subprocess.check_output(['docker','context','inspect','default'],text=True))[0]
    if not c['Endpoints']['docker']['Host'].startswith(('unix://','npipe://')):
        raise ValueError('Refusing a non-local Docker daemon')
    return ['docker','--context','default']


def cli(state,*args,**kwargs):
    if not (state/'owner.json').is_file() or json.loads((state/'owner.json').read_text()).get('project')!=PROJECT:
        raise ValueError('Run prepare before using this runtime')
    folder=ROOT/'environments/dev/compose'
    command=[*docker(),'compose','--project-name',PROJECT]
    for name in ('.env','compose.env','images.env'):
        command += ['--env-file',str(state/name)]
    command += ['-f',str(folder/'compose.yaml')]
    if google_credentials(state):
        command += ['--env-file',str(state/'google-oauth.env'),'-f',str(folder/'compose.google.yaml')]
    return subprocess.run([*command,*args],check=True,timeout=900,**kwargs)


def db_sql(state,sql):
    return cli(state,'exec','-T','postgresql','psql','-X','-qAt','-v','ON_ERROR_STOP=1','-U','postgres','-d','pawbridge',input=sql,text=True,capture_output=True).stdout.strip()


def migrate(state,backend,java):
    if db_sql(state,'SELECT name FROM public.pawbridge_local_environment;')!='dev':
        raise ValueError('Local dev database marker missing')
    db_sql(state,'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;')
    keys=dict(line.split('=',1) for line in (state/'.env').read_text().splitlines())
    for s in SERVICES:
        libs=backend/(s+'-service/build/migration/lib')
        if not libs.is_dir():raise ValueError(f'Build {s}-service migrationDistribution first')
        expected={str(p.relative_to(backend/(s+'-service/src/migration/resources'))):p.read_bytes() for p in (backend/(s+'-service/src/migration/resources/db/postgresql')).glob('*.sql')}
        packaged={}
        for jar in libs.glob('*.jar'):
            with zipfile.ZipFile(jar) as archive:
                packaged.update({n:archive.read(n) for n in archive.namelist() if n.startswith('db/postgresql/') and n.endswith('.sql')})
        if not expected or packaged != expected:raise ValueError('Stale migration distribution: '+s)
        env=os.environ.copy();url='jdbc:postgresql://127.0.0.1:15433/pawbridge';prefix=s.upper()+'_PG_MIGRATION_'
        env.update({prefix+'JDBC_URL':url,prefix+'CONFIRM_TARGET':url,prefix+'USERNAME':'pawbridge_dev_'+s+'_owner',prefix+'PASSWORD':keys['DEV_'+s.upper()+'_OWNER_PASSWORD']})
        cls='com.pawbridge.'+s+'service.migration.'+s.title()+'PostgresqlMigration'
        for command in ('migrate','validate'):
            subprocess.run([java,'-Xmx192m','-cp',str(libs/'*'),cls,command],env=env,check=True,timeout=120)
        schema='pawbridge_'+s;cdc='pawbridge_dev_'+s+'_cdc';outbox='outbox' if s in ('store','payment') else 'outbox_events'
        db_sql(state,f"CREATE TABLE IF NOT EXISTS {schema}.cdc_heartbeat (id integer PRIMARY KEY, touched_at timestamptz NOT NULL); INSERT INTO {schema}.cdc_heartbeat VALUES (1,clock_timestamp()) ON CONFLICT DO NOTHING; GRANT SELECT,UPDATE ON {schema}.cdc_heartbeat TO {cdc}; DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='{schema}_outbox') THEN CREATE PUBLICATION {schema}_outbox FOR TABLE {schema}.{outbox},{schema}.cdc_heartbeat; END IF; END $$;")
    write_private(state/'migrated.json',json.dumps({'services':list(SERVICES),'backendRevision':subprocess.check_output(['git','-C',str(backend),'rev-parse','HEAD'],text=True).strip()}))
    print('Five service migrations validated on local dev')


def register(state):
    if db_sql(state,'SELECT name FROM public.pawbridge_local_environment;')!='dev':raise ValueError('Wrong database')
    configs=json.loads((ROOT/'environments/dev/compose/connectors.json').read_text())
    for s,c in configs.items():
        req=urllib.request.Request('http://127.0.0.1:18383/connectors/dev-'+s+'-outbox/config',data=json.dumps(c).encode(),headers={'Content-Type':'application/json'},method='PUT')
        with urllib.request.urlopen(req,timeout=30) as r:
            if r.status not in (200,201):raise ValueError('Connector registration failed')
        print('Registered local connector:',s)


def smoke(state):
    report={'environment':'local-dev','checks':[],'fullE2E':False}
    if db_sql(state,'SELECT name FROM public.pawbridge_local_environment;')!='dev':raise ValueError('Wrong database')
    for name,port in PORTS.items():
        if name in ('photo-service','python-ai-service'):continue
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/actuator/health/readiness',timeout=10) as response:
            if json.load(response).get('status')!='UP':raise ValueError('Not ready: '+name)
        report['checks'].append(name+':ready')
    for route in ('/api/v1/animals?page=0&size=1','/api/products?page=0&size=1'):
        with urllib.request.urlopen('http://127.0.0.1:28080'+route,timeout=10) as response:
            if response.status!=200:raise ValueError('Public route failed')
            json.load(response)
        report['checks'].append(route+':200')
    for name in SERVICES:
        with urllib.request.urlopen('http://127.0.0.1:18383/connectors/dev-'+name+'-outbox/status',timeout=10) as response:
            status=json.load(response)
        if status['connector']['state']!='RUNNING' or not status['tasks'] or any(t['state']!='RUNNING' for t in status['tasks']):raise ValueError('CDC is not running: '+name)
        report['checks'].append(name+':cdc-running')
    write_private(state/'smoke-result.json',json.dumps(report,indent=2))
    print(json.dumps(report))


def verify_cdc(state):
    if db_sql(state,'SELECT name FROM public.pawbridge_local_environment;')!='dev':raise ValueError('Wrong database')
    probe=uuid.uuid4().hex;topic='dev.environment.probe.'+probe[:12]
    cli(state,'exec','-T','kafka','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','localhost:9092','--create','--topic',topic,'--partitions','1','--replication-factor','1')
    ids=[str(uuid.uuid4()) for _ in range(3)]
    for i,action in enumerate(('COMMIT','ROLLBACK','COMMIT')):
        payload=json.dumps({'probe':probe,'step':i})
        db_sql(state,f"SET ROLE pawbridge_dev_animal_app; BEGIN; INSERT INTO pawbridge_animal.outbox_events (aggregate_id,aggregate_type,created_at,event_id,event_type,payload,topic) VALUES ('{probe}','LOCAL_DEV_PROBE',clock_timestamp(),'{ids[i]}','LOCAL_DEV_PROBE','{payload}','{topic}'); {action}; RESET ROLE;")
    output=cli(state,'exec','-T','kafka','/opt/kafka/bin/kafka-console-consumer.sh','--bootstrap-server','localhost:9092','--topic',topic,'--from-beginning','--max-messages','2','--timeout-ms','30000','--property','print.headers=true','--property','print.key=true',text=True,capture_output=True).stdout
    rows=output.strip().splitlines()
    if len(rows)!=2 or not all('eventType:LOCAL_DEV_PROBE' in row and probe in row for row in rows):raise ValueError('CDC header/key contract failed')
    payloads=[json.loads(row.split('\t')[-1]) for row in rows]
    if [d['step'] for d in payloads]!=[0,2]:raise ValueError('CDC commit/rollback contract failed')
    report={'environment':'local-dev','probeTopic':topic,'committedEventsObserved':2,'rollbackEventObserved':False,'keyHeaderPayloadVerified':True,'businessConsumerVerified':False}
    write_private(state/'cdc-result.json',json.dumps(report,indent=2));print(json.dumps(report))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','config','up-data','up-events','up-apps','migrate','register-cdc','status','stop','smoke','verify-cdc'])
    parser.add_argument('--state',type=Path,default=Path.home()/'.local/state/pawbridge/dev')
    parser.add_argument('--backend',type=Path)
    parser.add_argument('--java',default='java')
    a=parser.parse_args();state=a.state.resolve()
    if a.command=='prepare':prepare(state)
    elif a.command=='config':cli(state,'--profile','apps','--profile','events','--profile','optional-ai','config','--quiet');print('Compose configuration valid')
    elif a.command=='up-data':
        cli(state,'up','-d','--wait','--wait-timeout','180','postgresql','redis','kafka','mail','local-access')
        topics=json.loads((ROOT/'environments/dev/compose/topics.json').read_text())+['__debezium-heartbeat.pawbridge-pg-'+s for s in SERVICES]
        existing=set(cli(state,'exec','-T','kafka','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','localhost:9092','--list',text=True,capture_output=True).stdout.splitlines())
        for topic in topics:
            if topic in existing:continue
            cli(state,'exec','-T','kafka','/opt/kafka/bin/kafka-topics.sh','--bootstrap-server','localhost:9092','--create','--if-not-exists','--topic',topic,'--partitions','1','--replication-factor','1')
    elif a.command=='up-events':
        if not (state/'migrated.json').exists():raise ValueError('Migrate all schemas first')
        cli(state,'--profile','events','up','-d','--wait','--wait-timeout','180','connect')
    elif a.command=='up-apps':
        if not (state/'migrated.json').exists():raise ValueError('Migrate all schemas first')
        cli(state,'--profile','apps','up','-d')
    elif a.command=='migrate':
        if not a.backend:raise ValueError('--backend required')
        migrate(state,a.backend.resolve(),a.java)
    elif a.command=='register-cdc':register(state)
    elif a.command=='smoke':smoke(state)
    elif a.command=='verify-cdc':verify_cdc(state)
    elif a.command=='status':cli(state,'--profile','apps','--profile','events','--profile','optional-ai','ps')
    elif a.command=='stop':cli(state,'--profile','apps','--profile','events','--profile','optional-ai','stop')

if __name__=='__main__':
    try:main()
    except (ValueError,subprocess.CalledProcessError,subprocess.TimeoutExpired) as e:
        print('Local dev command failed:',str(e) if isinstance(e,ValueError) else type(e).__name__,file=sys.stderr)
        sys.exit(1)
