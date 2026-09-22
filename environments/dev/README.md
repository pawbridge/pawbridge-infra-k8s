# 분리된 dev 환경 실행 계약

현재 운영이 사용하는 `values/`는 전환 완료 전까지 보존한다. 새 dev는 `isolated-values/`를 사용한다. 이미지 CI도 이 경로만 갱신한다. 운영 기준 설정은 `../prod/values/`에 보존한다.

## 구성과 자원

- namespace: `pawbridge-dev`. PostgreSQL/pgvector, Redis, Kafka/Connect와 앱 데이터를 운영과 분리한다.
- 앱·DB·Connect replicas는 기본 0, CDC connector는 stopped, APMS·사진 보관·갤러리 자동 수집은 꺼져 있다. Kafka CR은 별도의 수동 적용 대상이며 플랫폼 적용 시 broker 1개가 시작한다. `kubectl apply`를 승인 없이 실행하지 않는다.
- 전체 E2E 예산의 namespace 상한은 memory limits 12Gi / requests 8Gi, CPU limits 14 / requests 6, PVC 총합 24Gi다. 이는 필요한 현재 노드 여유가 확보됐다는 뜻이 아니다. 노드별 스케줄 가능량·여유 검증 후 기동한다.
- 네트워크는 같은 dev namespace와 DNS만 허용한다. 운영 DB/Redis/Kafka와 외부 결제·메일·R2에 기본 접근할 수 없다. 별도 dev 자격증명과 외부 연동별 허용 경로를 준비하기 전에는 해당 기능이 실패하는 것이 의도된 상태다.
- Strimzi operator가 dev namespace를 감시하도록 설정되어 있는지 확인한다. Connect의 Secret provider는 dev namespace Secret get만 허용한다. API server 통신과 operator 관리는 실제 주소·pod label에 맞는 정책을 적용 승인 시 추가한다. 운영 namespace 전체 허용으로 우회하지 않는다.

## 기동 순서 (승인 후)

1. 정확한 context와 namespace, 노드 여유, 전용 스토리지, dev Vault role/Secret 목록을 확인한다. `platform/kustomization.yaml` 렌더링을 검토한다.
2. namespace·경계·DB·Redis·Kafka를 적용한다. DB/Redis를 각각 1로 기동하고 readiness를 확인한다. PV는 dev 전용이며 운영 PV 연결을 금지한다.
3. 각 서비스의 dev app/migrator/CDC 계정·스키마를 만들고 기존 PostgreSQL migration distribution으로 스키마를 구성한다. 현재 migration CLI는 loopback 주소만 받으므로 **dev PostgreSQL Service를 별도 localhost 포트로 전달**하여 실행한다. 운영 접속값/덤프를 사용하지 않는다. migrate/validate, pgvector extension, CDC heartbeat/publication을 확인한다.
4. Connect의 dev Secret/권한을 확인하고 worker를 1로 기동한다. 다섯 connector를 running으로 전환한 뒤 가상 데이터를 넣어 Outbox→Kafka→소비자를 검증한다. 단순 pod Ready를 CDC 검증으로 간주하지 않는다.
5. `gitops/argocd/environments/dev`의 앱들을 수동 등록한다. 각 앱 values 목록에 `../../environments/dev/activated-values.yaml`을 추가하는 dev 전용 PR로 기동한다. HPA·자동 sync는 초기에는 끈다. 앱/DB 시작 조건을 모두 만족한 후 자동 sync 정책을 결정한다.
6. 로컬 첫 진입점은 `kubectl --context <검증한-context> -n pawbridge-dev port-forward service/api-gateway 18080:8080`. 프론트는 `VITE_API_BASE_URL=http://localhost:18080 npm run build:dev` 또는 dev 프리뷰를 사용한다. 공개 dev URL·OAuth redirect 등록은 별도 준비한다.
7. 로그인/게시판/상점/주문/테스트 결제/사진 업로드·검색을 확인하고 운영에 데이터·이벤트·파일이 생성되지 않았는지 확인한다. 차단된 외부 연동과 실제 AI 미실행을 E2E 완료로 표시하지 않는다.

## AI

dev GPU proxy는 `/home/vagrant/.local/run/pawbridge-gpu-dev`만 사용한다. 운영 socket을 공유하지 않는다. 별도 dev WSL 프로세스·DB role·갤러리·상태 디렉터리를 준비한 뒤 GPU 여유를 확인해야 한다. 운영 프로세스 중단이나 2개 모델 동시 로드를 이 변경이 승인하지 않는다. Python repo의 dev 예시와 실행 검증기를 사용한다.

## 중지와 복구

Argo가 자동 재기동하지 않는 수동 정책인지 먼저 확인한다. 앱/Connect를 0으로, connector를 stopped로 둔 뒤 DB/Redis를 0으로 낮춘다. Kafka는 Strimzi 버전에서 지원하는 중지 방식을 확인한 후 별도 승인해 중지한다. namespace/PVC를 삭제하는 종료 스크립트를 제공하지 않는다. 필요 시 마지막 검증된 dev 설정으로 복원한다.

## 운영 전환

기존 Application을 중복 생성하지 않는다. `gitops/argocd/environments/prod`는 **기존 이름**을 유지하고 main/prod values를 참조한다. main에 현재 운영 기준과 차트를 먼저 준비하고 동일 렌더링을 확인한 뒤 기존 Application source/project를 전환한다. 현재 운영의 dev 추적을 해제하기 전에는 legacy values를 삭제하거나 재사용하지 않는다. 기본 브랜치 변경은 필요 없다.
