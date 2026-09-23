# 로컬 dev 환경

개발 환경은 Windows PC의 로컬 Docker/WSL에서 실행한다. 운영 VM의 Kubernetes에 dev namespace·DB·Kafka를 추가하지 않는다. `main`은 운영, `dev`는 개발 검증 기준이며 기본 GitHub 브랜치는 dev를 유지해도 된다.

## 실행

일반 기동은 Git에서 관리하는 `compose/compose.yaml`을 Docker Compose v2로 직접 실행한다. 서버 설정은 이 YAML의 `environment`와 Spring `dev,postgresql` 프로필로 결정한다. Google 연결은 별도 `compose.google.yaml` override다. Python으로 Compose를 생성하지 않는다.

최초 DB 계정 준비·마이그레이션·CDC 등록·회귀 검증용 보조 도구만 Python 3 + PyYAML과 해당 Java/Gradle을 사용한다. 도구 설치를 자동 수행하지 않는다. 인프라 저장소 루트에서 실행한다.

```sh
python3 scripts/environments/local_dev.py prepare
python3 scripts/environments/local_dev.py config
python3 scripts/environments/local_dev.py up-data
```

이 명령은 PostgreSQL/pgvector, Redis, Kafka, 개발용 메일 수신기를 실행한다. 기본 상태 경로는 `~/.local/state/pawbridge/dev`이며 다른 폴더는 `--state /절대/경로`로 모든 명령에 동일하게 지정한다. 설정과 비밀번호를 운영에서 복사하지 않고 처음 한 번 무작위 생성한다. 재실행해도 비밀번호는 유지된다. 상태 디렉터리는 0700, `.env`는 0600이다. 비루트 컨테이너에 읽기 전용 마운트하는 설정 파일만 0444이며 부모 디렉터리는 비공개다. 이 경로 전체를 Git에 넣지 않는다.

다섯 백엔드 서비스 폴더에서 각각 `bash ./gradlew migrationDistribution`을 실행한 뒤:

```sh
python3 scripts/environments/local_dev.py migrate --backend /절대/경로/pawbridge-backend-k8s --java /Java17/bin/java
python3 scripts/environments/local_dev.py up-events
python3 scripts/environments/local_dev.py register-cdc
python3 scripts/environments/local_dev.py up-apps
python3 scripts/environments/local_dev.py status
python3 scripts/environments/local_dev.py smoke
python3 scripts/environments/local_dev.py verify-cdc
python3 scripts/environments/local_flows.py
```

마이그레이션은 소스 SQL과 패키지 일치, 로컬 DB의 dev 표식, loopback 주소를 확인한다. DB는 새 개발 데이터만 보관한다. 운영 덤프·PV·토픽·비밀번호를 재사용하지 않는다. Connect 등록은 스키마 초기화 후 수행하며, 다섯 서비스의 실제 Outbox 라우팅/헤더 설정을 사용한다.

`isolated-values/*.yaml`은 CI가 갱신하는 **이미지·마이그레이션 메타데이터**다. Kubernetes dev values가 아니다. `prepare`는 그 digest를 읽어 비밀값이 없는 `images.env`에 기록한다. Compose의 서비스·프로필·환경변수·자원·포트 정의는 Git의 YAML이 정본이다. 새 dev 이미지가 병합되면 이미지 목록을 갱신한 뒤 해당 로컬 서비스를 재기동한다. GitHub CI가 꺼진 PC를 자동 기동하거나 로컬에 배포하지 않는다.

## 설정을 확인하고 직접 실행

- `compose/compose.yaml`: 일반 설정, Spring 프로필, 자원 상한, 네트워크, 볼륨과 이미지 변수.
- `compose/compose.google.yaml`: Google 연결을 선택했을 때만 적용하는 추가 설정.
- `compose/ide-overrides.yaml`: 호스트 IDE 실행 시 달라지는 주소·포트.
- state의 `compose.env`: 비공개 state 디렉터리 위치. `images.env`: 검증한 이미지 digest 목록.
- state의 `vault/rendered/runtime.env`·`google.env`: Vault에서 받은 비밀값 전달 파일. 이전 `.env`·`google-oauth.env`는 복구용으로 보존하며 모두 Git에서 제외한다.

초기화·마이그레이션이 완료된 개발 환경은 Python 없이 다음 명령으로 기동할 수 있다. 아래는 기존 파일 방식의 기본 예이며, **현재 연결한 Vault 방식은 [별도 절차](vault/README.md)의 Agent 조회와 `vault/rendered` 파일을 사용한다.** `--profile apps`는 Docker Compose의 서비스 선택이며, Spring 프로필과 구분한다.

