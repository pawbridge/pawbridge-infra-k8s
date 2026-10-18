#!/usr/bin/env python3
"""Prepare inactive Mac credential/config fragments. No runtime/SQL/Git calls.

These are NOT a deployable environment: unresolved schedules, final Redis state,
CDC and native gallery contracts must be resolved in a separate execution step.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import secrets as random

import mac_compose_candidate as candidate
import mac_compose_secrets as external

APPS = ('animal', 'user', 'community', 'store', 'payment')
ROLES = ('postgres', 'pawbridge_vector') + tuple('pawbridge_'+s+'_'+kind for s in APPS for kind in ('app','cdc'))
ASSETS = candidate.ROOT / 'environments/mac/operating'
# No trial values are inherited. Only reviewed key names are emitted.
PUBLIC = {'api': 'https://api.pawbridge.kr', 'frontend': 'https://www.pawbridge.kr'}


def fresh_values():
    return {'roles': {role: random.token_hex(32) for role in ROLES},
            'jwt': random.token_urlsafe(64), 'redis': random.token_hex(32),
            'internal_api': random.token_hex(32), 'chatbot_ip_hash': random.token_hex(32),
            'visitors_signing': random.token_hex(32)}


def fragments(values):
    result = {}
    for svc in APPS:
        prefix=svc.upper()+'_POSTGRESQL_'
        result[svc+'-service'] = {
            prefix+'JDBC_URL':'jdbc:postgresql://postgres:5432/pawbridge',
            prefix+'USERNAME':'pawbridge_'+svc+'_app',
            prefix+'PASSWORD':values['roles']['pawbridge_'+svc+'_app'],
            'SPRING_DATA_REDIS_HOST':'redis', 'SPRING_DATA_REDIS_PORT':'6379',
            'SPRING_DATA_REDIS_PASSWORD':values['redis'],
            'JWT_SECRET':values['jwt'],
            'SPRING_FLYWAY_ENABLED':'false', 'SPRING_SQL_INIT_MODE':'never',
            'SPRING_JPA_HIBERNATE_DDL_AUTO':'validate',
            'SPRING_BATCH_JOB_ENABLED':'false', 'SPRING_BATCH_JDBC_INITIALIZE_SCHEMA':'never',
        }
    result['api-gateway']={'JWT_SECRET':values['jwt'],'SPRING_DATA_REDIS_HOST':'redis',
        'SPRING_DATA_REDIS_PORT':'6379','SPRING_DATA_REDIS_PASSWORD':values['redis']}
    result['animal-service'].update(PYTHON_AI_SERVICE_INTERNAL_API_KEY=values['internal_api'],
        APMS_PHOTO_OPTIMIZER_INTERNAL_API_KEY=values['internal_api'],CHATBOT_IP_HASH_SECRET=values['chatbot_ip_hash'],
        APMS_BATCH_SCHEDULE_ENABLED='false',APMS_PHOTO_ARCHIVE_ENABLED='false',
        TOURAPI_SCHEDULE_ENABLED='false',SHELTER_DIRECTORY_SCHEDULE_ENABLED='false')
    result['user-service'].update(REDIRECT_URI=PUBLIC['api']+'/login/oauth2/code/google',
        OAUTH2_REDIRECT_URI=PUBLIC['frontend']+'/oauth/callback')
    result['community-service'].update(DAILY_VISITORS_SIGNING_KEY=values['visitors_signing'])
    result['photo-service']={'INTERNAL_API_KEY':values['internal_api']}
    result['python-ai-service']={'INTERNAL_API_KEY':values['internal_api'],'LOST_GALLERY_SYNC_ENABLED':'false'}
    result['native-ai']={'INTERNAL_API_KEY':values['internal_api'],'LOST_GALLERY_SYNC_ENABLED':'false'}
    return result


def validate_values(values):
    external.require(set(values['roles'])==set(ROLES),'Role mapping differs')
    tokens=list(values['roles'].values())+[values[k] for k in ('jwt','redis','internal_api','chatbot_ip_hash','visitors_signing')]
    external.require(len(set(tokens))==len(tokens),'Internal values must be distinct')
    external.require(all(re.fullmatch(r'[A-Za-z0-9_-]{43,}',t) for t in tokens),'Invalid generated internal value')


def prepare(inputs, parent, source_roles):
    external.private(parent,True)
    external.require(parent.resolve()==parent,'Symlink parent chain refused')
    external.private(source_roles)
    external.require(parent == Path.home()/'Documents/msa-projects/pawbridge-secrets/runtime/production',
                     'Only the approved private production preparation root is allowed')
    expected='0cda92f414c34e712b3aad9253412a3a94b28ce76ebda4b601ff680c73ac6270'
    external.require(hashlib.sha256(source_roles.read_bytes()).hexdigest()==expected,'Role source differs from verified backup')
    text=source_roles.read_text()
    login=set(re.findall(r'^ALTER ROLE (\w+) WITH (?=[^\n;]*\bLOGIN\b)',text,re.M))
    external.require(set(ROLES)==login,'Original login roles differ; refusing role rename')
    _,release=candidate.load_current()
    schedule_doc=json.loads((ASSETS/'schedules.json').read_text())
    # Validate external source before generating any credentials or directories.
    source_hashes={n: candidate.digest(external.private(inputs/(n+'.env'))) for n in external.SCHEMA}
    supplied=external.load_inputs(inputs)
    values=fresh_values();validate_values(values)
    data=fragments(values)
    for name in data:
        for group in external.SERVICE_INPUTS.get(name,()):
            data[name].update({k:v for k,v in supplied[group].items() if not k.startswith('VITE_')})
    # No original/archive R2 credentials, administrator password or CDC credentials in app fragments.
    for name,env in data.items():
        external.require(values['roles']['postgres'] not in env.values(),'Admin credential in app fragment')
    external.require(source_hashes=={n:candidate.digest(inputs/(n+'.env')) for n in external.SCHEMA},'External inputs changed')
    run='prepared-'+random.token_hex(8)
    output=parent/run
    output.mkdir(mode=0o700)
    write=lambda name,text:external.write_private(output/name,text)
    for name,env in data.items():
        write(name+'.env.fragment',''.join(k+'='+v+'\n' for k,v in sorted(env.items())))
    write('db-role-credentials.json',json.dumps(values['roles'],indent=2)+'\n')
    write('redis-password.txt',values['redis']+'\n')
    write('cdc.properties',''.join(s+'.password='+values['roles']['pawbridge_'+s+'_cdc']+'\n' for s in APPS))
    write('frontend.env.fragment','VITE_API_BASE_URL='+PUBLIC['api']+'\nVITE_TOSS_CLIENT_KEY='+supplied['toss-test']['VITE_TOSS_CLIENT_KEY']+'\n')
    blocked=[x['id'] for x in schedule_doc['jobs'] if x['control']=='unavailable']
    receipt={'purpose':'inactive-operating-preparation','activationAllowed':False,
        'candidateSelectionSha256':candidate.digest(candidate.ASSETS/'current.json'),
        'images':release['images'],'externalInputHashes':source_hashes,
        'schedulerInventorySha256':candidate.digest(ASSETS/'schedules.json'),
        'paymentMode':'test-only','publicEndpointsVerifiedLive':False,
        'originalRoleSourceSha256':expected,'roleNames':list(ROLES),
        'preserveNoLoginOwners':True,'dbPasswordsApplied':False,
        'requiredBeforeActivation':['Windows current inventory','complete operating Compose and env contract',
         'Redis business-state handoff and cart verification','per-job standby/drain/active controls',
         'native gallery writer/launcher','TCP authentication and permission tests',
         'final data, CDC barrier and public cutover approval'],
        'uncontrolledScheduleIds':blocked,
        'files':{p.name:candidate.digest(p) for p in output.iterdir() if p.is_file()}}
    write('receipt.json',json.dumps(receipt,indent=2)+'\n')
    write('README.md','# 미적용 운영 설정 준비본\n\n실행 가능한 Compose가 아닙니다. *.env.fragment를 현재 서비스에 연결하지 마세요.\n'
         '새 DB 비밀번호는 파일로만 생성했으며 어떤 DB에도 적용하지 않았습니다. NOLOGIN owner/ACL은 변경하지 않습니다.\n'
         '외부 입력 원본은 보존했습니다. 내부 값은 서버용이며 프런트에는 공개 Toss 키만 전달합니다.\n'
         '백업 R2·원본 사진 R2 키는 아직 배포하지 않았습니다. 예약·Redis·CDC·native·최종 공개 전환 검증이 남아 있습니다.\n')
    return output,receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--parent',type=Path,required=True)
    parser.add_argument('--roles',type=Path,required=True)
    args=parser.parse_args()
    try:
        path,receipt=prepare(args.inputs,args.parent,args.roles)
    except Exception:
        raise SystemExit('Preparation refused; no secret values or raw exception printed') from None
    print('Inactive configuration prepared:',path)
    print('Database changes: 0; runtime changes: 0; activation allowed: false')
