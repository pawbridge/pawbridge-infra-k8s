# 관측 baseline 실행·검증 계약

이 문서는 같은 경로의 manifest와 `tests/` 실행에만 해당한다. 전체 계획과
운영 실행 결과의 정본은 Obsidian `Projects/pawbridge/14 관측성 성능과 테스트`다.
현재 구성은 **수동 동기화 후보**이며 Git 병합만으로 설치하거나 Slack에 전송하지 않는다.
기존 `infra/install-infra.sh`나 `infra/monitoring/values.yaml`을 실행하지 않는다.

## VM·서비스·파드별 사용량 확인

Grafana의 PawBridge 폴더에서 다음 화면을 선택한다. 각 화면 위 링크로 이동할 수 있다.

1. **운영 현황**: VM 세 대를 이름으로 비교한다. 네임스페이스를 선택하면 app별 메모리와
   CPU를 개별 막대로 표시한다. 서비스 막대의 링크를 누르면 해당 서비스 상세로 이동한다.
2. **서비스 상세**: 네임스페이스와 서비스를 선택한다. 위쪽 합계는 해당 서비스의 모든 파드를
   포함한다. 아래쪽은 선택한 파드마다 행과 그래프를 반복한다. 파드 선택으로 서비스 합계를 바꾸지 않는다.
3. **VM·파드 상세**: VM, 네임스페이스, 파드 순서로 선택한다. 위쪽은 해당 VM의 파드별 비교,
   아래쪽은 선택한 파드 하나의 사용량이다. app 이름표가 없는 배치·인프라 파드도 이 화면에서 찾는다.

CPU 단위 1은 한 코어다. 메모리는 working set이다. 요청량·제한량 비교선은 일반 컨테이너의
합계이며, 실제 사용량이나 스케줄러의 전체 파드 자원 계산과 같지 않다. 제한 미설정 컨테이너가
있으면 제한 합계를 파드 전체의 강제 상한으로 해석하지 않는다.
네트워크 속도는 bytes/second, 기간 누적 트래픽은 bytes다. 내부 통신을 포함한 증가량 추정치이며
과금용 계량이 아니다. hostNetwork 파드는 호스트 네트워크를 공유하므로 개별 앱 트래픽으로
해석하지 않는다. 수집 공백은 0이나 정상으로 채우지 않는다.
VM 루트 디스크 여유 공간은 파드별 저장 용량이 아니다.
JVM 힙·GC·HTTP 응답시간은 이 수집 범위에 없으며 대시보드 설치만으로 생기지 않는다.

서비스 연결은 기존 pod의 app 이름표를 사용한다. KSM에 pods=[app] 하나만 허용하며
전체 label·annotation 수집, 새 exporter, 앱 설정 변경은 하지 않는다.
적용 시 KSM Deployment의 수집 인자가 바뀌므로 해당 관측 파드가 교체된다.
app 이름표 수집을 시작하기 전 과거 서비스별 합계는 소급 생성되지 않는다.
상세 파드 행은 Grafana의 반복 행 기능을 사용하며 여러 파드를 한 선 묶음으로 합치지 않는다.

변경 검증:

```bash
python3 gitops/observability/tests/test_dashboard_queries.py /absolute/path/to/promtool
python3 gitops/observability/tests/test_grafana_contract.py /path/to/helm-render.yaml /path/to/resources-render.yaml
```

첫 검사는 네임스페이스 격리·복수 파드 합계·네트워크 집계·사라진 파드의 기간 트래픽을 검증한다.
두 번째 검사는 세 JSON의 실제 ConfigMap 포함과 Grafana 보안 설정을 검사한다.
반영 후에는 서비스 선택 목록, 두 파드의 반복 행, VM 이름, 빈 결과 표시를 실제 브라우저에서 확인한다.
소스·조회식·등록 API 검사만으로 브라우저 화면까지 확인했다고 기록하지 않는다.
롤백은 이전 Git revision으로 관측 Application을 동기화한다. 대시보드 DB를 직접 수정하거나 PVC를 삭제하지 않는다.

## 고정 입력

- Helm chart: `kube-prometheus-stack 89.2.4`
- 공식 배포 tgz SHA-256: `f4405216a734b182967ba8cc08169dd68a6e401688bb6885bffe0c07aad02b23`
  (HTTPS 다운로드와 해시 확인. provenance 서명 검증을 의미하지 않는다.)
