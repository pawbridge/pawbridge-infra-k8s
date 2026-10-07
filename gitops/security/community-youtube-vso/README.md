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
   데이터와 메타데이터 경로 두 개에 `read`만 허용하며 기본 정책을 포함하지 않는다.

3. V9와 복구 준비가 끝난 뒤 이 디렉터리의 Kustomize 리소스를 적용한다.
   **최초 공급 또는 키 변경은 Community 재시작을 유발할 수 있다.**
   `refreshAfter: 1m`은 Vault→Secret 동기화 주기이며 Google API 호출 주기가 아니다.
   환경변수 갱신에는 Pod 재시작이 필요하므로 restart target은 Community만 지정했다.
4. VaultStaticSecret의 정상 상태와 목적지 Secret `pawbridge/community-youtube-auth`를
   확인한다. 비밀값을 출력하지 않고 키 이름이 `YOUTUBE_DATA_API_KEY` 하나인지,
   값이 비어 있지 않은지만 확인한다. 기존 R2·DB Secret과 Gateway에는 공급하지 않는다.
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

## 배포 없이 검증한다

저장소 루트에서 실행한다. 운영 context나 키가 필요하지 않다.

```sh
python3 -m unittest discover -s scripts/environments -p 'test_home_video_runtime.py'
python3 -m unittest discover -s scripts/environments -p 'test_*.py'
python3 scripts/environments/validate.py --output /tmp/home-video-environment-render
kubectl kustomize gitops/security/community-youtube-vso
```

Helm은 기존 CI와 같은 3.15.4를 사용한다. PATH에 없으면 `HELM_COMMAND`로 기존
실행 파일을 지정한다. 테스트는 키 허용 목록·최소 권한·인증 참조와 실제 Helm 렌더를
검증하지만 Vault 로그인, Secret 생성이나 운영 배포를 대신하지 않는다.

변환·인증의 공급자 계약은 [VSO API reference](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/api-reference)와
[VSO 1.4.1 필드 필터 구현](https://github.com/hashicorp/vault-secrets-operator/blob/v1.4.1/helpers/template.go)을 따른다.
