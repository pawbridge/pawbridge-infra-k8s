# Store 검색 보안 연결과 최초 전환

이 문서는 MySQL의 상품/SKU를 Elasticsearch 검색 projection으로 만드는 최초 전환 순서를 고정한다. 모든 Argo CD Application은 수동 동기화이며, Git 반영만으로 운영 데이터가 바뀌지 않는다.

## 고정 계약

- MySQL이 상품/SKU의 원본이다.
- 물리 인덱스는 `store-products-vNNN`, 쓰기 alias는 `store-products-write`, 읽기 alias는 `store-products-read`다.
- Kafka key와 Elasticsearch `_id`는 SKU ID다.
- Sink는 완전한 snapshot을 `INSERT` 방식으로 같은 ID에 교체한다.
- ECK 기본 `elastic` superuser는 운영 애플리케이션에서 사용하지 않는다.
- Store는 reader, Kafka sink는 writer, 인덱스 Job은 bootstrap 계정을 사용한다.
- Writer와 bootstrap의 cluster 권한은 Elasticsearch 버전 확인과 readiness에 필요한 읽기 전용 `monitor`로 제한한다.
- ECK HTTP 인증서의 호스트 검증과 CA 검증을 끄지 않는다.
- Source의 `snapshot.mode=no_data`는 기존 outbox를 재발행하지 않는다. 전체 상품은 `sync-to-es.sql`을 정확히 한 번 실행해 새 이벤트로 발행한다.

## 준비와 등록

다음 두 스크립트는 `check`가 읽기 검증, `apply`가 Vault·Kubernetes Secret·MySQL 계정 변경이다. `apply`는 라이브 변경 승인을 받은 뒤에만 실행한다. 두 스크립트 모두 현재 context가 `pawbridge-vbox-k136`인지 확인하고 비밀값을 출력하지 않는다. Store 검색 bootstrap은 ECK CA가 아직 없는 fresh cluster에서도 시작할 수 있도록 `bootstrap`과 `trust` 단계로 분리한다.

```bash
infra/vault/sync-vault-internal-ca.sh check
infra/vault/configure-store-search-vso.sh check bootstrap
```

최초 구성 또는 drift 복구 때만 다음을 실행한다.

```bash
infra/vault/sync-vault-internal-ca.sh apply
infra/vault/configure-store-search-vso.sh apply bootstrap
```

`bootstrap`은 Vault policy·Kubernetes auth role·ECK file-realm credential·MySQL CDC 계정만 준비하며 ECK HTTP CA를 요구하지 않는다. Elasticsearch가 Ready가 되어 CA를 만든 뒤에만 다음 `trust` 단계를 실행한다. 이미 ECK와 CA가 있는 환경의 전체 drift 검사는 `check all`로 수행할 수 있다.

새 Application과 기존 AppProject 권한은 다음 경로의 Kustomize 결과를 등록한다. 등록은 Application CR을 만들 뿐 각 Application을 동기화하지 않는다.

- `gitops/argocd/store-search-vso`
- `gitops/argocd/elasticsearch-pilot`
- `gitops/argocd/store-search-index`
- `gitops/argocd/kafka-connect-pilot`
- `gitops/argocd/store-search-connectors`
- `gitops/argocd/store-search-read-cutover`
- `gitops/argocd/store-pilot`

## 수동 동기화 순서

아래 게이트는 건너뛰거나 병렬 실행하지 않는다.

1. `store-search-eck-auth-vso`
   - `databases` namespace의 VaultAuth와 reader/writer/bootstrap destination Secret 3개가 Ready인지 확인한다.
   - Secret 값은 읽지 않고 이름과 필수 key 존재만 확인한다.
2. `pawbridge-elasticsearch`
   - ECK가 reader/writer/bootstrap file-realm 계정과 세 역할을 반영한다.
   - Elasticsearch가 `Ready`이고 ECK HTTP CA Secret을 만들 때까지 다음 단계로 가지 않는다.