- Prometheus: `v3.13.3` LTS, 지원 종료 2027-07-31. 새 기능 계열 3.14는 선택하지 않는다.
- Alertmanager: `v0.34.0`. 위 두 이미지의 registry manifest digest는 values에 고정한다.
- Grafana: `12.4.10`/digest 고정. 12.4 계열은 2027-05-24까지 patch support다.
  고정 chart의 Grafana 13.2 기본값 대신 지원 중인 이전 major의 마지막 minor를 사용한다.
  Helm 렌더 검증은 통과했지만 이 조합의 실제 기동·로그인·저장 검증은 설치 게이트로 남는다.
- Operator `v0.93.1`, KSM `v2.20.0`, node-exporter `v1.12.1-distroless`,
  인증서 생성 Job `1.8.8`은 고정 chart의 종속 버전이다. 버전 변경 시 재렌더한다.
- 대상 context: `pawbridge-vbox-k136`, namespace `monitoring`.
- Argo App: `observability-baseline`. automated sync·prune는 설정하지 않는다.

## 범위와 자원 승인 게이트

Prometheus·Alertmanager·Grafana·Operator·KSM·Loki·Alloy는 cp1 고정, node-exporter만 각 노드에 배치한다.
워커 장애 직전 기록을 cp1에 보존하지만 cp1 또는 Windows 호스트 전체 중단에는
수집·알림도 중단된다. 외부 가용성 감시나 프로세스별 CPU/RSS/I/O 이력의 대체가 아니다.
앱 6개의 기존 ServiceMonitor, APMS 업무 성공, Kafka 이벤트 완료까지 수집한 것으로
표현하지 않는다. 첫 수집은 노드·컨테이너·Kubernetes 상태·관측기 자체뿐이다.

| cp1의 정상 실행분 | request | limit |
| --- | ---: | ---: |
| Prometheus | 384Mi | 512Mi |
| Alertmanager | 48Mi | 96Mi |
| Operator | 64Mi | 128Mi |
| KSM | 32Mi | 64Mi |
| reloader 2개 | 32Mi | 64Mi |
| node-exporter 1개 | 16Mi | 48Mi |
| Grafana | 128Mi | 256Mi |
| Loki | 256Mi | 768Mi |
| Alloy | 96Mi | 256Mi |
| 합계 | 1056Mi | 2192Mi |

각 워커 추가분은 node-exporter request 16Mi / limit 48Mi다. cp1의 인증서 Job은
request 32Mi / limit 64Mi가 추가되며 KSM rolling 교체도 추가 replica 여유가 필요하다.
이는 실제 사용량 측정치가 아니다. 기존 requests 합+새 requests가 Allocatable 안에
들어가는지, 실제 피크에 교체 여유까지 있는지 확인하고 **별도 설치 승인**을 받는다.
2026-09-10 사용자 승인 후 cp1만4→6GiB로 증설하고 정상 종료·기동했다.
게스트 총 메모리5921MiB, 가용3978MiB를 확인했다. 워커·데이터 디스크는 변경하지 않았다.
이는 재기동 직후 측정이며 관측 도구 설치나 운영 피크 검증은 아니다. 설치 직전 다시 측정한다.
현재 여유가 작으면 설치하지 않고 RAM 증설 또는 수집 예산 조정을 먼저 승인받는다.
자원 한도를 자동으로 늘리거나 기존 앱을 중단·축소하지 않는다.

Prometheus는 local-path 6Gi PVC, 7일 또는 4GB 중 먼저 도달하는 보관 기준을 사용한다.
WAL/head chunk는 별도 공간을 쓰므로 7일 보관이 보장되는 것은 아니다. local-path는
PVC의 표기 용량만큼 하드 쿼터를 보장하지 않는다. cp1 실제 디스크 여유도 확인한다.
Alertmanager는 1Gi PVC로 silence/알림 중복 억제 상태를 보존한다. Retain은 백업이 아니다.
Grafana는 2Gi PVC로 사용자·설정 DB를 보존하며 local-path 특성상 실제 디스크 쿼터를
보장하지 않는다. 단일 PVC를 쓰는 UI이므로 Recreate로 교체하고 잠깐의 화면 중단을
허용한다. 백엔드 앱의 배포 전략을 바꾸는 것은 아니다.

## Grafana 화면·인증과 로그 단계

