# Mac 갤러리 갱신·백업·로컬 감시

이 폴더의 `compose.json`은 Mac 운영에 사용한 전체 Compose의 매개변수 정의다. 비공개 고정 후보는 이 정의의 endpoint·host 경로를 렌더링한 파일이며, 실제 실행은 관리 실행본의 백업 경로 override와 서비스별 active env도 사용한다. [상위 실행 안내](../README.md)에서 공개 템플릿과 비공개 입력·Application Support 실행본의 경계를 확인한다. 이번 소스 게시와 단위 테스트는 운영 배포·복구 실행이 아니다.

## 구성과 실행 경계

`compose.json`은 기존 서비스 구성을 포함하는 **완전한 구성 파일**이다. 단순 override 병합으로 원래 PostgreSQL의 호스트 포트를 남기지 않는다. PostgreSQL은 내부망에만 있고 `local-native-access`가 호스트의 `127.0.0.1:35436`을 PostgreSQL로, `127.0.0.1:35437`의 갤러리 snapshot 경로만 Animal 8081로 전달한다. DNS는 Docker resolver로 다시 조회한다. Animal의 내부 API 인증은 그대로 필요하다. 다른 HTTP 경로는 404다. macOS 루프백 공개는 다른 로컬 프로세스까지 차단하는 인증 장치가 아니므로 DB와 feed 인증을 유지한다.

백업을 포함하는 `compose.json` 하나를 전체 정의로 사용한다. 과거 백업 없는 후보를 이 정의와 병합하지 않는다. `BACKGROUND_CONFIG`, `BACKGROUND_SCRIPTS`, `BACKGROUND_ROOT`, `BACKUP_R2_ENDPOINT`와 기존 `OPERATING_*`는 비공개 실행 설정에서 공급한다. 키·비밀번호를 이미지나 Git 파일에 넣지 않는다.

관리형 native 진입점은 `scripts/environments/mac_native_supervisor.py`이며 `mac_native_service.py`를 자식으로 실행한다. 서비스 실행기는 비공개 환경 JSON 파일 경로 하나를 받는다. Python 소스 전체 해시, 설치된 SAM3 패치, 로컬 가중치, DB 및 feed 대상과 영구 상태 위치를 검사한 후 기존 `app.lost_main`을 loopback:8001 / worker 1로 실행한다. 모델을 추가 프로세스로 복제하지 않는다. 앱 내부 `GalleryRefresh`가 API encoder와 추론 게이트를 공유한다. 비공개 source receipt와 다른 소스를 자동 허용하지 않는다. receipt는 실행 입력으로 보존하며 생성된 원본을 저장소에 넣지 않는다.

- standby/drain: `LOST_GALLERY_SYNC_ENABLED=false`.
- active: v2 paged feed, 900초 주기, 전용 영구 상태/임시 사진 폴더. 관리 실행본의 비공개 active 설정을 사용하며 저장소의 기본 standby 생성 결과로 대체하지 않는다.
- 갤러리가 없는 상태에서 sync=false인 API는 준비 완료가 되지 않을 수 있다. 자동 갤러리 쓰기는 최종 이관의 새 쓰기 경계를 넘는 동작이다.
- 재시작 시 저장된 성공 표시만 믿지 않고 입력과 게시 상태를 다시 확인한다. 실패 시 기존 게시 갤러리로 검색을 유지한다.

## 백업

`backup` 서비스는 `background` profile로 분리했다. 기존 백업 이미지의 PG17.11 / vector0.8.6 / age 및 Python 의존성을 사용한다. 새 `mac_backup_job.py`는 기존 `capture()`를 재사용하고 기존 `run_backup()`의 retention을 호출하지 않는다.

1. 새 run 폴더에 전체 DB·역할·복원 manifest를 age 공개키로 암호화한다.
2. R2의 run UUID 경로에 `If-None-Match: *`로 신규 객체만 쓴다.
3. 각 암호문을 GET으로 읽으며 크기와 SHA-256을 대조한다.
4. 3개 암호문 검증 후 완료표시를 게시하고 그 내용도 검증한다.

