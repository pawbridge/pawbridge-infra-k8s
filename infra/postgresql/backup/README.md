# PostgreSQL 압축·암호화 백업과 격리 복원

이 경로는 백업 프로그램·매니페스트·검증 절차의 버전 계약이다. 운영 실행 기록과
맥미니 이전 결정의 정본은 Obsidian `Projects/pawbridge/13 스토리지 백업과 복구`다.
**PR 병합만으로 예약 백업을 설치하거나 운영 DB를 백업하지 않는다.**

## 백업 계약

- 대상: PostgreSQL 17의 `pawbridge` DB 전체. 운영과 같은 pgvector 0.8.6 기반 이미지로 실행한다.
- 한국 시간 03:10·15:10, `Forbid`, Job 최장 45분, 프로그램 전체 40분, 재시도 1회.
  APMS 배치와의 비중첩이나 항상 12시간 이하의 데이터 손실을 보장하지 않는다.
- `pg_dump -Fc --compress=gzip:6`의 출력과 역할·멤버십·비밀번호 해시를 `age` 파이프로 암호화한다.
  평문 DB/역할 덤프 파일을 생성하지 않는다. 임시 작업 공간에는 암호문만 둔다.
- DB 덤프와 테이블 검증 기준은 같은 exported snapshot에서 읽는다. 역할은 별도 전역 카탈로그이므로
  전후 해시가 달라지면 업로드하지 않는다. 장시간 읽기 snapshot과 추가 테이블 조회 부하는 첫 운영 실행에서 측정한다.
- 전용 비공개 R2 Standard 버킷 `pawbridge-postgresql-backups`, prefix `postgresql/v1/`만 사용한다.
  기존 이미지 버킷·D/E 수동 백업·운영 PVC를 삭제하지 않는다.
- 날짜+UUID마다 새 키를 사용한다. 암호문 3개를 업로드한 뒤 다시 읽어 크기와 SHA-256을 검증하고,
  `complete.json`을 마지막에 기록한다. 완료 표시 없는 묶음은 복원 후보로 사용하지 않는다.
- 새 백업 완료 후 7일보다 오래된 묶음만 정리한다. 경계 시각과 최신 완료 백업을 보존한다.
  업로드 실패 시 보관 정리를 실행하지 않는다. R2 서버 Date와 실행기 시각이 5분 이상 다르거나,
  미래 시각·잘못된 경로·손상된 완료 표시가 있으면 삭제를 중단한다.
- 완료 표시를 먼저 제거하고 암호문을 정리한다. 중단된 오래된 암호문도 새 성공 후에만 정리한다.
  알 수 없는 파일과 prefix 밖 객체는 보존한다. 성공이 끊기면 7일 초과 백업도 남을 수 있다.
- 스키마·데이터·시퀀스·인덱스·권한·Flyway 테이블·확장을 논리 백업한다.
  물리 복제·PITR·Kafka offset/토픽·복제 slot·Vault·이미지 원본·클러스터 전체 백업은 제공하지 않는다.
  복구 시 CDC/Outbox 재처리 여부를 따로 정하고 업무 프로세스를 자동 기동하지 않는다.

## 키와 자격 증명

예약 작업에는 **공개 암호화 키만** 넣는다. `AGE-SECRET-KEY-...` 개인키는 Kubernetes,
Vault 예약 경로, Git, 로그, R2 버킷에 넣지 않는다. 적용 전에 사용자의 보관 위치를 정하고
PC 밖에도 개인키 사본을 보존하며 실제 복호화를 검증해야 한다. 개인키를 잃으면 백업을 복원할 수 없다.

R2 자격 증명은 해당 버킷의 Object Read & Write에만 한정한다. 버킷 생성용 계정 권한과 구분한다.
새 Vault KV-v2 `secret/pawbridge/dev/postgresql/backup-r2`에 `access-key-id`, `secret-access-key`만 둔다.
`vault-policy.hcl`/`vault-role.json`은 값 없는 적용 후보다. VSO는 기존 TLS 검증
`postgresql-admin-vault-connection`을 재사용하고 신규 두 키만 동기화한다. 자동 DB 재시작은 없다.

