# 관측성 비밀값 등록·동기화 실행 계약

이 문서는 `configure-observability-runtime.py`, CA 공급 도구와
`gitops/security/observability-vso`의 실행 계약이다. 전체 계획과 운영 결과는
Obsidian `Projects/pawbridge/14 관측성 성능과 테스트`에 기록한다.
현재는 소스 구현·오프라인 검증 단계이며 Vault 등록·Kubernetes 적용·Slack 전송은 미실행이다.

## 등록되는 값과 권한

| Vault KV v2 경로 (`secret/`) | Kubernetes Secret (`monitoring`) | 필드 |
| --- | --- | --- |
| `pawbridge/dev/observability/grafana` | `monitoring-grafana-admin` | `admin-user`, `admin-password` |
| `pawbridge/dev/observability/slack` | `monitoring-slack-webhook` | `url` |

Grafana와 Slack은 별도 ServiceAccount·VaultAuth·Vault role·read 정책을 사용한다.
각 전용 정책은 자기 경로의 data/metadata 조회만 허용한다. 기존 서비스와 같이 Vault의
`default` 정책은 유지한다. VSO1.4.1이 자기 인증 토큰을 갱신·조회·폐기하는 데 필요하며,
`token_no_default_policy=true`로 끄면 이 기능이 실패한다. 다른 서비스의 read 정책을 붙이지 않는다.

- Grafana 신규 계정 이름은 `pawbridge-admin`, 비밀번호는 안전한 난수32바이트로 생성한다.
  값은 화면·명령줄·Git에 출력하지 않는다. 등록 후 운영자가 Vault 웹 UI에서 확인한다.
- Slack은 일반 Incoming Webhook의 HTTPS 주소만 숨김 입력으로 받는다. API 토큰이나
  Workflow Builder 주소는 받지 않는다. 형식 검사는 실제 전송 성공을 증명하지 않는다.
- 기존 값은 재사용한다. 정책·role·필드 계약이 다르거나 삭제된 KV 버전이면 중단한다.
  신규 KV는 `cas=0`으로만 작성하며 이미 생긴 값을 덮어쓰지 않는다.
  Grafana KV가 없는데 관리자 Secret이나 Grafana Deployment/PVC가 있으면 새 비밀번호를
  만들지 않는다. 기존 로그인 정보·저장 데이터를 먼저 복구해야 한다.
- `check`는 임시 로그인 토큰 생성·폐기와 읽기만 수행한다. 누락을 자동 수정하지 않는다.
  `apply`도 변경이 없으면 새 스냅샷을 만들거나 사용자 입력을 반복하지 않는다.
- Vault 정책·role 생성에는 CAS가 없으므로 등록 도구를 동시에 여러 번 실행하지 않는다.
- Grafana Secret은 **최초 DB 초기화용**이다. Vault의 비밀번호만 바꿔도 이미 생성된
  Grafana DB 계정 비밀번호가 바뀌지는 않는다. 이후 암호 변경은 Grafana 지원 절차로 처리한다.
  두 VSS 모두 `rolloutRestartTargets`를 두지 않아 등록 자체가 재시작을 일으키지 않는다.

## 승인 후 등록 순서

1. 대상 `pawbridge-vbox-k136` context·시간·Vault Ready/언씰을 확인한다. 로컬 CLI context와
   관리자 kubeconfig의 context 이름은 다를 수 있으므로 확인 없이 이름만 바꾸지 않는다.
2. 제어 노드의 같은 디렉터리에 이 Python 도구와 기존
   `configure-animal-python-runtime.py`, `policies/observability-*-read.hcl`을 배치한다.
   기존 도우미에서는 임시 로그인·전송·정리만 재사용한다. MySQL·APMS·R2 경로는 호출하지 않는다.
3. `python3 configure-observability-runtime.py validate`로 정책을 오프라인 검사한다.
4. 승인된 운영자가 mode700의 새 `/tmp/pawbridge-*` 백업 폴더를 준비한 뒤 실행한다.

```bash
python3 configure-observability-runtime.py apply --target grafana --backup-dir <PRIVATE_BACKUP_DIR>
# Slack 주소가 준비되면 별도 실행하거나, 처음부터 --target all로 묶는다.
python3 configure-observability-runtime.py apply --target slack --backup-dir <PRIVATE_BACKUP_DIR>
```

5. 도구가 표시한 `vault-pre-observability-*.snapshot.gz`를 Windows의 기존
   `PawBridge-Private/vault-backups`로 복사하고 양쪽 SHA-256을 대조한다.
   스냅샷은 실제 비밀값을 포함한다. 채팅·Git에 첨부하지 않는다. 도구의 gzip 검사는
   실제 복원 검증이나 VM 밖 백업 완료를 뜻하지 않는다. 복사 검증 전 VSO를 동기화하지 않는다.
