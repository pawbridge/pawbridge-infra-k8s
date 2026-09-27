# 보호소 동물 탐색 운영 반영

이 절차는 배포 준비 자료다. PR 병합, DB 변경, Argo 동기화와 프론트 main 반영에는 최종 승인이 필요하다. 기존 MySQL schemaMigration Job을 켜지 않는다.

## 대상과 변경

- Backend #109의 dev 병합 `03d7a9e5d58b783848be647f79f29d1b98a33abf`에서 게시한 API·migration 이미지 쌍을 `environments/prod/values/animal-service.yaml`에 지정한다.
- PostgreSQL `pawbridge` DB의 `pawbridge_animal` 스키마에 V8을 적용한다. 기존 테이블·행을 삭제하거나 과거 관측값을 소급 생성하지 않는다.
- V8 원본은 백엔드 `animal-service/src/migration/resources/db/postgresql/V8__shelter_daily_observations.sql`이다. SHA-256: `b9b4eb1b11a3b468af00c205b68b11f0cad9b1fe744e1cd16c067bdc19ba7262`.
- 새 테이블은 `shelter_daily_observations`, 새 인덱스는 `idx_animals_shelter_protect_intake`다. 일별 기록은 웹 프로세스에서 KST 04:00에 생성한다. 첫 기록 전 빈 그래프는 정상이다.

## 적용 전 중단 조건

1. 운영 context·namespace·Application source와 현재 이미지 digest를 확인해 비공개 작업 기록에 보관한다. 이 PR은 다른 앱의 revision이나 설정을 갱신할 권한이 아니다.
2. 최신 PostgreSQL 백업의 파일·해시·복원 근거를 확인한다. 전환 전 오래된 MySQL 백업으로 대체하지 않는다.
3. 운영 Flyway `info`가 V1~V7 성공, V8 대기인지 확인한다. 예상 외 버전·checksum 불일치·실패 이력이 있으면 중단한다. `baseline`, `repair`, `clean`으로 우회하지 않는다.
4. 검증한 이미지와 운영 후보 digest가 같아야 한다. 격리 합성 데이터 검증은 실제 운영 데이터 성능·전체 Kafka 흐름 검증과 구분한다.
5. CREATE INDEX는 완료까지 동물 테이블 쓰기를 기다리게 할 수 있다. APMS 배치·장기 트랜잭션과 겹치지 않는 승인된 시간에 실행한다. 실패 시 API를 먼저 배포하지 않는다.

## PostgreSQL V8 적용

기존 PostgreSQL 전용 실행기 `com.pawbridge.animalservice.migration.AnimalPostgresqlMigration`을 사용한다. 이 실행기는 의도적으로 loopback JDBC URL만 받는다. 승인된 SSH 또는 port-forward로 정확한 DB에 연결하고 기존 인증 전달 절차를 사용한다. 접속 제한을 풀거나 운영 비밀번호를 명령 인자·출력·Git에 넣지 않는다.

운영의 `pawbridge_animal_migration`은 평소 `NOLOGIN`이다. 별도 승인 후 기존 비밀번호 검증값과 유효기간을 비공개 복구 파일에 보존하고, 이 역할 하나에만 단기 비밀번호와 만료 시각을 설정해 작업 동안 `LOGIN`을 허용한다. 앱 계정의 권한이나 비밀번호를 변경하지 않는다. 성공·실패 모두 `finally`에서 `NOLOGIN`과 원래 인증 상태를 복원하고 작업 세션이 남지 않았는지 확인한다. 만료 없음은 `NULL` 또는 `infinity`로 표현될 수 있으므로 의미를 비교하며 시스템 카탈로그를 직접 수정하지 않는다. 정리 실패 시 배포를 중단하고 보존한 복구 자료로 먼저 계정을 잠근다.

백업을 격리 복원할 때 운영 볼륨을 연결하지 않는다. 실제 덤프에는 임베딩이 포함될 수 있으므로 데이터 디렉터리를 메모리 제한보다 큰 tmpfs에 두지 않고 이번 검증 전용 디스크 볼륨을 사용한다. 복원 완료·검증 후 해당 컨테이너와 볼륨만 제거하고 외부 백업은 보존한다.

실행기에 전달할 환경변수는 다음 네 개다. 값은 비공개 환경 파일이나 비밀 전달 방식으로 공급한다.

- `ANIMAL_PG_MIGRATION_JDBC_URL`: `jdbc:postgresql://127.0.0.1:<확인한 포트>/pawbridge`
- `ANIMAL_PG_MIGRATION_CONFIRM_TARGET`: 위 URL과 정확히 동일한 값
- `ANIMAL_PG_MIGRATION_USERNAME`: 확인한 스키마 소유자 또는 승인된 migrator
- `ANIMAL_PG_MIGRATION_PASSWORD`: 해당 계정의 비밀값

동일 migration 이미지의 `/opt/migration/lib/*` classpath로 `info` → 승인 후 `migrate` → `validate`를 실행한다. API 이미지의 기본 entrypoint를 migration 실행으로 오인하지 않는다. 같은 네트워크 공간에서 loopback 터널이 보여야 한다.

V8 성공 후 앱 계정의 `USAGE`와 새 테이블 `SELECT`, `INSERT` 권한을 확인한다. 스키마 소유자의 기본 권한 설정에 따라 자동 부여될 수 있으므로 먼저 `has_schema_privilege`, `has_table_privilege`로 검사한다. 부족할 때만 승인된 역할에 새 테이블 권한을 부여한다. 슈퍼유저·전체 DB 권한을 앱에 부여하지 않는다.

마이그레이션 전후 기존 동물·보호소 건수와 Flyway 상태를 비교한다. 새 테이블이 비어 있는지 확인하고 테스트용 관측 행은 운영에 삽입하지 않는다.

## API와 화면 반영 순서

1. PostgreSQL 적용·권한·기존 데이터 보존 근거를 확인한다.
2. 후보와 현재 운영 설정을 렌더링해 이미지 이외의 Secret 참조·DB 주소·자원·스케줄이 같은지 확인한다. `schemaMigration.enabled: false`를 유지한다.
3. 승인한 Animal Application 하나만 새 revision으로 동기화하고 Ready·오류 로그·기존 동물 검색을 확인한다. 다른 서비스나 CronJob을 임의 실행하지 않는다.
4. 공개 `GET /api/shelters/discovery`와 `/api/shelters/{id}/observations`를 확인한다. 보호소 카드와 `/api/animals`의 보호소·접수일·PROTECT 조건 및 건수가 일치해야 한다.
5. 프론트 운영용 PR을 main에 반영한 뒤 데스크톱·모바일에서 목록 → 동물 검색 → 목록 복귀를 확인한다. 최초 이력은 다음 04:00 실행 후 별도로 확인한다.

## 롤백

이전 API digest와 Animal Application revision을 복원한다. 프론트가 먼저 공개됐다면 이전 프론트 배포도 복원한다. V8의 테이블·인덱스·이미 생성된 관측값은 보존한다. 이전 API는 추가 테이블에 의존하지 않는다. DB 전체 복원은 배포 후 새 업무 데이터를 잃을 수 있으므로 이미지 롤백과 별도 판단한다.

## 관련 변경

- Backend: https://github.com/pawbridge/pawbridge-backend-k8s/pull/109
- Frontend dev: https://github.com/pawbridge/pawbridge-frontend/pull/168
- 개발 이미지 참조: https://github.com/pawbridge/pawbridge-infra-k8s/pull/291 (로컬 Compose용, 운영 반영 아님)