운영 후보는 12시간 주기, 작업 제한 40분, 마지막 성공 15시간 초과를 이상 상태로 본다. 처음 활성화하면 즉시 한 번 실행하며, 이후 마지막 시도 시각을 기준으로 예약한다. 실패 후 즉시 무한 재시도하지 않는다. 단일 상태 폴더의 파일 잠금으로 중복 실행을 막는다. 성공·실패·시간 초과·이전 성공 시각을 원자적으로 기록한다. 재시작해도 기록된 주기 전에 중복 실행하지 않는다. 컨테이너 healthcheck는 작업 결과와 경과시간을 검사한다.

백업 컨테이너에는 PostgreSQL 백업용 비밀번호, 백업 전용 R2 키, age **공개키**만 연결한다. 복구 개인키나 native API 설정을 마운트하지 않는다. 작업 폴더 여유 공간이 5GiB 미만이면 실패한다. 암호문과 실패 근거는 보존한다. **로컬·R2의 자동 삭제는 없다.** 장기 운영 전에 용량과 보관/삭제 정책을 별도로 확정해야 한다. healthcheck의 unhealthy 표시는 Docker의 자동 재시작이나 사용자 알림을 뜻하지 않는다.

## 감시와 확인

`mac_background_jobs.py monitor <비공개 monitor.json>`은 백업 성공·실패·시간 초과·지연과 갤러리의 실패·정지·마지막 확인 시각을 집계한다. 갤러리 내용이 그대로인 304 확인도 정상적인 최신 확인으로 인정한다. 관리형 monitor LaunchAgent는 같은 관리 실행본의 `mac_background_jobs.py`와 비공개 monitor 설정을 사용하며 60초 주기로 실행한다. 저장소 소스 변경은 설치된 복사본을 자동 갱신하지 않는다.

`mac_background_jobs.py monitor-health <비공개 monitor.json>`은 집계 결과와 감시 기록 자체의 최신 여부(180초)를 확인한다. 기록 누락·손상·미래 시각·오래된 성공 기록을 정상으로 처리하지 않는다. 오류 상세·서명 URL·비밀번호를 상태 보고에 기록하지 않는다. stdout/stderr 진단 로그는 비공개 폴더에서만 보존한다.

현재 감시는 로컬 상태와 종료 코드까지다. 메일·메신저·외부 dead-man 알림은 설정하지 않았다. Mac 자체가 꺼졌을 때 외부로 알려주는 감시가 완료된 것으로 해석하지 않는다.

## 검증 범위

운영 source 연결은 고정 후보 receipt와 관리 실행본의 파일 해시 대조로 확인한다. 과거 격리 검증은 아래 기록과 Obsidian 이관 계획 및 비공개 검증 보고서에서 구분한다. 실제 OS 재부팅 뒤 전체 자동 복귀는 미검증이다. native·monitor 등록 및 한 번의 정상 실행을 재부팅 복귀 완료로 확대하지 않는다.

아래 날짜별 기록의 후보 활성 여부와 시험 상태는 해당 시점의 이력이다. 현재 실행 조합은 문서 첫머리와 상위 실행 안내를 따른다.

최초 갤러리 제어 시험은 합성 encoder를 사용했다. 후속 승인된 AI 교대 시험에서는 실제 Java v2 feed→R2 GET→SAM3/DINOv3 MPS→격리 PostgreSQL 게시와 사진 검색을 연결했고, 갱신 중 검색·추가/제외·변경 없음 재사용·실패 시 기존 검색 유지·자동 회복을 확인했다. 기존 AI는 원래 DB/설정으로 복귀 후 검색까지 확인했다. 대표 사진 1→2→1장의 격리 시험이며 전체 갤러리 규모·장기 부하·품질 동등성·로그인 후 LaunchAgent 복귀의 완료 근거는 아니다. 검색 입력은 기존 JPEG fixture이며 WebP 아카이브를 사용자 검색 입력으로 직접 보내면 현재 API 계약상 422다. 상세 수치와 실패/복구 근거는 Obsidian의 「2026-10-16 실제 MPS 갤러리 자동 갱신·동시 검색과 AI 교대 복귀」에 기록한다.