- Grafana는 VM host-only 네트워크의 NodePort로 접근한다. PC IP 한 개를 추가 허용하며
  익명 접근·공개 회원가입·자동 플러그인 설치·공개 도메인은 구성하지 않는다.
- 초기 관리자 자격 증명은 미리 준비할 `monitoring/monitoring-grafana-admin` Secret의
  `admin-user`/`admin-password` 키를 참조한다. 등록·Vault/VSO 연결 소스와 승인 후 실행 순서는
  [관측성 비밀값 실행 계약](../../infra/vault/observability-runtime.md)을 따른다. 운영 적용은 아직 하지 않았다.
  이 값은 최초 DB 초기화용이다. 기존 Grafana DB의 관리자 암호가 Secret 수정만으로
  자동 변경된다고 가정하지 않으며 이후 계정/암호 변경은 Grafana 지원 절차로 검증한다.
- Dashboard/Datasource sidecar와 Grafana Kubernetes RBAC를 끄고 토큰을 마운트하지 않는다.
  Git의 ConfigMap/파일 provisioning으로 한국어 패널6개와 Prometheus 연결을 공급한다.
  기본 대시보드/불필요한 외부 플러그인은 추가하지 않는다. 경보 발송은 Alertmanager가
  담당하며 Grafana 자체 unified alerting은 꺼서 이중 운영하지 않는다.
- Loki3.6.16/Alloy1.18.1을 digest로 고정하고 Loki datasource를 추가했다. 새 minor 기능이
  필요하지 않아 보안 backport가 있는 정식 patch를 사용하며 preview 기능은 켜지 않는다.
  Loki Helm chart의 gateway/canary/cache 부속 파드를 넣지 않고 두 Deployment만 사용한다.
- Alloy가 `pawbridge`의 서비스7개와 라벨 없는 `animal-service-batch-*` 배치 로그를 읽는다.
  별도 네임스페이스의 DB/Vault, cloudflared, Windows/노드 journal은 대상이 아니다.
  Kubernetes API 방식은 hostPath/root/DaemonSet 없이 동작하지만 kubelet CPU·네트워크 비용이
  있으며 `pods get/list/watch`, `pods/log get` Role만 부여한다. Secret API 권한은 없다.
- Loki는 **단일 노드·저용량 filesystem pilot**이다. 공식적으로 filesystem은 production 권장
  저장소가 아니며 고가용성/노드 디스크 손실 복구를 보장하지 않는다. 별도 비공개 object storage
  전환은 후속 설계이며 공개 상품 이미지 R2 버킷에 운영 로그를 저장하지 않는다.
  4Gi local-path PVC에 chunks/index/cache/WAL/compactor를 모두 보존한다. 72h 보관,
  24h index와 compactor 삭제를 켰지만 삭제 지연2h와 compaction 주기로 즉시 삭제되지는 않는다.
  local-path의 4Gi는 하드 쿼터가 아니므로 실제 VM 디스크 여유 경보를 함께 사용한다.
- 평균 수신 상한0.01MiB/s·burst1MiB, 최대500 stream, 로그1줄16KB, 조회1000줄/72h로
  제한한다. 상한 초과 시 누락될 수 있으며 실제 유입량에 맞는지 설치 후 검증해야 한다.
  Alloy는 알려진 인증/비밀값 패턴의 줄과 과도하게 긴 줄을 버린다. 임의의 개인정보까지 모두
  제거하는 기능이 아니며 애플리케이션에서 비밀을 로그에 쓰지 않는 것이 우선이다.
- index label은 cluster/namespace/app/pod/container와 Loki가 app에서 만드는 service_name이다.
  pod는 재시작 구분에 필요해 유지하되 72h·500 stream 제한으로 무제한 증가를 막는다.
  API reader에 필요한 UID는 Loki label로 전송하지 않는다. 조회의 detected_level 같은
  structured metadata와 실제 index label은 구분해 테스트한다.
- **무손실/정확히 한 번 수집은 보장하지 않는다.** GA-only 방침으로 Alloy의 실험 WAL/queue
  옵션은 넣지 않는다. 강제 종료 시 메모리 대기 로그, 재시도 소진·로그 회전 때는 누락될 수 있고
  재연결/재시작 때 중복도 가능하다. 전송 drop·Loki 수신 거부·WAL 오류 지표와 경보를 수집한다.
  Loki WAL은 받아들인 로그의 프로세스 재시작 복구용이지 디스크 장애 백업이 아니다.
