@echo off
setlocal
set DOCKER_ID=dorosiya
set VERSION=confluent-7.5.0

echo ========================================================
echo   Debezium Connect Custom Image Build ^& Push
echo   Docker ID: %DOCKER_ID%
echo   Version:   %VERSION%
echo ========================================================

cd infra\kafka

echo [Login] Logging in to Docker Hub...
docker login
if errorlevel 1 (
    echo Docker Login Failed!
    exit /b %errorlevel%
)

echo [Build] Building Docker Image...
docker build -t %DOCKER_ID%/pawbridge-debezium-connect:%VERSION% .
if errorlevel 1 (
    echo Docker Build Failed!
    exit /b %errorlevel%
)

echo [Push] Pushing to Docker Hub...
docker push %DOCKER_ID%/pawbridge-debezium-connect:%VERSION%
if errorlevel 1 (
    echo Docker Push Failed!
    exit /b %errorlevel%
)

echo.
echo ========================================================
echo   Image Built and Pushed Successfully!
echo ========================================================
pause
