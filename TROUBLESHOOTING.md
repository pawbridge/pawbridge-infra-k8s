# Pawbridge 인프라 트러블슈팅 기록

---

## [1] Grafana DS_PROMETHEUS 데이터소스 Not Found

**증상**
- Grafana 대시보드 임포트 시 `Datasource ${DS_PROMETHEUS} was not found` 오류
- 드롭다운에서 Prometheus 선택해도 적용 안 됨

**원인**
- Grafana 12 버전부터 App Platform API(`/apis/dashboard.grafana.app/v1beta1/`) 방식으로 변경
- 새 API가 `${DS_PROMETHEUS}` 변수를 임포트 시 치환하지 않음

**해결**
- `infra/monitoring/values.yaml`에서 Grafana 버전을 11.4.0으로 고정
```yaml
grafana:
  image:
    tag: "11.4.0"
```

---

## [2] Grafana CrashLoopBackOff (vagrant halt/up 후)

**증상**
- `vagrant halt` 후 `vagrant up` 하면 Grafana 파드가 CrashLoopBackOff

**원인**
- `initChownData` 가 PVC 마운트 경로 권한을 변경하려다 실패

**해결**
- `infra/monitoring/values.yaml`에서 비활성화
```yaml
grafana:
  initChownData:
    enabled: false
```

---

## [3] Elasticsearch / Redis 반복 종료 (메모리 부족)

**증상**
- worker2 메모리 사용률 92% → ES, Redis 파드 반복 종료

**원인**
- worker VM 메모리(8192MB)가 전체 서비스 운영에 부족

**해결**
- `vagrant/Vagrantfile` worker 메모리 8192 → 10240MB 증설
- `infra/database/elasticsearch-values.yaml` JVM 힙 1g → 512m 축소
```yaml
esJavaOpts: "-Xmx512m -Xms512m"
resources:
  requests:
    memory: "1Gi"
  limits:
    memory: "1.5Gi"
```

---

## [4] Elasticsearch CrashLoopBackOff (PostStartHookError)

**증상**
- ES 파드 재시작 시 `PostStartHookError` 발생 → CrashLoopBackOff 무한 루프
- 최초 기동 시에는 정상, 재시작 후부터 실패

**원인 (복합)**
1. `postStart` hook이 매 재시작마다 Nori 플러그인 설치 시도
   - PVC에 플러그인이 이미 존재하면 `already exists` 에러 → hook 실패
2. VirtualBox NAT 장시간 운영 시 네트워크 경로 소실 (아래 [11] 참고)
   - NAT가 죽은 상태에서 ES 재시작 → 외부 DNS 조회 불가 → Nori 다운로드 실패 → hook 실패

**해결**
- `infra/database/elasticsearch-values.yaml` postStart hook을 멱등성 있게 수정
  → 이미 설치된 경우 다운로드 시도 자체를 skip하여 NAT 상태와 무관하게 안정적으로 기동
```yaml
lifecycle:
  postStart:
    exec:
      command:
        - bash
        - -c
        - |
          if ! bin/elasticsearch-plugin list | grep -q analysis-nori; then
            bin/elasticsearch-plugin install --batch analysis-nori
          fi
```

---

## [5] Spring Boot 3.x Zipkin 트레이스 미수집

**증상**
- Zipkin UI에서 트레이스가 전혀 보이지 않음

**원인**
- Spring Boot 3.x에서 `SPRING_ZIPKIN_BASE_URL` 환경변수 deprecated
- 새로운 환경변수명 및 경로 변경됨

**해결**
- 전체 서비스 values.yaml에서 환경변수 교체 (18개 파일)
```yaml
# 변경 전
SPRING_ZIPKIN_BASE_URL: http://zipkin.tracing.svc.cluster.local:9411

# 변경 후
MANAGEMENT_ZIPKIN_TRACING_ENDPOINT: http://zipkin.tracing.svc.cluster.local:9411/api/v2/spans
```

---

## [6] Zipkin 외부 접근 불가

**증상**
- 클러스터 외부(호스트 PC)에서 Zipkin UI 접속 불가

**원인**
- Zipkin Service 타입이 ClusterIP로 설정되어 있어 외부 노출 안 됨

**해결**
- `infra/tracing/zipkin.yaml` Service 타입 ClusterIP → NodePort(30941) 변경
```yaml
spec:
  type: NodePort
  ports:
  - port: 9411
    targetPort: 9411
    nodePort: 30941
```

---

## [7] Grafana 대시보드 application 변수 N/A

**증상**
- JVM / HTTP Statistics 대시보드에서 application 드롭다운이 N/A

**원인**
- 각 서비스 application 설정에 `management.metrics.tags.application` 누락

**해결**
- 전체 서비스 values.yaml에 태그 추가
```yaml
MANAGEMENT_METRICS_TAGS_APPLICATION: animal-service  # 서비스별로 지정
```

---

## [8] 로그 패턴에 레벨 누락

**증상**
- Loki + Grafana에서 로그 조회 시 레벨(INFO/WARN/ERROR) 구분 불가

**원인**
- 각 서비스 logback 패턴에 `%level` 필드 누락

