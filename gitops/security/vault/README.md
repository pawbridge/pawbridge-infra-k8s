# Vault를 PC에서 직접 접속하기

목표 주소는 `https://vault.vault.svc:30820/ui/`다. NodePort는 웹 화면과 API의
8200 포트만 연결한다. 내부 Service와 Raft 8201 포트는 변경하지 않는다.

## 반영 전 조건 확인

1. 대상 context가 `pawbridge-vbox-k136`, Application이 `vault-baseline`인지 확인한다.
2. `vault-0`가 있는 노드의 host-only IP를 확인한다. 현재 구성은 w1의
   `192.168.57.12`다. `externalTrafficPolicy: Local`이므로 Vault 파드가 없는
   cp1이나 w2 주소로 접속하면 안 된다.
3. Windows host-only 어댑터가 `192.168.57.1`인지 확인한다. 다른 IP라면
   접근 정책의 `/32`를 검토한다. 대역 전체를 허용하지 않는다.
4. 30820 포트가 비어 있고, VM이 NAT와 host-only 어댑터만 사용하는지 확인한다.
   공유기의 포트 전달이나 Cloudflare 공개 경로를 추가하지 않는다.
5. 기존 인증서의 SAN에 `vault.vault.svc`가 있고 유효기간이 남았는지 확인한다.
   이 작업에서는 CA, 인증서, TLS Secret을 교체하지 않는다.

## 순서대로 반영

1. `gitops/argocd/vault-baseline-pilot/project.yaml`의 NetworkPolicy 허용을 먼저 반영한다.
   Application은 이 AppProject 자체를 동기화하지 않는다.
2. 승인된 커밋으로 `vault-baseline`을 prune 없이 수동 동기화한다.
   `vault-private-access` 정책의 sync wave `-1`이 UI Service보다 먼저 적용된다.
3. `vault-ui`가 NodePort 30820, `externalTrafficPolicy: Local`,
   `publishNotReadyAddresses: true`인지 확인한다.
4. 별도 승인 후 Windows hosts 파일에 아래 매핑을 한 번 추가한다.
   기존 같은 이름의 항목이 있다면 먼저 확인하고, 중복으로 추가하지 않는다.

   ```text
   192.168.57.12 vault.vault.svc
   ```

   파일 위치는 `C:\Windows\System32\drivers\etc\hosts`다. 수정에는 관리자 권한이
   필요하다. 예약 작업, 상주 프로세스, SSH 터널은 만들지 않는다.
5. Windows에서 기존 Vault CA를 신뢰하는지 확인한다. 인증서 경고가 나면
   로그인을 멈춘다. 필요하면 인증서 지문을 대조한 **공개 CA 인증서만** 사용자
   신뢰 저장소에 별도 승인 후 등록한다. 개인키를 복사하거나 TLS 검증을 끄지 않는다.

## 접속과 기존 기능 검증

- `https://vault.vault.svc:30820/ui/`의 TLS 검증과 로그인 화면을 확인한다.
  IP 주소를 URL에 직접 넣으면 기존 인증서 SAN과 일치하지 않는다.
- 사용자 비밀번호나 언씰 키는 채팅에 보내지 않는다.
- Vault 파드 UID, 재시작 횟수, PVC를 변경 전과 대조한다. 이 변경은 파드 교체가
  필요하지 않다. 검증을 위해 운영 Vault를 일부러 seal하거나 재시작하지 않는다.
- 기존 VaultStaticSecret의 상태와 최근 동기화 시간을 확인한다. API 접근은
  `vault-secrets-operator-system`의 컨트롤러에 계속 허용한다.
- Windows에서는 접속되고, 다른 애플리케이션 namespace에서는 새 NodePort와
  Vault API 접근이 차단되는지 값 조회 없이 확인한다.
- `publishNotReadyAddresses`와 active-only 선택 해제로, 실제 재부팅 후 seal된
  상태에서도 언씰 화면에 접근할 수 있다. VM 자체가 꺼져 있으면 접속할 수 없다.

NetworkPolicy는 관리자 권한을 가진 노드에 대한 보안 경계가 아니다. 노드에서
출발한 일부 트래픽은 예외이므로, host-only 네트워크와 VM 방화벽 범위를 함께
유지한다. 파드가 다른 노드로 이동하면 hosts의 IP도 확인해야 한다.

## 원상복구

접속만 실패하고 VSO는 정상이라면 NodePort 설정과 hosts 매핑부터 확인한다.
VSO까지 영향을 받으면 새 `vault-ui` Service를 먼저 제거해 PC 접속을 닫는다.
그다음 새 `vault-private-access` 정책을 제거해 기존 통신을 복구한다.
이어서 `ui.enabled: false`인 이전 커밋으로 되돌린다.
`Prune=false`이므로 파일을 Git에서 없애는 것만으로는
Service와 NetworkPolicy가 삭제되지 않는다. 삭제는 정확한 두 이름을 확인하고
별도 승인 후 수행한다. 기존 Vault Service, StatefulSet, TLS Secret, PVC는 삭제하지 않는다.

Windows에서는 이번에 추가한 hosts 항목만 제거한다. 기존 CA 신뢰는 임의로
삭제하지 않는다. 임시 관리 접속이 필요하면 승인된 수동 port-forward를 사용하되
Windows 예약 작업으로 상주시켜 대체하지 않는다.

## 로컬 계약 검사

저장소 루트에서 실행한다. 렌더 파일은 저장소 밖에 둔다.

```sh
python3 infra/vault/tests/check_private_access.py
python3 infra/vault/tests/check_private_access.py \
  --before /tmp/vault-before.yaml \
  --after /tmp/vault-after.yaml \
  --security /tmp/vault-security.yaml
```

`before`와 `after`는 같은 Vault Helm `0.34.1`로 렌더한다. UI Service 외에 기존
StatefulSet, ConfigMap, RBAC, Service가 바뀌면 검사가 실패한다.