3. ECK 신뢰 자료와 runtime VSO
   - `infra/vault/configure-store-search-vso.sh apply trust`로 현재 ECK CA와 PKCS12 truststore를 Vault에 기록한다.
   - 이어 `store-search-runtime-vso`를 동기화하고 `pawbridge`의 reader·PEM CA, `kafka`의 writer·PKCS12·MySQL CDC destination이 모두 Ready인지 확인한다.
   - CA fingerprint와 PKCS12 hash를 스크립트가 검증하며 값 자체는 출력하지 않는다.
4. `store-search-index-v001`
   - 기존 동명 인덱스나 write alias가 있으면 실패한다.
   - strict mapping과 `store-products-write`만 원자적으로 만든다.
   - `store-products-read`는 아직 만들지 않는다.
5. `pawbridge-kafka-connect`
   - Secret config provider, 최소 Secret `get` RBAC, PKCS12 truststore mount를 반영한다.
   - Connect worker가 Ready이고 세 Secret만 읽을 수 있는지 확인한다.
6. `store-search-sink`
   - Connector와 task가 `RUNNING`인지 확인한다.
   - ECK HTTPS 연결과 write alias 접근이 성공해야 한다.
7. `store-search-source`
   - Sink가 먼저 정상인 경우에만 동기화한다.
   - Connector와 task가 `RUNNING`인지 확인한다.
   - MySQL은 `log_bin=ON`, `binlog_format=ROW`, `binlog_row_image=FULL`, `server_id=13601`이어야 한다. Vault bootstrap `check`도 이 계약을 검사한다.
8. 백엔드 저장소의 `store-service/src/main/resources/sync-to-es.sql`을 Store DB에 정확히 한 번 실행한다.
   - 실행 중 Store 쓰기를 동결한다.
   - Source/Sink task, consumer lag, DLQ가 모두 정상인지 확인한다.
   - MySQL canonical manifest와 `store-products-v001`을 필드 단위로 대조한다.
9. `store-search-read-cutover-v001`
   - strict mapping, 단일 write-alias 대상, 문서 수 1개 이상을 다시 확인한 뒤에만 read alias를 만든다.
   - 이 Job의 문서 수 검사는 최소 안전망이다. 8단계의 MySQL 전체 manifest 필드 대조와 sink lag/DLQ 검증을 대체하지 않는다.
10. `store-service-dev`
   - PreSync Job이 read alias, mapping contract, 문서 수를 다시 확인한다.
   - 이 검증에 실패하면 Store Deployment는 갱신되지 않는다.

상세 데이터 검증과 쓰기 동결/재개 절차는 백엔드 저장소의 `infrastructure/elasticsearch/STORE_ES_REINDEX.md`를 함께 따른다.

## 실패와 재실행

- `store-search-index-v001`은 기존 상태를 덮어쓰지 않는다. 부분 실패했다면 인덱스와 alias의 실제 상태를 확인한 뒤 새 버전 `v002`로 복구할지 결정한다.
- `store-search-read-cutover-v001`은 비어 있는 인덱스를 공개하지 않는다. 기존 read alias가 있으면 목표 인덱스와 일치하는지 확인한다.
- Source를 동기화하기 전 실패는 데이터 이벤트를 만들지 않는다.
- Source 동기화 뒤 실패는 consumer lag와 DLQ를 보존하고 원인을 수정한다. Kafka offset이나 기존 인덱스를 임의로 삭제하지 않는다.
- Store PreSync 실패를 애플리케이션 성공으로 간주하지 않는다.

## 비밀 회전

- Store reader Secret 변경은 VSO가 Store Deployment 재시작을 요청한다.
- ECK file-realm Secret 변경은 ECK 상태가 다시 Ready가 된 뒤 사용한다.
- KafkaConnector는 VSO의 자동 restart 대상이 아니다. writer, MySQL CDC 또는 truststore를 회전하면 `gitops/stateful/kafka-connect/kafka-connect.yaml`의 `pawbridge.kr/store-search-secret-epoch` 값을 올려 GitOps rolling restart를 만들고, Sink 다음 Source 순서로 상태를 확인한다.
- 이전 credential은 새 worker와 connector가 정상인 것을 확인한 뒤에만 폐기한다.
