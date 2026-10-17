#!/usr/bin/env python3
"""Validate the pinned full-stack candidate. This module never starts a runtime."""
import argparse,hashlib,json,os,re
from pathlib import Path
import mac_compose as guard
ROOT=Path(__file__).resolve().parents[2]
ASSETS=ROOT/'environments/mac/candidate'
SERVICES={'postgres','redis','kafka','connect','mail','access','api-gateway','user-service','animal-service','community-service','store-service','payment-service','photo-service','python-ai-service'}
PORTS={35435,38225,38283,*range(38200,38208)}
VARIABLES={'CANDIDATE_STATE','CANDIDATE_PROJECT','CANDIDATE_OWNER','CANDIDATE_UID','CANDIDATE_GID'}
def require(ok,message):
    if not ok:raise guard.GuardError(message)
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def validate_template(doc,release):
    version=release.get('version',1)
    require(version in (1,2),'Unknown candidate release version')
    if version==2:
        require(release.get('scheduler')=={'kind':'in-process','enabled':False,'cron':'0 */30 * * * *','zone':'Asia/Seoul'},'Candidate scheduler activation is not approved')
    require(set(doc.get('services',{}))==SERVICES,'Candidate service inventory differs')
    require(doc.get('name')=='${CANDIDATE_PROJECT}','Candidate project differs')
    require(doc.get('networks')=={'isolated':{'name':'${CANDIDATE_PROJECT}','internal':True,'labels':{'com.pawbridge.candidate-owner':'${CANDIDATE_OWNER}'}},'egress-relay':{'name':'${CANDIDATE_PROJECT}-relay','labels':{'com.pawbridge.candidate-owner':'${CANDIDATE_OWNER}'}}},'Candidate network boundary differs')
    require(doc.get('volumes')=={'postgres':{'name':'${CANDIDATE_PROJECT}-pg','external':True},'kafka':{'name':'${CANDIDATE_PROJECT}-kafka','external':True}},'Candidate volume boundary differs')
    for name,s in doc['services'].items():
        key={'postgres':'MAC_IMAGE_POSTGRES','redis':'MAC_IMAGE_REDIS','kafka':'MAC_IMAGE_EVENTS','connect':'MAC_IMAGE_EVENTS','mail':'MAC_IMAGE_MAIL','access':'MAC_IMAGE_ACCESS'}.get(name,'MAC_IMAGE_'+name.upper().replace('-','_'))
        require(s.get('image')==release['images'][key] and re.fullmatch(r'sha256:[a-f0-9]{64}',s['image']),'Unpinned candidate image')
        require(s.get('platform')=='linux/arm64' and s.get('pull_policy')=='never','Candidate architecture or pull policy differs')
        require(s.get('container_name')=='${CANDIDATE_PROJECT}-'+name,'Candidate name differs')
        require(s.get('restart')=='no' and s.get('read_only') is True and s.get('user')=='${CANDIDATE_UID}:${CANDIDATE_GID}','Candidate runtime identity differs')
        require(s.get('cap_drop')==['ALL'] and not s.get('cap_add') and s.get('security_opt')==['no-new-privileges:true'],'Unsafe capabilities')
        require(not any(k in s for k in ('privileged','network_mode','pid','ipc','devices','device_cgroup_rules','build','extends','volumes_from','extra_hosts')),'Unsafe candidate override')
        require(isinstance(s.get('mem_limit'),int) and 0<s['mem_limit']<=1024**3 and s.get('memswap_limit')==s['mem_limit'] and s.get('cpus')==1.0 and s.get('pids_limit')==256,'Candidate resource bounds differ')
        require(s.get('labels')=={'com.pawbridge.candidate-owner':'${CANDIDATE_OWNER}','com.pawbridge.environment':'full-stack-candidate'},'Candidate owner labels differ')
        require(s.get('environment')==({'APMS_BATCH_SCHEDULE_ENABLED':'false'} if version==2 and name=='animal-service' else None),'Unexpected candidate environment override')
        require(s.get('env_file')==['${CANDIDATE_STATE}/secrets/'+name+'.env'],'Candidate env file boundary differs')
        require(s.get('networks')==(['isolated','egress-relay'] if name=='access' else ['isolated']),'App network boundary differs')
        require(set(s.get('ports',[]))==({f'127.0.0.1:{p}:{p}' for p in PORTS} if name=='access' else set()),'Candidate exposed ports differ')
        for m in s.get('volumes',[]):
            if m.get('type')=='volume':require((name,m.get('source'),m.get('target')) in {('postgres','postgres','/var/lib/postgresql/data'),('kafka','kafka','/var/lib/kafka')},'Foreign data volume')
            else:
                source=m.get('source','');tail=source.removeprefix('${CANDIDATE_STATE}/')
                require(m.get('type')=='bind' and source.startswith('${CANDIDATE_STATE}/') and tail.split('/')[0] in ('guard','secrets') and '..' not in Path(tail).parts and m.get('read_only') is True and m.get('bind')=={'create_host_path':False},'Unsafe bind mount')
                require('docker.sock' not in m.get('target',''),'Docker socket mount forbidden')
    return len(SERVICES)
