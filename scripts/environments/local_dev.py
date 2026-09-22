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
PG = 'pgvector/pgvector@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f'
KAFKA = 'dorosiya/pawbridge-debezium-connect@sha256:3fec7b3140917505648943bab214696fb7b30c103d2c4f6483143c1d8ff274a0'
REDIS = 'redis@sha256:ee64a64eaab618d88051c3ade8f6352d11531fcf79d9a4818b9b183d8c1d18ba'
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


def base(image_ref, memory, cpus='1.0'):
    return {'image': image_ref, 'pull_policy': 'missing', 'restart': 'no', 'mem_limit': memory,
            'cpus': cpus, 'pids_limit': 256, 'networks': ['dev'], 'security_opt': ['no-new-privileges:true'],
            'logging': {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}}


def health(command):
    return {'test': ['CMD-SHELL', command], 'interval': '5s', 'timeout': '5s', 'retries': 30, 'start_period': '30s'}


def app_env(name, host=False):
    db = '127.0.0.1:15433' if host else 'postgresql:5432'
    kafka = '127.0.0.1:19092' if host else 'kafka:9092'
    redis = '127.0.0.1' if host else 'redis'
    env = {'SPRING_PROFILES_ACTIVE': 'dev,postgresql', 'SPRING_KAFKA_BOOTSTRAP_SERVERS': kafka,
           'SPRING_DATA_REDIS_HOST': redis, 'SPRING_DATA_REDIS_PORT': '16379' if host else '6379',
           'REDIS_HOST': redis, 'REDIS_PORT': '16379' if host else '6379',
           'SPRING_DATA_REDIS_PASSWORD': '${DEV_REDIS_PASSWORD}', 'SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE': '3',
           'SPRING_DATASOURCE_HIKARI_MINIMUMIDLE': '0', 'SPRING_JPA_HIBERNATE_DDL_AUTO': 'validate',
           'SPRING_BATCH_JOB_ENABLED': 'false', 'SPRING_BATCH_JDBC_INITIALIZE_SCHEMA': 'never',
           'MANAGEMENT_TRACING_ENABLED': 'false', 'MANAGEMENT_HEALTH_MAIL_ENABLED': 'false',
           'JAVA_TOOL_OPTIONS': '-Xms64m -Xmx384m -Duser.timezone=Asia/Seoul',
           'JWT_SECRET': '${DEV_JWT_SECRET}', 'JWT_ACCESS_TOKEN_EXPIRATION': '3600000', 'JWT_REFRESH_TOKEN_EXPIRATION': '1209600000',
           'R2_ACCESS_KEY_ID': 'local-unconfigured', 'R2_SECRET_ACCESS_KEY': 'local-unconfigured', 'R2_REGION': 'auto',
           'R2_ENDPOINT': 'http://unconfigured.invalid', 'R2_BUCKET_NAME': 'pawbridge-dev-images', 'R2_PUBLIC_BASE_URL': 'http://unconfigured.invalid',
           'GOOGLE_CLIENT_ID': 'local-unconfigured', 'GOOGLE_SECRET_KEY': 'local-unconfigured',
           'GOOGLE_EMAIL': 'local@example.invalid', 'GOOGLE_EMAIL_SECRET_KEY': 'local-unconfigured',
           'SPRING_MAIL_HOST': '127.0.0.1' if host else 'mail',
           'SPRING_MAIL_PORT': '11025' if host else '1025',
           'SPRING_MAIL_PROPERTIES_MAIL_SMTP_AUTH': 'false',
           'SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_ENABLE': 'false',
           'SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_REQUIRED': 'false',
           'TOSS_SECRET_KEY': 'test_unconfigured',
           'APMS_API_BASE_URL': 'http://unconfigured.invalid', 'APMS_API_SERVICE_KEY': 'local-unconfigured',
           'APMS_PHOTO_ARCHIVE_ENABLED': 'false', 'TOURAPI_ENABLED': 'false', 'TOURAPI_SCHEDULE_ENABLED': 'false',
           'SHELTER_DIRECTORY_SCHEDULE_ENABLED': 'false', 'LOST_GALLERY_FEED_ENABLED': 'false',
           'LOST_SEARCH_PYTHON_URL': 'http://127.0.0.1:18091' if host else 'http://unconfigured.invalid:18091',
           'PYTHON_AI_SERVICE_INTERNAL_API_KEY': '${DEV_INTERNAL_API_KEY}', 'CHATBOT_IP_HASH_SECRET': '${DEV_INTERNAL_API_KEY}',
           'REDIRECT_URI': 'http://localhost:28080/login/oauth2/code/google', 'OAUTH2_REDIRECT_URI': 'http://localhost:5184/oauth/callback'}
    for target, port in [('user',8080),('animal',8081),('community',8082),('store',8083),('payment',8084)]:
        env[target.upper()+'_SERVICE_URL'] = 'http://127.0.0.1:'+str(PORTS[target+'-service']) if host else f'http://{target}-service:{port}'
    env['PYTHON_AI_SERVICE_URL'] = 'http://127.0.0.1:28087' if host else 'http://python-ai-service:8000'
    if name != 'api-gateway':
        short = name.removesuffix('-service'); upper=short.upper()
        env.update({upper+'_POSTGRESQL_JDBC_URL':'jdbc:postgresql://'+db+'/pawbridge', upper+'_POSTGRESQL_USERNAME':'pawbridge_dev_'+short+'_app', upper+'_POSTGRESQL_PASSWORD':'${DEV_'+upper+'_APP_PASSWORD}'})
    else:
        env['SPRING_PROFILES_ACTIVE']='dev'
        env['SPRING_APPLICATION_JSON']=json.dumps({'spring':{'cloud':{'gateway':{'globalcors':{'cors-configurations':{'[/**]':{'allowedOriginPatterns':['http://localhost:5184','http://127.0.0.1:5184'],'allowedMethods':['GET','POST','PUT','PATCH','DELETE','OPTIONS'],'allowedHeaders':['Authorization','Content-Type','Accept','x-user-id'],'allowCredentials':True}}}}}}})
    if host: env['SERVER_PORT']=str(PORTS[name])
    return env


