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
echo "[1/5] Helm 레포지토리 추가..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts || true
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx || true
helm repo update

# Ingress Controller 설치
echo ""
echo "[2/5] Ingress Controller 설치..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  --namespace ingress-nginx \
  --create-namespace \
  --set controller.service.type=NodePort \
  --set controller.service.nodePorts.http=30080 \
  --set controller.service.nodePorts.https=30443 \
  --wait

# Prometheus + Grafana 설치
echo ""
echo "[3/5] Prometheus + Grafana 설치..."
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  -f "${SCRIPT_DIR}/monitoring/values.yaml" \
  --wait --timeout 10m

# Kubernetes Dashboard 설치
echo ""
echo "[4/5] Kubernetes Dashboard 설치..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
kubectl apply -f "${SCRIPT_DIR}/dashboard/dashboard.yaml"

# Zipkin 설치
echo ""
echo "[5/5] Zipkin 설치..."
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
echo "[접속 정보]"
echo "  Grafana:    http://grafana.local (admin / admin123)"
echo "  Dashboard:  https://dashboard.local"
echo "  Zipkin:     http://zipkin.local"
echo ""
echo "[hosts 파일 설정 필요]"
echo "  Windows: C:\\Windows\\System32\\drivers\\etc\\hosts"
echo "  192.168.56.70 grafana.local dashboard.local zipkin.local"
echo ""
echo "[Dashboard 토큰 생성]"
echo "  kubectl -n kubernetes-dashboard create token admin-user"
echo ""

# Pod 상태 확인
echo "[현재 Pod 상태]"
kubectl get pods -A | grep -E "monitoring|tracing|kubernetes-dashboard|ingress-nginx"
