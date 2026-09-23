# 로컬 개발 Vault 연결

서비스 설정은 `../compose/compose.yaml`, 비밀값의 원본은 기존 Vault의 `secret/pawbridge/local-dev/runtime`과 `secret/pawbridge/local-dev/google`이다. 운영이 쓰는 과거 `pawbridge/dev/...` 경로와 구분한다. Agent는 애플리케이션 YAML을 생성하지 않고 필요한 비밀값만 비공개 파일에 공급한다.

## 구성과 권한

- `read-policy.hcl`: 두 개발 경로의 읽기와 자기 토큰 검사·갱신·폐기만 허용한다. 운영 데이터 읽기·개발 비밀값 쓰기·관리 권한은 없다.
- `agent.hcl`: AppRole 로그인, CA와 서버 이름을 검증한 HTTPS 연결, 두 파일 렌더링 후 종료.
- `compose.yaml`: Agent만 실행하는 별도 Compose 프로젝트. 앱·데이터 네트워크와 연결되지 않으며 공개 포트가 없다. 메모리 128MiB·CPU 0.25·PID 64 상한, 비루트·읽기 전용 루트·capability 제거를 적용한다.
- CPU 0.25는 한 코어의 25%에 해당하는 CPU 시간 제한이다. 예약이나 사용량 보장이 아니다. 메모리 상한 초과는 OOM 종료 원인이 될 수 있다.
- 고정된 Vault 2.0.4 이미지 digest를 사용한다. `pull_policy: never`이므로 승인 없이 이미지를 설치하지 않는다.

현재 Vault UI Service는 `externalTrafficPolicy: Local`이다. 실제 Vault Pod가 실행되는 `192.168.57.12:30820`에 연결하며 인증서의 `vault.vault.svc.cluster.local` 이름을 검증한다. Pod가 다른 노드로 이동하면 실제 위치를 확인하고 주소를 갱신한다. TLS 검증을 끄거나 운영 Service 정책을 바꾸지 않는다.

## 최초 등록

Vault 관리자 로그인으로 다음 **개발 전용 항목**을 등록한다. 관리자 비밀번호는 숨김 입력을 사용하고 채팅·Git·명령행 인자에 넣지 않는다.

1. KV v2 `secret/pawbridge/local-dev/runtime`: 기존 개발 PostgreSQL/Redis/JWT/서비스 키 20개. 이미 개발 DB가 있으므로 새 비밀번호로 교체하지 않는다.
2. KV v2 `secret/pawbridge/local-dev/google`: 사용자 승인으로 공유한 Google Client ID와 Secret 두 개.
3. `pawbridge-local-dev-read` 정책, 전용 AppRole auth mount `pawbridge-local-dev`, role `compose-read`.
4. role은 위 정책만 사용하고 default 정책을 부여하지 않는다. token TTL 15분, max TTL 1시간, Secret ID TTL 30일이다. Secret ID는 만료되면 관리자 권한으로 다시 발급해야 하며 자동 연장된다고 가정하지 않는다.

기존 값은 읽어 비교하고 다른 값이 있으면 중지한다. 새 KV는 CAS=0으로 생성한다. 실제 운영 Secret·JWT·DB 계정은 변경하지 않는다. 등록 전 snapshot·변경 목록을 비공개로 보존하고 작업 종료 시 임시 관리자/검증 토큰을 폐기한다. 일회성 등록 프로그램은 애플리케이션 코드에 포함하지 않는다.

Git 밖 state(`~/.local/state/pawbridge/dev`)의 `vault/credentials/role-id`, `vault/credentials/secret-id`, `vault/ca.crt`를 준비한다. 자격증명 파일 0600·디렉터리 0700을 유지한다. 이 폴더와 렌더링 파일은 커밋하지 않는다. 최초 인증 수단 자체가 없어지는 구조는 아니다.

## 실행

인프라 저장소 루트에서 실행한다. `vault/rendered` 디렉터리가 현재 UID 소유의 0700으로 준비되어 있어야 한다. UID/GID가 1000이 아닌 환경은 아래 변수를 실제 값으로 전달한다.

```sh
PAWBRIDGE_STATE="$HOME/.local/state/pawbridge/dev"
PAWBRIDGE_DEV_UID="$(id -u)" PAWBRIDGE_DEV_GID="$(id -g)" \
docker --context default compose \
  --env-file "$PAWBRIDGE_STATE/compose.env" \
  -f environments/dev/vault/compose.yaml \
  run --rm vault-agent
```

Agent가 성공 종료한 경우에만 다음 개발 서비스를 기동한다. 파일이 남아 있다는 사실만으로 이번 Vault 조회가 성공했다고 판단하지 않는다. 실행 중인 앱에 대한 비밀 회전·자동 재기동 기능은 아니다.

```sh
docker --context default compose \
  --env-file "$PAWBRIDGE_STATE/vault/rendered/runtime.env" \
  --env-file "$PAWBRIDGE_STATE/compose.env" \
  --env-file "$PAWBRIDGE_STATE/images.env" \
  --env-file "$PAWBRIDGE_STATE/vault/rendered/google.env" \
  -f environments/dev/compose/compose.yaml \
  -f environments/dev/compose/compose.google.yaml \
  --profile apps --profile events up -d
```

Vault 파일에는 평문 비밀이 있으므로 보호가 필요하다. 설정 출력은 `config --quiet`를 사용한다. 일반 `config` 결과나 env 파일 내용을 공유 로그에 출력하지 않는다. 두 파일이 기존 개발 값과 일치함을 확인한 뒤 `vault/enabled`에 `pawbridge-local-dev`를 기록하면 기존 초기화·검증 보조 도구도 Vault 공급 파일을 선택한다. 해당 표시가 있을 때 파일이 없거나 권한이 잘못되면 로컬 `.env`로 우회하지 않고 실패한다.

상시 실행 Agent는 없다. `exit_after_auth`는 인증과 템플릿 렌더링 완료 후 종료한다. Agent의 제한된 로그인 토큰은 sink 파일로 저장하지 않으며 TTL 만료로 폐기된다. 자격증명 갱신 후 파일이 바뀌어도 실행 중인 앱 환경변수는 바뀌지 않는다. DB 비밀번호 회전은 DB와 앱을 함께 조율할 별도 작업이다.

## 복구

- 로컬만 복구: Agent 실행을 중단하고 `vault/enabled`를 비공개 별도 이름으로 보관한다. 기존 `.env`·`google-oauth.env`를 명시적으로 선택해 설정을 검증한다. 이 파일들은 최초 전환 확인 중 보존하며 임의 삭제·재생성하지 않는다.
- 권한 취소: 관리자 승인 범위에서 **새 전용 auth mount** `pawbridge-local-dev`를 비활성화해 관련 인증과 토큰을 폐기한다. 운영 `kubernetes`·`userpass` auth에는 손대지 않는다.
- 완전 철회가 필요하면 변경 기록상 이번에 생성한 두 KV 경로와 전용 정책만 삭제한다. 기존 경로가 발견됐던 경우에는 삭제하지 않는다. 삭제는 별도 승인 후 수행한다.
- 전체 Raft snapshot 복원은 다른 운영 변경을 되돌릴 수 있으므로 일반 롤백 수단으로 사용하지 않는다.

## 참고

- [Vault Agent 템플릿](https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/template)
- [AppRole 자동 인증](https://developer.hashicorp.com/vault/docs/agent-and-proxy/autoauth/methods/approle)