DB 관리자 비밀번호는 기존 `pawbridge-postgresql-admin-auth` Secret의 파일을 읽는다.
명령행에 비밀번호를 전달하거나 값을 로그로 출력하지 않는다. 복구 다운로드는 원본 DB·Vault 없이
R2 endpoint와 별도로 확보한 읽기용 자격 증명만 필요하다. 복원된 앱 계정 인증에는 각 앱의 비밀번호가 별도로 필요하다.

## 처음 적용하는 순서 — 별도 운영 승인 필요

1. 대상 클러스터·노드·Vault 상태와 기존 DB/PVC 식별자를 다시 확인한다.
2. 사용자 개인키 보관 위치·PC 밖 사본을 확정하고 공개키만 추출한다.
3. 전용 R2 버킷의 비공개/Standard 설정과 해당 버킷 전용 키를 만들고 Vault/VSO에 등록한다.
4. 병합된 dev 커밋에서 `PostgreSQL encrypted backup` 워크플로를 수동 실행하고 publish를 명시한 경우에만
   기존 Docker Hub 레지스트리에 같은 검증 이미지를 게시한다. registry의 실제 digest를 확인한다. 임의 digest나 `UNPUBLISHED`를 사용하지 않는다.
5. `render.py`로 공개키·R2 endpoint·이미지 digest를 넣되 **중지 상태로 렌더링**한다.
   검토한 Job/CronJob 수집 변경을 현재 고정 관측 차트에 적용한다. 기존 DB StatefulSet은 변경하지 않는다.
6. 중지된 CronJob에서 첫 수동 Job을 만들고 `BACKUP_CONTENT_PROOF=true`로 전행 기준을 포함한다.
   운영 DB에는 읽기만 수행한다. w2의 공간·메모리·DB 지연과 성공 로그를 확인한다.
7. R2에서 내려받은 암호문으로 아래 격리 복원을 실행한다. 새 대상에는 운영 볼륨·네트워크를 연결하지 않는다.
8. 복호화·모든 테이블의 행/내용 해시·역할/비밀번호 해시·멤버십·Flyway 기록·확장·5개 앱 계정의 원래 허용된 테이블 조회·
   벡터 연산·재시작 후 결과를 확인한다. 실제 운영 데이터 기준이 통과한 뒤 예약을 활성화한다.
9. 첫 예약 성공, 최신 성공 시각, R2 비공개 상태, 경보 평가와 실제 자원 영향을 확인한다.
   Slack 시험 발송은 별도 승인 없이 실행하지 않는다.

운영용 후보 렌더 예시(기존 출력 덮어쓰기 거부):

```sh
python3 infra/postgresql/backup/render.py \
  --image 'dorosiya/pawbridge-postgresql-backup@sha256:<실제 게시 digest>' \
  --endpoint 'https://<계정 ID>.r2.cloudflarestorage.com' \
  --recipient-file /private/recovery/recipient.txt \
  --output /private/recovery/backup-suspended.yaml
```

`--activate`는 검증 후 활성화 후보를 만들 때만 붙인다. 이 명령은 클러스터·Vault·R2를 변경하지 않는다.
기본 매니페스트는 `suspend: true`이고 이미지·endpoint·공개키가 미설정이라 작업이 실행돼도 성공할 수 없다.
상위 PostgreSQL kustomization/Argo Application에는 백업을 자동 연결하지 않는다.

## 장애 시 다운로드와 신규 격리 복원

`download.py --run <완료된 실행 ID> --destination <새 폴더>`는 DB 없이 R2에서 암호문만 내려받는다.
`R2_ENDPOINT`, `R2_ACCESS_KEY_FILE`, `R2_SECRET_KEY_FILE`을 파일로 제공한다. 기존 폴더 덮어쓰기와
16GiB 초과 묶음을 거부하고 다운로드 체크섬을 확인한 뒤에만 로컬 완료 표시를 만든다.
R2 계정 접근도 잃은 상황을 대비해 Cloudflare 계정 복구 수단을 별도로 보관해야 한다.

