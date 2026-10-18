# Mac 일반 서비스 운영 구성과 예약·CDC 검증

이 폴더는 Mac 운영 구성의 예약 제어와 CDC 정의를 관리한다. 전체 Compose 정본은 [background-jobs-20261016/compose.json](../background-jobs-20261016/compose.json)이며, 실제 실행본·active 설정·백업 경로 override의 경계는 [상위 실행 안내](../README.md)를 따른다. 이 파일들로 Windows 종료·공개 전환·기존 데이터 복구를 자동 실행하지 않는다.

## 출처와 실행 경계

- Backend 기준: `501d513b76a7340c6a03fcb34ebb55f0d1984650`.
- Infra 기준: `281a511458a215eee4181d89d9938796907ea6b4`, 기존 `chore/mac-compose-bootstrap` 작업 폴더.
- 위 SHA는 구성 준비 당시 기준이다. 운영 이미지에 포함된 후속 변경의 출처는 비공개 실행 receipt와 해당 소스 변경으로 별도 대조한다. 과거 후보 release를 현재 배포 manifest로 사용하지 않는다.
- 운영 비밀값과 렌더 결과는 저장소 밖의 `pawbridge-secrets/runtime/production`에서 관리한다. 실제 env·credential·receipt는 이 폴더에 복사하지 않는다.
- `mac_compose_operating_bundle.py`는 파일만 생성한다. Docker·SQL·외부 API를 실행하지 않는다. `receipt.json`의 `activationAllowed=false`를 유지한다.

## 예약 모드의 의미

| 모드 | 새 수집·사진 보관 | 정리·홈 영상 갱신 | 장바구니 DB 동기화·검색 projection |
| --- | --- | --- | --- |
| standby | 끔 | 끔 | 끔 |
| drain | 끔 | 끔 | 켬 |
| active | 켬 | 켬 | 켬 |

`standby`는 **읽기 전용 모드가 아니다**. API·수동 작업·Kafka 소비자는 유지된다. 검증과 전환 대기에는 공개 진입과 수동 실행을 별도로 차단한다. CDC heartbeat·PostgreSQL 내부 유지관리도 계속된다. 모든 쓰기를 멈추려면 해당 앱/소비자/Connect를 단계에 맞춰 정상 종료해야 한다.

- `pawbridge.jobs.cleanup-enabled`: Animal/User Outbox, Community 정리·채팅 만료·쪽지 만료. 기본값 true로 기존 동작을 보존한다. 채팅 탈퇴 처리와 공용 Clock bean은 비활성화하지 않는다.
- `pawbridge.jobs.refresh-enabled`: Community 홈 영상 갱신.
- `pawbridge.jobs.cart-sync-enabled`: Store 장바구니 동기화. 기본값 true, 10초 간격.
- 기존 APMS·TourAPI·Photo·projection 속성을 명시한다. APMS는 한국 시간 30분 간격, Photo는 Windows 회신 기준 60초다. ShelterDirectory는 새로 활성화하지 않는다.
- Spring `@Scheduled` 목록만으로 SmartLifecycle APMS·Photo의 정지 여부를 단정하지 않는다. 해당 활성 속성과 소스/기존 전용 실행 검증도 함께 본다.
- 환경 파일 변경은 실행 중 JVM에 즉시 반영되지 않는다. 변경된 모드로 해당 **운영 후보 컨테이너를 재생성**해야 한다. 단순 restart는 기존 환경을 유지한다.
- 아래 격리 시험 기록은 당시 검증 범위다. 현재 운영 작업의 실행 여부나 마지막 성공은 비공개 실행 기록에서 별도로 확인한다.

## 새 대상의 데이터·연결 준비 순서

다음은 승인된 새 빈 대상을 준비하는 계약이다. 기존 운영 볼륨에 초기화·복원·slot 생성을 다시 실행하는 절차가 아니다.

