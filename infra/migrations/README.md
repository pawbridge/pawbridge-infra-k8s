# Flyway 배포 연결과 실패 복구

## 적용 범위와 현재 상태

이 절차는 5개 서비스의 `schemaMigration` Helm 설정, `accounts/`, `vault-roles/`, `tests/`에 대응한다.
현재 변경은 기본 비활성 구성이다. 계정 생성·비밀번호 설정·Vault 등록·Argo Application 등록·baseline·배포를 실행하지 않는다.
기존 서비스의 Hibernate 값이나 Argo 자동 배포 설정도 바꾸지 않는다.

DB 변경과 새 API 배포를 같은 승인된 릴리스로 묶기 위해 Job, Secret 참조, 해당 AppProject의 Job 허용을 함께 관리한다.
백엔드 migrationDistribution과 API 이미지의 동일 소스 리비전 여부는 별도 확인한다.
문자열 digest 일치 검사는 이미지 내용이나 출처를 증명하지 않는다.

## 활성화 전에 준비할 것

1. 대상 컨텍스트·namespace·서비스·스키마와 현재 API digest, 적용할 migration digest를 기록한다.
2. 현재 운영 DDL을 다시 대조한다. 이전 조사 시점 이후 변경된 테이블을 누락하지 않는다.
3. 백업 위치·생성 시각·SHA-256·보존 기간과 허용 가능한 데이터 손실(RPO), 복구 시간(RTO)을 정한다.
4. 별도 MySQL에 복원해 행 검증·Flyway 이력·재시작 후 지속성을 확인한다. 이 리허설의 시험 데이터 성공만으로 운영 백업을 검증했다고 표시하지 않는다.
5. 초기 편입은 서비스별 V1 baseline 대상으로 별도 승인받는다. baseline은 구조 일치를 검사하지 않으므로 DDL 비교가 선행돼야 한다.
6. 아래 계정·Vault 준비를 끝내고 Secret Ready와 필요한 키의 존재만 확인한다. 값은 출력하지 않는다.
7. API와 배치의 Hibernate 자동 DDL을 중단하고 이전 API도 신규 스키마에서 동작하는지 확인한다.
8. 아래 로컬 검증을 통과한 뒤 비운영 Argo에서 PreSync 실패 차단·실패 Job 보존·승인된 재개를 시험한다.
9. 위 근거가 준비된 서비스만 활성화한다. 현재 운영 동시 변경은 승인 범위에 포함하지 않는다.

### 동시 작업과 소스 변경을 먼저 확인할 것

운영 적용 전에는 다음 경계를 다시 확인한다. 이전 로컬 시험이 성공했더라도 이 확인을 생략하지 않는다.

1. 대상 서비스의 실행 중 Job, CronJob, 이력 보정과 수동 데이터 작업을 확인한다. 해당 스키마의 보정 작업과 baseline·DDL 전환을 동시에 진행하지 않는다. 다른 작업을 임의로 중단하지 말고 담당 작업과 적용 시간을 조정한다.
2. 백엔드와 인프라의 최신 `origin/dev`, 실제 배포 API digest를 확인한다. 작업 시작 이후 변경이 있으면 엔티티·SQL·Gradle 패키징·배포 값에 미치는 영향을 대조한다.
3. 로컬 변경을 보존한 상태로 최신 소스를 통합하고 영향받은 패키징 시험을 다시 실행한다. 커밋·병합·push는 각각 승인 범위에 맞춰 실행한다.
4. 기존 VaultStaticSecret의 Ready는 기존 앱 인증의 근거로만 사용한다. 신규 migration 전용 role·KV·VaultAuth·Secret 준비와 실제 인증을 별도로 확인한다.
5. 운영에 설치된 CRD의 필드 지원 여부와 실제 인증 성공을 구분한다. 읽기 전용 CRD 대조만 통과한 상태를 배포·인증 검증 완료로 기록하지 않는다.

## 전용 계정과 Vault를 연결할 것

서비스별 고정 매핑은 다음과 같다. `<service>`는 animal, user, community, store, payment 중 하나다.