`drill.py`는 새 폴더에 initdb하고 TCP listener 없이 Unix 소켓만 열어 복원한다.
개인키와 앱 비밀번호 파일은 읽기 전용으로 전달한다. 비밀번호 폴더에는
`pawbridge_animal_app`, `pawbridge_user_app`, `pawbridge_community_app`,
`pawbridge_store_app`, `pawbridge_payment_app` 이름의 파일 5개가 각각 한 비밀번호만 담아야 한다.
이미지는 postgres UID/GID999로 실행하므로 그 사용자만 개인키·비밀번호를 읽을 수 있게 준비한다.

```sh
python3 /opt/backup/drill.py \
  --directory /recovery/bundle --identity /run/recovery/identity.txt \
  --app-password-directory /run/recovery/apps \
  --workdir /work/new-drill --report /work/drill-report.json \
  --allow-isolated-restore
```

위 명령은 검증된 이미지의 **새 격리 컨테이너**에서 실행한다. `--network=none`, 기존 운영/개발
DB 볼륨 미연결, 비밀 파일 읽기 전용, 제한된 CPU/메모리와 충분한 신규 복원 공간을 적용한다.
전체 논리 복원에는 압축 전 데이터·인덱스·임시 파일 공간이 필요하므로 암호문 크기만으로 공간을 잡지 않는다.
복원 폴더와 보고서가 이미 있으면 거부한다. 실패 자료는 비공개로 보존하고 새 대상으로 재시도한다.

PostgreSQL 17의 역할 `GRANTED BY`는 최초 bootstrap 역할과 맞아야 한다. 원본/복원 모두 `postgres`를
bootstrap으로 사용하고 새 DB의 `CREATE ROLE postgres` 한 줄만 중복 생성에서 제외한다.
역할 속성·비밀번호 해시·멤버십은 원본대로 복원한다. 복원용 postgres는 OS postgres peer 접속,
앱 계정은 원본 비밀번호 SCRAM 인증을 사용한다. 기존 DB나 비-bootstrap 역할이 있으면 복원을 거부한다.
`restore.py`의 단계 보고서만으로 재시작·앱 조회 완료라고 판단하지 않는다. 최종 `drill.py` 보고서를 확인한다.

정기 백업의 `BACKUP_CONTENT_PROOF=false`는 덤프와 동일 snapshot의 행 수와 앱의 조회 허용 테이블 기준이다.
첫 수동 검증은 `true`로 전행 내용 검증도 수행한다. 둘을 같은 수준의 증거로 표현하지 않는다.
앱 테이블 SELECT 성공은 전체 API·업무 E2E 검증과 다르다. Flyway는 복원된 기록을 읽고 새 migration을 실행하지 않는다.
복원은 운영 서비스로 자동 전환하지 않는다. 실제 장애 전환에는 CDC와 쓰기 재개 경계를 별도 검토한다.

## 알림·자원·롤백

기존 kube-state-metrics에 읽기 전용 `jobs`, `cronjobs` 수집과 list/watch 자체 지표를 추가한다.
백업 경보는 별도 PrometheusRule로 설치한다. 최근 Job 최종 실패(재시도 실패/과거 실패 제외),
성공 없는 15시간(최초 설치는 생성 시각부터), 하루 이상 중지, 리소스/수집 누락을 구분한다.
PC/클러스터 전체 중단 시 내부 Prometheus와 알림도 멈춘다. 외부 감시를 대신하지 않는다.