1. Windows의 신규 쓰기 차단, 남은 업무·cart·소비 완료, 최종 백업 및 Windows 자동 재시작 차단은 최종 승인 범위다. 새 대상에는 승인된 최종 백업을 사용하며 과거 검증 dump로 대체하지 않는다.
2. 새 PG 저장소를 `postgres` 초기 관리자(OID 10), PG 17.11, `en_US.utf8`로 초기화한다. 검토한 원본 역할 SQL에서 이미 존재하는 `CREATE ROLE postgres;` 한 문장만 생략하고 나머지 속성·멤버십·owner·ACL을 보존한다. 이 예외는 원본 SQL 파일에 덮어쓰지 않는다.
3. 전체 custom dump를 `pg_restore --create --exit-on-error`로 복원한다. 기존 DB를 재사용하거나 `--clean`으로 덮어쓰지 않는다. 원래 5개 publication을 보존하고 subscription/slot은 새로 연결하지 않는다.
4. 새 DB에만 새 역할 비밀번호를 적용한다. `pg_hba.conf`는 local/TCP 모두 SCRAM이며 이 폴더의 기본 HBA는 `postgres` TCP를 loopback으로 제한한다. 전체 운영 정의는 background 폴더의 HBA로 전용 백업망 주소도 허용한다. 앱·CDC·vector 역할의 인증, 최소 권한, 확장 소유자를 검증한다.
5. 빈 Kafka 저장소를 별도 init 단계에서 format하고 `topics.json`을 생성한다. 원본 Kafka 데이터나 Connect 내부 offset 토픽을 복사하지 않는다. Redis는 최종 중단 시 0키·미반영 장바구니 없음이 다시 확인될 때만 빈 저장소로 시작한다.
6. 앱/수동 쓰기를 시작하기 전에 `connectors.json`의 5개 커넥터를 등록한다. 원본 publication, 새 `pawbridge_mac_*` slot, `snapshot.mode=no_data`, `publication.autocreate.mode=disabled`, `slot.drop.on.stop=false`를 사용한다.
7. connector/task RUNNING뿐 아니라 slot 5개 활성·flush 위치와 새 이벤트 전달을 확인한다. **slot 생성 전 쓰기는 no_data로 복구되지 않는다.** 과거 Outbox가 존재해도 최종 원본 처리 완료 근거가 있으면 재발행하지 않는다. 그 근거가 없으면 진행하지 않는다.
8. 동일 DB/slot/Kafka 저장소로 재시작한다. offsets나 slot을 임의 삭제·재생성하지 않는다. WAL 보관 한도 초과/slot 무효화는 운영 중단·복구 판단 대상으로 둔다.

## 컨테이너·비밀값

- 이미지 ID 고정, `pull_policy=never`, 비루트, capability 제거, 읽기 전용 루트, CPU/메모리/PID·로그 크기 제한.
- PG·Redis·Kafka·Connect는 내부 네트워크만 사용한다. 앱의 외부 연동 네트워크와 구분한다.
- 전체 운영 정의에서는 PG가 직접 호스트 포트를 열지 않고 로컬 접근 중계가 `127.0.0.1:35436`의 PG와 `127.0.0.1:35437`의 갤러리 feed를 제공한다. Gateway는 `127.0.0.1:38300`을 사용한다. Tunnel은 별도 정의다.
- Kafka/Connect 로그는 읽기 전용 이미지 내부가 아닌 제한된 `/tmp`에 기록한다. Redis는 AOF everysec, RDB, 384MiB/noeviction이다.
- `env_file`은 Compose 2.30+의 `format: raw`를 사용한다. `.env` 값으로 이미지를 빌드하지 않는다.
- 서버용 키를 서비스별로 배분한다. 앱에 postgres 관리자·CDC·백업 R2 키를 주지 않는다. Animal에는 사진 원본 보관과 갤러리 서명에 필요한 별도 원본 R2 필드를 연결한다. 프런트용 키는 앱 env에서 제외한다.
- PostgreSQL·Kafka 데이터 디렉터리 초기화와 복원은 별도 절차다. 이 파일들을 만들었다는 이유로 `compose up`을 실행하지 않는다.

## 2026-10-16 격리 검증 근거

2026-10-16 검증에서는 원본 덤프 전체를 새 DB에 복원하고 **빈 Kafka**로 시작했다. 기존 Mac/Windows와 기존 AI는 유지했다. 시험망은 internal/no host ports이며 외부 키 대신 합성 값, native 중계는 호스트 접근 대신 503을 반환한다.

- 새 12개 역할 TCP 인증, PG/확장/locale·publication 5개 확인.
- 과거 Outbox 425개(Store 422, Payment 3)의 재발행 0건.
- 정상 가동 중 5건 + Connect 정지 중 5건: topic/key/header/payload 일치. 같은 slot과 offset으로 재개, 관찰된 중복 0건. Community의 빈 images 배열 유지도 확인했다.
- Java 6개/Python·Photo readiness, 실제 새 계정 접속, 예약 대기 상태 확인. 별도 scheduler 억제 agent는 사용하지 않았다.
- 최초 찜 이벤트를 소비자 시작 전에 넣고 한 번 처리, 취소 이벤트로 격리 데이터의 count 원복.
- cart timer를 끈 12초 동안 DB 반영 없음, drain으로 전환 후 10초 주기 동기화로 반영. Animal/Community/Store projection 등록과 cleanup 계속 비활성 확인.
- Backend 단위 테스트 9개, 운영 설정·비밀값 계약 테스트 30개 통과. 실제 외부 수집·AI 갤러리 갱신/품질·장기 장애·재부팅 시험 결과는 포함하지 않는다.

격리 실행 세부 로그·데이터·REPORT는 사용자 로컬의 `operating-verification-*` 폴더에 보존한다. 최신 결과는 Obsidian의 이관 계획에 기록한다.

실제 native/monitor 관리는 Application Support 아래 관리 실행본을 사용한다. 이 폴더의 기본 Compose 생성 결과만으로 native source·Python/SAM3 경로·LaunchAgent 설치·active env·백업 override까지 재현했다고 판단하지 않는다. 실제 OS 재부팅 후 전체 자동 복귀는 미검증이다.
