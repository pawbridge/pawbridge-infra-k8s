#!/bin/bash
# PawBridge 인프라 설치 스크립트
# 사용법: ./install-infra.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================"
echo "  PawBridge 인프라 설치"
echo "========================================"

# Helm repo 추가
echo ""
echo "[1/9] Helm 레포지토리 추가..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts || true
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
helm repo add bitnami https://charts.bitnami.com/bitnami || true
helm repo add strimzi https://strimzi.io/charts/ || true
helm repo update

# Ingress Controller 설치
echo ""
echo "[2/9] Ingress Controller 설치..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.type=NodePort \
  --set controller.service.nodePorts.http=30080 \
  --set controller.service.nodePorts.https=30443 \
  --wait

# MySQL 설치
echo ""
echo "[3/9] MySQL 설치..."
helm upgrade --install mysql bitnami/mysql \
  --namespace default \
  -f "${SCRIPT_DIR}/database/mysql-values.yaml" \
  --wait --timeout 5m

# Redis 설치
echo ""
echo "[4/9] Redis 설치..."
helm upgrade --install redis bitnami/redis \
  --namespace default \
  -f "${SCRIPT_DIR}/database/redis-values.yaml" \
  --wait --timeout 3m

# Strimzi Kafka Operator 설치
echo ""
echo "[5/9] Strimzi Kafka Operator 설치..."
kubectl create namespace kafka --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install strimzi strimzi/strimzi-kafka-operator \
  --namespace kafka \
  -f "${SCRIPT_DIR}/kafka/strimzi-values.yaml" \
  --wait --timeout 5m

# Kafka Cluster 생성
echo ""
echo "[6/9] Kafka Cluster 생성..."
kubectl apply -f "${SCRIPT_DIR}/kafka/kafka-cluster.yaml"
echo "Kafka 클러스터 시작 대기 중 (약 2분)..."
sleep 60

# Prometheus + Grafana 설치
echo ""
echo "[7/9] Prometheus + Grafana 설치..."
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  -f "${SCRIPT_DIR}/monitoring/values.yaml" \
  --wait --timeout 10m

# Kubernetes Dashboard 설치
echo ""
echo "[8/9] Kubernetes Dashboard 설치..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
kubectl apply -f "${SCRIPT_DIR}/dashboard/dashboard.yaml"

# Zipkin 설치
echo ""
echo "[9/9] Zipkin 설치..."
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
echo "  MySQL:  mysql.default.svc.cluster.local:3306 (root / root123)"
echo "  Redis:  redis-master.default.svc.cluster.local:6379"
echo "  Kafka:  pawbridge-kafka-bootstrap.kafka.svc.cluster.local:9092"
echo ""
echo "[모니터링 접속 정보]"
echo "  Grafana:    kubectl port-forward -n monitoring svc/prometheus-grafana 3000:80"
echo "  Dashboard:  kubectl port-forward -n kubernetes-dashboard svc/kubernetes-dashboard 8443:443"
echo "  Zipkin:     kubectl port-forward -n tracing svc/zipkin 9411:9411"
echo ""

# Pod 상태 확인
echo "[현재 Pod 상태]"
kubectl get pods -A | grep -E "mysql|redis|kafka|strimzi|monitoring|tracing|kubernetes-dashboard|ingress-nginx"
