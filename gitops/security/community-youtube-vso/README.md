# Community 영상 키 공급과 배포 절차

이 디렉터리는 홈 영상 API의 서버 키 공급을 준비한다. 키 등록, 정책 생성,
Kubernetes 적용, DB 변경과 운영 배포는 별도 승인을 받은 뒤 실행한다.
이 PR의 병합이나 로컬 렌더링만으로 공급과 배포가 완료되지 않는다.

## 적용 전에 확인한다

1. 현재 접속 대상의 context, 노드 UID, namespace와 Argo Application UID를 확인한다.
   과거 Vagrant 환경이나 이름만 같은 클러스터를 운영 대상으로 선택하지 않는다.
2. Community와 Gateway의 실제 image, Argo source revision 및 `helm.valuesObject`를
   비밀값 없는 복구 자료로 보존한다. `community-service-dev`와 `api-gateway-dev`는
   2026-10-07 점검에서 운영 namespace `pawbridge`를 사용하는 수동 동기화 앱이었다.
   실행 직전에 다시 확인한다.
3. Vault가 언씰 상태인지, 기존 `secret` mount가 KV-v2인지 확인한다.
   `pawbridge/vault-internal-ca`의 `ca.crt` 존재와 현재 Vault TLS 연결도 확인한다.
   TLS 검증을 끄거나 기존 mount 버전을 변경하지 않는다.
4. 기존 VSO 1.4.1과 Kubernetes auth mount `kubernetes`를 확인한다.
   아래 이름과 같은 Secret, Vault 정책 및 역할이 이미 있으면 중단한다.
   소유권과 복구본을 먼저 확인하고 기존 자원을 덮어쓰지 않는다.
5. Community PostgreSQL V1~V8의 성공 이력, 스키마 소유자와 앱 권한을 확인한다.
   Community 백업의 격리 복원과 V9 리허설을 검증한 뒤 운영 V9만 적용한다.
   `home_video_board`와 `home_videos`의 소유자는 `pawbridge_community_migration`이어야 한다.
   앱에는 필요한 DML 권한만 부여하고 DDL 권한을 추가하지 않는다.

`environments/prod/values/community-service.yaml`의 `schemaMigration.enabled`는
`false`로 유지한다. 기존 Helm Job은 MySQL runner이므로 이 플래그를 켜서 PostgreSQL
V9를 적용하지 않는다. `existingSchemaVerified`와 기존 `recoveryReference`도 V9 검증
근거가 아니다. 별도로 승인한 PostgreSQL runner와 loopback 접속 절차를 사용한다.

## 승인 후 키 공급을 연결한다

1. 기존에 보관한 YouTube Data API 키를 Vault KV-v2 mount `secret`의
   `pawbridge/dev/community/youtube`에 `YOUTUBE_DATA_API_KEY` 이름으로 등록한다.
   키를 채팅, 명령 인자, Git, 프런트 `VITE_` 설정이나 기존 R2 경로에 넣지 않는다.
   값이 이미 있으면 변경 전에 복구본을 확보하고 기존 필드를 보존한다.
2. 승인된 관리자 세션에서 다음 정책과 역할만 등록한다.
   기존 광역 정책, auth mount 설정과 다른 서비스의 역할은 변경하지 않는다.

   ```sh
   vault policy write community-youtube-read infra/vault/policies/community-youtube-read.hcl
   vault write auth/kubernetes/role/community-youtube-read @infra/vault/roles/community-youtube-read.json
   ```

   이 역할은 `pawbridge/community-youtube-vault-auth`의 `vault` audience에만 묶인다.
   데이터와 메타데이터 경로 두 개에는 `read`만 허용한다. VSO가 자기 토큰을
   관리하도록 `auth/token/lookup-self`에 `read`, `auth/token/renew-self`와
   `auth/token/revoke-self`에 `update`를 허용한다. 기본 정책은 포함하지 않는다.
   토큰 생성, 다른 토큰 관리, 다른 KV 경로 읽기와 키 쓰기는 허용하지 않는다.
   TTL과 max TTL은 모두 10분이다. 갱신 요청으로 최대 수명을 늘리지 않는다.

   VSO 1.4.1은 renewable 토큰으로 로그인한 직후 `renew-self`를 호출한다.
   자기 관리 권한 없는 KV read-only 정책은 로그인에 성공해도 여기서 403으로
   중단될 수 있다. 관리자 userpass 로그인 성공은 이 전용 역할의 연결 검증이 아니다.