## 2026-10-16 통합 점검에서 발견한 백업 연결 충돌

기존 후보는 백업이 `PGUSER=postgres`로 TCP 접속하지만 HBA가 postgres의 loopback만 허용해 실제 접속이 거부됐다. 전체 Compose는 `backup-private` 내부망 `10.203.254.0/29`를 추가했다. PostgreSQL `.2`와 백업 `.3`만 연결하고, 새 `pg_hba.conf`는 `.3/32`의 SCRAM 접속만 추가 허용한다. 일반 서비스는 이 망에 참여하지 않으며 백업은 일반 서비스 private 망에 참여하지 않는다. 백업 자격증명과 egress는 기존 정책을 유지한다. 중복 선언된 동일 마운트도 한 번만 남겼다.

전체 정의는 `${BACKGROUND_CONFIG}/pg_hba.conf`를 마운트한다. 백업 전용 네트워크와 HBA를 함께 유지한다. 배치 전 기존 Docker/호스트 경로와 서브넷 충돌을 검사해야 하며 같은 주소를 쓰는 후보를 동시에 올리지 않는다. 백업은 처음 활성화할 때 즉시 실행하므로 최종 전환 승인 없이 background profile을 활성화하지 않는다.

격리 DB에서 수정 전 HBA 거부를 재현했고 수정 후 올바른 비밀번호의 접속 성공, 잘못된 비밀번호 거부, 일반 서비스 private 주소의 postgres 접속 거부를 확인했다. 이 수정으로 전체 통합 검증이 끝난 것은 아니다. 후속 CDC 초기화 중 PostgreSQL 논리 스냅샷 파일 누락 PANIC과 Payment task 실패가 발생해 전체 앱·AI·정기 백업 동시 시험은 진행하지 않았다. 당시 원인은 미확인이었으며 후속 저장 경로 검증과 보완 결과는 아래에 기록했다. fsync/data_sync_retry를 변경하거나 slot/WAL/파일을 삭제하지 않았다. 새 후보는 비활성이고 원본 후보도 보존했다.


## Linux 데이터 볼륨과 CDC 파일 오류 보완

PostgreSQL 논리 snapshot 저장 중 파일 열기 ENOENT가 PANIC으로 이어진 격리 시험을 조사했다. macOS 공유 bind에서 기존 파일을 여러 프로세스가 atomic replace한 직후 O_RDWR로 여는 최소 재현은 30,000회 중 4,645회 실패했고, Docker Linux 볼륨에서는 30,000회 모두 성공했다. 같은 PostgreSQL 이미지의 DB 복사본을 Linux 볼륨에 두면 5개 CDC 동시 초기화와 재시작 시험이 통과했다. 특정 FUSE 구현의 내부 결함이나 모든 부하에서의 안정성까지 입증한 것은 아니다.

전체 Compose 정의는 PostgreSQL·Kafka·Redis 쓰기 데이터를 external named volume에 둔다. Kafka·Redis 변경은 같은 공유 파일 경로를 피하는 예방 조치이며 해당 두 엔진에서 동일 장애를 재현했다는 뜻은 아니다. 설정·비밀값과 백업 산출물은 기존 제한된 host bind를 유지한다. `fsync=on`, `data_sync_retry=off`를 유지하며 PostgreSQL 이미지나 버전을 바꾸지 않는다.

볼륨 이름은 `${OPERATING_PROJECT}-postgres-data`, `-kafka-data`, `-redis-data`다. `external: true`이므로 Compose가 없던 데이터 볼륨을 조용히 만들지 않는다. 기존 host 폴더를 자동 복사·삭제하거나 기존 slot/offset을 자동 초기화하지 않는다. `mac_compose_operating_bundle.prepare_bundle()`의 신규 출력에도 같은 저장 계약을 적용한다. 낮은 수준의 `compose()` 함수와 과거 생성 파일은 역사 검증용으로 남아 있으므로 운영 실행에 직접 사용하지 않는다.

