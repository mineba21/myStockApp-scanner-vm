# STEP2: 스캐너 조회 전용 MCP

작성: 2026-09-11. 현재는 **로컬 stdio MCP + 인증된 스캐너 조회 API** 구현이다.
2026-09-11 사용자 승인으로 `0df412f`를 GitHub main과 운영 VM에 배포했다.
조회 전용 키를 설정하고 API 서비스를 재시작했다. 공개 health=200,
인증된 잘못된 입력=422, 무인증/조회 범위 밖 요청=401 확인(운영 DB 조회 없음).
앱 등록과 자동 알림 설정은 아직 수행하지 않았다.

## 이 Mac에서 간단히 연결

별도 MCP 가상환경 `integrations/scanner_mcp/.venv` 설치 완료.
API origin은 `https://161.33.212.161`, 키는 `~/.config/mystockapp/scanner-read.token`에
권한 600으로 보관한다. 키 자체는 Git과 설정 예시에 포함하지 않았다.

- **Codex**: `integrations/scanner_mcp/codex.mac.example.toml` 내용을 기존
  `~/.codex/config.toml`에 합친다. 기존 내용을 덮어쓰지 않는다. 앱을 다시 열어 도구를 확인한다.
- **Claude Desktop**: `integrations/scanner_mcp/claude.mac.example.json`의 `scanner-read` 항목을
  `~/Library/Application Support/Claude/claude_desktop_config.json`의 `mcpServers`에 합친다.
  기존 MCP 설정은 유지하고 앱을 완전히 종료한 뒤 다시 연다.
- 연결 후 “스캐너의 최근 미국 종목을 조회하고 경고도 함께 알려줘”로 사용한다.
- **ChatGPT 웹/모바일 직접 연결**: 로컬 설정만으로 연결되지 않는다. 원격 MCP 전송과
  해당 클라이언트 인증 연동을 추가해야 한다. 위 API origin은 등록할 MCP URL이 아니다.

예시 파일은 이 Mac 전용 실제 경로다. 다른 컴퓨터에서는 경로와 조회키 전달을 별도로 설정한다.

## 설계와 범위

먼저 `AGENTS.md`, `CLAUDE.md`, Step1/Step2 review checklist를 읽었다.
기존 `/api/results`는 실계좌 사이징 조회를 호출할 수 있어 재사용하지 않았다.
새 `/api/scanner-read/*`는 저장된 ScanResult/ScanLog만 조회하며 계좌·주문·설정·알림을 호출하지 않는다.
DB를 수정하지 않고 엄격 심사에서 거부된 행은 목록과 상세 양쪽 모두 제외한다.
legacy NULL은 통과로 꾸미지 않고 `legacy_unassessed`로 표시한다.
D+3 확정 및 경고 전용 전략은 변경하지 않는다. 저장된 경고를 별도 필드로 전달한다.
시세는 스캔 당시 저장값이며 현재가가 아니다. weekly basis 자체가 저장되지 않은 행에 이를 만들어 넣지 않는다.

도구:

| MCP 도구 | HTTP GET | 내용 |
|---|---|---|
| scanner_status | /api/scanner-read/status | 최근 KR/US 스캔 기록, 상태, 시각 |
| scanner_signals | /api/scanner-read/signals | 후보·업데이트 목록, 필터, 페이지 커서 |
| scanner_signal | /api/scanner-read/signals/{id} | 한 종목의 저장된 판정 지표·경고 |

계좌자산·추천수량·알림 전송 여부·오류 traceback은 응답에서 제외한다.
MCP 도구의 readOnlyHint뿐 아니라 서버 인증과 GET 경로 허용 목록으로 제한한다.
MCP 도구에는 임의 URL·SQL·POST·주문 기능이 없다. 조회 결과의 문자열은 지시가 아닌 비신뢰 데이터다.

## 인증

서버 환경변수 `SCANNER_READ_TOKEN`에 32자 이상의 독립 난수를 사용한다.
기존 `SITES_API_KEY`를 재사용하면 API는 503으로 차단한다.
조회키로 계좌·주문·삭제·일반 results API에 접근하면 401이다.
조회키 미설정이면 조회 API도 503이다. 기존 주문용 키로 조회 전용 경로 접근도 허용하지 않는다.
TLS 인증서를 검증하는 HTTPS API origin을 사용한다. 로컬 테스트/SSH 터널에만 loopback HTTP를 허용한다.
리다이렉트는 따라가지 않으며 키를 명령행·Git·대화에 넣지 않는다.

