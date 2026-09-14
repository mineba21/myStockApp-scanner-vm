# 계좌 현황 조회 전용 MCP

작성: 2026-09-15. 사용자가 승인한 계좌 보고 기능을 스캐너 MCP와 분리해 제공한다.

## 원래 의도와 이번 변경

- `docs/scanner_read_mcp.md`의 계좌 차단은 의도된 동작이다. 스캐너 조회키와 기존
  `scanner:read` 범위를 변경하지 않는다.
- WEB의 `/api/holdings`, `/api/portfolio-summary`는 키움 실계좌를 읽는 기존 기능이다.
  그러나 일반 사이트 자격 증명을 요구하고 수동 보유자료가 섞일 수 있어 MCP가 직접 사용하지 않는다.
- 이번 작업은 구현 오류 수정이 아니라 사용자가 승인한 기능 확장이다. 별도
  `PORTFOLIO_READ_TOKEN`, `portfolio:read` OAuth scope, OS 서비스, OAuth DB를 사용한다.

## 승인된 도구

| MCP 도구 | HTTP GET | 내용 |
|---|---|---|
| `portfolio_overview` | `/api/portfolio-read/overview` | account1·account2·account4의 현금, 평가액, 평가손익 요약 |
| `portfolio_holdings` | `/api/portfolio-read/holdings?account=...` | 전체 또는 지정 계좌의 키움 실계좌 보유 종목 |

`portfolio_holdings`의 account는 `ALL`, `account1`, `account2`, `account4`만 허용한다.
개별 종목은 반환된 보유종목 행으로 보고하며 별도 종목 도구는 만들지 않는다.

## 계좌와 데이터 의미

- account1: 자유투자, 미국주식
- account2: 퀀트투자, 미국주식. ETF 차액 재분배 계좌지만 이 MCP는 주문 기능이 없다.
- account4: ISA, 국내주식
- 키움 실계좌 조회 결과만 제공한다. 수동 입력 보유자료와 거래내역은 제외한다.
- KRW와 USD를 임의로 합치지 않는다. 미국계좌의 원화 환산값은 키움이 제공한 값만 전달한다.
- `observed_at`은 MCP API 응답 시각이고, 계좌 `updated_at`과 종목
  `price_updated_at`이 실제 스냅샷 시각이다.
- 모든 값이 0인 계좌는 빈 계좌인지 원천 조회 누락인지 단정하지 않고 경고를 반환한다.
- `orderable_cash`는 현황 보고용이다. ETF 주문 예산이나 실주문 가능 근거로 사용하지 않는다.

## 노출하지 않는 정보

- 실제 계좌번호, App Key/Secret, 키움 OAuth 토큰, 내부 DB ID와 메모
- 주문, 정정, 취소, 환전, 입출금, 거래내역 변경
- 수동 보유자료, 주문내역, 자산배분 주문 상태

포트폴리오 조회키로 일반 API, 스캐너 API, 자산배분·주문 API에 접근하면 401이다.
허용 경로의 GET 이외 요청은 403이다. 전용 키가 사이트·스캐너 키와 같으면 503으로 차단한다.

## 원격 배포와 연결

- 원격 URL: `https://portfolio.161-33-212-161.sslip.io/mcp`
- OAuth: authorization code + S256 PKCE, DCR, 소유자 승인
- scope: `portfolio:read`
- 서비스: `mystockapp-portfolio-mcp`, loopback 8003
- 상태: 구현·격리 테스트 후 운영 배포 및 ChatGPT/Claude 연결 결과를 아래에 기록한다.

## 보고 형식

전체 계좌는 조회시각, 계좌별 현금·주식 평가액·평가손익, 통화를 나누어 보고한다.
보유종목은 계좌별로 종목명·티커·수량·평균매입가·현재가·평가액·평가손익·수익률과
시세시각을 보고한다. 같은 티커가 여러 계좌에 있으면 합치지 않고 계좌별로 표시한다.

## 격리 검증

- `tests/test_portfolio_read_api.py`: 모의 키움 응답만 사용해 필드 투영, 계좌 필터,
  수동 보유 제외, 키 분리, 경로·메서드 차단, 일반화된 오류를 검증한다.
- `integrations/portfolio_mcp/protocol_smoke.py`: localhost 가짜 API와 실제 stdio MCP로
  초기화, 도구 2개 목록, 호출, 잘못된 계좌 차단을 검증한다.
- `integrations/portfolio_mcp/remote_smoke.py`: 임시 OAuth DB와 가짜 계좌 응답으로
  `portfolio:read`, 소유자 승인, DCR/PKCE, 도구 2개를 검증한다.
- 테스트에서 운영 DB, 키움 실계좌, 주문 API를 호출하지 않는다.
