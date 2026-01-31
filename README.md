# PawBridge Infrastructure K8s

PawBridge MSA Kubernetes 배포 구성 (Helm Charts, ArgoCD)

## 구조

```
├── charts/                    # Helm Charts
│   ├── api-gateway/
│   ├── user-service/
│   ├── animal-service/
│   ├── community-service/
│   ├── store-service/
│   └── payment-service/
├── environments/              # 환경별 values
│   ├── dev/
│   └── prod/
└── README.md
```

## 서비스 목록

| 서비스 | 포트 | 설명 |
|--------|------|------|
| api-gateway | 8080 | API Gateway (외부 노출) |
| user-service | 8080 | 사용자 서비스 |
| animal-service | 8081 | 동물 서비스 |
| community-service | 8082 | 커뮤니티 서비스 |
| store-service | 8083 | 스토어 서비스 |
| payment-service | 8084 | 결제 서비스 |

## 배포 방법

### 수동 배포 (Helm)
```bash
helm upgrade --install api-gateway ./charts/api-gateway -f environments/dev/values.yaml
```

### GitOps (ArgoCD) - Phase 2
ArgoCD Application 등록 후 자동 배포