- Loki/Alloy ConfigMap은 내용 해시 이름을 사용해 설정 수정 시 Recreate한다. 고정 이름인
  Grafana dashboard generator만 해시를 끈다. 설정 교체 중 잠깐 수집/조회 중단을 허용한다.
  자동 prune가 없으므로 이전 해시 ConfigMap은 남을 수 있으며 현재/롤백 참조 확인 후 별도 정리한다.
- Loki는 자체 사용자 인증이 없어 monitoring 내부 Alloy/Grafana/Prometheus Pod에서만 들어오도록
  NetworkPolicy를 둔다. **현재 정책은 ingress만 제한하며 egress 격리는 미구현이다.** 운영 설치 전
  CNI의 실제 정책 집행과 허용/차단 통신을 검증한다. 이를 확인하기 전 공개 노출하지 않는다.

## kubelet 인증서 알림 적용과 확인

node-exporter는 Kubernetes가 제공한 파드 배치 노드 이름을 `node` 라벨로 수집한다.
메모리·디스크 경보의 Slack 대상에는 수집기 파드보다 VM 이름이 우선 표시된다.
이 라벨 추가는 파드 재시작 없이 수집 설정을 갱신하지만 기존 시계열과 경보의 식별자가
바뀐다. 기존 기록은 보존되며 활성 경보는 해소·재발생할 수 있고 대기 시간도 다시 계산된다.

이 구성은 kubelet 서버 인증서의 만료와 승인 대기를 알린다. 인증서를 자동 승인하거나
Vault 웹 인증서를 관리하지 않는다. 기존 버전의 kubelet 지표와 kube-state-metrics를
사용하며 이미지·차트 버전을 바꾸지 않는다.

- 만료 30일 미만·7일 이상은 warning, 7일 미만과 이미 만료된 값은 critical이다.
  조건이 5분간 유지되면 알린다. 두 단계가 동시에 울리지 않으며 갱신 후 해소된다.
- 노드별 잔여기간 누락·`+Inf`·`NaN`은 별도 경보다. 전체 노드 상태 지표가 사라지는
  경우에는 기존 `PawBridgeMonitoringMissing` 경보를 함께 확인한다.
- CSR은 `kubernetes.io/kubelet-serving` 요청만 대상으로 한다. 생성 후 10분을 넘긴
  미처리 상태가 5분간 유지되면 알린다. 승인·거절·실패·발급 완료 요청은 제외한다.
- CSR이 0개면 객체별 지표도 없는 것이 정상이다. 수집기의 list 또는 watch 성공 카운터와
  최근 오류를 따로 감시한다. 초기 데이터를 watch로 받으면 list 카운터가 없을 수 있다.
  성공 카운터는 요청 성공 이력이며 초기 동기화 완료나 최신 데이터 보장은 아니다. 이 경보는 모든 종류의 수집기 정지를
  감지한다고 보장하지 않는다. 승인 후 발급 실패를 별도 경보로 구현한 것도 아니다.
- 추가 RBAC는 CSR `list`, `watch`뿐이다. 상태 수집기 자체 지표는 내부 포트에서
  필요한 CSR 카운터만 수집한다. Secret 값·인증서 요청 본문은 지표로 전송하지 않는다.

배포 전 고정 차트 렌더와 `test_rules.py`, `check_render.py`, `test_notifications.py`를
실행한다. 실제 운영 적용은 별도 승인이 필요하다. CSR 읽기 권한·상태 수집기 Service와
Deployment·ServiceMonitor를 먼저 반영하고 새 지표가 수집되는지 확인한 뒤 경보와
Slack 템플릿을 반영한다. 상태 수집기는 설정 변경으로 교체되지만 앱·Vault는 재시작하지 않는다.