```sh
PAWBRIDGE_STATE="$HOME/.local/state/pawbridge/dev"
docker --context default compose \
  --env-file "$PAWBRIDGE_STATE/.env" \
  --env-file "$PAWBRIDGE_STATE/compose.env" \
  --env-file "$PAWBRIDGE_STATE/images.env" \
  -f environments/dev/compose/compose.yaml \
  --profile apps --profile events up -d
```

Google 연결을 사용하면 위 명령의 `--profile` 앞에 아래 두 인자를 추가한다. 자격증명이 없으면 Google override가 실패하도록 되어 있다.

```sh
--env-file "$PAWBRIDGE_STATE/google-oauth.env" \
-f environments/dev/compose/compose.google.yaml
```

기동 전 문법 검사는 `up -d` 대신 `config --quiet`, 상태 조회는 `ps`, 중지는 `stop`이다. 일반 `config` 출력에는 해석된 비밀값이 포함될 수 있으므로 공유 로그에는 출력하지 않는다. 개발 데이터가 있는 기존 볼륨을 그대로 사용한다. 이전에 state에 생성됐던 `compose.yaml`은 새 실행 도구가 사용하지 않는다. 롤백 확인 전에는 임의 삭제하지 않는다.

## Vault와 환경변수의 역할

`.env`는 필수 비밀 저장소가 아니다. Compose는 환경변수 또는 `--env-file`에서 값을 주입받을 수 있다. Vault를 원본 저장소로 쓰더라도 전달 방식으로 환경변수나 비공개 파일을 사용할 수 있다.

현재 운영 매니페스트는 Vault Secrets Operator가 Vault를 읽어 Kubernetes Secret으로 동기화하고, 서비스에 환경변수로 주입한다. 로컬 Compose에는 Kubernetes Operator가 없으므로 같은 매니페스트만으로 자동 연결되지 않는다. Spring이 Vault를 직접 읽는 의존성을 추가하는 방법도 있지만 DB·Redis·Kafka Connect까지 같은 방식으로 처리하지 못한다. 로컬 전체 환경에는 Vault Agent가 필요한 개발 비밀값을 공급하고 기존 Compose가 받는 방식을 우선 검토한다. Agent 템플릿은 비밀 전달 파일을 렌더링할 뿐, 서비스 구성 YAML을 생성하지 않는다.

**2026-09-23 로컬 Vault 연결을 적용했다.** 개발 전용 `secret/pawbridge/local-dev/runtime`·`google` 경로와 AppRole을 등록했고 운영 경로 접근 거부를 확인했다. Vault Agent는 별도 Compose로 일회 실행되어 비공개 `vault/rendered/runtime.env`·`google.env`를 공급한 뒤 종료한다. 기존 DB/JWT/Google 값 22개와 일치함을 확인했고, `vault/enabled` 표시로 기존 보조 도구도 Vault 공급 파일을 읽는다. 파일이 누락되면 이전 `.env`로 자동 우회하지 않는다. 기존 로컬 파일은 복구용으로 보존했다.

최초 승인·관리자 로그인과 구현/검증은 완료했다. 평상시 실행은 [Vault 연결 절차](vault/README.md)의 Agent 조회 → 성공 시 Compose 기동 순서다. 상시 Agent나 실행 중인 앱의 자동 비밀 회전은 구성하지 않았다. Secret ID의 유효기간은 30일이며 만료 전에 별도 재발급이 필요하다. 운영의 과거 `secret/pawbridge/dev/...` 경로는 운영이 사용하므로 개발용으로 취급하지 않는다.
Vault가 렌더링한 파일도 비밀값을 포함한다. 파일 권한·보관 위치는 보호해야 하며, 갱신했다고 실행 중인 컨테이너 환경변수가 자동 갱신되는 것은 아니다. 비밀 회전 시 해당 개발 서비스 재생성을 검증해야 한다. 인증에 필요한 초기 자격증명까지 없어지는 구조는 아니다.

참고: [Compose 환경변수](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/), [Vault Agent 템플릿](https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/template).

## IDE로 백엔드 수정

컨테이너로 실행한 같은 서비스를 먼저 멈추고 IDE에서 실행한다. 생성된 `<service>.env`를 IDE의 환경변수 입력/지원되는 env-file 플러그인으로 불러온다. 이 파일은 셸 스크립트가 아니므로 `source`로 실행하지 않는다. Gateway는 다른 서비스의 호스트 포트를 참조한다. 일부 서비스만 IDE로 실행할 때는 호출 대상의 해당 `*_SERVICE_URL`도 loopback 포트에 맞춘다. 컨테이너의 localhost는 호스트가 아니므로 컨테이너 gateway가 IDE 서비스에 자동 연결된다고 가정하지 않는다. 초기에는 여섯 서비스를 모두 컨테이너 또는 모두 IDE로 실행하는 방식이 명확하다.