- DB: `pawbridge_<service>`
- DB 계정: `pawbridge_<service>_migrator`
- Vault KV-v2: `secret/pawbridge/dev/<service>/schema-migration`, 필드 `password`
- Vault policy와 Kubernetes auth role: `<service>-schema-migration-read`
- 인증 ServiceAccount: `<service>-schema-migration-vault`, namespace `pawbridge`, audience `vault`
- Kubernetes Secret: `<service>-schema-migration`, 필드 `password`

`accounts/<service>.sql`은 대상 스키마에만 권한을 주며 계정을 **잠긴 상태로** 만든다.
기존 계정이 있으면 실패한다. 자동 비밀번호 교체나 기존 계정 수정은 하지 않는다.
DROP·전역 권한·GRANT OPTION은 주지 않는다. 추후 삭제 DDL은 별도 권한·복구 검토가 필요하다.

승인된 관리자 세션에서 다음 순서로 준비한다.

1. 기존 계정과 KV 경로의 존재 여부를 확인한다. 하나라도 존재하면 값을 덮지 말고 불일치를 조사한다.
2. 대상 서비스의 SQL만 실행한다. 잠긴 상태인지와 SHOW GRANTS 결과를 확인한다.
3. 안전한 입력·표준입력 경로로 새 비밀번호를 Vault에 최초 등록하고 동일 값을 DB 계정에 설정한다. 화면·명령 인자·Git·임시 평문 파일에는 남기지 않는다.
4. 양쪽 등록이 확인된 뒤 계정을 해제한다. 중간 실패 시 계정을 잠근 채로 유지한다. 자동 삭제하거나 재발급하지 않는다.
5. `infra/vault/policies/<service>-schema-migration-read.hcl`과 `vault-roles/<service>.json`을 각각 해당 policy와 `auth/kubernetes/role/<service>-schema-migration-read`에 등록한다.
6. `gitops/argocd/schema-migration-vso`를 수동 등록·sync한다. 다른 Application에 속한 리소스의 sync-wave는 전역 순서를 보장하지 않는다.
7. 서비스별 VaultStaticSecret Ready와 Secret 키를 확인한 뒤 Job을 활성화한다.

현재 비밀번호 생성·주입·잠금 해제의 운영 자동화 스크립트는 제공하지 않는다. 기존 앱 계정용 bootstrap을 그대로 실행하면 안 된다.
Vault CA Secret과 Kubernetes auth mount는 기존 구성을 사용한다. Job은 Vault 토큰이나 Kubernetes API 토큰을 받지 않는다.
VSO 인증용 ServiceAccount와 Job의 실행 권한을 혼동하지 않는다.
기존 API 계정의 DDL 권한 축소는 별도 전환 단계다. 이 변경은 앱 권한을 변경하지 않는다.

## 실행 이미지를 준비할 것

`Dockerfile`에 공식 Eclipse Temurin 17 JRE Jammy의 linux/amd64 digest를 고정한다.
서비스 하나의 `build/migration`만 build context로 사용한다.
`Dockerfile`의 COPY 대상은 해당 디렉터리의 `lib/`뿐이다. 테스트 fixture나 전체 저장소를 넣지 않는다.
현재 승인은 로컬 빌드·검증까지다. 이미지 push와 운영 배포는 별도 승인 대상이다.
runtime 갱신은 Dockerfile의 digest를 바꾸고 5개 서비스 패키징 시험을 다시 통과시킨다.
다른 CPU 아키텍처에 이 amd64 digest를 그대로 적용하지 않는다.

```bash
docker build \
  -f <infra-worktree>/infra/migrations/Dockerfile \
  -t <approved-migration-image-tag> <backend-worktree>/<service>-service/build/migration
```

완성 이미지의 digest, API digest, 백엔드 소스 SHA, SQL checksum을 릴리스 근거에 함께 기록한다.
다른 MS migration JAR 두 개를 같은 이미지에 넣지 않는다.

## 배포 Job을 활성화할 것

