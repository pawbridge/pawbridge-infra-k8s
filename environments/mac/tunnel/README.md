# Mac 운영 Tunnel 구성

이 정의는 2026-10-17 Mac 단독 공개 전환에 사용한 cloudflared 추가 Compose다.
기존 API origin은 같은 운영 네트워크의 `http://api-gateway:8080`이다. 이 소스 게시가
새 connector 실행이나 Cloudflare 변경을 뜻하지는 않는다.

## 고정 실행 계약

- ARM64 cloudflared `2026.8.3`, digest `sha256:4ecc4283c0f7cc127522e5ef261595cfe8571ee28f5c8d3751b24f52f9d04ebe`.
- 운영 이름 `pawbridge-prod-cloudflared`, Compose 프로필 `public-entry`.
- 비루트 사용자, 읽기 전용 루트, capability 제거, 새 권한 획득 금지, CPU·메모리 제한.
- 토큰은 외부의 제한된 파일을 읽기 전용 마운트하고 `--token-file`로 읽는다.
  이미지·환경변수·Git에 토큰 값을 넣지 않는다.
- 비공개 env에는 UID/GID와 토큰 파일 경로만 공급한다. 호스트 포트를 공개하지 않는다.
- 원격 관리 Tunnel의 ingress는 Cloudflare 설정에서 공급된다. 계정 응답 원문과
  실제 토큰·렌더 Compose는 비공개 자료이며 이 저장소에 넣지 않는다.

## 배포와 복구 경계

이 YAML은 기존 운영 Compose와 함께 사용하는 정의다. 기본 Compose 없이 단독으로
기동하거나 새 프로젝트 이름으로 실행하면 실제 네트워크·소유권이 달라질 수 있다.
준비 당시의 오프라인 옵션·ingress 검증과 이후 실제 단독 연결·공개 HTTP 검증은
서로 다른 근거이며, 실제 전환 결과는 Obsidian 최종 전환표 T10~T12에 기록했다.

후속 배포에서는 실행 대상·현재 connector·origin·토큰 소유권을 먼저 확인한다.
같은 Tunnel의 Windows/Mac connector를 동시에 활성화하지 않는다. `public-entry`
프로필은 승인 절차를 강제하는 보안 장치가 아니다.

Mac에서 신규 업무 쓰기가 발생했으므로 주소만 Windows로 되돌리는 단순 복귀는
허용하지 않는다. 계획 정지·재배포는 데이터 경계와 자동 재시작 제어를 포함한 별도
운영 절차를 따른다. 이 문서 게시 과정에서는 connector·서비스·Cloudflare를 변경하지
않았으며, Tunnel 삭제·토큰 교체·데이터 볼륨 삭제도 하지 않았다.