6. 실패하면 성공한 단계까지 남을 수 있다. 자동 되돌리기·삭제·반복 실행은 하지 않는다.
   원인 확인 후 재실행하면 일치하는 기존 값은 재사용한다. 임시 토큰 폐기에 실패했다면
   안내된 실패를 해결하기 전 임시 세션을 지우지 않는다.

## 승인 후 Kubernetes 동기화 순서

1. 기존 관측 소스의 `monitoring` Namespace만 먼저 준비한다. 관측 워크로드는 아직 설치하지 않는다.
   이 Namespace의 Git 소유자는 관측 baseline이며 secret-sync App에는 중복 포함하지 않는다.
2. `sync-vault-internal-ca.sh check monitoring`으로 동일 namespace의 CA를 확인한다.
   없거나 다르면 원본 인증서·기존 대상 소유권을 확인하고 승인 후
   `sync-vault-internal-ca.sh apply monitoring`을 실행한다. 기존 인수 없는 실행은
   `databases pawbridge kafka`만 대상으로 유지된다. 인증·통신 오류는 누락으로 취급하지 않는다.
   이 도구는 Namespace를 만들지 않으며 `ca.crt`만 복사한다. TLS 개인키는 복사하지 않는다.
3. 배포된 VSO controller의 namespace watch·RBAC와 Vault8200 통신을 확인한다.
   새 Connection은 `monitoring/vault-internal-ca`를 참조하고 CA/서버 이름 검증을 유지한다.
4. `gitops/argocd/observability-secrets`의 전용 Project와 Grafana App을 승인 후 등록하고
   수동 동기화한다. AppProject는 `monitoring`의 SA와 VSO CR 세 종류만 허용한다.
   기존 Secret이 있으면 소유권을 먼저 확인한다. `overwrite: false`를 우회하지 않는다.
5. VaultStaticSecret Ready와 생성된 Secret의 **키 이름만** 확인한다. 실제 값·전체 Secret
   JSON을 출력하지 않는다. Grafana 관리자 Secret 준비 후 관측 baseline을 별도 승인으로 설치한다.
6. Slack KV가 준비되면 `gitops/argocd/observability-secrets/slack` App을 별도 등록·수동 동기화한다.
   이 App은 Grafana App이 만든 공통 Connection/Project에 의존하므로 먼저 적용하지 않는다.
   Slack이 아직 없어도 Grafana용 App은 독립적으로 준비할 수 있다.
7. Slack Secret 존재만으로 발송되지 않는다. 실제 발송 승인 후 관측 Application의
   values 목록에 기존 `slack-values.yaml`을 연결하고 별도 검증한다. 발송·복구 메시지의 실제
   한국어 수신, Secret 변경 후 Alertmanager 반영 여부는 그 단계의 완료 조건이다.

## 복구 경계

자동 sync·prune는 모두 끈다. VSS에는 Argo 삭제/정리 방지 annotation을 둔다.
문제 시 관측 설치/발송을 보류하며 기존 앱·MySQL·Vault 전체를 재기동하지 않는다.
VSS/생성 Secret/공통 CA를 삭제하면 의존 서비스가 영향을 받을 수 있으므로 자동 삭제하지 않는다.
CA 갱신은 source 인증서와 연결 검증 후 별도 승인으로 진행한다. Vault 스냅샷 전체 복원은
다른 서비스의 변경을 되돌릴 수 있으므로 이 작업의 일반 롤백으로 사용하지 않는다.

## 오프라인 검증

```bash
python3 -m unittest discover -s infra/vault/tests -p 'test_observability_*.py' -v
bash -n infra/vault/sync-vault-internal-ca.sh
python3 infra/vault/tests/check_observability_render.py <RENDER_DIRECTORY>
```

마지막 검사는 Kustomize로 만든 `grafana.yaml`, `slack.yaml`, `argocd.yaml`,
`argocd-slack.yaml`을 입력으로 받는다. 각각 security 기본/Slack, argocd 기본/Slack 경로의
실제 렌더여야 한다. 모의 테스트는 운영 Vault 권한·CA 연결·실제 VSO 동기화 성공을 증명하지 않는다.

근거: [VSO1.4.1 토큰 수명 관리](https://github.com/hashicorp/vault-secrets-operator/blob/v1.4.1/vault/client.go),
[VSO API](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/api-reference),
[Slack Incoming Webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/).