아래는 값의 형태이며 그대로 적용할 수 있는 운영 설정이 아니다.

```yaml
image:
  digest: "sha256:<api-digest>"
env:
  SPRING_JPA_HIBERNATE_DDL_AUTO: validate
schemaMigration:
  enabled: true
  image: "<migration-image>@sha256:<migration-digest>"
  apiImageDigest: "sha256:<api-digest>"
  existingSchemaVerified: true
  recoveryReference: "<approved-baseline-and-restore-evidence-reference>"
```

`existingSchemaVerified`와 `recoveryReference`는 검토 결과를 표시하는 값이며 자동 검증 증거가 아니다.
초기 편입과 백업 검증이 끝나지 않았으면 값을 채워 우회하지 않는다.
Job은 고정 스키마와 계정만 사용하며, 300초 제한·재시작 없음·backoffLimit 0으로 실행한다.
실행 시간이 부족하면 부분 DDL·잠금 해제 여부를 확인한 뒤 제한 변경을 검토한다. 단순 시간 초과를 안전한 취소로 취급하지 않는다.

기존 읽기 전용 PreSync 검사(wave 0) 뒤 migration Job(wave 10)을 실행한다. Job이 성공해야 일반 Deployment 적용으로 넘어간다.
실패 Job에는 TTL·BeforeHookCreation·HookFailed 삭제 정책을 두지 않는다.
이 정책은 실패 근거를 보존하지만 관리자의 강제 삭제·선택적 sync·직접 Helm 배포까지 막는 보안 경계는 아니다.
선택적 sync는 hook을 실행하지 않으므로 금지한다. 이 차트를 `helm upgrade`로 직접 배포하지 않는다.
Job의 backoffLimit 0도 분산 환경에서 정확히 한 번 실행을 보장하지 않는다. Flyway 이력과 DB 잠금이 함께 필요하다.

## 실패하면 먼저 중단할 것

1. 정확한 서비스의 Argo sync와 자동 재조정을 승인된 절차로 중단한다. 다른 서비스의 자동화를 통째로 끄지 않는다.
2. 실패 Job·Pod 상태·시간·이미지 digest·소스 SHA·Flyway 이력·실제 DDL을 보존한다. 비밀값을 덤프하지 않는다.
3. DB 변경이 시작됐는지 확인한다. Pod 종료만 보고 DB 작업·잠금도 끝났다고 가정하지 않는다.
4. 신규 배포를 멈춘다. 기존 API가 변경된 구조와 호환되지 않으면 해당 서비스의 쓰기와 관련 배치·consumer도 중단한다.
5. 다음 분기에 따라 복구한다. 실패 Job을 지우거나 `repair`·`baseline`을 실행해 우회하지 않는다.

| 실패 경계 | 복구 경로 | 재개 조건 |
| --- | --- | --- |
| Secret·접속·검증 실패, DDL 미시작 확인 | 원인 수정, 기존 앱 유지 | DDL 무변경·정상 연결 확인 |
| DDL 일부 적용 | 실제 변경과 실패 이력 대조 후 승인된 전진 수정 또는 복구 | 구조·이력·데이터 일치와 미완료 DB 작업 없음 |
| DB 변경 완료, API 배포 실패 | 새 DB와 호환되는 이전 API digest로 복귀 | 이전 앱의 읽기·쓰기·이벤트 호환성 확인 |
| 데이터 손상 또는 전진 수정 불가 | 쓰기 통제 후 별도 DB에 백업 복원, 검증 후 승인된 전환 | 데이터 손실 범위 합의·복원·CDC 정합성 검증 |

MySQL DDL은 일부가 남을 수 있다. SQL 실패 후 새 버전 파일만 추가해도 실패 이력이 남으면 진행되지 않는다.
수동 복구와 이력 정합성 확인 후 `repair`가 필요할 수 있지만, 이 도구에는 해당 명령이 없다. 별도 승인된 관리자 절차로 처리한다.
성공한 migration 파일의 내용을 바꾸지 않는다.
앱만 롤백할 때 migration 이미지를 과거 것으로 함께 되돌리지 않는다. 현재 DB 이력을 검증할 수 있는 migration 이미지와 앱 호환성 근거를 유지한다.
`schemaMigration.enabled=false`로 우회해서 앱을 배포하는 것은 일반 복구 방법이 아니다.

