# 회원 채팅의 두 앱 검증

이 override는 기존 로컬 `pawbridge-dev`의 PostgreSQL·Redis·회원 서비스를 재사용한다.
운영 VM·Vault 값·R2·운영 DB와 연결하지 않는다. 기존 데이터 볼륨을 삭제하지 않는다.

## 준비

Community와 Gateway의 승인된 기능 브랜치에서 Java 17로 `test bootJar`를 실행한다.
User 서비스 소스는 변경하지 않으며 같은 기준본의 기존 내부 회원 계약을 빌드한다.
오래된 dev 이미지에 해당 계약이 없으면 최신 기준본의 User JAR도 시험에 mount한다.
Community는 `migrationDistribution`도 빌드한 뒤 기존 dev 절차로 V10까지 마이그레이션한다.
마이그레이션 전 `public.pawbridge_local_environment`의 이름이 `dev`인지 확인한다.
운영 URL을 입력받거나 기존 마이그레이션 이력을 repair/clean하지 않는다.

`MEMBER_CHAT_COMMUNITY_JAR`와 `MEMBER_CHAT_GATEWAY_JAR`에는 검증한 두 JAR의 절대 경로를 지정한다.
`MEMBER_CHAT_USER_JAR`는 같은 기준본의 변경하지 않은 User JAR 경로다.
기존 dev의 비공개 runtime·compose·images env 파일과 기본 compose.yaml을 사용하면서
`-f environments/dev/compose/compose.chat-test.yaml --profile chat-test`를 추가한다.
비밀이 포함되는 일반 `config` 대신 `config --quiet`로 검사한다.

기존 Gateway는 Community A, loopback `28180`의 테스트 Gateway는 Community B에 연결한다.
서로 다른 두 브라우저의 WebSocket을 A/B에 연결해 공유 Redis 전달을 검증한다.
브라우저 화면은 기존 dev 프론트 `5184`를 사용하며 `VITE_MEMBER_CHAT_ENABLED=true`를 지정한다.
Gateway B에는 개발 계정의 동일한 JWT 인증을 적용하며 인증 필터를 우회하지 않는다.

## 실행

이 저장소의 `scripts/environments/member_chat_test.py`는 기존 비공개 dev 상태 파일을 사용한다.
`--backend`에는 위 세 JAR를 빌드한 기능 작업 폴더를 지정한다. 기본 실행 중인 앱이 이번
시험의 기동 기록에 없으면 재생성을 거부한다. 승인된 시험 앱만 새 JAR로 재생성한다.

```bash
python3 scripts/environments/member_chat_test.py --backend /absolute/backend-worktree migrate
python3 scripts/environments/member_chat_test.py --backend /absolute/backend-worktree up
python3 scripts/environments/member_chat_test.py --backend /absolute/backend-worktree accounts
```

`accounts`는 실제 로컬 이메일 검증·가입·로그인 API로 합성 회원 3명을 만든다. 계정 값은
비공개 상태 폴더의 `member-chat-test-accounts.json`에 저장하고 출력하지 않는다.
프론트 실행기에는 `PAWBRIDGE_CHAT_FIXTURE_FILE`, `PAWBRIDGE_PLAYWRIGHT_MODULE`,
`PAWBRIDGE_BROWSER_EXECUTABLE`의 기존 절대 경로를 지정한다. 실행기는 도구를 설치하지 않는다.

프론트 기능 폴더에서 아래 검사를 **순차** 실행한다. 차단·기록을 생성하는 시나리오를
재실행하거나 기존 쪽지를 검사할 때는 `accounts`로 별도 합성 회원을 준비한다.

```bash
node tests/run-member-chat.browser.cjs
PAWBRIDGE_CHAT_RECOVERY=true node tests/run-member-chat.browser.cjs
PAWBRIDGE_CHAT_SECURITY=true node tests/run-member-chat.browser.cjs
```

복구 시나리오는 실제 앱 확인 응답 한 번을 브라우저에서 누락시키고, 시험 peer 앱을
중지·재기동한다. 실제 로컬 Redis 일시 정지는 최대 6초이며 자동 복구와 종료 시 복구를
적용한다. Redis 장애와 보안 연결 상한 시험을 동시에 실행하지 않는다. 이 검사는 Kubernetes
롤링 배포·공개 프록시·운영 용량을 검증한 결과가 아니다.

## 필수 확인

- 첫 메시지 전에 방 없음, 양방향 첫 메시지 경쟁에도 회원 쌍당 방 하나.
- 저장 뒤 앱 확인 응답, 같은 전송 키 재시도와 따닥 전송에 원문 하나.
- A/B 간 새 수신, 본문 없는 알림, 오프라인 로그인 복구, 연결 재시작 후 누락 조회.
- 타인 방 조회·구독·위조 발신·재사용 티켓 거부, 허용 Origin·로그인 만료.
- 읽음은 활성 창에 보인 메시지까지만, 숨김은 개인 상태이고 새 수신으로 복귀.
- 기존 쪽지의 전송·SSE·차단 기능 유지.

## 종료와 복구

기동 전 실행 중인 컨테이너 목록을 보존한다. 완료 후 이번 검증으로 기동한 서비스만 `stop`한다.
`down -v`, volume prune, 다른 프로젝트 중지, 운영 재배포를 실행하지 않는다.
테스트 peer 두 서비스는 다음 검증에서 재사용할 수 있는 중지 상태로 둔다.
기본 서비스의 임시 JAR mount는 서비스 중지 후 override 없이 `up --no-start --no-deps`로
기본 이미지 설정을 복원한다. 생성된 기능 이미지·로컬 빌드 결과는 운영 배포가 아니다.
