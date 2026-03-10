#!/bin/bash
# PawBridge 인프라 설치 스크립트
# 사용법: ./install-infra.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "  PawBridge 인프라 설치"
echo "========================================"

# 사전 검사: 필수 파일 및 디렉토리 확인
echo "[0/10] 필수 파일 및 설정 확인..."

# 실패 시 자동 진단 덤프 함수
dump_diag() {
  local ns="${1:-databases}"
  echo "==== DIAG DUMP (ns=${ns}) ===="
  kubectl get nodes -o wide || true
  kubectl get pods -A -o wide || true
  kubectl get events -A --sort-by=.lastTimestamp | tail -n 200 || true

  if kubectl get ns "$ns" >/dev/null 2>&1; then
    kubectl get all -n "$ns" -o wide || true
    kubectl get pvc -n "$ns" -o wide || true
    kubectl get pv -o wide | grep -E "$ns|data-mysql-0|pvc-" || true
    kubectl describe pod -n "$ns" mysql-0 2>/dev/null | sed -n '1,260p' || true
    kubectl logs -n "$ns" mysql-0 -c mysql --tail=300 2>/dev/null || true
    kubectl logs -n "$ns" mysql-0 -c mysql --previous --tail=300 2>/dev/null || true
  fi
  echo "==== END DIAG DUMP ===="
}

REQUIRED_FILES=(
  "database/mysql-values.yaml"
  "database/redis-values.yaml"
  "database/elasticsearch-values.yaml"
  "kafka/strimzi-values.yaml"
  "kafka/kafka-cluster.yaml"
  "kafka/debezium-connect"
  "monitoring/values.yaml"
  "dashboard/dashboard.yaml"
  "tracing/zipkin.yaml"
  "logging/loki-values.yaml"
  "logging/promtail-values.yaml"
)

for file in "${REQUIRED_FILES[@]}"; do
  if [ ! -e "${SCRIPT_DIR}/${file}" ]; then
    echo "❌ 오류: 필수 파일/디렉토리가 없습니다: ${SCRIPT_DIR}/${file}"
    exit 1
  fi
done
echo "✅ 필수 파일 확인 완료."

# 사전 검사: vm.max_map_count (Elasticsearch 필수)
VM_MAX_MAP_COUNT=$(sysctl -n vm.max_map_count)
if [ "$VM_MAX_MAP_COUNT" -lt 262144 ]; then
  echo "❌ 오류: vm.max_map_count가 너무 낮습니다 ($VM_MAX_MAP_COUNT). 최소 262144 필요."
  echo "   해결법: 'sudo sysctl -w vm.max_map_count=262144' 실행 (모든 노드)"
  exit 1
fi
echo "✅ vm.max_map_count 확인 완료."

# 사전 검사: NodePort 충돌 확인 (30080, 30443)
if ss -tuln | grep -qE ":30080|:30443"; then
  echo "⚠️  경고: 포트 30080 또는 30443이 이미 사용 중일 수 있습니다."
fi

# Local Path Provisioner 확인 (Vagrantfile에서 이미 설치됨)
echo ""
echo "[1/10] Local Path Provisioner 확인..."
if kubectl get storageclass local-path &>/dev/null; then
  echo "Local Path Provisioner 이미 설치됨"
else
  echo "Local Path Provisioner 설치 중..."
  kubectl apply -f https://raw.githubusercontent.com/rancher/local-path-provisioner/v0.0.34/deploy/local-path-storage.yaml
  kubectl wait --for=condition=available --timeout=120s deployment/local-path-provisioner -n local-path-storage || true
  kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
fi

# Helm repo 추가
echo ""
echo "[2/10] Helm 레포지토리 추가..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts || true
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
helm repo add bitnami https://charts.bitnami.com/bitnami || true
helm repo add strimzi https://strimzi.io/charts/ || true
helm repo add elastic https://helm.elastic.co || true
helm repo add grafana https://grafana.github.io/helm-charts || true
helm repo update

# Ingress Controller 설치
echo ""
echo "[3/10] Ingress Controller 설치..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.type=NodePort \
  --set controller.service.nodePorts.http=30080 \
  --set controller.service.nodePorts.https=30443 \
  --set controller.extraArgs.enable-ssl-passthrough=true \
  --wait

# MySQL 설치 (Bitnami Helm - startup probe 시간 충분히 설정됨)
echo ""
echo "[4/10] MySQL 설치..."
# databases 네임스페이스 및 시크릿 사전 주입 (Security Fix)
kubectl create namespace databases --dry-run=client -o yaml | kubectl apply -f -
# 주의: 모든 secrets를 apply하면 pawbridge 등 다른 네임스페이스가 없어서 에러 발생함.
# 인프라 단계에서는 DB용 시크릿만 적용.
kubectl apply -f "${SCRIPT_DIR}/../secrets/mysql-auth-secrets.yaml"

# (옵션) 이전 실패 흔적 정리: PVC 남아 있으면 비번/초기화가 안 바뀜
# (수정) 설치 직전 무지성 PVC 삭제 제거 (StatefulSet 꼬임 방지)
# kubectl delete pvc -n databases data-mysql-0 --ignore-not-found=true

helm upgrade --install mysql bitnami/mysql \
  --namespace databases \
  -f "${SCRIPT_DIR}/database/mysql-values.yaml" \
  --wait --timeout 30m || {
    echo "❌ MySQL 설치가 Ready 상태로 완료되지 않았습니다. 아래 로그/이벤트를 확인하세요."
    dump_diag "databases"
    exit 1
  }