## DB 복원이 필요하면 확인할 것

1. 대상 서비스 쓰기와 관련 API·CronJob·consumer를 식별해 중단하고 진행 중 트랜잭션이 끝났는지 확인한다.
2. 실패 당시 DB와 백업 SHA-256, 시간, 데이터 손실 예상 구간을 보존한다. 오래된 백업 덮어쓰기를 금지한다.
3. 운영 원본이 아닌 별도 DB에 복원한다. 다른 MS 스키마를 통째로 덮지 않는다.
4. 테이블·인덱스·주요 행·Flyway 이력과 재시작 후 지속성을 검사한다.
5. Outbox, Debezium binlog/GTID, Kafka offset, 소비자 중복 처리와 검색 인덱스의 관계를 확인한다.
6. 논리 복원 DB에 기존 CDC offset을 그대로 이어 붙이지 않는다. 이벤트 유실·중복·외부 결제 상태까지 대조한 별도 복구 승인을 받는다.
7. 대상 DB 전환·계정·연결 변경안을 승인받은 뒤 작은 범위로 쓰기를 재개하고 관측한다.

백업 복원은 백업 이후 쓰기를 잃을 수 있다. 현재 시점 복구(PITR)가 가능한지는 별도 binlog 보존·복구 시험으로 증명한다.
공유 MySQL 전체 복원은 5개 MS와 이벤트 처리를 함께 조정하는 별도 사고 복구다. 서비스 하나의 실패에 기본 적용하지 않는다.

## 실패 Job을 재개할 것

1. 원인·DDL 상태·복구 결과·배포할 정확한 이미지 조합을 승인받는다.
2. 실패 Job과 로그를 보존한 뒤 정확한 namespace와 Job 이름을 확인한다.
3. 승인된 해당 Job만 삭제한다. 전체 Job 삭제나 label 기반 일괄 삭제를 사용하지 않는다.
4. 전체 Application sync로 재개한다. 성공한 Flyway 이력과 새 Deployment health를 확인한다.
5. 실제 사용자 읽기·쓰기, 배치·이벤트 처리를 검증한 뒤 자동 재조정을 복구한다.

## 로컬 검증을 실행할 것

검증 스크립트는 추가 이미지를 내려받지 않는다. Helm 3.15.4, MySQL 8.4.12와 Python PyYAML이 필요하다.
복원 리허설은 기존 animal API 이미지의 Java 17을 사용한다. 패키징 시험은 Dockerfile에 고정한 공식 runtime이 로컬에 있어야 한다.
누락된 이미지는 대상을 확인하고 별도로 내려받은 뒤 실행한다.

```bash
python3 -B infra/migrations/tests/check_contracts.py
python3 -B infra/migrations/tests/recovery_drill.py --run-disposable \
  --distribution <backend-worktree>/animal-service/build/migration
```

두 번째 명령은 외부 포트 없는 DB 두 개를 만들고 시험 후 자체 생성 리소스만 제거한다.
시험 백업은 합성 데이터이며 프로세스 메모리에만 보관한다. 운영 백업 파일이나 비밀번호를 읽지 않는다.
검증 범위는 5개 MS Helm·Secret 계약과 animal 시험 DB의 부분 DDL 실패·재실행 차단·해시 검사·격리 복원·재시작이다.
실제 Argo 컨트롤러 실패 동작, Vault 로그인, 운영 백업, 5개 MS 전체 데이터 복원, CDC 복구는 이 시험의 범위가 아니다.

## 이미지와 Job 명령을 함께 검증할 것

다음 시험은 서비스별 패키지의 SQL이 현재 소스와 일치하는지 먼저 확인한다.
실제 Dockerfile로 시험 이미지 5개를 만들고, Helm에서 렌더링한 Job의 Java 명령·인자·전용 계정 이름으로 실행한다.
DB 주소와 비밀번호만 외부 포트 없는 시험 DB에 맞춘다. non-root, 읽기 전용 파일시스템, capability 제거와 자원 제한을 적용한다.

