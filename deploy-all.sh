ENV=${1:-local}

echo "========================================================"
echo "  PawBridge K8s Deployment Script (Linux/VM)"
echo "  Environment: ${ENV}"
echo "========================================================"

echo ""
echo "[1/2] Applying Secrets..."
kubectl create namespace pawbridge --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f secrets/

echo ""
echo "[2/2] Deploying Services via Helm..."

deploy_service() {
    local service_name=$1
    echo "  - Deploying ${service_name}..."
    helm upgrade --install ${service_name} ./charts/${service_name} \
        -n pawbridge --create-namespace \
        -f environments/${ENV}/values/${service_name}.yaml
}

deploy_service "user-service"
deploy_service "animal-service"
deploy_service "community-service"
deploy_service "store-service"
deploy_service "payment-service"
deploy_service "api-gateway"

echo ""
echo "========================================================"
echo "  All Services Deployed Successfully!"
echo "========================================================"
echo "  Run 'kubectl get pods -n pawbridge' to check status."
echo "========================================================"
