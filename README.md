# Pawbridge Infrastructure (Kubernetes)

반려동물 입양 플랫폼 **PawBridge**의 Kubernetes 인프라스트럭처 레포지토리입니다.
AWS EC2 단일 배포 환경에서 **Kubernetes(k8s) 기반의 컨테이너 오케스트레이션 환경**으로 전환하였습니다.

## 프로젝트 배경 (Project Background)
본 프로젝트는 **AWS EC2 환경에서의 서비스 운영 비용 절감**과 **클라우드 네이티브 기술 내재화**를 목표로 시작되었습니다.

- **비용 최적화**: 관리형 서비스(EKS)의 비용 부담을 해결하기 위해, 고사양 로컬 환경(Ryzen 7800X3D, 64GB RAM)을 활용하여 Kubernetes 클러스터를 직접 구축
- **기술 내재화**: EC2 기반의 단순 배포 방식에서 벗어나, Vanilla Kubernetes와 MSA 아키텍처를 밑바닥부터 구성하며 운영 역량 확보
- **운영 확장성**: 보안성이 확보된 로컬 VM 환경에서 실제 도메인 연결과 외부 트래픽 처리가 가능한 프로덕션 수준의 인프라 구축

---

## 시스템 아키텍처 (System Architecture)

### 1. 인프라스트럭처
- **환경**: On-Premise (Local VirtualBox Based)
- **클러스터**: Kubernetes v1.30.0 (Control Plane 1 + Worker 2)
- **네트워킹**: Ingress-Nginx, CoreDNS Service Discovery

### 2. 데이터 파이프라인 (CDC)
**MySQL → Debezium CDC (Kafka) → Elasticsearch** 실시간 데이터 동기화 파이프라인 구축
- **CDC**: Debezium Connector를 통한 변경 데이터 캡처 (MySQL Binlog)
- **Search**: `analysis-nori` (한국어 형태소 분석기) 적용
- **데이터 흐름**: 공공데이터 배치 수집(Animal Service) → MySQL 저장 → Kafka 메시지 발행 → Elasticsearch 실시간 인덱싱
<img width="1041" height="786" alt="Image" src="https://github.com/user-attachments/assets/de0e0646-588c-4ce9-b3f5-7cec49e8f210" />
---

## 주요 기능 (Key Features)

| 기능 | 설명 | 기술 스택 |
|---|---|---|
| **MSA 배포** | 6개 마이크로서비스 컨테이너 오케스트레이션 | Helm, Kubernetes |
| **게이트웨이 라우팅** | 단일 진입점 관리 및 라우팅 | Spring Cloud Gateway, Ingress |
| **실시간 동기화** | RDB 변경사항 실시간 검색엔진 반영 | Kafka, Debezium, Elasticsearch |
| **한국어 검색** | `analysis-nori` 플러그인 기반 검색 최적화 | Elasticsearch |

---

## 배포 프로세스 (Deployment Process)
본 프로젝트는 **Vagrant**를 통해 로컬 Kubernetes 클러스터를 프로비저닝하고, **Helm**을 사용하여 마이크로서비스를 통합 배포합니다.

1.  **Cluster Provisioning**: `Vagrantfile` 기반 3-Node Cluster 자동 구축 (Control Plane + 2 Workers)
2.  **Service Deployment**: `deploy-all.sh` 스크립트를 통한 원클릭 통합 배포 (Helm Upgrade)
3.  **Data Verification**: 공공데이터 수집 배치 실행 및 CDC 파이프라인 동작 검증 (Elasticsearch Indexing)
<img width="1920" height="920" alt="Image" src="https://github.com/user-attachments/assets/393a5460-8de9-45d4-a389-d921c37101b3" />
---

## 구성 전략 (Configuration Strategy)

### 서비스 디스커버리 (Service Discovery)
- **Eureka 제거**: Kubernetes의 Native Service DNS(`CoreDNS`)로 완전 대체
- **효과**: 운영 복잡도 감소 및 리소스 효율성 증대

### 설정 관리 (Configuration Management)
- **Config Server 제거**: Helm Values 및 Kubernetes `Secret`으로 전환
- **보안**: 민감 정보(DB Password, AWS Key)는 암호화된 `Secret`으로 분리 관리

---

## 디렉토리 구조
- `charts/`: 서비스별 Helm Chart 템플릿
- `environments/`: 환경별(dev, local) 설정 값 (Values)
- `infra/`: 미들웨어(DB, Kafka, ES) 인프라 구성
