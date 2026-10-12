# 방문 집계 서명키 공급과 운영 전환

이 디렉터리는 승인된 B안 방문 기능의 운영 계약이다. 키 값·관리자 토큰·DB 비밀번호는 포함하지 않는다.

## 적용 순서

1. 실제 CP 신원·context·Argo source·이미지와 복구용 기존 설정을 확인한다.
2. Community 스키마 custom-format 백업을 VM 밖 비공개 경로에 저장하고 해시·격리 복원을 검증한다.
3. 발행된 migration 이미지의 PostgreSQL standalone 번들에서 V1~V10 checksum을 검증한다. V11만 pending인 경우에 한해 loopback SSH 연결과 `pawbridge_community_migration` 역할로 실행한다. 기존 MySQL `schemaMigration` Hook은 비활성으로 유지한다. `clean`, `baseline`, `repair`, 다른 버전 반복은 허용하지 않는다.
4. V11 테이블의 앱 계정 SELECT·INSERT·UPDATE·DELETE 권한과 기존 테이블 지문을 확인한다. 기존 DB 계정/비밀번호를 교체하지 않는다.
5. `secret/pawbridge/prod/community/visits`에 신규 32바이트 이상 난수의 Base64 `DAILY_VISITORS_SIGNING_KEY`를 등록한다. CAS 0으로 기존 값 덮어쓰기를 거부하며 값은 출력하지 않는다.
6. `infra/vault/policies/community-visits-read.hcl`와 역할 JSON을 등록한 뒤 이 Kustomize 리소스 4개를 명시적 `pawbridge` namespace에 적용한다. 기존 동일 이름이 있으면 UID/spec을 먼저 대조한다. Secret에 서명키 하나만 있고 VSS 소유 UID·Ready/Healthy가 맞는지 확인한다.
7. 실제 production Argo Community/Gateway의 source와 inline override 중 승인된 image·visitor 값만 갱신한다. 자동 동기화·prune·다른 서비스는 변경하지 않는다.
8. 실제 공개 identity/count/today와 관리자 접근 제어·CORS 성공 후 프런트 방문 변경만 main에 승격한다. Cloudflare 운영 빌드는 `build:prod`, `wrangler deploy --env prod`, `VITE_DAILY_VISITORS_ENABLED=true`를 사용한다. test opt-in은 사용하지 않는다.
9. 실제 브라우저에서 최초 집계·같은 쿠키 재집계·탭·모바일 푸터와 관리자 통계를 확인한다. 시험 요청은 실제 당일 집계에 포함되며 임의 차감하지 않는다. 임시 관리자 토큰은 자기 폐기하고 자신이 만든 Pod 토큰 파일만 제거한다.

## 공급 경계

정책은 해당 KV 데이터/메타데이터 읽기와 자기 토큰 조회·갱신·폐기만 허용한다. 역할은 `pawbridge/community-visits-vault-auth`, audience `vault`, TTL/max TTL 600초와 no-default-policy에 고정한다. VSO는 TLS CA/서버명 검증을 유지하고 `_raw`와 다른 키를 제외한다. Community는 Secret 참조로만 키를 받으며 Gateway/프런트에 키를 공급하지 않는다.

## 복구

방문 기능과 공개 표시를 먼저 비활성화한다. 백업한 Argo source/inline override와 이전 불변 이미지, Cloudflare 이전 100% version으로 되돌린다. V11·일별 집계와 새 서명키는 보존한다. down migration·데이터 삭제·기존 Secret 덮어쓰기·전역 cleanup은 하지 않는다. 인증/동기화 실패는 성공으로 기록하지 않는다.

## 검증 명령

`python3 -m unittest scripts/environments/test_daily_visitors_runtime.py`는 Helm 렌더링과 정적 계약을 확인한다. `python3 -B infra/vault/tests/community_visits_integration.py --run-isolated`는 이미 캐시된 고정 Vault 이미지로 소유된 격리 fixture만 사용한다. 실제 운영 Vault–VSO–앱–브라우저 검증과 구분한다.