# ✅ helm 성공 후에도 실제로 Ready인지 한 번 더 강제 확인
echo "MySQL StatefulSet & Pod Ready 검증..."
kubectl rollout status statefulset/mysql -n databases --timeout=30m
kubectl wait --for=condition=Ready pod -n databases -l app.kubernetes.io/instance=mysql --timeout=30m

# MySQL 접속 테스트 (Client Pod 사용)
echo "🔍 MySQL 접속 테스트 중..."
MYSQL_ROOT_PASSWORD=$(kubectl get secret -n databases mysql-auth -o jsonpath='{.data.mysql-root-password}' | base64 -d)
kubectl run mysql-client -n databases --rm -i --tty \
  --image=mysql:8.0 \
  --env MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD}" \
  --command -- bash -lc 'mysql -h mysql.databases.svc.cluster.local -uroot -p"$MYSQL_ROOT_PASSWORD" -e "SHOW DATABASES;"' || echo "⚠️ MySQL 접속 테스트 실패 (로그 확인 필요)"

# Redis 설치 (Bitnami Helm)
echo ""
echo "[5/10] Redis 설치..."
helm upgrade --install redis bitnami/redis \
  --namespace databases \
  -f "${SCRIPT_DIR}/database/redis-values.yaml" \
  --wait --timeout 5m

# Elasticsearch 설치 (Single Node)
echo ""
echo "[5.5/10] Elasticsearch 설치..."
helm upgrade --install elasticsearch elastic/elasticsearch \
  --namespace databases \
  --version 7.17.3 \
  -f "${SCRIPT_DIR}/database/elasticsearch-values.yaml" \
  --wait --timeout 10m

# Strimzi Kafka Operator 설치
echo ""
echo "[6/10] Strimzi Kafka Operator 설치..."
kubectl create namespace kafka --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install strimzi strimzi/strimzi-kafka-operator \
  --namespace kafka \
  --version 0.39.0 \
  -f "${SCRIPT_DIR}/kafka/strimzi-values.yaml" \
  --wait --timeout 5m

# Kafka Cluster 생성
echo ""
echo "[7/10] Kafka Cluster 생성..."
kubectl apply -f "${SCRIPT_DIR}/kafka/kafka-cluster.yaml"
echo "Kafka 클러스터 시작 대기 중 (약 3분)..."
# Kafka Ready 대기 (최대 5분)
for i in {1..30}; do
  READY=$(kubectl get kafka pawbridge -n kafka -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
  if [ "$READY" = "True" ]; then
    echo "Kafka 클러스터 Ready!"
    break
  fi
  echo "Kafka 대기 중... ($i/30)"
  sleep 10
done

# Debezium Kafka Connect 배포 (Kafka 브로커가 준비된 후)
echo ""
echo "[7.5/10] Debezium Kafka Connect 배포..."
helm upgrade --install debezium-connect "${SCRIPT_DIR}/kafka/debezium-connect" \
  --namespace kafka \
  --wait --timeout 5m
echo "Debezium Connect 시작 대기 중..."
sleep 30

# Metrics Server 설치 (HPA 및 kubectl top 필수)
echo ""
echo "[7.8/10] Metrics Server 설치 (공식 Helm)..."
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ || true
helm repo update
helm upgrade --install metrics-server metrics-server/metrics-server \
  --namespace kube-system \
  --set args={--kubelet-insecure-tls} \
  --wait

# Prometheus + Grafana 설치
echo ""
echo "[8/10] Prometheus + Grafana 설치..."
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  -f "${SCRIPT_DIR}/monitoring/values.yaml" \
  --wait --timeout 10m

# Kubernetes Dashboard 설치
echo ""
echo "[9/10] Kubernetes Dashboard 설치..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
kubectl apply -f "${SCRIPT_DIR}/dashboard/dashboard.yaml"

# Loki + Promtail 설치
echo ""
echo "[9.5/10] Loki + Promtail 설치 (로그 수집)..."
kubectl create namespace logging --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install loki grafana/loki \
  --namespace logging \
  -f "${SCRIPT_DIR}/logging/loki-values.yaml" \
  --wait --timeout 5m
helm upgrade --install promtail grafana/promtail \
  --namespace logging \
  -f "${SCRIPT_DIR}/logging/promtail-values.yaml" \
  --wait --timeout 5m

# Zipkin 설치
echo ""
echo "[10/10] Zipkin 설치..."
# Tracing 네임스페이스 명시적 생성 (안전장치)
kubectl create namespace tracing --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "${SCRIPT_DIR}/tracing/zipkin.yaml"

# 설치 완료 대기
echo ""
echo "설치 완료 대기 중..."
sleep 30

# 상태 확인
echo ""
echo "========================================"
echo "  설치 완료!"
echo "========================================"
echo ""
echo "[데이터베이스 접속 정보]"
echo "  MySQL:  mysql.databases.svc.cluster.local:3306 (root / root123)"
echo "  Redis:  redis-master.databases.svc.cluster.local:6379"
echo "  Kafka:  pawbridge-kafka-bootstrap.kafka.svc.cluster.local:9092"
echo ""
echo "[모니터링 접속 정보]"
echo "  Grafana:    kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80"
echo "  Dashboard:  kubectl port-forward -n kubernetes-dashboard svc/kubernetes-dashboard 8443:443"
echo "  Zipkin:     kubectl port-forward -n tracing svc/zipkin 9411:9411"
echo ""

# Pod 상태 확인 (grep 실패해도 스크립트 중단되지 않도록 || true 추가)
echo "[현재 Pod 상태]"
kubectl get pods -A | grep -E "mysql|redis|kafka|strimzi|monitoring|tracing|kubernetes-dashboard|ingress-nginx|local-path|logging" || true