3. V9와 복구 준비가 끝난 뒤 이 디렉터리의 Kustomize 리소스를 적용한다.
   **최초 공급 또는 키 변경은 Community 재시작을 유발할 수 있다.**
   `refreshAfter: 1m`은 Vault→Secret 동기화 주기이며 Google API 호출 주기가 아니다.
   환경변수 갱신에는 Pod 재시작이 필요하므로 restart target은 Community만 지정했다.
4. VaultStaticSecret의 `Ready=True`와 `Healthy=True`, 목적지 Secret
   `pawbridge/community-youtube-auth`의 소유권을 함께 확인한다. 변경이 없으면
   `SecretSynced=False`일 수 있으므로 이 조건 하나로 실패를 판단하지 않는다.
   Secret의 owner reference가 해당 VaultStaticSecret의 UID와 일치해야 한다.
   비밀값을 출력하지 않고 키 이름이 `YOUTUBE_DATA_API_KEY` 하나인지,
   값이 비어 있지 않은지 확인한다. 기존 R2·DB Secret과 Gateway에는 공급하지 않는다.
   `overwrite: false`는 다른 주체가 만든 목적지 Secret의 인수를 방지한다.
   `excludeRaw: true`와 키 허용 목록은 `_raw` 및 다른 필드의 복사를 막는다.

키 등록 유무와 Google의 실제 키 인증은 다른 검증이다. 관리자 영상 정보 조회가
실제 YouTube API로 성공하는지 운영 반영 후 따로 확인한다. 새 키 발급은 필요하지 않다.

## 승인한 이미지와 참조를 배포한다

1. 검증한 Infra commit에 고정해 Community Argo source를 갱신한다.
   기존 `helm.valuesObject`가 values 파일을 덮는지 확인하고 image와 전용 Secret 참조를
   맞춘다. 다른 inline 설정과 다른 서비스는 보존한다.
2. Community만 동기화하고 Ready, 기존 쪽지 동작, 영상 API의 DB 접근을 검증한다.
   `community-youtube-auth`는 필수 참조다. Secret이 없으면 새 Pod가 시작되지 않는다.
   Secret 존재만으로 키 유무나 유효성이 증명되지 않으므로 앞 단계의 검증을 생략하지 않는다.
3. Gateway를 동기화하고 공개·관리자 영상 경로와 인증 경계를 확인한다.
4. 별도 승인한 프런트 main 승격과 Cloudflare 빌드를 확인한다.
   관리자 등록·게시·숨김·순서 변경, 홈 최대 3개 표시와 공식 YouTube 재생을
   실제 API 및 데스크톱·모바일 Playwright 조작으로 검수한다.
   운영 영상 데이터의 생성·변경도 해당 승인 범위에 포함한다.