| 대상 | 호스트 접속 |
|---|---|
| PostgreSQL | 127.0.0.1:15433 / DB pawbridge |
| Redis | 127.0.0.1:16379 |
| Kafka | 127.0.0.1:19092 |
| Kafka Connect | 127.0.0.1:18383 |
| Gateway | 127.0.0.1:28080 |
| User / Animal / Community / Store / Payment | 28081 / 28082 / 28083 / 28084 / 28085 |
| 개발 메일 수신함 / SMTP | 127.0.0.1:18025 / 11025 |
| 프론트 | 프론트 저장소에서 `npm run dev:local`, localhost:5184 |

## 자원과 외부 연동

- PostgreSQL 768MiB, Redis 256MiB, Kafka 1GiB, Connect 1GiB, local-access 64MiB 상한. 여섯 Java 앱은 각 640MiB이며 heap은 384MiB다. 필요할 때만 기동한다. 이 상한의 합이 실제 메모리 사용량을 뜻하지 않는다.
- 데이터·앱 네트워크는 internal이며, `local-access` NGINX만 별도 access 네트워크에서 loopback 포트를 받는다. 고정된 내부 서비스로만 TCP를 중계하며 범용 HTTP 프록시가 아니다. 내부 네트워크만 사용하면 Docker에서 호스트 포트가 노출되지 않는 실행 결과를 반영했다. 컨테이너에서 운영 VM·공개 API로 나가는 일반 경로를 제공하지 않는다. 실제 차단 여부는 실행 환경에서 검증한다. IDE 프로세스에는 이 Docker 네트워크 경계가 적용되지 않으므로 생성된 개발 설정을 확인한다.
- APMS·TourAPI·보호소 수집·사진 보관·갤러리 자동 갱신은 기본 중지한다. 기본 외부 키는 작동하지 않는 개발 자리표시자다. OAuth·실제 외부 메일 발송·Toss·R2·외부 LLM은 별도 dev 자격증명/허용 네트워크를 준비하기 전에는 실제 성공하지 않는다. 해당 미실행 범위를 전체 E2E 성공으로 표현하지 않는다.
- 사진 처리/CPU AI는 `optional-ai` profile이며 기본 기동하지 않는다. GPU는 기존 운영 모델을 중단하거나 두 벌로 로드하지 않는다. Python 저장소의 `deploy/dev`에 별도 localhost:18091 실행 경계를 정의한다.
- 앱 시작은 프로세스 생성일 뿐 기능 검증 완료가 아니다. 공개 조회, 인증, 쓰기, CDC·소비자 처리, 외부 테스트 연동을 구분해 확인한다.

## 중지·복구

```sh
python3 scripts/environments/local_dev.py stop
```

이 프로젝트의 컨테이너만 중지하고 개발 DB·Kafka 볼륨은 보존한다. `down -v`, 전체 Docker prune, 운영 VM 종료를 사용하지 않는다. 설정 변경 실패는 이전 Git revision의 실행 도구와 **같은 state 경로**로 복구한다. 스키마 변경은 별도 호환성/복원 계획이 필요하며 이미지 복귀만으로 되돌려졌다고 판단하지 않는다. 상태 디렉터리와 볼륨은 한 쌍이므로 비밀번호 파일만 삭제하지 않는다.

`smoke`는 여섯 서버 readiness, 동물·상품 공개 조회와 다섯 CDC task를 확인한다. `verify-cdc`는 고유한 dev 검증 토픽과 가상 Outbox 이벤트를 추가해 커밋 2건·롤백 1건의 전달, 키·헤더·JSON을 확인한다. 실제 업무 소비자의 상태 변경이나 OAuth·결제·GPU까지 검증하는 명령은 아니다. 검증 데이터는 로컬 dev에 남고 운영에는 쓰지 않는다.


## 실제 회원가입·로그인·게시글 검증

`python3 scripts/environments/local_flows.py`는 실제 Gateway/API를 통해 이메일 발송 → 코드 검증 → 일반 회원가입 → 비밀번호 로그인 → 내 정보 조회 → 텍스트 게시글 작성·조회·검색을 실행한다. 미인증 요청, 틀린 인증코드와 비밀번호의 거부도 확인한다. DB에 인증 완료 표시를 넣거나 JWT를 직접 만들어 인증을 우회하지 않는다.

개발용 메일은 `http://127.0.0.1:18025/`에서 JSON으로 확인한다. `tester@example.invalid`처럼 **@example.invalid** 주소만 수신하며 실제 수신자 주소는 SMTP 550으로 거부한다. 외부 SMTP로 중계하지 않는다. 메일은 최근 50개·각 최대 64KiB만 메모리에 보관하며 메일 수신기를 재시작하면 사라진다. SMTP 11025는 IDE 실행용 loopback 포트다. 컨테이너에서는 `mail:1025`로 접속한다.