**해결**
- 전체 서비스 logback 설정에 로그 레벨 추가

---

## [9] k6 상품 목록 0% 성공률

**증상**
- k6 부하테스트에서 상품 목록 API 요청이 모두 실패

**원인**
- API Gateway 라우팅 경로가 `/api/products/**` 인데 스크립트에서 `/api/v1/products/**` 로 요청

**해결**
- `load-test.js` 엔드포인트 수정
```javascript
// 변경 전
http.get(`${BASE_URL}/api/v1/products?page=0&size=20`)
// 변경 후
http.get(`${BASE_URL}/api/products?page=0&size=20`)
```

---

## [10] k6 Prometheus 연동 (remote write)

**증상**
- k6 결과가 Grafana에서 보이지 않음

**원인**
- Prometheus에 remote write receiver가 비활성화 상태

**해결**
- `infra/monitoring/values.yaml`에 설정 추가
```yaml
prometheus:
  service:
    type: NodePort
    nodePort: 30909
  prometheusSpec:
    enableRemoteWriteReceiver: true
```
- k6 실행 시 remote write 옵션 추가
```bash
k6 run --out experimental-prometheus-rw load-test.js
```

---

## [11] VirtualBox NAT 장시간 운영 시 인터넷 단절

**증상**
- VM을 장시간(수 시간 이상) 켜놓으면 외부 인터넷 접근 불가
- `ping 8.8.8.8` → `Destination Net Unreachable`
- `git pull`, `helm upgrade`, 외부 이미지 다운로드 등 모두 실패
- `vagrant reload` 후 정상 복구됨

**원인**
- VirtualBox NAT 엔진의 알려진 장시간 운영 버그
- 호스트 PC 절전과 무관하게 VirtualBox NAT 라우팅이 내부적으로 소실됨
- 로컬 개발 환경(Vagrant + VirtualBox)의 구조적 한계

**해결 (임시)**
- 호스트 PC에서 `vagrant reload`로 VM 재시작
```bash
vagrant reload k8s-master k8s-worker1 k8s-worker2
```

**근본 해결 (선택)**
- Vagrantfile 네트워크를 NAT → Bridged로 전환
  - 쿠버네티스 노드 간 통신(192.168.56.x)은 host-only 네트워크를 사용하므로 영향 없음
  - 인터넷용 어댑터만 교체하는 것으로 `vagrant reload`만으로 적용 가능

**참고**
- 이 문제로 인해 [4]의 ES CrashLoopBackOff가 연쇄적으로 발생할 수 있음
- [4]의 멱등성 패치 적용 후에는 NAT가 죽어도 이미 설치된 서비스는 계속 정상 운영됨

---

## [12] K6 부하테스트 p95 1.4s 병목 (DB 커넥션 풀 고갈 + JDBC 배치 미적용)

**증상**
- K6 100VUs 부하테스트 시 동물 검색 API p95 응답 시간 1.4초 기록
- 동시 접속이 몰리면 응답이 급격히 느려지는 패턴

**원인 (복합)**
1. **HikariCP 커넥션 풀 고갈 (Connection Pool Starvation)**
   - 스프링 부트 기본값 HikariCP `maximum-pool-size: 10`
   - 100VUs가 동시에 DB 요청 시 90개의 톰캣 스레드가 Hikari Wait Queue에서 대기(Block) → 응답 지연
2. **JDBC 배치 모드 미적용**
   - MySQL 드라이버의 `rewriteBatchedStatements` 옵션이 꺼져 있어, JPA가 모아놓은 쿼리 배열을 FOR문으로 1건씩 개별 전송

**해결**
- `environments/dev/values/animal-service.yaml` 및 `charts/animal-service/values.yaml` 수정
```yaml
# HikariCP 커넥션 풀 스케일업 (기본 10 → 30)
SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE: "30"

# JDBC 배치 모드 활성화 (URL 파라미터 추가)
SPRING_DATASOURCE_URL: jdbc:mysql://...?rewriteBatchedStatements=true
```

**참고**
- 풀 사이즈 30 × HPA maxReplicas 3 = 최대 90 커넥션 사용 가능
- MySQL 기본 `max_connections`(151)을 초과하지 않도록 전체 서비스 풀 합산 모니터링 필요
- 백엔드 코드 레벨의 N+1 쿼리 최적화(JOIN FETCH)와 함께 적용하여 시너지 효과 극대화

---

## [13] 분산 락 도입에 따른 K8s Redis 환경 변수 맵핑 보완

**증상**
- 앱 내부(Spring Boot)에서 Redisson 클라이언트가 Redis 연결 정보를 파싱할 때 환경이나 라이브러리 버전에 따라 인프라 URL 인식에 실패할 여지 존재

**원인**
- 12-Factor App 원칙의 유연한 바인딩(Relaxed Binding)에 따라 기존에는 K8s가 `SPRING_DATA_REDIS_URL` 파라미터 1개만 주입하고 있었음
- 일부 특정 로직이나 하위 호환이 필요한 컴포넌트에서는 명시적인 Host 및 Port 분리 파라미터를 요구함