백업 w2 요청 100m/256Mi, 제한 1CPU/512Mi, 암호문 작업 공간 8GiB, 임시 메모리 32MiB.
DB를 잠시 정지하지 않고 읽기 snapshot으로 백업하지만 읽기 I/O·VACUUM 지연·네트워크 부하가 생길 수 있다.
한 번의 dump lock 대기는 30초로 제한한다. 첫 실제 실행에서 크기·소요시간·지연을 측정하고 한계를 조정한다.
표준 NetworkPolicy는 R2 FQDN 자체를 제한하지 못한다. DNS·대상 DB와 공개 TCP443 egress만 허용하며
프로그램이 검토된 R2 HTTPS endpoint 형식과 고정 버킷을 검사한다. egress가 R2만 허용된 것이라고 표현하지 않는다.

중단/롤백은 CronJob을 suspend하고 이 작업의 활성 Job만 정리한다. 이미 업로드한 백업과
복호화 개인키, 기존 DB/PVC는 보존한다. 필요 시 신규 R2 키만 폐기한다. 관측 변경은 기존 고정 release로
되돌린다. DB 복원/덮어쓰기/운영 키 변경을 백업 설정 롤백에 섞지 않는다.

## 비용과 맥미니 이전

2026-10-01 공식 R2 Standard 기준 저장 $0.015/GB-month, 무료 10GB-month와
Class A 100만/Class B 1,000만 요청, 인터넷 전송 무료다. 무료 구간은 계정 전체 사용량을 합산한다.
저장량은 대략 **실제 암호문 1회 크기 × 14회**다. 보관 정리 지연/부분 실패는 추가 사용량을 만든다.
예를 들어 1회 0.2GB면 약 2.8GB이나 실제 크기는 첫 운영 백업으로 측정해야 한다.
7일 보관에는 최소 30일 저장 과금이 있는 Infrequent Access를 사용하지 않는다.

맥미니에서 Compose로 바꾸면 같은 백업 컨테이너를 **macOS launchd**가 하루 두 번 실행한다.
쿠버네티스용 CronJob/지표 경보만 교체한다. Docker 준비 확인, 기동 시 마지막 성공 확인/누락 보완,
중복 실행 방지, 종료 코드·성공 시각 감시가 필요하다. 개인키·R2 보관·7일 정책은 동일하다.
macOS scheduler는 잠자기 중 예약을 깨운 뒤 처리하지만 전원이 꺼진 동안의 누락을 모두 재생하지 않는다.
맥미니 예약 파일을 현재 환경에 설치하지 않았다.

## 로컬 검증

```sh
docker build --pull=false -t pawbridge-postgresql-backup:validation infra/postgresql/backup
docker run --rm --pull=never --network=none --read-only --cap-drop=ALL \
  --security-opt=no-new-privileges --cpus=2 --memory=2g --pids-limit=256 \
  --tmpfs=/tmp:rw,nosuid,nodev,size=256m \
  --tmpfs=/work:rw,nosuid,nodev,size=512m,uid=999,gid=999 \
  -e PAWBRIDGE_BACKUP_TEST_CONTAINER=true --entrypoint=python3 \
  pawbridge-postgresql-backup:validation -m unittest discover -s tests -p 'test_*.py' -v
python3 infra/postgresql/backup/verify_manifests.py -v
python3 infra/postgresql/backup/verify_alerts.py /absolute/path/to/promtool
```

PR CI는 검증만 수행한다. 수동 dev 실행에서 publish=true인 경우에만 이미지 게시가 가능하다.
실제 PostgreSQL/age 테스트는 합성 데이터만 사용한다. R2 전송은 메모리 S3 대역이므로 실제 R2 연결·운영 데이터 복원을 증명하지 않는다.

## 공식 근거

- [PostgreSQL 17 pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)
- [역할 백업](https://www.postgresql.org/docs/17/app-pg-dumpall.html), [PG17 GRANT](https://www.postgresql.org/docs/17/sql-grant.html)
- [age](https://github.com/FiloSottile/age)
- [R2 SDK](https://developers.cloudflare.com/r2/examples/aws/boto3/), [R2 비용](https://developers.cloudflare.com/r2/pricing/)
- [CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/)
- [Apple launchd 예약](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html)