새 빈 대상의 초기 준비 계약은 다음과 같다. 기존 운영 볼륨에 다시 실행하는 복구 절차가 아니다.

1. 최종 전환 대상과 `OPERATING_PROJECT`·`OPERATING_OWNER`·UID/GID를 확정한다. 동일 프로젝트가 이미 실행 중이거나 같은 이름의 볼륨이 있으면 중단하고 기존 소유권·데이터를 먼저 확인한다. 운영 이름으로 시험 볼륨을 만들지 않는다.
2. 승인된 새 대상의 **빈 Linux 로컬 볼륨 3개만** 생성하고 각각 `com.pawbridge.operating-owner` 라벨을 붙인다. `docker volume inspect`에서 이름·라벨·driver=local·별도 driver option 없음과 사용 중인 컨테이너 없음까지 확인한다. 기존 볼륨의 라벨이나 소유권을 추측해서 바꾸지 않는다.
3. 그 이름을 참조하는 후보에서 **`--profile bootstrap run --rm --no-deps storage-init`만** 실행한다. profile을 켜고 전체 `up`을 실행하면 다른 서비스도 시작될 수 있으므로 금지한다. 초기화기는 네트워크·비밀값 없이 3개 mount가 명시적인 Linux 파일시스템이고 전부 빈 상태인지 먼저 확인한 후 루트 디렉터리만 UID/GID·0700으로 바꾼다. 파일 생성·재귀 chown·데이터 삭제는 하지 않는다. 빈 상태에서는 재실행 가능하지만 하나라도 데이터가 있으면 거부한다.
4. 새 PG 볼륨에 검증된 초기화·최종 dump 복원을 수행하고 새 Kafka 볼륨은 새 cluster ID로 한 번 format한다. PG 이미지의 일반 서비스 진입점은 `postgres`여서 자동 initdb를 대신하지 않는다. 이미 데이터가 있는 볼륨에는 `storage-init`·format을 실행하지 않는다. Redis는 전환 직전 인계 판정에 따라 초기화/복원한다. 각 도구 역시 동일한 볼륨 이름을 사용해야 한다.
5. 새 DB의 publication·slot과 새 Kafka/Connect offset 정책을 확인한 뒤 CDC, 앱, AI 순으로 검증한다. Windows의 writer 동결·최종 백업·외부 진입 전환 승인 경계는 그대로다. Windows 또는 과거 시험의 slot/offset을 무조건 복사하지 않는다.

Linux 볼륨은 macOS 폴더에서 직접 열 수 있는 백업이 아니다. Docker Desktop 초기화나 볼륨 삭제 시 손실될 수 있으므로 암호화 dump/R2 백업과 복원 검증은 별도로 유지한다. `down -v`나 volume prune을 운영 종료 절차로 사용하지 않는다.

이번 CDC 시험은 합성 Outbox 15건의 topic/key/id/eventType/payload, 관찰된 누락·중복 0, Connect 재시작 2회, PostgreSQL·Kafka 재시작 1회, 5개 슬롯 LSN 진전과 최종 active/RUNNING을 확인했다. 기존 Mac 컨테이너·AI와 Windows를 변경하지 않았다. 당시에는 전체 앱·AI·백업 통합 검증이 남아 있었다. 이후 2026-10-16 전체 격리 통합 검증이 통과했으며, 아래 최신 실행 기준과 검증 경계를 따른다. 이 문장은 당시 후보 검증 상태이며 현재 운영 적용 여부의 근거로 사용하지 않는다.


## 최종 후보 이름과 네이티브 실행 경로 — 2026-10-16

운영 Compose 프로젝트는 `pawbridge-prod`다. `mac_operating_names.configure()`가 서비스 키와 내부 DNS는 유지하고 컨테이너 표시 이름을 정한다. 예: `pawbridge-prod-gateway`, `-user`, `-animal`, `-community`, `-store`, `-payment`, `-photo`, `-python-api`, `-postgres`, `-redis`, `-kafka`, `-kafka-connect`, `-native-ai-relay`, `-native-db-gallery-bridge`, `-backup-scheduler`. `-storage-init`는 bootstrap profile의 일회성 도구다. 기존 컨테이너·볼륨을 이름 변경으로 재사용하지 않는다.