추가 설치를 피하기 위해 이미 보유한 Python 3.11 이미지의 `smtpd` 표준 라이브러리를 사용한다. 이 모듈은 Python 3.12에서 제거됐으므로 이미지 digest를 유지하고, 버전 갱신 시 메일 수신기를 대체해야 한다. 운영 메일 서버가 아니며 dev의 internal network와 loopback 포트에서만 실행한다. 메모리 상한은 64MiB다.

게시글 검색은 PostgreSQL 쓰기 트랜잭션에서 갱신되므로 검색 성공만으로 Kafka 소비자 성공을 주장하지 않는다. 별도로 존재하지 않는 가상 동물에 찜 요청을 한 번 보내 `User Outbox → CDC → Kafka → Animal 소비자 재시도/보상 Outbox → CDC → Kafka → User 보상 소비자`를 확인한다. 테스트용 찜이 제거되고 보상 처리 기록까지 저장되어야 통과한다. 이 과정에서는 개발 로그에 의도적인 동물 없음 오류/재시도가 남는다.

한 번 실행할 때 합성 계정 1개와 텍스트 게시글 1개를 만든다. 개발 DB에 보존하며 자동으로 데이터를 지우지 않는다. 실행 결과는 state의 `flows-result.json`, 마지막 성공한 계정은 `last-verification-account.json`에 0600 권한으로 저장한다. 비밀번호·JWT·인증코드는 결과 보고서나 콘솔에 출력하지 않는다. 이전 단계 실패 시 그때까지 생성된 합성 데이터가 남을 수 있다.

이 검증은 API 통합 검증이다. 브라우저 전체 사용자 흐름, Google OAuth, 실제 이메일 전달, Toss 결제, R2 업로드, SAM3/DINOv3 품질 검증은 포함하지 않는다. Google 로그인은 아래 선택 설정으로 별도 연결한다. Toss·R2 등 나머지 외부 연동은 미설정이다.


## 기존 Google OAuth 클라이언트로 로컬 로그인

Google 등록 앱의 Client ID·Secret만 운영과 공유할 수 있다. 개발 서버·DB·회원·JWT·Redis·세션은 별개다. 공유 키를 교체하거나 해당 Google 클라이언트를 삭제하면 양쪽이 영향을 받는다. 사용자 승인 없이 운영 Secret을 자동 조회하거나 복사하지 않는다.

기존 웹 클라이언트의 승인된 리디렉션 URI에는 `http://localhost:28080/login/oauth2/code/google`가 필요하다. Google 동의 화면의 승인된 도메인과는 다른 설정이다. 프론트는 `http://localhost:5184/login`으로 접속한다. `localhost`와 `127.0.0.1`은 쿠키/브라우저 저장소가 다르므로 OAuth를 시험할 때는 localhost로 통일한다.

Git 밖의 state 디렉터리에 `google-oauth.env`를 0600 권한으로 준비한다. 허용 필드는 `GOOGLE_CLIENT_ID`, `GOOGLE_SECRET_KEY` 두 개뿐이다. 다른 서비스의 비밀값을 넣지 않는다. 실제 값을 채팅·명령행 인자·Git에 넣지 않는다.

`prepare` → `config` → `up-apps` 순서로 반영한다. 해당 파일이 없으면 Google 연동은 비활성 설정 그대로다. 파일이 있으면 user-service에만 주입하고 Google 전용 중계 컨테이너를 시작한다. 중계는 최대 64MiB이며 공개 포트를 열지 않는다. `/token`, `/userinfo`, `/jwks`만 고정된 Google HTTPS 주소로 연결하고 서버 인증서를 검증한다. 사용자 서버 자체는 internal network를 유지한다. IDE용 user-service.env는 기존 Google HTTPS 기본 경로를 사용한다.

Google 로그인 화면으로 이동했다는 것만으로 완료 처리하지 않는다. 사용자가 Google에서 로그인한 후 localhost 프론트로 돌아오는 것, 개발 DB의 Google 회원·refresh token 저장, 개발 JWT로 내 정보 조회가 확인되어야 전체 로그인 성공이다. Google 비밀번호·MFA·동의는 사용자가 직접 진행한다.

로컬 연결을 되돌릴 때는 user-service를 중지하고 `google-oauth.env`를 Git 밖의 별도 비공개 경로로 보관한 뒤 `prepare`와 `up-apps`를 실행한다. 기존 `google-oauth-egress` 컨테이너가 남아 있으면 해당 컨테이너만 중지한다. 운영 Google 클라이언트나 운영 주소는 삭제하지 않는다.
