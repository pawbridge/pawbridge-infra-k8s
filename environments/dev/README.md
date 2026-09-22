# 로컬 dev 환경

개발 환경은 Windows PC의 로컬 Docker/WSL에서 실행한다. 운영 VM의 Kubernetes에 dev namespace·DB·Kafka를 추가하지 않는다. `main`은 운영, `dev`는 개발 검증 기준이며 기본 GitHub 브랜치는 dev를 유지해도 된다.

## 실행

Python 3 + PyYAML, Docker Compose v2, 서비스가 요구하는 Java/Gradle을 사용한다. 별도 설치를 자동 수행하지 않는다. 인프라 저장소 루트에서 실행한다.

```sh
python3 scripts/environments/local_dev.py prepare
python3 scripts/environments/local_dev.py config
python3 scripts/environments/local_dev.py up-data
```

이 명령은 PostgreSQL/pgvector, Redis, Kafka만 실행한다. 기본 상태 경로는 `~/.local/state/pawbridge/dev`이며 다른 폴더는 `--state /절대/경로`로 모든 명령에 동일하게 지정한다. 설정과 비밀번호를 운영에서 복사하지 않고 처음 한 번 무작위 생성한다. 재실행해도 비밀번호는 유지된다. 상태 디렉터리는 0700, `.env`는 0600이다. 비루트 컨테이너에 읽기 전용 마운트하는 설정 파일만 0444이며 부모 디렉터리는 비공개다. 이 경로 전체를 Git에 넣지 않는다.

다섯 백엔드 서비스 폴더에서 각각 `bash ./gradlew migrationDistribution`을 실행한 뒤:

```sh
python3 scripts/environments/local_dev.py migrate --backend /절대/경로/pawbridge-backend-k8s --java /Java17/bin/java
python3 scripts/environments/local_dev.py up-events
python3 scripts/environments/local_dev.py register-cdc
python3 scripts/environments/local_dev.py up-apps
python3 scripts/environments/local_dev.py status
python3 scripts/environments/local_dev.py smoke
python3 scripts/environments/local_dev.py verify-cdc
```

마이그레이션은 소스 SQL과 패키지 일치, 로컬 DB의 dev 표식, loopback 주소를 확인한다. DB는 새 개발 데이터만 보관한다. 운영 덤프·PV·토픽·비밀번호를 재사용하지 않는다. Connect 등록은 스키마 초기화 후 수행하며, 다섯 서비스의 실제 Outbox 라우팅/헤더 설정을 사용한다.

`isolated-values/*.yaml`은 CI가 갱신하는 **이미지·마이그레이션 메타데이터**다. Kubernetes dev values가 아니다. `prepare`가 그 digest를 읽어 Compose를 생성한다. 새 dev 이미지가 병합되면 `prepare` 후 해당 로컬 서비스를 재기동한다. GitHub CI가 꺼진 PC를 자동 기동하거나 로컬에 배포하지 않는다.

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
| 프론트 | 프론트 저장소에서 `npm run dev:local`, localhost:5184 |

## 자원과 외부 연동

- PostgreSQL 768MiB, Redis 256MiB, Kafka 1GiB, Connect 1GiB, local-access 64MiB 상한. 여섯 Java 앱은 각 640MiB이며 heap은 384MiB다. 필요할 때만 기동한다. 이 상한의 합이 실제 메모리 사용량을 뜻하지 않는다.
- 데이터·앱 네트워크는 internal이며, `local-access` NGINX만 별도 access 네트워크에서 loopback 포트를 받는다. 고정된 내부 서비스로만 TCP를 중계하며 범용 HTTP 프록시가 아니다. 내부 네트워크만 사용하면 Docker에서 호스트 포트가 노출되지 않는 실행 결과를 반영했다. 컨테이너에서 운영 VM·공개 API로 나가는 일반 경로를 제공하지 않는다. 실제 차단 여부는 실행 환경에서 검증한다. IDE 프로세스에는 이 Docker 네트워크 경계가 적용되지 않으므로 생성된 개발 설정을 확인한다.
- APMS·TourAPI·보호소 수집·사진 보관·갤러리 자동 갱신은 기본 중지한다. 기본 외부 키는 작동하지 않는 개발 자리표시자다. OAuth·메일·Toss·R2·외부 LLM은 별도 dev 자격증명/허용 네트워크를 준비하기 전에는 실제 성공하지 않는다. 해당 미실행 범위를 전체 E2E 성공으로 표현하지 않는다.
- 사진 처리/CPU AI는 `optional-ai` profile이며 기본 기동하지 않는다. GPU는 기존 운영 모델을 중단하거나 두 벌로 로드하지 않는다. Python 저장소의 `deploy/dev`에 별도 localhost:18091 실행 경계를 정의한다.
- 앱 시작은 프로세스 생성일 뿐 기능 검증 완료가 아니다. 공개 조회, 인증, 쓰기, CDC·소비자 처리, 외부 테스트 연동을 구분해 확인한다.

## 중지·복구

```sh
python3 scripts/environments/local_dev.py stop
```

이 프로젝트의 컨테이너만 중지하고 개발 DB·Kafka 볼륨은 보존한다. `down -v`, 전체 Docker prune, 운영 VM 종료를 사용하지 않는다. 설정 변경 실패는 이전 Git revision의 실행 도구와 **같은 state 경로**로 복구한다. 스키마 변경은 별도 호환성/복원 계획이 필요하며 이미지 복귀만으로 되돌려졌다고 판단하지 않는다. 상태 디렉터리와 볼륨은 한 쌍이므로 비밀번호 파일만 삭제하지 않는다.

`smoke`는 여섯 서버 readiness, 동물·상품 공개 조회와 다섯 CDC task를 확인한다. `verify-cdc`는 고유한 dev 검증 토픽과 가상 Outbox 이벤트를 추가해 커밋 2건·롤백 1건의 전달, 키·헤더·JSON을 확인한다. 실제 업무 소비자의 상태 변경이나 OAuth·결제·GPU까지 검증하는 명령은 아니다. 검증 데이터는 로컬 dev에 남고 운영에는 쓰지 않는다.
