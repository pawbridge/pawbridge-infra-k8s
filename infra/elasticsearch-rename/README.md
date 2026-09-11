# 공용 Elasticsearch 이름 전환

이 디렉터리는 기존 `store-search`를 `pawbridge-elasticsearch`로 이전할 때 사용한다.
일반 애플리케이션 배포 디렉터리가 아니며 전체를 재귀 적용하지 않는다.

## 구성

- `original`: 전환 준비 당시 원본 ES 구성. 소스 복구용 기준이며 새로운 빈 환경을 완성하는 설정은 아니다.
- `source`: 원본에 별도 snapshot PVC와 일회성 작업 계정을 연결하는 오버레이.
- `target`: 앱 트래픽 없는 복원 검증용 오버레이. 전용 PVC와 임시 계정을 사용한다.
- `update-trust-bundle.py`: 기존·신규 공개 CA를 네 Vault trust 경로에 함께 등록하는 대화형 도우미.

계정 비밀번호는 포함하지 않는다. source/target 오버레이를 적용하기 전에 각각의 임시 basic-auth Secret이 필요하다.
대상 검증용 메모리 제한은1536Mi, JVM heap은512Mi다. 운영 설정과 다르므로 부하 검증 없이 운영 적정값으로 간주하지 않는다.

## 순서와 중단 조건

1. 원본 snapshot이 SUCCESS이고 VM 밖 복사본의 파일 해시가 일치하는지 확인한다.
2. 새 ES의 전용 볼륨에 복원하고 기존 ID·임베딩·매핑·별칭을 검증한다. 운영 security feature state를 격리 검증 환경에 복제하지 않는다.
3. 관리자 터미널에서 `python3 update-trust-bundle.py --apply`를 실행한다. 도우미는 지정된 제어 노드와 Kubernetes context만 허용한다.
4. 도우미가 `BACKUP_READY`를 출력하면 snapshot 및 `trust-before.json`을 승인된 VM 밖 비공개 위치에 복사한다. 해시를 검증한 담당자가 출력된 receipt 경로에 `{"sha256":"검증한 snapshot 해시"}`를 기록한다. 복사·검증 없이 receipt를 만들지 않는다.
5. 도우미는 네 trust 경로를 순서대로 갱신한다. 다른 편집과 충돌하면 CAS로 중단한다. 각 VSO 공개 CA와 Deployment 재기동 완료를 확인한 뒤 다음 경로로 넘어간다.
6. 서비스·배치·Kafka 쓰기 중단 및 Argo 자동 동기화 일시 보류 순서를 확정한다. 적용할 Git 변경이 준비되기 전에는 중단하지 않는다.
7. 쓰기 중단 후 최종 snapshot을 생성·검증하고 대상에 반영한다. 최초 검증용 snapshot 이후 생긴 데이터 차이를 무시하지 않는다.
8. 대상 운영 계정·CA·연결 설정을 확인한 뒤 앱과 커넥터를 전환한다. 오프셋을 초기화하지 않는다.
9. 검색과 이벤트 처리 및 재시작 후 데이터 보존을 검증하고 임시 계정을 정리한다. 원본 데이터 볼륨 삭제는 별도 승인 대상으로 남긴다.

## 실패 시

- 인증서 준비 단계에서는 기존 CA를 제거하지 않는다. 일부 경로 반영 후 실패해도 원본 인증서를 계속 신뢰한다. 수정 전 KV 버전과 백업은 recovery 디렉터리에 보존한다.
- 백업 receipt 대기 또는 서비스 복귀 대기가 만료되면 나머지 경로는 적용하지 않는다. 실패 원인과 이미 반영된 경로를 확인한 뒤 재개한다.
- 전환 전에 실패하면 앱 연결을 원본에 유지한다. 양쪽 데이터가 달라진 뒤의 롤백은 쓰기 재개 여부와 누락 이벤트를 먼저 확인한다.
- `DeleteOnScaledownOnly` 상태에서 ES nodeSet을0으로 축소하면 데이터 PVC가 삭제될 수 있다. 원본을 보존하려고 무심코0으로 축소하지 않는다.
- snapshot 저장소 파일을 복사할 때는 해당 저장소의 쓰기를 막는다. 최종 snapshot 생성 전에는 다시 쓰기를 허용해야 한다.

## 로컬 검증

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s infra/elasticsearch-rename -p 'test_*.py'
```

운영 연결 전환과 실제 Vault 로그인·갱신 검증은 위 오프라인 테스트와 별개다.