```bash
python3 -B infra/migrations/tests/package_drill.py --run-disposable \
  --backend-worktree <backend-worktree>
```

패키징 시험은 Dockerfile의 FROM을 읽어 동일한 공식 Java 17 runtime을 사용한다.
시험 스크립트에 별도의 runtime digest를 중복 관리하거나 빌드 인자로 다른 기반 이미지를 주입하지 않는다.
시험은 이미지를 내려받거나 push하지 않는다. 완료 시 이 시험의 라벨이 일치하는 컨테이너·익명 볼륨·시험 이미지 태그만 제거한다.
Docker 빌드 캐시에 대한 전역 prune은 실행하지 않는다.
Argo 컨트롤러의 Job 실행, Vault 인증과 운영 계정 주입은 이 시험에 포함되지 않는다.

## 검증 기록

2026-09-11 공식 Eclipse Temurin 17 JRE Jammy(linux/amd64) 기반으로 5개 MS 이미지 통합 시험을 다시 통과했다.
실행 버전은 OpenJDK 17.0.20+8이며 기반 이미지 digest는 `sha256:24cd8eed18b5976441d27b45823490eb5e8efff4b3ecdc632e442717ea66f160`이다.
서비스별 실제 SQL 패키지 일치, 이미지 빌드, Helm Job 명령 실행, 잠긴 계정·확인 누락 거절, 최초 적용·재실행·validate·잘못된 스키마 거절을 확인했다.
UID 1000, 읽기 전용 루트, capability 제거, 512MiB 메모리 제한과 격리된 시험 DB에서 실행했다.
시험이 만든 컨테이너·익명 볼륨·시험 이미지 태그는 제거했다. 내려받은 공식 기반 이미지와 Docker 빌드 캐시는 로컬에 남는다.
이는 공식 runtime의 로컬 실행 근거다. 이미지 push·릴리스 소스 리비전 확정·Argo/Vault 연결·운영 DB 적용은 하지 않았다.

2026-09-11 최초 이미지 통합 시험은 기존 animal API 이미지를 호환성 기반으로 사용했다. 5개 MS 각각 Dockerfile 빌드와 렌더링된 Job 명령 실행을 통과했다.
서비스별 잠긴 계정 거절·대상 확인 누락 거절·최초 V1 적용·재실행 시 0건 적용·이력 검증·잘못된 스키마 거절을 확인했다.
현재 소스 SQL과 패키징된 SQL 일치도 확인했다. 시험 컨테이너·익명 볼륨·시험 이미지 태그를 제거했으며 이미지 push는 하지 않았다.
이 기록은 시험용 기반 이미지에서의 실행 근거다. 운영 runtime 선정·Argo 컨트롤러·Vault 인증·운영 DB 검증을 대신하지 않는다.

2026-09-11 Helm 3.15.4 실제 렌더링을 포함한 계약 검증 162개 항목이 통과했다. 5개 MS의 기본 비활성, 활성화 거절 조건, Job 순서·실패 보존, Vault role·policy·Secret 경계를 포함한다.

2026-09-11 로컬 리허설에서 부분 DDL 잔존, 실패 이력의 재실행 차단, 백업 해시 불일치 거절, 별도 DB 복원, 다른 스키마 보존, 재시작 후 지속성을 확인했다.
5개 잠긴 계정의 SQL 실행과 animal 계정의 실제 V1 적용·타 스키마 DDL 거절도 확인했다.
시험 컨테이너와 익명 볼륨은 제거했다. 운영 변경·운영 백업 복원·실제 Argo 실패 재개·Vault 인증 검증은 미실행이다.

## 공식 근거

- [Argo CD sync phases와 hook 보존](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/)
- [Flyway 트랜잭션과 부분 DDL 실패](https://documentation.red-gate.com/flyway/flyway-concepts/migrations/migration-transaction-handling)