def compose():
    pg={**base(PG,'768m'), 'ports':['127.0.0.1:15433:5432'], 'shm_size':'128m',
        'environment':{'POSTGRES_DB':'pawbridge','POSTGRES_PASSWORD':'${DEV_POSTGRES_PASSWORD:?Run prepare}', 'POSTGRES_INITDB_ARGS':'--auth-host=scram-sha-256'},
        'command':['postgres','-c','wal_level=logical','-c','max_replication_slots=10','-c','max_wal_senders=10','-c','max_connections=60','-c','shared_buffers=128MB','-c','max_slot_wal_keep_size=256MB'],
        'volumes':['pg-data:/var/lib/postgresql/data','./init.sql:/docker-entrypoint-initdb.d/010-dev.sql:ro'],
        'healthcheck':health('pg_isready -U postgres -d pawbridge')}
    redis={**base(REDIS,'256m'), 'ports':['127.0.0.1:16379:6379'], 'volumes':['redis-data:/data','./redis.conf:/usr/local/etc/redis/redis.conf:ro'],
        'command':['redis-server','/usr/local/etc/redis/redis.conf'], 'environment':{'REDISCLI_AUTH':'${DEV_REDIS_PASSWORD}'}, 'healthcheck':health('redis-cli ping | grep -q PONG')}
    kafka={**base(KAFKA,'1024m'), 'ports':['127.0.0.1:19092:19092'], 'volumes':['kafka-data:/var/lib/kafka','./kafka.properties:/tmp/kafka.properties:ro'],
        'environment':{'KAFKA_HEAP_OPTS':'-Xms256m -Xmx512m','LOG_DIR':'/tmp/kafka-logs','KAFKA_GC_LOG_OPTS':'-Xlog:gc=warning:stdout'},
        'command':['/bin/bash','-ec','/opt/kafka/bin/kafka-storage.sh format --ignore-formatted --standalone -t MDEyMzQ1Njc4OWFiY2RlZg -c /tmp/kafka.properties && exec /opt/kafka/bin/kafka-server-start.sh /tmp/kafka.properties'],
        'healthcheck':health('/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list >/dev/null')}
    connect={**base(KAFKA,'1024m'), 'profiles':['events'], 'ports':['127.0.0.1:18383:8083'],
        'volumes':['./connect.properties:/tmp/connect.properties:ro','./cdc.properties:/run/secrets/cdc.properties:ro'],
        'command':['/opt/kafka/bin/connect-distributed.sh','/tmp/connect.properties'], 'environment':{'KAFKA_HEAP_OPTS':'-Xms256m -Xmx512m','LOG_DIR':'/tmp/kafka-logs','KAFKA_GC_LOG_OPTS':'-Xlog:gc=warning:stdout'},
        'depends_on':{'postgresql':{'condition':'service_healthy'},'kafka':{'condition':'service_healthy'}}, 'healthcheck':health('curl -fsS http://localhost:8083/connector-plugins >/dev/null')}
    volume_init={**base(KAFKA,'64m'), 'user':'0:0', 'network_mode':'none',
        'volumes':['kafka-data:/var/lib/kafka'], 'command':['/bin/sh','-ec','mkdir -p /var/lib/kafka/data && chown 1001:0 /var/lib/kafka/data']}
    volume_init.pop('networks')
    kafka['depends_on']={'kafka-volume-init':{'condition':'service_completed_successfully'}}
    services={'postgresql':pg,'redis':redis,'kafka-volume-init':volume_init,'kafka':kafka,'connect':connect}
    services['mail']={**base('python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534','64m','0.25'),
        'user':'65534:65534','read_only':True,
        'ports':['127.0.0.1:18025:8025','127.0.0.1:11025:1025'],
        'command':['python','-B','/app/mail_sink.py'], 'volumes':['./mail_sink.py:/app/mail_sink.py:ro'],
        'healthcheck':health('true')}
    for name in ('api-gateway',*(s+'-service' for s in SERVICES)):
        port=8080 if name in ('api-gateway','user-service') else {'animal-service':8081,'community-service':8082,'store-service':8083,'payment-service':8084}[name]
        services[name]={**base(image(name),'640m'), 'profiles':['apps'], 'environment':app_env(name),
            'ports':[f'127.0.0.1:{PORTS[name]}:{port}'],
            'depends_on':{'postgresql':{'condition':'service_healthy'},'redis':{'condition':'service_healthy'},'kafka':{'condition':'service_healthy'}}}
    # CPU and photo servers are opt-in; no implicit GPU load or external LLM/R2 use.
    for name in ('photo-service','python-ai-service'):
        services[name]={**base(image(name),'512m'), 'profiles':['optional-ai'], 'ports':[f'127.0.0.1:{PORTS[name]}:8000'],
            'environment':{'INTERNAL_API_KEY':'${DEV_INTERNAL_API_KEY}','LOST_STORAGE_BACKEND':'postgresql','LOST_GALLERY_SYNC_ENABLED':'false','LOST_PG_DSN':'postgresql://pawbridge_dev_vector:${DEV_VECTOR_PASSWORD}@postgresql:5432/pawbridge'}}
    services['mail']['healthcheck']['test']=['CMD','python','-c',"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8025/health')"]
    services['user-service']['depends_on']['mail']={'condition':'service_healthy'}
    ports=[port for service in services.values() for port in service.pop('ports',[])]
    services['local-access']={**base('nginxinc/nginx-unprivileged@sha256:adf5042a17f4ecdd200c595fa9ffd1be37efb18f89a830bd1a00e4ab4d59d42c','64m','0.25'),
        'networks':['dev','access'], 'ports':[p.rsplit(':',1)[0]+':'+p.split(':')[1] for p in ports],
        'command':['nginx','-c','/etc/nginx/local-dev.conf','-g','daemon off;'],
        'volumes':['./access.conf:/etc/nginx/local-dev.conf:ro']}
    return {'name':PROJECT,'services':services,'networks':{'dev':{'internal':True},'access':{}},'volumes':{'pg-data':{},'redis-data':{},'kafka-data':{}}}


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
    write_private(state/'owner.json',json.dumps({'project':PROJECT,'runtime':'local-compose'}))
    write_private(state/'init.sql',sql_init(keys));write_private(state/'redis.conf','appendonly yes\nmaxmemory 128mb\nmaxmemory-policy noeviction\nrequirepass '+keys['DEV_REDIS_PASSWORD']+'\n')
    write_private(state/'cdc.properties',''.join(s+'.password='+keys['DEV_'+s.upper()+'_CDC_PASSWORD']+'\n' for s in SERVICES))
    for name in ('api-gateway',*(s+'-service' for s in SERVICES)):
        values=app_env(name,host=True)
        for k,v in values.items():
            values[k]=re.sub(r'\$\{([A-Z_]+)\}',lambda m:keys[m[1]],v)
        write_private(state/(name+'.env'),''.join(k+'='+v+'\n' for k,v in values.items()))
    for name in ('kafka.properties','connect.properties','access.conf','mail_sink.py'):
        write_private(state/name,(ROOT/'environments/dev/compose'/name).read_text())
    write_private(state/'compose.yaml',yaml.safe_dump(compose(),sort_keys=False))
    for name in ('init.sql','redis.conf','cdc.properties','kafka.properties','connect.properties','access.conf','mail_sink.py'):
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
    return subprocess.run([*docker(),'compose','--project-name',PROJECT,'--env-file',str(state/'.env'),'-f',str(state/'compose.yaml'),*args],check=True,timeout=900,**kwargs)


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
