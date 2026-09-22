#!/usr/bin/env python3
"""Exercise real local APIs with a synthetic user; never accepts a remote URL.

Creates one user and one text-only post per run. It sends email only to the local
SMTP sink, retains dev fixtures, and does not claim OAuth/payment/GPU coverage.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from local_dev import db_sql, write_private


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Redirects are forbidden in local verification')


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def request(path, data=None, token=None, content_type='application/json', expected=(200,201)):
    headers={'Content-Type':content_type}
    if token:headers['Authorization']='Bearer '+token
    if isinstance(data,dict):data=json.dumps(data).encode()
    req=urllib.request.Request('http://127.0.0.1:28080'+path,data=data,headers=headers)
    try:
        with HTTP.open(req,timeout=15) as response:
            status=response.status;body=response.read()
    except urllib.error.HTTPError as error:
        status=error.code;body=error.read()
    if status not in expected:raise ValueError(f'{path}: expected {expected}, received HTTP {status}')
    return json.loads(body) if body else None


def verify(state):
    if db_sql(state,'SELECT name FROM public.pawbridge_local_environment;')!='dev':
        raise ValueError('Local dev database marker missing')
    run=uuid.uuid4().hex[:12];email='dev-'+run+'@example.invalid';password=secrets.token_urlsafe(24)
    checks=[]
    report={'environment':'local-dev','run':run,'checks':checks,'fullE2E':False,
            'notCovered':['Google OAuth','external email delivery','Toss payment','R2 upload','SAM3/DINOv3'],
            'result':'running','startedAt':datetime.now(timezone.utc).isoformat()}
    try:
        request('/api/v1/users/me',expected=(401,403));checks.append('unauthenticated-request-denied')
        request('/api/v1/email/send',{'email':email})
        deadline=time.monotonic()+10;code=None
        while time.monotonic()<deadline:
            with HTTP.open('http://127.0.0.1:18025/messages',timeout=5) as r:messages=json.load(r)
            for message in messages:
                if email in message['to']:
                    found=re.search(r'>\s*(\d{6})\s*</',message['body'])
                    if found:code=found.group(1)
            if code:break
            time.sleep(.25)
        if not code:raise ValueError('SMTP verification code not captured')
        wrong='000000' if code!='000000' else '999999'
        request('/api/v1/email/verify',{'email':email,'code':wrong},expected=(400,401,422))
        checks.append('incorrect-email-code-denied')
        verified=request('/api/v1/email/verify',{'email':email,'code':code})
        if verified['data']['verified'] is not True:raise ValueError('Verification response not true')
        checks.append('smtp-email-code-verified')
        signup=request('/api/v1/users/signup',{'email':email,'name':'개발검증','password':password,'rePassword':password,'role':'ROLE_USER'})
        uid=int(signup['data']['userId']);checks.append('signup-through-api')
        request('/api/v1/auth/login',{'email':email,'password':'wrong-'+password},expected=(401,403))
        checks.append('incorrect-password-denied')
        login=request('/api/v1/auth/login',{'email':email,'password':password})['data']
        token=login['accessToken'];checks.append('password-login-through-api')
        me=request('/api/v1/users/me',token=token)['data']
        if int(me['userId'])!=uid or me['email']!=email:raise ValueError('Authenticated identity mismatch')
        checks.append('gateway-jwt-authenticated-profile')
        boundary='dev'+run
        title='개발환경 검증 '+run
        fields={'title':title,'content':'로컬 개발 환경 회원 인증과 게시글 검색 검증입니다.','boardType':'COMMUNICATION'}
        body=''.join('--'+boundary+'\r\nContent-Disposition: form-data; name="'+key+'"\r\n\r\n'+value+'\r\n' for key,value in fields.items())+'--'+boundary+'--\r\n'
        post=request('/api/v1/posts',body.encode(),token,'multipart/form-data; boundary='+boundary)['data']
        pid=int(post['postId'])
        if int(post['authorId'])!=uid:raise ValueError('Post author mismatch')
        fetched=request('/api/v1/posts/read/'+str(pid))['data']
        if fetched['title']!=title or int(fetched['authorId'])!=uid:raise ValueError('Post read mismatch')
        checks.append('authenticated-post-write-and-public-read')
        deadline=time.monotonic()+45;seen=False
        while time.monotonic()<deadline:
            rows=request('/api/v1/posts/search?keyword='+urllib.parse.quote(run))['data']
            seen=any(int(row['postId'])==pid for row in rows)
            if seen:break
            time.sleep(1)
        else:raise ValueError('Post is not searchable')
        checks.append('new-post-searchable')
        # PostgreSQL indexes posts in the write transaction; its old ES consumer is disabled.
        # Exercise the live user -> animal -> user compensation consumers instead.
        missing_animal=900000000000+int(run[:8],16)
        if db_sql(state,f"SELECT count(*) FROM pawbridge_animal.animals WHERE id={missing_animal};")!='0':
            raise ValueError('Synthetic missing animal ID unexpectedly exists')
        request('/api/v1/favorites/'+str(missing_animal),{},token)
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            processed=db_sql(state,f"SELECT count(*) FROM pawbridge_user.processed_events p JOIN pawbridge_animal.outbox_events o ON o.payload::jsonb->>'eventId'=p.event_id WHERE o.payload::jsonb->>'userId'='{uid}' AND o.payload::jsonb->>'animalId'='{missing_animal}' AND p.event_type='ROLLBACK_FAVORITE_ADDED';")
            removed=request('/api/v1/favorites/'+str(missing_animal)+'/check',token=token)['data'] is False
            if int(processed)>0 and removed:break
            time.sleep(1)
        else:raise ValueError('Actual favorite compensation consumers did not complete')
        checks.append('favorite-outbox-cdc-kafka-compensation-consumers')
        write_private(state/'last-verification-account.json',json.dumps({'email':email,'password':password,'userId':uid,'postId':pid},indent=2))
        report.update(result='passed',userId=uid,postId=pid)
    except Exception as error:
        report.update(result='failed',errorType=type(error).__name__)
        raise
    finally:
        report['finishedAt']=datetime.now(timezone.utc).isoformat()
        write_private(state/'flows-result.json',json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,default=Path.home()/'.local/state/pawbridge/dev')
    args=parser.parse_args()
    verify(args.state.resolve())