## 설치와 연결

Python 3.11 이상. API 서버 패키지와 섞지 않고 별도 가상환경에서 설치한다.
아래 `/ABS/REPO`, `/ABS/MCP-VENV`, `/ABS/PRIVATE/scanner-read.token`, 도메인은 실제 경로로 바꾼다.
키 파일에는 서버의 SCANNER_READ_TOKEN과 같은 값만 넣고 권한을 600으로 제한한다.
이 키는 키움 App Key가 아니며 스캐너 조회 API 전용이다.

```sh
python3 -m venv /ABS/MCP-VENV
/ABS/MCP-VENV/bin/pip install -r /ABS/REPO/integrations/scanner_mcp/requirements.txt
```

로컬 MCP 클라이언트 설정 예시 (`mcpServers` 형식 지원 클라이언트):

```json
{
  "mcpServers": {
    "scanner-read": {
      "command": "/ABS/MCP-VENV/bin/python",
      "args": ["/ABS/REPO/integrations/scanner_mcp/server.py"],
      "env": {
        "SCANNER_API_URL": "https://YOUR-API-HOST",
        "SCANNER_READ_TOKEN_FILE": "/ABS/PRIVATE/scanner-read.token"
      }
    }
  }
}
```

Codex의 로컬 MCP 설정은 [공식 MCP 문서](https://learn.chatgpt.com/docs/extend/mcp)를 참고한다.
설정 예시는 저장소의 `integrations/scanner_mcp/codex.example.toml`에 있다.
`SCANNER_API_URL`은 Sites 화면 URL이 아니라 API origin이다. Sites bridge에는 조회키를 넣지 않는다.
사이트 인증을 우회하거나 SITES_API_KEY를 MCP에 넣어서 해결하지 않는다.

현재 구현은 stdio이므로 클라이언트가 로컬 프로세스를 실행해야 한다.
ChatGPT 웹의 원격 연결로 바로 등록할 HTTP MCP URL은 아직 없다.
웹·클라우드 사용은 별도 원격 MCP 전송 계층 및 해당 클라이언트 인증 방식 통합이 필요하다.
클라이언트 설정 파일을 자동 변경하거나 기존 연결을 덮어쓰지 않았다.

## 페이지·중복 처리와 한계

최초 조회는 최근 7일, 최대 100행. 명시적 after는 최근 366일 내 UTC ISO8601만 허용한다.
정렬은 scan_time, id 오름차순이며 동일 시각의 행도 빠짐없이 조회한다.
`has_more=true`일 때 next_cursor의 after/after_id와 through 및 필터를 그대로 다음 요청에 전달한다.
페이지가 끝나면 next_cursor를 보존하되 다음 정기 조회에서 through를 빼 새 상한 시각을 받는다.
market/signal_type을 변경하면 해당 필터용 커서를 새로 시작한다.

- event_key: 행 ID + 스캔 시각. 같은 응답의 중복 처리 방지에 사용한다.
- signal_key: 시장 + 티커 + 신호 종류 + 신호일. 동일 신호의 업데이트인지 구별한다.
- 원본 행은 재스캔 때 갱신되므로 모든 중간 변경을 보존하는 이벤트 원장은 아니다.
- 삭제/거부 전환은 별도 이벤트로 전달되지 않는다. 계정 간 동시 commit의 완전한 이벤트 전달도 보장하지 않는다.
- 자동 알림·영속 알림 커서·정확히 한 번 전달은 이번 구현에 포함하지 않았다.

## 검증

`tests/test_scanner_read_api.py`: 임시 SQLite만 사용. 인증/권한 분리, 거부 제외,
계좌 필드 제외, brokerage 미호출, 동일 시각 페이지, 재스캔 갱신, 빈 결과/잘못된 요청 확인.

```sh
/ABS/MCP-VENV/bin/python /ABS/REPO/integrations/scanner_mcp/protocol_smoke.py
```

이 검사는 localhost 가짜 API + 실제 SDK stdio 프로세스로 initialize/tools/list/tools/call,
3개 도구, 주문 도구 부재, 잘못된 인자 차단을 검증한다. 운영 API·DB·키움은 호출하지 않는다.
SDK는 공식 Python SDK 1.26.0을 별도 requirements에 고정했다.

검증 결과: 전체 격리 테스트 551개 통과. 별도 MCP stdio smoke 테스트 통과
(프로토콜 초기화·3개 조회 도구·오류 인자·리다이렉트 차단 포함).
