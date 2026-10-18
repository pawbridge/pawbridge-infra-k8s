"""Build an operating service bundle without activating it.

An isolated verification uses the same app images/configuration with fresh secrets,
no egress/host ports, stub providers and restart=no. Public cutover is separate.
"""
import copy,json,re,os,uuid
from pathlib import Path
import mac_compose_operating as prep

SERVICES=('postgres','redis','kafka','connect','api-gateway','user-service','animal-service','community-service','store-service','payment-service','photo-service','python-ai-service','native-relay')
APP_PORTS={'api-gateway':8080,'user-service':8080,'animal-service':8081,'community-service':8082,'store-service':8083,'payment-service':8084,'photo-service':8000,'python-ai-service':8000}
ROOT=prep.candidate.ROOT
ASSETS=ROOT/'environments/mac/operating'
MODE_FLAGS={
 'standby':{'cleanup':'false','refresh':'false','cart':'false','projection':'false','collection':'false'},
 'drain':{'cleanup':'false','refresh':'false','cart':'true','projection':'true','collection':'false'},
 'active':{'cleanup':'true','refresh':'true','cart':'true','projection':'true','collection':'true'},
}

def environments(values,inputs,mode='standby'):
    prep.validate_values(values)
    prep.external.require(mode in MODE_FLAGS,'Unknown scheduling mode')
    f=MODE_FLAGS[mode];env=prep.fragments(values)
    for svc in APP_PORTS:
        if svc in ('photo-service','python-ai-service'):continue
        # Collection session locks consume connections alongside APMS worker transactions.
        # Keep Animal at its source default; idle=0 avoids reserving all ten connections.
        maximum_pool_size='10' if svc=='animal-service' else '3'
        env[svc].update(SPRING_PROFILES_ACTIVE='dev' if svc=='api-gateway' else 'dev,postgresql',
            JAVA_TOOL_OPTIONS='-Xms32m -Xmx256m -XX:ActiveProcessorCount=2 -Duser.timezone=Asia/Seoul',
            SPRING_KAFKA_BOOTSTRAP_SERVERS='kafka:9092',SPRING_KAFKA_ADMIN_AUTO_CREATE='false',
            SPRING_KAFKA_CONSUMER_AUTO_OFFSET_RESET='earliest',
            SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE=maximum_pool_size,SPRING_DATASOURCE_HIKARI_MINIMUMIDLE='0',
            SPRING_JPA_SHOW_SQL='false',LOGGING_LEVEL_ORG_HIBERNATE_SQL='OFF',
            MANAGEMENT_TRACING_ENABLED='false',MANAGEMENT_HEALTH_MAIL_ENABLED='false',
            MANAGEMENT_ENDPOINTS_WEB_EXPOSURE_INCLUDE='health,info,scheduledtasks',
            SERVER_SHUTDOWN='graceful',SPRING_LIFECYCLE_TIMEOUT_PER_SHUTDOWN_PHASE='360s',
            PAWBRIDGE_JOBS_CLEANUP_ENABLED=f['cleanup'],PAWBRIDGE_JOBS_REFRESH_ENABLED=f['refresh'],
            PAWBRIDGE_JOBS_CART_SYNC_ENABLED=f['cart'])
        if svc!='api-gateway':
            prefix=svc.split('-')[0].upper()+'_POSTGRESQL_'
            env[svc].update({prefix+'POOL_MAX':maximum_pool_size,prefix+'POOL_MIN':'0',prefix+'MAX_POOL_SIZE':maximum_pool_size,prefix+'MIN_IDLE':'0'})
    routes={'USER_SERVICE_URL':'http://user-service:8080','ANIMAL_SERVICE_URL':'http://animal-service:8081',
        'COMMUNITY_SERVICE_URL':'http://community-service:8082','COMMUNITY_CHAT_WEBSOCKET_URL':'ws://community-service:8082',
        'STORE_SERVICE_URL':'http://store-service:8083','PAYMENT_SERVICE_URL':'http://payment-service:8084','PYTHON_AI_SERVICE_URL':'http://python-ai-service:8000'}
    env['api-gateway'].update(routes)
    for svc in ('animal-service','community-service','store-service','payment-service'):
        env[svc].update(USER_SERVICE_URL=routes['USER_SERVICE_URL'])
    origins=['https://www.pawbridge.kr','https://pawbridge.kr']
    env['api-gateway']['SPRING_APPLICATION_JSON']=json.dumps({'spring':{'cloud':{'gateway':{'globalcors':{'cors-configurations':{'[/**]':{
        'allowedOriginPatterns':origins,'allowedMethods':['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
        'allowedHeaders':['Authorization','Content-Type','Accept','x-user-id'],'allowCredentials':True}}}}}}},separators=(',',':'))
    env['animal-service'].update(PYTHON_AI_SERVICE_URL=routes['PYTHON_AI_SERVICE_URL'],
        LOST_SEARCH_PYTHON_URL='http://native-relay:18091',APMS_PHOTO_OPTIMIZER_URL='http://photo-service:8000',
        APMS_BATCH_SCHEDULE_ENABLED=f['collection'],APMS_BATCH_COLLECTION_CRON='0 */30 * * * *',
        APMS_PHOTO_ARCHIVE_ENABLED=f['collection'],APMS_PHOTO_ARCHIVE_INTERVAL_MS='60000',
        TOURAPI_ENABLED='true',TOURAPI_SCHEDULE_ENABLED=f['collection'],SHELTER_DIRECTORY_SCHEDULE_ENABLED='false',
        PAWBRIDGE_ANIMAL_QUERY_PROJECTION_SCHEDULE_ENABLED=f['projection'],
        PAWBRIDGE_SHELTER_OBSERVATIONS_CRON='0 0 4 * * *' if mode=='active' else '-',
        LOST_GALLERY_FEED_ENABLED='true',LOST_GALLERY_FEED_INTERNAL_API_KEY=values['internal_api'])
    env['community-service'].update(MEMBER_CHAT_ENABLED='true' if mode=='active' else 'false',
        DAILY_VISITORS_ENABLED='true' if mode=='active' else 'false',
        MEMBER_CHAT_NAMESPACE='pawbridge',MEMBER_CHAT_ALLOWED_ORIGINS=','.join(origins),
        DAILY_VISITORS_ALLOWED_ORIGINS=','.join(origins),SEARCH_REBUILD_ENABLED=f['projection'],
        R2_OBJECT_PREFIX='')
    env['store-service'].update(SEARCH_REBUILD_ENABLED=f['projection'])
    env['user-service'].update(SPRING_MAIL_HOST='smtp.gmail.com',SPRING_MAIL_PORT='587',
        JWT_ACCESS_TOKEN_EXPIRATION='3600000',JWT_REFRESH_TOKEN_EXPIRATION='1209600000',
        ANIMAL_SERVICE_URL=routes['ANIMAL_SERVICE_URL'],STORE_SERVICE_URL=routes['STORE_SERVICE_URL'],
        COMMUNITY_SERVICE_URL=routes['COMMUNITY_SERVICE_URL'],
        SPRING_MAIL_PROPERTIES_MAIL_SMTP_AUTH='true',SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_ENABLE='true',
        SPRING_MAIL_PROPERTIES_MAIL_SMTP_STARTTLS_REQUIRED='true',SPRING_MAIL_PROPERTIES_MAIL_SMTP_SSL_CHECKSERVERIDENTITY='true')
    env['payment-service']['STORE_SERVICE_URL']=routes['STORE_SERVICE_URL']
    env['python-ai-service'].update(LLM_PROVIDER='gemini',LOST_STORAGE_BACKEND='postgresql',
        LOST_PG_DSN='postgresql://pawbridge_vector:'+values['roles']['pawbridge_vector']+'@postgres:5432/pawbridge',LOST_GALLERY_SYNC_ENABLED='false')
    for svc in APP_PORTS:
        for group in prep.external.SERVICE_INPUTS.get(svc,()):
            env[svc].update({k:v for k,v in inputs[group].items() if not k.startswith('VITE_')})
    originals=inputs['r2-originals']
    env['animal-service'].update({
        'APMS_PHOTO_ARCHIVE_'+target:originals['R2_'+source]
        for target,source in [('ENDPOINT','ENDPOINT'),('REGION','REGION'),('BUCKET','BUCKET_NAME'),
                              ('ACCESS_KEY_ID','ACCESS_KEY_ID'),('SECRET_ACCESS_KEY','SECRET_ACCESS_KEY')]})
    env['animal-service'].update({
        'LOST_GALLERY_FEED_STORAGE_'+target:originals['R2_'+source]
        for target,source in [('ENDPOINT','ENDPOINT'),('ACCESS_KEY_ID','ACCESS_KEY_ID'),('SECRET_ACCESS_KEY','SECRET_ACCESS_KEY')]})
    env['postgres']={'PGDATA':'/var/lib/postgresql/data/pgdata'}
    env['redis']={}
    env['kafka']={'KAFKA_HEAP_OPTS':'-Xms128m -Xmx256m','LOG_DIR':'/tmp/kafka-logs'}
    env['connect']={'KAFKA_HEAP_OPTS':'-Xms128m -Xmx384m','LOG_DIR':'/tmp/connect-logs'}
    env['native-relay']={}
    env['native-ai'].update(LOST_STORAGE_BACKEND='postgresql',LOST_SEARCH_DEVICE='mps',
        LOST_PG_DSN='postgresql://pawbridge_vector:'+values['roles']['pawbridge_vector']+'@127.0.0.1:35436/pawbridge',
        LOST_GALLERY_SYNC_ENABLED='false',LOST_PG_POOL_MAX_SIZE='2',
        LOST_SEARCH_VISUAL_PROFILE='sam3-animal-focus',LOST_SEARCH_INDEX='animals-lost-dinov3-sam3-delivery-v1')
    return env


def connector_configs():
    doc=json.loads((ROOT/'environments/mac/compose/connectors.json').read_text())
    for svc,cfg in doc.items():
        cfg.update(name='pawbridge-mac-'+svc)
        cfg.update({'database.user':'pawbridge_'+svc+'_cdc','slot.name':'pawbridge_mac_'+svc+'_outbox',
            'publication.name':'pawbridge_'+svc+'_outbox','snapshot.mode':'no_data','publication.autocreate.mode':'disabled','slot.drop.on.stop':'false'})
    return doc


def topics():
    doc=json.loads((ROOT/'environments/mac/compose/topics.json').read_text())
    for topic in doc['topics']:topic['name']=topic['name'].replace('mac-rehearsal-connect','pawbridge-mac-connect')
    if not any(t['name']=='payment' for t in doc['topics']):
        doc['topics'].append({'name':'payment','partitions':1,'replicationFactor':1,'config':{'cleanup.policy':'delete','retention.ms':-1}})
    return doc


def compose(images,isolated=False):
    specs={}
    for svc in SERVICES:
        key={'postgres':'MAC_IMAGE_POSTGRES','redis':'MAC_IMAGE_REDIS','kafka':'MAC_IMAGE_EVENTS','connect':'MAC_IMAGE_EVENTS','native-relay':'MAC_IMAGE_ACCESS'}.get(svc,'MAC_IMAGE_'+svc.upper().replace('-','_'))
        mem={'postgres':1024,'redis':512,'kafka':640,'connect':768,'native-relay':64}.get(svc,512 if svc in ('photo-service','python-ai-service') else 640)*1024**2
        spec={'image':images[key],'platform':'linux/arm64','pull_policy':'never','container_name':'${OPERATING_PROJECT}-'+svc,
            'restart':'no' if isolated else 'unless-stopped','read_only':True,'user':'${OPERATING_UID}:${OPERATING_GID}',
            'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'mem_limit':mem,'memswap_limit':mem,'cpus':1.0,'pids_limit':256,
            'stop_grace_period':'400s' if svc=='animal-service' else '60s',
            'labels':{'com.pawbridge.operating-owner':'${OPERATING_OWNER}'},
            'env_file':[{'path':'${OPERATING_SECRETS}/'+svc+'.env','format':'raw'}],
            'networks':['private'],'tmpfs':['/tmp:rw,nosuid,size=64m'],
            'logging':{'driver':'json-file','options':{'max-size':'10m','max-file':'3'}}}
        def mount(source,target,readonly=True):
            spec.setdefault('volumes',[]).append({'type':'bind','source':source,'target':target,'read_only':readonly,'bind':{'create_host_path':False}})
        if svc=='postgres':
            mount('${OPERATING_STATE}/postgres','/var/lib/postgresql/data',False)
            mount('${OPERATING_STATE}/config/pg_hba.conf','/etc/postgresql/pg_hba.conf')
            spec['tmpfs'].append('/var/run/postgresql:rw,nosuid,size=8m,mode=0777')
            spec['entrypoint']=['postgres'];spec['command']=['-c','listen_addresses=*','-c','wal_level=logical','-c','max_replication_slots=5',
                '-c','max_wal_senders=8','-c','max_slot_wal_keep_size=512MB','-c','shared_buffers=128MB','-c','max_connections=80',
                '-c','work_mem=4MB','-c','temp_file_limit=256MB','-c','autovacuum=on','-c','log_statement=none','-c','log_min_error_statement=panic',
                '-c','hba_file=/etc/postgresql/pg_hba.conf']
            spec['healthcheck']={'test':['CMD','pg_isready','-U','postgres','-d','pawbridge'],'interval':'10s','timeout':'3s','retries':6}
            if not isolated:spec['ports']=['127.0.0.1:35436:5432']
        elif svc=='redis':
            mount('${OPERATING_STATE}/redis','/data',False);mount('${OPERATING_SECRETS}/redis.conf','/redis.conf')
            spec['entrypoint']=['redis-server'];spec['command']=['/redis.conf']
        elif svc=='kafka':
            mount('${OPERATING_STATE}/kafka','/var/lib/kafka',False);mount('${OPERATING_STATE}/config/kafka.properties','/config/kafka.properties')
            spec['entrypoint']=['/opt/kafka/bin/kafka-server-start.sh'];spec['command']=['/config/kafka.properties']
        elif svc=='connect':
            mount('${OPERATING_STATE}/config/connect.properties','/config/connect.properties');mount('${OPERATING_SECRETS}/cdc.properties','/run/secrets/cdc.properties')
            spec['entrypoint']=['/opt/kafka/bin/connect-distributed.sh'];spec['command']=['/config/connect.properties']
            spec['healthcheck']={'test':['CMD','curl','-fsS','http://localhost:8083/'],'interval':'15s','timeout':'4s','retries':6}
        elif svc=='native-relay':
            mount('${OPERATING_STATE}/config/native-relay.conf','/etc/nginx/nginx.conf')
            spec['entrypoint']=['nginx'];spec['command']=['-g','daemon off;']
            if not isolated:spec['networks'].append('egress')
        elif svc not in ('photo-service','python-ai-service'):
            spec['entrypoint']=['java','-jar','/app.jar'];spec['command']=[]
            if svc=='api-gateway' and not isolated:spec['ports']=['127.0.0.1:38300:8080']
        else:
            spec['command']=['uvicorn','app.photo_main:app' if svc=='photo-service' else 'app.main:app',
                '--host','0.0.0.0','--port','8000','--workers','1','--limit-concurrency','4','--backlog','8','--no-access-log']
        if not isolated and svc in APP_PORTS:spec['networks'].append('egress')
        specs[svc]=spec
    return {'name':'${OPERATING_PROJECT}','services':specs,'networks':{'private':{'internal':True},**({} if isolated else {'egress':{}})}}


def validate(doc,isolated):
    prep.external.require(set(doc['services'])==set(SERVICES),'Service inventory differs')
    for name,s in doc['services'].items():
        prep.external.require(re.fullmatch('sha256:[a-f0-9]{64}',s['image']) and s['pull_policy']=='never','Image not pinned')
        prep.external.require(s['read_only'] and s['cap_drop']==['ALL'] and s['security_opt']==['no-new-privileges:true'],'Unsafe container rights')
        prep.external.require(not any(k in s for k in ('privileged','network_mode','devices','volumes_from')),'Unsafe runtime override')
        prep.external.require(s['user']=='${OPERATING_UID}:${OPERATING_GID}','Unexpected runtime user')
        prep.external.require(0<s['mem_limit']<=2*1024**3 and s['memswap_limit']==s['mem_limit'] and 0<s['cpus']<=2 and 0<s['pids_limit']<=256,'Resource limits required')
        prep.external.require(s['env_file']==[{'path':'${OPERATING_SECRETS}/'+name+'.env','format':'raw'}],'Unexpected env source')
        for m in s.get('volumes',[]):
            prep.external.require(m['source'].startswith(('${OPERATING_STATE}/','${OPERATING_SECRETS}/')) and '..' not in Path(m['source']).parts,'Mount escapes owned roots')
            prep.external.require(m.get('bind',{}).get('create_host_path') is False,'Missing host path creation guard')
            if m['source'].startswith('${OPERATING_SECRETS}/'):prep.external.require(m['read_only'],'Secret mount must be read only')
        if isolated:prep.external.require(not s.get('ports') and s['networks']==['private'] and s['restart']=='no','Isolation changed')
        else:
            expected=['private','egress'] if name in APP_PORTS or name=='native-relay' else ['private']
            prep.external.require(s['networks']==expected and s['restart']=='unless-stopped','Unexpected operating networks or restart policy')
            expected_ports={'postgres':['127.0.0.1:35436:5432'],'api-gateway':['127.0.0.1:38300:8080']}.get(name,[])
            prep.external.require(s.get('ports',[])==expected_ports,'Unexpected published ports')
    prep.external.require(doc['networks']['private']['internal'] is True,'Private data network required')
    if isolated:prep.external.require(set(doc['networks'])=={'private'},'External network forbidden')
    return True


def configuration_files(isolated=False):
    kafka=(ROOT/'environments/mac/compose/kafka.properties').read_text()
    kafka=kafka.replace(',EXTERNAL://:39092','').replace(',EXTERNAL://127.0.0.1:39092','').replace(',EXTERNAL:PLAINTEXT','')
    connect=(ROOT/'environments/mac/compose/connect.properties').read_text()
    connect=connect.replace('pawbridge-mac-rehearsal-connect','pawbridge-mac-connect').replace('mac-rehearsal-connect','pawbridge-mac-connect')
    connect+='offset.flush.interval.ms=1000\n'
    # The isolated variant cannot reach the existing native host process.
    nginx='pid /tmp/nginx.pid;\nerror_log /dev/stderr warn;\nevents { worker_connections 128; }\n'
    if isolated:
        nginx+='http { access_log off; client_body_temp_path /tmp/body; proxy_temp_path /tmp/proxy; fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi; server { listen 18091; return 503; } }\n'
    else:
        nginx+='stream { resolver 127.0.0.11 valid=10s ipv6=off; server { listen 18091; set $upstream host.docker.internal:8001; proxy_connect_timeout 3s; proxy_timeout 120s; proxy_pass $upstream; } }\n'
    roles=','.join(role for role in prep.ROLES if role!='postgres')
    hba='local all all scram-sha-256\nhost all postgres 127.0.0.1/32 scram-sha-256\nhost all postgres ::1/128 scram-sha-256\n'
    hba+='host pawbridge '+roles+' all scram-sha-256\n'
    return {'kafka.properties':kafka,'connect.properties':connect,'native-relay.conf':nginx,'pg_hba.conf':hba}


def prepared_values(directory):
    external=prep.external
    external.require(os.getuid()!=0,'Root host preparation refused')
    external.private(directory,True)
    receipt=json.loads(external.private(directory/'receipt.json').read_text())
    external.require(receipt['activationAllowed'] is False and receipt['dbPasswordsApplied'] is False,'Only inactive prepared credentials accepted')
    for name,digest in receipt['files'].items():
        external.require(Path(name).name==name,'Unexpected credential path')
        external.require(prep.candidate.digest(external.private(directory/name))==digest,'Prepared credential input changed')
    def fragment(svc):return external.parse_env(external.private(directory/(svc+'.env.fragment')).read_text(),svc)
    animal=fragment('animal-service');community=fragment('community-service')
    values={'roles':json.loads(external.private(directory/'db-role-credentials.json').read_text()),
        'jwt':animal['JWT_SECRET'],'redis':external.private(directory/'redis-password.txt').read_text().strip(),
        'internal_api':animal['PYTHON_AI_SERVICE_INTERNAL_API_KEY'],'chatbot_ip_hash':animal['CHATBOT_IP_HASH_SECRET'],
        'visitors_signing':community['DAILY_VISITORS_SIGNING_KEY']}
    prep.validate_values(values)
    return values


def operating_compose(images):
    """Current operating storage contract; compose() remains the base topology builder."""
    import mac_operating_storage
    import mac_operating_names
    doc=compose(images,False);validate(doc,False)
    return mac_operating_names.configure(mac_operating_storage.configure(doc,images['MAC_IMAGE_PYTHON_AI_SERVICE']))


def prepare_bundle(inputs,credentials,parent,images):
    """Write only inactive private files. Does not call Docker, SQL or a provider."""
    external=prep.external
    expected=Path.home()/'Documents/msa-projects/pawbridge-secrets/runtime/production'
    external.require(parent==expected and parent.resolve()==parent,'Unexpected operating output root')
    external.private(parent,True)
    values=prepared_values(credentials);supplied=external.load_inputs(inputs)
    hashes={n:prep.candidate.digest(external.private(inputs/(n+'.env'))) for n in external.SCHEMA}
    doc=operating_compose(images)
    output=parent/('operating-'+uuid.uuid4().hex[:12]);output.mkdir(mode=0o700)
    write=lambda p,text:external.write_private(p,text)
    for d in ('state','state/config','modes'):(output/d).mkdir(mode=0o700)
    for name,content in configuration_files().items():write(output/'state/config'/name,content)
    for mode in MODE_FLAGS:
        dest=output/'modes'/mode;dest.mkdir(mode=0o700)
        envs=environments(values,supplied,mode)
        for svc,env in envs.items():write(dest/(svc+'.env'),''.join(k+'='+v+'\n' for k,v in sorted(env.items())))
        write(dest/'cdc.properties',''.join(s+'.password='+values['roles']['pawbridge_'+s+'_cdc']+'\n' for s in prep.APPS))
        write(dest/'redis.conf','bind 0.0.0.0\nprotected-mode yes\nport 6379\nrequirepass '+values['redis']+'\ndir /data\nappendonly yes\nappendfsync everysec\nsave 900 1\nmaxmemory 384mb\nmaxmemory-policy noeviction\n')
    write(output/'db-role-credentials.json',json.dumps(values['roles'],indent=2)+'\n')
    write(output/'compose.json',json.dumps(doc,indent=2)+'\n')
    write(output/'connectors.json',json.dumps(connector_configs(),indent=2)+'\n')
    write(output/'topics.json',json.dumps(topics(),indent=2)+'\n')
    write(output/'compose.env','OPERATING_PROJECT=pawbridge-prod\nOPERATING_OWNER='+uuid.uuid4().hex+'\nOPERATING_UID='+str(os.getuid())+'\nOPERATING_GID='+str(os.getgid())+'\nOPERATING_STATE='+str(output/'state')+'\nOPERATING_SECRETS='+str(output/'modes/standby')+'\nBACKGROUND_SCRIPTS='+str(ROOT/'scripts/environments')+'\n')
    external.require(hashes=={n:prep.candidate.digest(inputs/(n+'.env')) for n in external.SCHEMA},'External inputs changed during render')
    receipt={'purpose':'inactive-operating-bundle','activationAllowed':False,'dbPasswordsApplied':False,
        'credentialSource':str(credentials),'externalInputHashes':hashes,'images':images,'defaultMode':'standby',
        'remainingActivationGates':['final Windows write/consumer barrier and new final dump','fresh external Linux data volumes, empty-only storage-init, then DB restore and reviewed original role mapping',
            'fresh Kafka storage and slot/offset gate before allowing writers','native gallery writer, permanent state and sync launcher validation',
            'non-pruning scheduled encrypted backup and monitoring','public ingress single-origin approval'],
        'files':{str(p.relative_to(output)):prep.candidate.digest(p) for p in output.rglob('*') if p.is_file()}}
    write(output/'receipt.json',json.dumps(receipt,indent=2)+'\n')
    write(output/'README.md','# 미적용 Mac 운영 구성\n\n기본은 standby 예약 모드이며 컨테이너·DB·Tunnel을 시작하지 않았습니다.\n'
          'standby는 읽기 전용이 아닙니다. API와 Kafka 소비자는 살아 있으므로 외부 진입·수동 실행을 별도로 차단해야 합니다.\n'
          'modes/active를 현재 서비스에 연결하지 마세요. receipt.json의 남은 전환 조건을 먼저 충족해야 합니다.\n'
          'native-ai.env는 연결 설정이며 모델 경로·운영 갤러리 writer/launcher 검증까지 끝난 실행기는 아닙니다.\n')
    return output,receipt
