# Mac 운영 구성의 소스와 실행 경계

이 디렉터리는 Mac 운영에 사용한 Compose 정의와 설정 준비 코드를 관리한다. 저장소의 템플릿, 비공개 렌더 결과, 실제 실행본은 서로 다른 자료다. 파일을 게시하거나 단위 테스트를 통과한 것으로 새 배포나 복구를 완료했다고 표현하지 않는다.

## 정본과 연결

- [background-jobs-20261016/compose.json](background-jobs-20261016/compose.json)은 일반 서비스, Linux 데이터 볼륨, native 로컬 중계, 백업을 포함하는 전체 Compose 정의다. [실행 경계와 관리형 진입점](background-jobs-20261016/README.md)을 함께 읽는다.
- [runtime-controls-20261016](runtime-controls-20261016/README.md)은 예약 모드와 CDC 계약을 설명한다. 이 폴더의 Kafka·Connect·connector·topic·native relay 정의를 사용한다.
- `scripts/environments/mac_compose_operating.py`는 검토된 외부 입력 9개와 보존된 roles SQL로 비공개 credential fragment를 만든다. `mac_compose_operating_bundle.py`는 standby/drain/active 환경, CDC 및 기본 Compose를 준비한다. 두 도구 모두 Docker·SQL·외부 API를 실행하지 않는다.
- 기본 Compose 생성기는 백업·로컬 native 접근 중계·LaunchAgent·Python 환경 설치까지 만들지 않는다. 전체 운영 구성을 재현할 때는 위 전체 Compose 정의와 별도 관리형 native/monitor 실행 절차가 필요하다.

## 비공개 입력과 실제 실행본

비밀값과 생성 결과는 저장소 밖 `pawbridge-secrets/runtime/production`에 둔다. 입력과 기존 파일을 덮어쓰지 않으며 디렉터리 0700, 파일 0600을 사용한다. 역할 SQL, DB/JWT/Redis/API/R2 키, 실제 env·native/monitor JSON·receipt·plist·로그·백업 산출물은 커밋하지 않는다. 앱에는 관리자·CDC·백업 자격증명을 공급하지 않는다. `env_file`은 `format: raw`로 읽는다.

운영 native/monitor 실행본은 사용자 Library의 `Application Support/PawBridge/production-20261017/runtime-v1`에 있다. 이 실행본은 소스·관리 코드·비공개 설정·상태를 고정하여 사용한다. 저장소 파일을 수정해도 이 복사본이 자동 갱신되지는 않는다. 실제 실행 설정을 이 문서의 예시로 복사하지 않는다.

최초 운영 Compose 실행 조합은 비공개 고정 후보의 `compose.json`과 관리 실행본의 `backup-path-override.json`이었다. 백업 작업 경로는 이 override로 관리 실행본에 연결한다. 활성 앱은 서비스별 비공개 active env를 사용하며, 기본 standby env만 다시 연결하면 현재 동작을 재현하지 못한다. native active 설정과 monitor 설정도 관리 실행본의 비공개 파일을 사용한다. 경로만 바꿔 새 배포를 실행하지 말고 해당 환경의 승인된 실행·복구 절차를 따른다.

현재 앱의 후속 설정은 서비스별 비공개 실행본에서 확인한다. 공유키 교체 뒤 Animal에는 최대 DB 연결 수 10 / 최소 유휴 0을 적용한 단일서비스 구성이 추가됐다. 다른 Java DB 서비스는 최대 3을 유지한다. `mac_compose_operating_bundle.py`의 새 생성 계약도 이 값을 사용하지만, 생성기 수정이 기존 렌더본을 자동 갱신하지는 않는다.

Animal을 이전 rotation 구성으로 다시 생성하면 최대 연결 수가 3으로 돌아갈 수 있다. 실제 재생성에는 최신 비공개 receipt와 Obsidian의 서비스별 구성 경로를 확인하고, 해당 서비스의 정상 종료 완료 후 승인된 단일서비스 구성만 사용한다. 현재 인증값을 포함한 렌더본과 원복 파일은 Git에 추가하지 않는다.

