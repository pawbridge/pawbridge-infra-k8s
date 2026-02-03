@echo off
setlocal

echo ========================================================
echo   PawBridge K8s Deployment Script
echo ========================================================

echo.
echo [1/2] Applying Secrets...
kubectl apply -f secrets/
if errorlevel 1 (
    echo [Error] Failed to apply secrets!
    exit /b %errorlevel%
)

echo.
echo [2/2] Deploying Services via Helm...

echo   - Deploying user-service...
helm upgrade --install user-service ./charts/user-service -n pawbridge --create-namespace
if errorlevel 1 exit /b %errorlevel%

echo   - Deploying animal-service...
helm upgrade --install animal-service ./charts/animal-service -n pawbridge --create-namespace
if errorlevel 1 exit /b %errorlevel%

echo   - Deploying community-service...
helm upgrade --install community-service ./charts/community-service -n pawbridge --create-namespace
if errorlevel 1 exit /b %errorlevel%

echo   - Deploying store-service...
helm upgrade --install store-service ./charts/store-service -n pawbridge --create-namespace
if errorlevel 1 exit /b %errorlevel%

echo   - Deploying payment-service...
helm upgrade --install payment-service ./charts/payment-service -n pawbridge --create-namespace
if errorlevel 1 exit /b %errorlevel%

echo   - Deploying api-gateway...
helm upgrade --install api-gateway ./charts/api-gateway -n pawbridge --create-namespace
if errorlevel 1 exit /b %errorlevel%

echo.
echo ========================================================
echo   All Services Deployed Successfully!
echo ========================================================
echo   Run 'kubectl get pods -n pawbridge' to check status.
echo ========================================================
pause