def load_bundle(selection=None,expected_sha=None):
    folder=ASSETS
    if selection is not None:
        require(isinstance(selection,str) and re.fullmatch(r'releases/\d{8}-[a-z][a-z0-9-]*',selection),'Invalid release selection')
        folder=ASSETS/selection
        require(folder.resolve().is_relative_to(ASSETS.resolve()),'Release escapes candidate directory')
        require(isinstance(expected_sha,str) and re.fullmatch(r'[a-f0-9]{64}',expected_sha) and digest(folder/'release.json')==expected_sha,'Pinned release changed')
    else:
        require(expected_sha is None,'Release hash without selection')
    release=json.loads((folder/'release.json').read_text());doc=json.loads((folder/'compose.yaml').read_text())
    require(digest(folder/'compose.yaml')==release['candidateComposeSha256'],'Candidate template changed')
    if selection is not None:
        require(release.get('version')==2 and release.get('parentReleaseSha256')==digest(ASSETS/'release.json'),'Invalid candidate parent')
        parent=json.loads((ASSETS/'release.json').read_text())
        require(release.get('inputs')==parent['inputs'],'Unexpected candidate source delta')
        expected=json.loads((ASSETS/'compose.yaml').read_text())
        require(digest(ASSETS/'compose.yaml')==parent['candidateComposeSha256'],'Parent template changed')
        expected['services']['animal-service']['image']=release['images']['MAC_IMAGE_ANIMAL_SERVICE']
        expected['services']['animal-service']['environment']={'APMS_BATCH_SCHEDULE_ENABLED':'false'}
        require(doc==expected,'Unexpected candidate template delta')
        require(set(release['images'])==set(parent['images']) and {k for k in parent['images'] if parent['images'][k]!=release['images'][k]}=={'MAC_IMAGE_ANIMAL_SERVICE'},'Unexpected candidate image delta')
    for rel,sha in release['inputs'].items():
        p=ROOT/rel;require(p.resolve().is_relative_to(ROOT.resolve()) and digest(p)==sha,'Candidate source configuration changed')
    validate_template(doc,release)
    return doc,release

def load_current():
    current=json.loads((ASSETS/'current.json').read_text())
    require(set(current)=={'releaseBundle','releaseSha256'},'Invalid current release selector')
    return load_bundle(current['releaseBundle'],current['releaseSha256'])

def validate_files(state):
    state=guard.state_path(state)
    require(state.parent==Path.home()/'.local/state/pawbridge' and re.fullmatch(r'full-stack-candidate-\d{8}T\d{6}-[a-f0-9]{8}',state.name),'Unexpected candidate state root')
    guard.private_path(state,0o700,directory=True)
    data=guard.read_json(state/'state.json');suffix=state.name.removeprefix('full-stack-candidate-')
    require(data['path']==str(state) and data['name']=='pawbridge-full-stack-'+suffix.lower() and data['endpoint']==guard.desktop_endpoint(),'Candidate state identity mismatch')
    require(data['uid']==os.getuid() and data['gid']==os.getgid() and re.fullmatch(r'[a-f0-9]{32}',data['owner']),'Candidate owner mismatch')
    require(data['volume']==data['name']+'-pg' and data['kafka_volume']==data['name']+'-kafka' and data['network']==data['name'],'Candidate target mismatch')
    doc,release=load_bundle(data.get('releaseBundle'),data.get('releaseSha256'))
    require(data['images']==release['images'],'Candidate runtime image set changed')
    for rel,sha in data['private_input_hashes'].items():
        p=state/rel;require(p.resolve().is_relative_to(state.resolve()),'Private input path escapes state')
        guard.private_path(p,0o600);require(digest(p)==sha,'Private candidate input changed')
    return data,doc,release

def native_memory_gate(pressure):
    require(pressure==1,'Native duplicate start blocked: host memory pressure is not normal')

def render(state):
    data,doc,release=validate_files(state)
    values={'CANDIDATE_STATE':str(state),'CANDIDATE_PROJECT':data['name'],'CANDIDATE_OWNER':data['owner'],'CANDIDATE_UID':str(data['uid']),'CANDIDATE_GID':str(data['gid'])}
    def replace(x):
        if isinstance(x,dict):return {k:replace(v) for k,v in x.items()}
        if isinstance(x,list):return [replace(v) for v in x]
        if isinstance(x,str):
            for k,v in values.items():x=x.replace('${'+k+'}',v)
            require('${' not in x,'Unresolved candidate expression')
        return x
    return replace(doc)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--state',required=True,type=Path);args=p.parse_args()
    doc=render(args.state);print('Candidate validated: '+str(len(doc['services']))+' services. No runtime changes.')