## 예약 작업과 상태 인계

`schedules.json`은 고정 backend 소스와 승인된 APMS 변경에서 수집한 작업 목록이다. 실제 활성 상태나 모든 writer의 중지를 입증하는 자료는 아니다.

- standby는 새 수집·정리·cart/projection 예약을 억제하지만 API·수동 작업·Kafka 소비자를 중지하지 않는다.
- drain은 새 수집·정리를 막고 cart DB 반영과 projection을 허용한다. dirty=0 또는 한 번의 lag=0만으로 잔여 처리 완료를 판단하지 않는다.
- active는 승인된 작업을 활성화한다. 다른 원본 writer와 공개 connector의 비활성 상태를 먼저 확인해야 한다.

전역 scheduling 플래그와 `@Scheduled` 목록만으로 APMS·사진 보관 SmartLifecycle·시작 이벤트까지 중지했다고 판단하지 않는다. 모드 전환에는 해당 서비스의 env 선택과 컨테이너 재생성이 필요하다.

## 검증과 변경 한계

출처 연결은 고정 후보의 source receipt 및 관리 실행본과의 파일 해시 대조로 확인한다. 세부 이관·격리 시험·운영 확인 기록은 Obsidian `Projects/pawbridge`와 비공개 실행 기록에서 관리한다. 저장소 단위 테스트는 합성 입력과 임시 폴더로 계약을 검사하며 운영 데이터·모델·DB·Docker에 접속하지 않는다. 백업 라이브러리의 `hashlib.file_digest`를 사용하는 테스트에는 Python 3.11 이상이 필요하다.

native/monitor의 관리형 실행과 실제 OS 재부팅 후 전체 자동 복귀는 별도 검증이다. 실제 재부팅 복귀는 미검증이며, 이번 소스 게시로 운영·복구자료·실행 중 서비스가 변경되지는 않는다.


## 관리형 실행 정의 예시

`launchd/kr.pawbridge.native.plist.example`와 `kr.pawbridge.monitor.plist.example`는
운영에서 사용하는 재시작·예약 정책을 보존한 경로 자리표시자 예시다. 실제 운영
plist 또는 비밀값 설정 파일을 복사한 것이 아니다. `@@PYTHON@@`, `@@RUNTIME@@`,
`@@PRIVATE_NATIVE_CONFIG@@`, `@@PRIVATE_MONITOR_CONFIG@@`를 새 실행본의 검증된
절대 경로로 치환해야 한다. monitor의 `PYTHONPATH`는 새 실행본의 scripts 경로다. native 예시는 scripts와 `@@SAM3_PACKAGE_PARENT@@`를 함께 사용한다. 후자는 검증된 MPS 패치의 `sam3/` 디렉터리를 포함하는 상위 폴더여야 하며, 실제 가중치나 설정 경로가 아니다. native 진입점은 승인된 SAM3를 import할 수 있어야 하고 시작 전에 패치 해시를 검사한다.

native 정의는 supervisor를 통해 모델 자식 1개를 관리하며, monitor는 기존 60초
로컬 상태 검사를 실행한다. 이 예시를 현재 등록된 서비스 옆에 추가로 등록하지
않는다. 기존 launchd 등록과 운영 프로세스는 이번 소스 게시에서 변경하지 않았다.
실제 재부팅·정전 뒤 전체 서비스 자동 복귀는 별도 미검증이다.


## 이관 후 소스 보존 커밋

`production-source-map.json`은 2026-10-17 운영 빌드 출처와 일치하는 앱 코드의
2026-10-18 게시 커밋·파일 해시 및 기존 ARM64 이미지 참조를 연결한다. 게시 커밋은
운영 기동 뒤 만들어졌으므로 그 커밋으로 이미지를 다시 빌드·배포했다는 뜻은 아니다.
로컬 이미지 ID를 새 기기에서 곧바로 pull할 수 있다고 가정하지 않는다. 운영에
사용한 모델·데이터·비공개 설정은 별도 보존 자료이며 이 JSON이나 Git에 넣지 않는다.
