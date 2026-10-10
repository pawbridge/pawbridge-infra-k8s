# 회원 채팅 운영 반영

이 절차는 `values/community-service.yaml`, `values/api-gateway.yaml`과
`gitops/security/redis-auth-vso/client-vault-static-secret.yaml`에 연결된다.
PR 병합만으로 현재 운영 Application이 동기화되지는 않는다.
운영 DB 쓰기·Application 변경·동기화·프론트 main 병합은 최종 승인 후 실행한다.

## 배포 전에 확인한다

1. 현재 운영 VM·Kubernetes context·namespace와 대상 두 Application을 식별한다.
   현재 사용하는 대상은 `pawbridge` namespace의 Community와 Gateway다.
   Application 이름의 `dev`만 보고 개발 환경으로 판단하지 않는다.
2. 현재 Application의 source·inline values·이미지와 프론트 활성 버전을
   비공개 복구 폴더에 보존한다. 오래된 Git 이미지 값을 롤백 기준으로 사용하지 않는다.
3. 게시된 Community 앱·migration 이미지가 같은 소스이고 Gateway가 해당 채팅
   경로를 지원하는 소스인지 OCI 라벨과 digest를 대조한다.
4. 기존 `redis-client-auth`의 Ready·Healthy와 `pawbridge/redis-auth`의
   `redis-password` 필드가 공급되는지 값 출력 없이 확인한다.
   Redis 비밀번호·Vault 경로·정책은 새로 만들거나 교체하지 않는다.

## Community V10을 먼저 적용한다

1. Community 스키마를 VM 밖에 백업하고 격리 복원한다.
   복원한 V1~V9의 이력·체크섬·업무 데이터와 같은 소스의 pending V10을 검증한다.
2. PostgreSQL 전용 승인 절차로 V10만 적용한다.
   스키마 적용 역할의 소유권, 앱 CRUD 권한, 앱 schema CREATE 금지를 확인한다.
   loopback 대상·DB 식별자·버전 제한을 완화하지 않는다.
3. 새 채팅 테이블 4개와 Flyway V10 성공, 기존 업무 데이터 보존을 확인한다.
   실패하면 앱 배포로 넘어가지 않는다.

`schemaMigration.enabled=false`를 유지한다. 기존 Helm Job은 MySQL용이며
새 PostgreSQL migration 이미지와 호환되지 않는다. `existingSchemaVerified=false`와
빈 `recoveryReference`는 이 PR이 운영 V10·복원을 완료했다는 주장을 하지 않기 위한 값이다.
기존 MySQL 검증 기록을 PostgreSQL V10의 증거로 사용하지 않는다.

## Community와 Gateway를 반영한다

1. 현재 inline values를 보존하면서 이 PR의 승인된 차트·prod 값과 합쳐 렌더한다.
   Community의 이미지·채팅 환경·Redis Secret 참조와 Gateway 이미지만 달라지는지 확인한다.
2. 기존 Redis 공급 리소스에는 Community restart target 하나만 추가한다.
   다른 소비자·인증·필드·KV 값은 유지한다. 이번 적용 때문에 Redis를 재시작하지 않는다.
3. Community를 동기화하고 이미지·Ready·Redis 인증·기존 쪽지와 영상 조회를 확인한다.
4. Gateway를 동기화하고 외부 HTTPS·WebSocket 접속과 인증 거부 경로를 확인한다.

Community Redis는 HOST·PORT와 Secret 기반 PASSWORD로 연결한다.
`SPRING_DATA_REDIS_URL`은 렌더에서 제외한다. URL은 별도 password 설정보다
우선하므로 두 설정을 함께 공급하지 않는다.
[Spring Boot Redis 설정 설명](https://docs.spring.io/spring-boot/3.4/appendix/application-properties/index.html#application-properties.data.spring.data.redis.url).

운영 Redis 구분값은 `pawbridge-prod`이며 로컬 `local-dev`와 섞지 않는다.
허용 Origin은 `https://www.pawbridge.kr`, `https://pawbridge.kr` 두 개다.
wildcard나 로컬 개발 주소를 운영 목록에 추가하지 않는다.

## 프론트 main을 승격하고 검증한다

1. 최신 main의 다른 작업을 보존하고 승인된 채팅 변경만 별도 PR로 승격한다.
2. 기존 Cloudflare main 빌드의 `VITE_MEMBER_CHAT_ENABLED=true`를 승인 후 공급한다.
   Vite 빌드 값이며 Worker 런타임 binding만 바꾸면 화면에 적용되지 않는다.
3. main 자동 빌드 성공·커밋·활성 버전을 확인한 뒤 전용 검수 계정으로
   대화 시작·수신·답장·읽음·다시 접속·차단을 데스크톱과 모바일에서 검증한다.
   운영 시험 메시지 작성·정리도 승인 범위에 포함한다.

## 실패하면 해당 앱만 복구한다

- V10 실패: Community·Gateway·프론트를 배포하지 않는다.
- 앱 실패: 저장한 직전 Application spec·이미지로 대상 앱만 복구하고 readiness를 확인한다.
- 프론트 실패: 직전 활성 Cloudflare 버전으로 복구한다.
- 새 채팅 데이터가 생긴 뒤에는 V10 테이블·Flyway 이력을 자동 삭제하지 않는다.
  DB 전체 복원은 새 업무 쓰기를 잃을 수 있으므로 별도 판단과 승인이 필요하다.

이 PR에는 실제 migrate·sync·Secret 변경·Cloudflare 설정 변경·운영 검수가 포함되지 않는다.