**해결**
- `environments/dev/values/animal-service.yaml` 수정
```yaml
# 기존
SPRING_DATA_REDIS_URL: redis://redis-master.databases.svc.cluster.local:6379

# 추가 보완 (명시적 Host/Port 선언)
SPRING_DATA_REDIS_HOST: redis-master.databases.svc.cluster.local
SPRING_DATA_REDIS_PORT: "6379"
```
- 추가로 백엔드 코드의 `application.yml` 내부에 해당 환경변수를 참조하는 Placeholder를 선언하여 인프라-백엔드 간 아키텍처 명세 일치화

---

## [14] Debezium Connect CrashLoopBackOff (PVC 초기화 후 connect-offsets 토픽 정책 문제)

**증상**
- PVC 마이그레이션 후 Debezium Connect 파드가 CrashLoopBackOff
- 커넥터 등록 시도 불가

**원인**
- PVC 초기화로 Kafka 데이터가 날아가면서 `connect-offsets` 토픽이 재생성됨
- 재생성된 토픽의 `cleanup.policy`가 `delete`로 설정되어 있어 Debezium이 오프셋 데이터를 정상적으로 유지하지 못함
- Debezium Connect는 `connect-offsets` 토픽의 `cleanup.policy`가 반드시 `compact`여야 정상 동작

**해결**
- Kafka 파드 내부에서 토픽 정책 변경
```bash
kubectl exec -it <kafka-pod> -n kafka -- bash
/opt/kafka/bin/kafka-configs.sh \
  --bootstrap-server localhost:9092 \
  --entity-type topics \
  --entity-name connect-offsets \
  --alter --add-config cleanup.policy=compact
```
- Debezium Connect 파드 재시작 후 커넥터 재등록

**참고**
- 커넥터 등록 절차: `infrastructure/CONNECTOR_REGISTER.md` 참고
- PVC 초기화 발생 시 ES `animals_template` 재등록도 함께 필요 (아래 [15] 참고)

---

## [15] Elasticsearch animals_template 미등록으로 인한 동적 매핑 문제

**증상**
- Alias Swap 방식 배치 실행 후 동물 검색 API 결과 0건 반환
- `curl http://localhost:9200/animals/_mapping`에서 `status` 필드가 `keyword` 대신 `text` 타입으로 조회됨

**원인**
- `ElasticsearchIndexService.reindexAllAnimals()`의 `indexOps.create()`가 매핑 없이 빈 인덱스 생성
- ES 인덱스 템플릿(`animals_template`)이 등록되어 있지 않아 동적 매핑으로 fallback
- `status`, `species`, `gender` 등 Enum 문자열 필드가 `text` 타입으로 추론됨
- term query가 `text` 필드의 분석된 소문자 토큰과 불일치 → 0건 반환
- ES는 템플릿 없이도 `indexOps.create()`를 에러 없이 통과시키므로 로그에 오류가 남지 않음

**해결**
- ES에 `animals_template` 인덱스 템플릿 등록 (`animals_*` 패턴 자동 적용)
```bash
# 마스터 노드에서 실행 (ES 포트포워드 필요)
kubectl port-forward svc/elasticsearch-master 9200:9200 -n databases &
curl -X PUT http://localhost:9200/_index_template/animals_template \
  -H "Content-Type: application/json" \
  -d '{"index_patterns":["animals_*"],"template":{"settings":{"index.default_pipeline":"animal-date-converter", ...},"mappings":{...}}}'
```
- 전체 템플릿 JSON은 `infrastructure/elasticsearch/create-index.json` 기반 (snake_case 필드명)
- 등록 확인: `curl http://localhost:9200/_index_template/animals_template`
- 템플릿 등록 후 배치 재실행: `POST /api/v1/batch/apms/sync`

**참고**
- VM 재생성, PVC 초기화 등 ES 데이터가 초기화되는 상황에서 반드시 템플릿 먼저 등록 후 배치 실행
- `infrastructure/elasticsearch/animals-index-mapping.json`은 camelCase 필드명으로 실제 데이터와 불일치 → `create-index.json` 사용할 것

---

## [16] HPA 스케일아웃 중 배치 실행 시 DB 커넥션 경합

**증상**
- 배치 실행 중 Step 1 DB Write 소요시간이 평소 ~4s에서 ~7s로 급증
- 로그에 새 파드의 Spring Boot 기동 로그가 배치 로그 중간에 섞여 출력
- 전체 배치 시간이 46~57s → 1m3s로 증가

**원인**
- HPA가 스케일아웃한 직후 배치를 실행하면 새로 뜬 파드들이 HikariCP 커넥션 풀을 점유
- 배치 파드의 DB 커넥션 대기 발생 → Step 1 Write 지연

**해결**
- 배치 성능 측정 시 파드가 안정화된 후 실행
- HPA 스케일아웃 직후에는 측정값을 신뢰하지 않을 것

**참고**
- 배치는 스케줄러로 정해진 시간에만 실행되므로 운영 중 상시 발생하는 문제는 아님
- 정상 환경 기준 배치 소요 시간: **46~57s**
