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
- 상태: 2026-09-15 운영 배포와 ChatGPT·Claude 웹 연결을 완료했다.

### 웹 연결 확인 결과

- ChatGPT: `myStockApp 계좌현황` 플러그인으로 OAuth 연결했다.
  - 앱 ID: `asdk_app_6aa889a2b1c08191a8c335fb7c9ed2db`
  - 버전 ID: `asdk_app_v_6aa889a2b1c88191b0d7274d7296b112`
  - 개발자 화면에서 `portfolio_holdings`, `portfolio_overview` 두 읽기 작업과
    `portfolio:read` 범위를 확인했다.
- Claude: `MyStockApp Portfolio` 사용자 지정 커넥터로 OAuth 연결했다.
  - 연결 화면에서 읽기 전용 도구가 2개인 것을 확인했다.
  - 표시 이름은 `Portfolio holdings`, `Portfolio overview`이다.
- 기존 `myStockApp 스캐너`/`MyStockApp Scanner` 연결은 변경하지 않았다.
- 연결 검증 중에는 도구 목록과 권한만 확인했다. 키움 실계좌 데이터와 주문 API는 호출하지 않았다.

### 사용 예시

- 전체 계좌: `myStockApp 계좌현황으로 전체 계좌를 요약해줘.`
- 특정 계좌 보유종목: `MyStockApp Portfolio로 account2 보유 종목을 보여줘.`

실제 대화에서 위처럼 요청하면 해당 서비스가 키움 실계좌 조회 API를 호출한다. ChatGPT와
Claude의 기본 도구 권한 설정에 따라 각 조회 전에 사용자 승인을 요구할 수 있다.

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

## 2026-11-05 미국 계좌 평가 합계 표시 수정

- 구현 오류: 보유종목은 `ust21070`인데 사이트 계좌 카드와 MCP 요약의 평가액은
  `ust21120`, 손익은 `ust21121`을 사용했다. 동일 조회에서 자유투자의 원장 총평가액과
  종목 합계는 일치했지만 통화별 평가 보고는 달랐다. 모든 종목의 `qty`와 `poss_qty`가
  같았으므로 미결제 수량 때문이라고 단정하지 않는다. 증권사 TR 간 차이의 내부 원인은 미확정이다.
- `/api/portfolio-summary`와 `/api/portfolio-read/overview`는 `holdings_basis=True`로
  `ust21070`의 총평가액·총손익·총수익률 및 원화 총평가액·총손익을 함께 사용한다.
  공식 필드: https://openapi.kiwoom.com/guide/apiGuideContents/32/ust21070
- 원장이 미완료이거나 필수 합계가 누락/비정상이면 다른 TR이나 0으로 대체하지 않고 조회 오류로 표시한다.
- 현금·주문가능액·출금가능액은 기존 예수금 보고 그대로다. 원화 추정 총자산과 현금 환율은
  별도 통화별 보고값을 보존하며 `valuation_source`, `exchange_rate_source`,
  `estimated_assets_source`로 출처를 구분한다. 이 총자산을 원장 평가액+현금과 같은 기준으로 해석하지 않는다.
- 이는 현황 표시 수정이다. 기존 스캐너 사이징의 자산 기준과 ETF/주문 현금은 변경하지 않는다.
  기본 요약과 보고용 요약은 공유 캐시를 덮어쓰지 않는다.
- 검증: 가짜 키움 응답으로 TR 간 차이, 원장/종목 합계 일치, 손익·원화 필드 동기화,
  누락 시 차단, 국내 계좌, 두 HTTP 경로 및 기존 사이징 기준 보존을 확인한다.

### 배포 확인

2026-11-05 KST 운영 VM의 기존 최신 변경을 보존하고 위 수정만 적용했다.
로컬 및 VM의 격리 테스트는 각각 68개 통과, diff 공백 검사 통과.
서비스 재시작 후 health 200, 사이트 `/api/portfolio-summary`와 외부 계좌 MCP 읽기 조회에서
자유투자 USD 31,739.37, 퀀트투자 USD 56,966.52로 각각 종목 평가액 합계와 일치했다.
Sites 소스 변경·재배포는 필요하지 않았다. 실주문과 운영 DB를 이용한 테스트는 하지 않았다.
원장 총손익은 증권사 합계값을 그대로 사용한다. 같은 조회의 행별 손익 합과 각각
USD 0.07/0.02 차이가 남아 있으며, 증권사 내부 수수료/반올림 방식 때문이라고 단정하지 않는다.
