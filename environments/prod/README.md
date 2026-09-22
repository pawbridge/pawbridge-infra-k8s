# 운영 설정과 승격

`values/`는 2026-09-22 운영 Argo가 실제 참조한 커밋·values·허용 목록의 Argo 파라미터 조합을 합친 기준이다. photo-service의 별도 고정 커밋도 보존했다. 기준 참조는 `../environment-contract.json`에 있다. 현재 운영 Application은 이 파일을 아직 사용하지 않는다.

`main` 기준선 준비 후 기존 Application의 source만 검증·승인해 전환한다. dev에서 검증한 공통 차트와 이미지 digest를 승격하되 운영 DB·Secret·자원·스케줄을 dev에서 복사하지 않는다.

`python3 scripts/environments/promote_image.py --help`의 로컬 도구는 검증된 dev revision/digest 및 E2E 근거 파일을 확인한 후, 선택한 서비스의 **이미지와 migration 이미지 쌍만** prod values에 반영한다. Git push/merge/apply는 실행하지 않는다. source의 main 통합 결과가 검증한 코드와 같은지 별도로 확인해야 한다. 동일 commit SHA를 강제하면 merge commit 방식의 정상 승격도 막을 수 있으므로 코드 diff와 provenance로 검토한다.

이미지 rollback은 이전 prod values commit으로 되돌린다. DB migration이 적용되었다면 이전 바이너리 호환성과 데이터 복구 가능성을 먼저 확인한다. 이미지 rollback을 데이터 rollback으로 표현하지 않는다.
