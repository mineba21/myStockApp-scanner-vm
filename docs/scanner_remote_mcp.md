# 원격 스캐너 MCP

2026-09-11. 로컬 MCP의 조회 도구 3개를 OAuth로 보호한 Streamable HTTP 서버.
운영 배포 URL: `https://161.33.212.161/mcp`.

## 인증과 권한

- OAuth 등록/발견, authorization code + S256 PKCE, 정확한 redirect/resource/scope 대조.
- 연결할 때마다 소유자 승인 화면을 표시한다. 별도 256비트 난수 승인 암호가 필요하다.
- 초기 전달용 원문 파일은 본인 Mac으로 전달한 뒤 서버에서 삭제한다. 서버는 SHA256 검증값만 보관한다. 사용자 선택의 짧은 암호로 대체하지 않는다.
- 승인 폼은 CSRF 세션, Secure/HttpOnly/SameSite 쿠키, Origin 확인, iframe 금지.
- 코드 2분, 승인 요청 10분, access token 1시간, refresh token 30일.
- 코드/refresh 사용은 SQLite 트랜잭션으로 단일 소비. refresh 재사용 감지 시 같은 연결의 토큰 폐기.
- 토큰은 DB에서 해시 키로 저장. 서버 재시작 후에도 유효 연결 유지. revoke로 같은 연결의 토큰 폐기.
- scope는 scanner:read 하나. 키움·운영 앱 토큰을 클라이언트에 전달하지 않는다.
- 서비스는 별도 OS 사용자, 별도 가상환경, loopback 8001. 홈 접근 금지, 쓰기는 전용 OAuth 상태 디렉터리만 허용.
- Nginx는 해당 MCP/OAuth 경로만 전달. 본문 64KB 제한, IP별 요청 제한, 해당 경로 access log 비활성.

## 웹 연결

ChatGPT의 앱/커넥터 생성(개발자 기능이 지원되는 계정) 또는 Claude의 사용자 지정 커넥터에서
MCP URL을 입력하고 OAuth로 연결한다. 자동 클라이언트 등록을 지원하므로 일반적으로
client ID/secret을 수동 입력하지 않는다. 승인 화면에서 반환 주소가 본인이 연결하는 앱인지 확인하고
별도 승인 암호를 입력한다. 암호를 GPT/Claude 대화에 붙이지 않는다.

웹 클라이언트의 기능 제공 범위/관리자 정책에 따라 커스텀 MCP 메뉴가 없을 수 있다.
서비스 배포/프로토콜 검증과 사용자 계정에서의 연결 완료는 구분한다.
원격 서버가 실행되므로 Mac을 켜둘 필요는 없다. 자동 알림은 별도 예약 설정이며 이번 작업에 포함하지 않는다.

## 소스와 검증

`integrations/scanner_mcp/remote.py`, `remote_smoke.py`, `deploy/mystockapp-mcp.service`, `deploy/scanner-mcp.nginx.conf`.
MCP SDK 1.26의 표준 OAuth 처리기를 사용한다. 공개 클라이언트 revoke에 불필요한 secret 필드를 요구하는
SDK 동작은 표준 클라이언트 인증기를 유지한 자체 revoke 라우트로 보완한다.

격리 HTTP 테스트: 발견 메타데이터, 무인증 차단, PKCE 실패, 코드 재사용, resource 오류,
소유자 승인·CSRF, HTTPS redirect, refresh 회전/재사용, revoke, 재시작, 도구 3개와 가짜 결과 조회.
실제 스캐너 DB/키움 API는 테스트하지 않는다.

공식 근거:
- https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
- https://developers.openai.com/plugins/build/auth

## 2026-09-11 운영 배포 확인

구현 커밋 `d230605`, 실행환경 격리 보완 `7cdf692`를 GitHub main과 운영 VM에 배포했다.
서비스 active, 인증 메타데이터 200, 무인증 MCP POST 401, 기존 API health 200 확인.
HTTPS 인증서 검증을 유지한 curl로 확인했다. Mac 기본 Python의 CA 저장소 오류가 있어 curl을 사용했다.
전체 격리 테스트 551개와 별도 원격 OAuth/MCP 테스트 6개 통과.
운영 스캐너 DB·계좌를 테스트로 조회하지 않았다. 실제 ChatGPT/Claude 계정 연결은 사용자가 진행해야 한다.

승인 암호: 이 Mac의 `~/.config/mystockapp/remote-mcp-approval.txt` (권한 600).
서버 초기 전달 파일은 삭제 완료. Git에 암호를 넣지 않는다.

- ChatGPT: 설정 → Security and login → Developer mode를 활성화하고, Plugins의 +에서
  원격 MCP URL을 추가한다. 계정/조직에 따라 메뉴 제공 여부가 다를 수 있다.
- Claude: Customize → Connectors → + → Add custom connector에서 URL을 추가한다.
- 인증 방식 선택이 있으면 OAuth. 별도 client ID/secret은 자동 등록을 사용한다.
- 열리는 서버 승인 화면에만 위 파일의 암호를 입력한다. 대화창에 붙이지 않는다.
- 연결 후 새 대화에서 도구를 선택하고 “최근 스캐너 결과와 경고를 알려줘”라고 요청한다.

공식 연결 안내:
- https://developers.openai.com/plugins/deploy/connect-chatgpt
- https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