네트워크는 `pawbridge-prod-private`, `-egress`, `-backup-private`, 새 운영 데이터 볼륨은 `pawbridge-prod-postgres-data`, `-redis-data`, `-kafka-data`다. 시험 프로젝트에는 별도 `pawbridge-pinned-native-validation-<시각>-<식별자>`를 사용한다. 의미 있는 이름은 소유권 검사를 대신하지 않는다.

AI LaunchAgent는 영구 위치의 Python/venv를 사용하고 `EnvironmentVariables.PYTHONPATH`로 검증한 최적화 SAM3 패키지를 명시해야 한다. 기존 venv의 interpreter가 임시 폴더에 연결돼 있었고 기존 LaunchAgent에는 SAM3 경로가 없었기 때문이다. 셸에서 성공했던 시험은 그 경로를 상속하고 있어 자동 시작의 증거가 되지 않았다.

새 비활성 후보에는 기존 Python·패키지를 파일 해시 대조 후 복사하고 새 venv의 interpreter 경로만 고정했다. 기존 AI runtime은 수정하지 않았다. 최소 환경에서 실제 `mac_native_service.py`를 실행해 MPS 모델 기동, Gateway 사진 검색, 갤러리 자동 1→2→1 갱신, 갱신 실패 중 검색 유지 및 회복을 확인했고 원래 AI 검색으로 복귀했다. 당시 시험은 LaunchAgent 등록이나 실제 재부팅 시험을 포함하지 않았다. 현재 관리 실행본의 등록 상태와 별개로 실제 재부팅 복귀는 미검증이다.

검증 근거는 앞선 전체 앱·AI·암호화 백업 통합 시험과 이번 이름/영구 native 실행 경로 변경의 추가 검증이다. 이번에 전체 백업과 모든 앱을 다시 시험한 것으로 표현하지 않는다. 최종 운영 DB·외부 진입 전환, 전체 운영 규모 갤러리 및 실제 재부팅 검증은 별도 남은 항목이다. 비공개 후보 `receipt.json`의 `activationAllowed=false`는 준비 상태 기록이며 그 자체가 수동 Docker 명령을 차단하는 장치는 아니다.

## 2026-10-17 native 계획 정지 보완

`mac_native_supervisor.py`가 기존 `mac_native_service.py`를 같은 process group의 자식으로 실행한다. 모델 소스·설정·검색 계약은 유지한다. LaunchAgent는 이 관리 코드를 진입점으로 하고 `KeepAlive.SuccessfulExit=false`를 사용한다. 자식의 예상외 종료는 코드0도 재시작 대상으로, 계획 TERM/INT는 자식 TERM1회 후 정리를 기다려 재시작을 억제한다. 관리 프로세스의 종료0은 정리 성공 판정이 아니며 실제 child 로그·PID·종료 코드를 확인한다.

현재 macOS 합성 시험에서 `KeepAlive=true`의 실행 중 job은 disable 뒤에도 재시작했다. 그러므로 disable→TERM만 원래 후보에 적용하지 않는다. 해당 변경분의 receipt·plist·정지 명령은 비공개 이관 기록에 보존한다. 현재 관리 실행본은 supervisor를 진입점으로 사용하며 실제 설정 파일을 저장소 예시로 복사하지 않는다. 계획 정지는 disable→관리 프로세스 TERM→정상 정리·PID 종료·재시작 없음 확인→bootout 순서다. ExitTimeOut=0, detached child, AbandonProcessGroup=true를 사용하지 않는다. 실제 OS 종료/관리 프로세스 장애까지 무강제 종료를 보장하지 않는다.

관련 단위 테스트는 `python3 -m unittest -v test_mac_native_supervisor.py`로 scripts/environments에서 실행한다. 9 tests와 실제 launchd 합성 시험을 통과했으며 모델 재기동과 실제 재부팅 검증을 구분한다.
