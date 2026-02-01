# PawBridge 인프라 도구

이 폴더는 Kubernetes 클러스터의 인프라 도구들을 Helm values / manifest로 관리합니다.

## 구조

```
infra/
├── monitoring/
│   └── values.yaml      # Prometheus + Grafana 설정
├── tracing/
│   └── zipkin.yaml      # Zipkin 분산 추적
├── dashboard/
│   └── dashboard.yaml   # Kubernetes Dashboard
└── install-infra.sh     # 설치 스크립트
```

## 설치 방법

```bash
# Master 노드에서 실행
vagrant ssh k8s-master
cd /vagrant/infra  # 또는 파일 복사 후 해당 경로에서
chmod +x install-infra.sh
./install-infra.sh
```

## 접속 정보

| 서비스 | URL | 인증 |
|--------|-----|------|
| Grafana | http://grafana.local | admin / admin123 |
| Dashboard | https://dashboard.local | Token 필요 |
| Zipkin | http://zipkin.local | 없음 |

## hosts 파일 설정 (Windows)

`C:\Windows\System32\drivers\etc\hosts` 파일에 추가:

```
192.168.56.70 grafana.local dashboard.local zipkin.local
```