이미지는 SHA 태그가 아닌 CI가 게시한 OCI index digest로 실행한다.
Community 소스는 `82a9cfa648ebfb122fad1f44c16d843985eb611b`, Gateway 소스는
`6ae772456bcea1b5e149585193188d50a7d5d83b`다.
[Community CI](https://github.com/pawbridge/pawbridge-backend-k8s/actions/runs/37555309679)와
[Gateway CI](https://github.com/pawbridge/pawbridge-backend-k8s/actions/runs/37555386464)의
성공 결과와 Docker Hub digest를 2026-10-07 대조했다.
`environments/dev`의 자동 이미지 PR은 별도이며 이 절차에 포함하지 않는다.

## 문제가 생기면 현재 앱으로 복구한다

1. 프런트·Gateway·Community 중 변경한 대상만 직전 배포와 Argo spec으로 복구한다.
   영상 문제 때문에 현재 정상인 쪽지 기능이나 User 서비스를 이전 단계로 되돌리지 않는다.
2. Community의 기존 Secret 참조를 복구하고 Ready를 확인한다.
   사용 중인 Secret이나 VaultStaticSecret을 먼저 삭제하지 않는다.
   VSO가 생성한 Secret은 소유 리소스 삭제 시 함께 제거될 수 있다.
3. 추가된 V9 테이블과 영상 데이터는 남겨 둔다.
   Flyway history 편집, 테이블 삭제와 자동 down migration을 실행하지 않는다.
4. Vault 등록을 되돌려야 하면 보존한 이전 버전과 정책·역할 복구본을 사용한다.
   새 키의 폐기나 Secret 정리는 의존 관계를 확인하고 별도 승인 후 실행한다.

## 정적 검사와 실제 연동시험을 구분한다

아래 정적 검사는 저장소 루트에서 실행한다. 운영 context나 키가 필요하지 않다.

```sh
python3 -m unittest discover -s scripts/environments -p 'test_home_video_runtime.py'
python3 -m unittest discover -s scripts/environments -p 'test_*.py'
python3 scripts/environments/validate.py --output /tmp/home-video-environment-render
kubectl kustomize gitops/security/community-youtube-vso
```

Helm은 기존 CI와 같은 3.15.4를 사용한다. PATH에 없으면 `HELM_COMMAND`로 기존
실행 파일을 지정한다. 테스트는 키 허용 목록·최소 권한·인증 참조와 실제 Helm 렌더를
검증하지만 Vault 로그인, Secret 생성이나 운영 배포를 대신하지 않는다.

### 실제 Vault에서 ACL과 토큰 생명주기를 시험한다

운영 키·토큰·snapshot 없이 로컬 Docker의 합성 값으로만 시험한다. 실행 전에
로컬 Docker 접근과 아래 고정 이미지가 캐시에 있는지 확인한다. runner는 이미지를
받지 않고 `--pull=never`로 실행한다. 이미지 다운로드는 별도로 승인받는다.

```sh
python3 -B infra/vault/tests/community_youtube_integration.py --run-isolated
```

Vault 2.0.4의 고정 digest는
`hashicorp/vault@sha256:5be49781ecf78bfe775c5309c6a4d9f4e9e040b6c885c99eb2b12fb69855e1a2`다.
Docker context `default`의 로컬 소켓만 허용한다. 네트워크는 `none`이고 포트·bind
mount·영구 volume·privileged 권한이 없다. 메모리 상한은 384MiB, CPU는 0.5개다.
이미지의 기본 volume 경로도 tmpfs로 대체한다. 이번 실행의 라벨과 이름을 대조해
시험 컨테이너만 종료·제거한다. 운영 컨테이너나 기존 볼륨은 정리하지 않는다.

runner가 실제 Vault에 요청해 다음을 확인한다.

- 이전 KV read-only 정책은 키 읽기를 허용하지만 자기 조회·갱신·폐기를 HTTP 403으로 거부한다.
- 수정 정책은 자기 조회·갱신을 허용하고, 1시간 갱신 요청도 max TTL 600초를 넘기지 않는다.
- 다른 비밀 경로, 상위 경로 목록, 키 쓰기, 정책 쓰기, 토큰 생성과 다른 토큰 관리가 HTTP 403이다.
- 자기 폐기 후 해당 토큰의 조회와 비밀값 읽기가 HTTP 403이다. 저장한 KV는 남는다.
- 만료 경계는 별도 3초짜리 합성 토큰으로 가속 시험한다. 운영 역할의 10분 설정은 변경하지 않는다.

이 시험의 성공 JSON에는 `vsoControllerTested=false`가 명시된다. 실제 Vault ACL
시험이지 Kubernetes 인증·VSO 컨트롤러·Secret 동기화 성공이 아니다.

### 실제 VSO와 Kubernetes 인증·Secret 동기화를 시험한다

다음 runner는 별도 Kubernetes API, Vault 2.0.4와 VSO 1.4.1을 실제로 연결한다.
운영 kubeconfig·CA·키·영구 볼륨을 복사하지 않는다. 앞의 Vault ACL 시험과 함께
통과해야 운영 정책 보완의 검증 근거가 된다. 로컬 성공은 운영 공급 성공이 아니다.

```sh
python3 -B infra/vault/tests/community_youtube_vso_integration.py --run-isolated
```

로컬 Docker의 `default` 소켓과 캐시에 있는 다음 고정 이미지만 사용한다.
`--pull=never`이므로 이미지가 없으면 중단한다. 다운로드는 별도 승인 사항이다.

- Vault: 앞의 2.0.4 digest.
- K3s 1.36.1: `rancher/k3s@sha256:08fdebd14db9ab7d5ea821d5bfa95d02341a6ef886842fcc8d9dfd0e9fa9e0cd`
- VSO 1.4.1: `hashicorp/vault-secrets-operator@sha256:1314beb4df53650d1a8c0f70eab1de516e4362329a541156fef5acefbdc18cc8`

K3s는 agentless로 API와 저장소만 실행하고 VSO는 별도 Docker 프로세스로 실행한다.
시험 전용 내부 네트워크의 주소를 사용하며 호스트 포트를 공개하지 않는다.
세 컨테이너의 메모리 상한 합계는 2,432MiB, CPU 상한 합계는 2개다.
privileged·bind mount·영구 volume을 사용하지 않고 기본 volume도 tmpfs로 대체한다.
Vault는 시험용 TLS 인증서를 발급하고 VSO는 CA와 서버명을 검증한다.
API 접속 인증서는 이 시험의 Kubernetes에서 새로 만든다.

runner는 저장소의 역할 및 네 가지 VSO 리소스를 사용한다. VaultConnection의 주소와
서버명만 시험용으로 바꾸며 SSL 검증과 키 필터는 유지한다. 역할 TTL·max TTL 600초와
`token_no_default_policy=true`도 유지한다. 다음을 실제 요청으로 확인한다.

1. 이전 정책에서는 VSO의 `renew-self` HTTP 403이 발생하고 목적지 Secret이 없다.
2. 정책 보완 뒤 `Ready=True`·`Healthy=True`, Secret 소유권과 단일 키 허용 목록이 맞는다.
3. 실제 Kubernetes TokenRequest로 만든 잘못된 계정·namespace·audience는 Vault가 거부한다.
4. 합성 키 변경이 목적지 Secret에 반영되고 관련 없는 필드는 복사되지 않는다.
5. 최초 토큰의 백그라운드 갱신과 그 뒤 새 로그인을 Vault audit HMAC·시각으로 연결한다.
   로그인 직후 갱신이나 이전 재시도 로그인을 성공으로 세지 않는다.
   VSO는 실제 만료 전에 재인증할 수 있으므로 특정 초 이후의 로그인만 요구하지 않는다.
   역할 설정을 줄이지 않고 관찰 시간은 최소 601초로 유지한다.
6. 601초 이상 관찰한 뒤 합성 값을 다시 바꾸고 Secret 반영까지 확인한다.
   과거 Secret이나 남아 있는 Ready 상태만으로 성공을 판정하지 않는다.

초기화와 두 번의 1분 동기화 대기를 포함해 약 12~15분이 필요하다.
출력은 검사명·판정·경과 시간뿐이다. 토큰, audit 원문과 키 값은 출력하지 않는다.
성공·실패 모두 실행별 이름과 소유 라벨을 대조해 세 컨테이너와 내부 네트워크만
제거한다. 다운로드한 이미지와 기존 Docker 자원은 남긴다. 정리가 실패하면 실패로
보고하며 전역 prune으로 대신하지 않는다.

이 시험은 운영 Pod의 Kubernetes RBAC·리더 선출·영구 캐시·장애 복구를 검증하지 않는다.
VSO의 API 접속에는 시험 전용 관리자 인증서를 사용한다. rollout target은 replicas 0의
시험용 Deployment이므로 Community 앱 재시작, DB, Google API와 화면 E2E도 범위 밖이다.
운영 정책 적용 후에는 실제 서비스 계정 권한, Secret 공급과 앱 상태를 별도로 확인한다.
컨트롤러 시험이나 운영 공급 확인을 실행하지 않았다면 각각 미실행으로 보고한다.

### 기존 전용 정책을 보완할 때의 복구 경계

신규 등록 절차의 동명 자원 거부를 제거해서 기존 리소스를 덮어쓰지 않는다.
403 보완은 별도 승인된 작업으로 현재 정책을 비공개 복구 파일에 읽어 보존한 뒤
정확히 해당 정책만 갱신한다. 등록된 키·역할·기본 정책·auth mount는 바꾸지 않는다.
실패 시 보존한 정책으로만 복구한다. VSS·Secret 삭제, VSO 전체 재시작과 V9
되돌리기는 이 보완에 포함하지 않는다. 새 앱 배포는 실제 공급 확인 후에만 재개한다.

변환·인증의 공급자 계약은 [VSO API reference](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/api-reference)와
[VSO 1.4.1 필드 필터 구현](https://github.com/hashicorp/vault-secrets-operator/blob/v1.4.1/helpers/template.go)을 따른다.
토큰 생명주기는 [VSO 1.4.1 client 구현](https://github.com/hashicorp/vault-secrets-operator/blob/v1.4.1/vault/client.go#L460-L470)과
[Vault token API](https://developer.hashicorp.com/vault/api-docs/auth/token)를 따른다.
audit의 `token_ttl`·HMAC·시각 필드는
[Vault audit schema](https://developer.hashicorp.com/vault/docs/audit/schema)를 따른다.
만료 전 교체 판정은 VSO가 사용하는
[Vault API LifetimeWatcher 구현](https://github.com/hashicorp/vault/blob/eff87a134a94/api/lifetime_watcher.go)과
[VSO 1.4.1 캐시 교체 구현](https://github.com/hashicorp/vault-secrets-operator/blob/v1.4.1/vault/client_factory.go#L788-L800)을 따른다.