적용 후 세 노드의 `kubelet_certificate_manager_server_ttl_seconds`가 유한한 값이며
올바른 `node` 라벨을 갖는지 확인한다. CSR list 또는 watch 성공 카운터가 0보다 큰지도 확인한다.
초기 조회를 watch에 포함하는 방식은 [Kubernetes streaming lists](https://kubernetes.io/docs/reference/using-api/api-concepts/#streaming-lists)를 따른다.
실제 인증서를 삭제하거나 악성 CSR을 제출해 시험하지 않는다. 별도 승인한 합성 알림으로
한국어 발생·복구 Slack 수신을 확인하고 시험 데이터를 제거한다. 로컬 템플릿 검사는
실제 Slack 수신 성공을 뜻하지 않는다.

되돌릴 때는 이번 경보 5개를 먼저 제거하고 이전 Slack·수집 설정을 복원한다.
그다음 CSR 조회 권한을 이전 상태로 돌린다. 기존 인증서·PVC·Secret 값은 변경하지 않는다.
이 절은 배포 절차이며 현재 운영 반영 여부는 Obsidian의 최신 검증 기록을 따른다.

## 보안·연결 게이트

시간 게이트가 먼저다. 절전·재부팅 후 Windows/WSL과 VM UTC를 비교하고 NTP의
실제 잔여 보정량을 확인한다. 2026-09-10 재개 시 세 VM에서 약 5시간 8분 지연을
측정했다. Node Ready나 NTP source 선택만으로 시각 정상화를 판정하지 않는다.
시각 불일치 중 인증서 발급·설치 검증을 진행하지 않으며, 운영 중 강제 시간 변경은
별도 유지보수 승인 없이 실행하지 않는다.

**TLS 선행 작업 완료:** 별도 승인 후 2026-09-10 중앙/3노드 serverTLSBootstrap과
노드별 kubelet 1회 재시작, 신원 검증 CSR3개 승인, CA/IP 검증 실제 HTTPS 지표9경로
HTTP200을 확인했다. 상세 근거와 설정 백업은 Obsidian 최신 기록을 따른다. 인증서 자동
승인과 기존 Metrics Server의 insecure 옵션 제거는 미실행이다. Prometheus ServiceAccount/
NetworkPolicy로 실제 수집하는 검증·자원/방화벽·관리자 Secret·영속성 게이트는 여전히 남는다.

1. kubelet 수집은 HTTPS와 CA 검증을 사용한다. 대상 serving 인증서가 일치하지 않으면
   수집 실패를 고친다. `insecureSkipVerify: true`로 우회하지 않는다.
2. Prometheus/Alertmanager 서비스는 ClusterIP만 사용한다. 공개 도메인·NodePort를 만들지 않는다.
   내부 HTTP 지표와 관리 UI는 NetworkPolicy로 제한한다. 클러스터 내부 전체 mTLS를
   구현했다고 주장하지 않는다. 이 두 서비스의 UI 점검은 승인된 port-forward로만 한다.
3. node-exporter는 호스트 네트워크 통계를 위해 hostNetwork와 읽기 전용 호스트 mount를
   사용한다. hostPID는 끈다. 일반 Pod NetworkPolicy만으로 9100 포트를 보호할 수 없으므로
   VM NIC·Windows/VM 방화벽에서 외부 접근이 차단되는지 설치 전에 확인한다.
4. 고정 upstream Operator의 ClusterRole에는 Secret/ConfigMap/StatefulSet 관리 권한이 있다.
   `namespaces` watch 제한이 RBAC의 클러스터 권한을 제거하지 않는다. 신규 CRD·webhook·
   ClusterRole 설치 권한과 영향을 승인할 때 함께 검토한다. 최소 읽기 전용 역할이라고 부르지 않는다.
5. 기본 receiver는 `disabled`다. 실제 전송 승인 전에는 `slack-values.yaml`을 App에 추가하지 않는다.
   Slack 활성화 시에도 `PawBridge.*` 경보만 허용하고 다른 경보는 버린다.
6. 실제 웹훅은 비공개 경로로 `monitoring/monitoring-slack-webhook` Secret의 `url` 키에 공급한다.
   값은 채팅·values·렌더 출력·Git에 쓰지 않는다. 기존 Vault 관리 흐름으로 연결할 경우
   전용 read 정책/role과 VSO 동기화는 별도 보안 승인 후 설정하고, 아직 존재한다고 가정하지 않는다.
   존재 여부와 키 이름만 확인한 뒤 `api_url_file` 경로 마운트를 검증한다.
   이 저장소에는 runtime Secret 또는 실제 웹훅을 포함한 예제 파일을 만들지 않는다.

## 오프라인 검증

### 최초 설치 순서

`failurePolicy: Fail`과 TLS 검증을 유지한 채 아래 순서로 **전체 App을 동기화**한다.
개별 리소스만 선택해 동기화하면 인증서 hook이 실행되지 않으므로 최초 설치와 복구에는 사용하지 않는다.

1. `PreSync`: 고정 chart의 기존 Job이 인증서 Secret을 생성한다. 기존 인증서는 재사용한다.
2. `Sync` wave 0: Operator와 webhook 설정을 적용하고 정상 기동을 기다린다.
3. `Sync` wave 1: 기존 admission-patch Job이 webhook에 신뢰할 CA를 등록한다.
4. `Sync` wave 2: CA 등록 성공 후 일반 Git 관리 리소스인 `PrometheusRule`을 적용한다.

chart 기본값의 admission-patch는 `PostSync`라서 인증서 신뢰가 없는 최초 설치에서
규칙 등록과 서로 기다릴 수 있다. `values.yaml`의 Argo hook annotation으로 이 Job만
`Sync`로 옮긴다. 기존 인증서 Job용 RBAC는 `PreSync/PostSync`로 유지한다.
Argo는 성공한 hook 정리를 동기화 완료 때 수행하므로 CA 등록 중 필요한 권한이 유지된다.
`BeforeHookCreation,HookSucceeded`로 다음 전체 동기화에서도 patch Job을 다시 실행한다.
CA 등록 실패 시 규칙 적용으로 넘어가지 않으며 실패 Job은 조사할 수 있도록 남긴다.

기존 관측 App의 전체 동기화도 같은 순서를 사용한다. 이번 변경은 앱 Pod template,
PVC, 이미지 버전, 데이터 보관 기간을 변경하지 않는다. 오프라인 순서 검사가
실제 빈 클러스터의 설치 성공을 증명하는 것은 아니다.

근거: [Argo hook 처리](https://argo-cd.readthedocs.io/en/stable/user-guide/helm/#helm-hooks),
[단계·순서·hook 정리](https://argo-cd.readthedocs.io/en/stable/user-guide/sync-waves/).

### 렌더와 검사

설치된 Helm, Python/PyYAML, promtool, amtool을 사용한다. 스크립트는 도구를 설치하지 않는다.
고정 chart를 사용하여 다음 두 조합 모두 렌더한다. `--include-crds --kube-version 1.36.0`
옵션을 포함하고 Kubernetes API에는 제출하지 않는다.

- 기본: `values.yaml`
- 발송 opt-in: `values.yaml` + `slack-values.yaml` (실제 웹훅 불필요)

```bash
kubectl kustomize gitops/observability > /tmp/render-resources.yaml
kubectl kustomize gitops/argocd/observability > /tmp/render-argocd.yaml
kubectl kustomize gitops/argocd/observability-slack > /tmp/render-argocd-slack.yaml
python3 gitops/observability/tests/test_install_order.py /tmp/render-base.yaml /tmp/render-resources.yaml /tmp/render-argocd.yaml /tmp/render-argocd-slack.yaml
python3 gitops/observability/tests/check_render.py /tmp/render-base.yaml --resources /tmp/render-resources.yaml
python3 gitops/observability/tests/check_render.py /tmp/render-slack.yaml --resources /tmp/render-resources.yaml --slack
python3 gitops/observability/tests/test_rules.py /path/to/promtool
python3 gitops/observability/tests/test_notifications.py /tmp/render-base.yaml /tmp/render-slack.yaml
python3 gitops/observability/tests/test_grafana_contract.py /tmp/render-base.yaml /tmp/render-resources.yaml
PYTHONDONTWRITEBYTECODE=1 python3 gitops/observability/tests/test_logs_contract.py /tmp/render-resources.yaml
PYTHONDONTWRITEBYTECODE=1 python3 gitops/observability/tests/test_logs_runtime.py /tmp/render-base.yaml
```

`check_render.py`에는 **오프라인 Helm 결과만** 전달한다. live Secret 출력은 금지한다.
13개 규칙·20시나리오로 발생/정상/복구와 완료 배치·과거 OOM 오탐을 확인한다.
`test_logs_runtime.py`는 사전 다운로드된 정확한 Loki/Alloy/Grafana 이미지와 기존 Python 이미지를
사용한다. 호스트 포트/외부 네트워크 없이 가짜 로그만 넣고 필터·Loki WAL 재시작·Grafana 조회를
검증한다. 생성한 고유 Docker 컨테이너/가짜 데이터 볼륨만 제거한다. 실제 Kubernetes API tail,
RBAC/NetworkPolicy 집행, 운영 부하, 72h 경과 후 삭제는 별도의 운영 검증으로 남는다.
이 검사는 upstream CRD의 전체 OpenAPI/CEL 검증이나 실제 지표 수집 검증을 대체하지 않는다.
promtool 구버전으로 실행한 결과는 사용한 버전을 기록하고 배포 후보 버전 검증과 구분한다.
amtool로 opt-in 설정·한국어 템플릿을 검증할 때는 임시 가짜 웹훅 파일을 사용하고
네트워크를 차단한다. 실제 전송 성공이라고 기록하지 않는다.
`test_notifications.py`는 사전 승인하여 내려받은 고정 Alertmanager 0.34.0 이미지만
`--pull=never --network=none`으로 실행한다. 기본/opt-in 설정과 발생/복구 메시지의
한국어·대상·안내·KST 표시를 검증하며 실제 연결 값은 필요하지 않다.

## 승인 후 설치·완료 조건

1. 컨텍스트·기존 소유 리소스 충돌·자원 여유·버전·CRD/RBAC·방화벽을 확인한다.
2. AppProject/Application을 등록하고 수동 sync한다. CRD는 ServerSideApply로 처리한다.
   기존 모니터링 release가 있으면 덮어쓰지 않는다. admission TLS를 끄거나 실패를 무시하지 않는다.
3. Operator/Prometheus/Alertmanager/KSM Ready, node-exporter 3개 Ready 및 PVC Bound의
   노드/경로를 확인한다. kubelet 인증 실패나 빈 target 목록을 정상으로 처리하지 않는다.
   Grafana Ready/로그인 성공·비로그인 차단·Prometheus datasource 조회·한국어 패널6개에
   실제 series 표시를 확인한다. Grafana 재시작 후 DB/대시보드 보존도 검증한다.
4. `up`, `kube_node_info`, `node_memory_MemAvailable_bytes`,
   `container_memory_working_set_bytes`, `container_cpu_usage_seconds_total`의 실제 series와
   시간 범위 조회를 확인한다. 일시적인 `up == 1`만으로 완료하지 않는다.
5. 실제 수집량·TSDB head series·샘플 수·메모리 피크와 rule/reload 오류를 확인한다.
   자원 압력이 생기면 설치 확대를 멈추고 롤백한다.
6. 승인된 수집기 재시작 후 이전 시각의 기록을 조회해 영속성을 확인한다.
   운영 워커 강제 종료·메모리 고갈 시험은 하지 않는다.
7. 비공개 Slack 연결과 전송 승인을 받은 뒤 opt-in을 적용한다. 무해한 테스트 경보의
   한국어 발생·복구를 사용자가 실제 수신해야 알림까지 완료다. Slack 자체 장애/같은 호스트
   전원 꺼짐 때 이 경로만으로 알리지 못하는 한계를 남긴다.
8. 서비스/배치의 비밀 없는 sentinel을 Loki·Grafana에서 조회한다. Alloy 재시작 gap/중복,
   Loki 재시작 전후 로그/PVC 보존, 실제 유입량·drop/WAL 오류 지표를 검증한다.
   72h 보관 만료/삭제는 해당 시간이 지난 실제 증거가 있어야 완료로 표시한다.

### Slack 발송을 선택적으로 활성화

기본 `gitops/argocd/observability`는 발송하지 않는다. Slack Secret의 `url` 키가
준비되고 실제 발송을 승인받았으면 아래 형제 overlay를 렌더·검토한 뒤 적용한다.
기본 Application을 복사하지 않고 `slack-values.yaml` 참조만 추가한다.

```bash
kubectl kustomize gitops/argocd/observability-slack
# 운영 적용 승인 후에만 실행
kubectl --context=pawbridge-vbox-k136 apply -k gitops/argocd/observability-slack
```

Application 변경 후 승인된 Git revision으로 전체 수동 동기화한다. 자동 동기화와 prune는
계속 꺼 둔다. Secret 마운트 추가로 Alertmanager 파드는 교체될 수 있으며 잠깐 알림 공백이
생길 수 있다. 기존 서비스·DB·Vault 파드는 재시작하지 않는다.
활성화하면 현재 발생 중인 `PawBridge.*` 경보도 전송 대상이다. 테스트만 전송한다고 가정하지 않는다.
의도적인 서비스 장애 대신 식별 가능한 테스트 경보를 사용하고 한국어 발생·복구 수신을 확인한다.
Slack API 요청 성공과 사용자가 채널에서 실제 확인한 결과를 구분해 기록한다.

발송 롤백은 기본 `gitops/argocd/observability`를 다시 적용한 후 전체 수동 동기화한다.
이 작업은 발송 설정만 되돌리며 Secret, PVC, 과거 지표·로그를 삭제하지 않는다.

### Grafana 비공개 접속

Windows에서 `http://192.168.57.11:30300`에 접속한다. 별도 터미널이나 Windows 예약 작업은
필요 없다. VM과 Grafana가 실행 중이어야 하며 호스트 절전 중에는 접속할 수 없다.

Grafana Service만 NodePort 30300을 사용한다. `externalTrafficPolicy: Local`은 외부 요청의
원래 IP를 보존한다. Grafana가 cp1에 고정되어 있으므로 워커 주소로는 접속하지 않는다.
`grafana-windows-access`는 PC의 host-only IP `192.168.57.1/32`에서 Grafana의 TCP3000으로
오는 트래픽을 허용한다. 기존 monitoring 내부 통신 허용은 유지한다.

이 정책은 노드 자체 트래픽까지 격리하는 방화벽이 아니다. NodePort의 노드 인터페이스
설정을 전역 변경하지 않으며, NAT와 host-only 구성을 유지한다. 브리지 NIC나 공유기 포트
전달을 추가하지 않는다. HTTP는 암호화되지 않으므로 외부 네트워크로 확장할 때는 TLS를
먼저 설계한다. PC IP나 Grafana 배치 노드가 바뀌면 접근 정책과 주소를 함께 검토한다.

초기 관리자 이름은 `pawbridge-admin`이며
비밀번호는 운영자가 Vault의 `secret/pawbridge/dev/observability/grafana`에서 확인한다.
비밀번호를 채팅이나 명령줄에 붙이지 않는다. 이미 변경한 Grafana 암호는 Vault 초기값과
다를 수 있다. 비로그인 차단·한국어 대시보드·Prometheus 지표·Loki 로그 조회를 확인한다.

반영 시 PC의 로그인 화면 및 health 성공, 허용하지 않은 출발지의 접속 실패를 모두 확인한다.
기존 Windows SSH 예약 작업은 이 검증 뒤에만 제거한다. 실패하면 Service를 ClusterIP로
되돌리고 `nodePort`와 `externalTrafficPolicy`를 제거한다. 새 접근 정책만 삭제하며 기존
내부 정책과 PVC는 유지한다. Git 원복 시 prune가 꺼져 있으므로 새 정책의 별도 제거도 확인한다.

## 롤백 경계

- 발송만 중단하려면 opt-in values 참조를 제거한 Git 변경을 승인 후 수동 sync한다.
- 자원 압력 시 수동 sync App을 정지 상태로 유지하고, 승인 후 이 release의 Prometheus/
  Alertmanager replicas와 Grafana/Operator/KSM/Loki/Alloy를 0으로 내려 영향부터 격리한다. node-exporter는
  정확한 이 release DaemonSet만 별도 제거 승인 대상으로 잡는다.
- PVC/PV·local-path 경로·전역 CRD·공유 리소스는 삭제하지 않는다. Helm uninstall이나
  App cascade 삭제를 무조건 실행하지 않는다. 보관 데이터 복구는 원래 cp1 볼륨을 확인한 뒤 진행한다.
- MySQL/ES/Kafka/Vault 데이터, 기존 앱 replica, DNS와 터널은 이 롤백 대상이 아니다.

공식 근거: [LTS](https://prometheus.io/docs/introduction/release-cycle/),
[3.13.3 변경 내역](https://github.com/prometheus/prometheus/releases/tag/v3.13.3),
[영속 저장](https://prometheus.io/docs/prometheus/latest/storage/),
[경보 테스트](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/),
[Slack 설정](https://prometheus.io/docs/alerting/latest/configuration/).

로그 구성 근거: [Loki patch](https://github.com/grafana/loki/releases/tag/v3.6.16),
[Alloy patch](https://github.com/grafana/alloy/releases/tag/v1.18.1),
[API 수집 한계](https://grafana.com/docs/alloy/v1.18/reference/components/loki/loki.source.kubernetes/),
[filesystem 한계](https://grafana.com/docs/loki/latest/operations/storage/filesystem/),
[로그 보관](https://grafana.com/docs/loki/latest/operations/storage/retention/).
