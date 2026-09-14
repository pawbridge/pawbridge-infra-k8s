# 운영 GPU 연결 프록시

animal-service의 실종 후보 요청만 WSL GPU로 전달하는 내부 ClusterIP 서비스다. 모델·벡터·사진을 저장하거나 추론하지 않는다. cp1에 준비된 SSH Unix 소켓을 UID 1000으로 연결한다. 별도 NodePort, 공용 DNS, GatewayPorts 변경은 없다.

## 계약

- `GET /livez`: 프록시 자체 liveness. GPU 장애 때문에 프록시를 반복 재시작하지 않는다.
- `/health`: GPU health 전달. 연결 실패나 GPU 준비 미완료이면 readiness 실패.
- `POST /internal/animals/lost-candidates`: 기존 내부 키를 그대로 전달하고 Authorization/Cookie/X-User-Id는 제거한다. GPU가 키를 검증한다.
- 다른 경로는 404, 후보 경로의 다른 메서드는 403. 본문 최대 6MiB, 동시 처리 최대 2건(초과 503), upstream 연결 3초/읽기 50초, 프록시 재시도 없음.
- NetworkPolicy는 같은 namespace의 app=animal-service Pod만 인입 허용한다. 실제 CNI 적용 검증은 배포 시 수행한다. 파일 소켓 연결은 TCP egress를 필요로 하지 않는다.
- CPU request/limit 10m/200m, 메모리 16Mi/64Mi, 임시 볼륨 최대 32Mi. 단일 프록시가 cp1에 묶이므로 cp1이나 호스트 GPU 장애 시 후보 검색을 제공할 수 없다.

## 적용 순서

1. Python 저장소의 `deploy/connect_vm.py`와 system unit을 설치하고 전용 소켓을 먼저 준비한다. 소켓 디렉터리와 UID가 values 및 securityContext와 일치해야 한다. 기본 경로는 Directory 타입 hostPath이므로 누락되면 Pod가 시작되지 않는다.
2. 기존 GPU key와 운영 animal-service key를 비노출로 확인한다. 변경이 필요하면 백업 후 갤러리 완료 시점에 수행한다.
3. 정확한 검토 commit의 chart를 lint/render한다. 승인된 초기 배포에서 렌더된 프록시 네 리소스만 먼저 적용한다. 이것은 실제 운영 변경이며 로컬 render와 구분한다.
4. 프록시가 Ready인 상태에서 animal-service와 동일한 호출·인증 계약으로 후보 응답을 확인하고 다른 Pod의 접근 차단을 확인한다.
5. 그 이후 이 PR의 animal-service URL 전환을 병합한다. 기존 animal-service Argo 자동 동기화가 배포를 시작한다. 원래 PYTHON_AI_SERVICE_URL은 유지되므로 기존 Python 서비스의 다른 기능은 그 경로를 계속 사용한다.
6. PR이 dev에 포함되면 `gitops/argocd/lost-search-gpu-proxy`의 AppProject/Application을 승인된 bootstrap으로 등록한다. 새 디렉터리를 추가한 것만으로 Argo가 자동 발견하지 않는다. 최초 sync가 초기 적용 자원을 인수했는지 확인한다.
7. 실제 Pod imageID, Synced/Healthy, Ready, 인증/크기/실제 검색 결과를 확인한다. Gateway·화면 공개와 공개 도메인 E2E는 후속 배포다.

```sh
helm lint charts/lost-search-gpu-proxy -f environments/dev/values/lost-search-gpu-proxy.yaml
helm template lost-search-gpu-proxy charts/lost-search-gpu-proxy -n pawbridge -f environments/dev/values/lost-search-gpu-proxy.yaml
```

## 복구

URL 전환 전 실패하면 animal-service를 변경하지 않고 새 프록시를 복구한다. 전환 후 실패하면 먼저 animal-service의 LOST_SEARCH_PYTHON_URL을 이전 설정으로 복원한다. Argo selfHeal이 수동 변경을 덮지 않도록 승인된 Git revert 또는 명시적 Argo override를 사용하고 복구 후 해제한다. 이전 CPU 경로가 실종 검색 성공을 보장하지는 않는다.

기존 스크립트/unit으로 터널을 복원하고 feed 및 로컬 미리보기를 확인한다. 새 Application을 등록했다면 자동 sync를 먼저 중지해야 초기 자원 정리가 되살아나지 않는다. GPU 모델, R2, ES 인덱스·별칭은 이 프록시 복구에서 삭제/변경하지 않는다.
