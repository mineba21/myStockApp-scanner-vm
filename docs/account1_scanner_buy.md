# 스캐너 후보 → 자유투자(account1) 미국주식 매수

2026-10-21 사용자 요청으로 추가. 스캐너 카드의 매수 버튼에서 account1(자유투자, 미국주식 전용)로
지정가 매수 1건을 보낸다. 국내 계좌(account4 ISA) 주문은 연결하지 않았다.
account2 ETF 차액 재분배 흐름과는 코드·저널·설정 스위치가 모두 분리되어 있다.

## 구성

- `trading/account1_buy_orders.py`: 영속 SQLite 저널과 상태 전이, account1 실계좌 어댑터.
- `web/scanner_order_api.py`: `/api/scanner-orders/*`. 기존 `SITES_API_KEY` 인증을 그대로 쓴다.
  `scanner:read`·`portfolio:read` 토큰은 이 경로에 접근할 수 없다(기존 미들웨어 범위 유지).
- `trading/kiwoom_execution_evidence.py`: `ExecutionEvidence(account="account1")`을 허용한다.
  기본값은 계속 account2이며 account2 동작은 바뀌지 않는다.
- 화면: 스캔 결과 카드의 `🛒 매수 · 자유투자` → 주문 티켓. KR 카드는 비활성.

## 흐름과 안전장치

1. **미리보기** `POST /api/scanner-orders/buy/preview {result_id, quantity, limit_price}`
   - 종목·손절가·Strict 결과는 브라우저 값이 아니라 DB의 스캔 결과에서 읽는다.
   - US가 아니거나 Strict 거부(`strict_filter_passed=false`)면 차단한다.
   - `usa20101`을 ND/NY/NA에 조회해 종목·거래소가 정확히 일치하는 응답 **하나**만 채택한다.
     0건이나 2건 이상이면 차단한다(거래소 추정 금지).
   - 지정가가 키움 현재가보다 10% 넘게 높으면 오입력으로 보고 차단, 10% 넘게 낮으면 경고.
   - 지정가 ≤ 손절가면 차단. 손절가가 있으면 주당·총 손절 손실을 보여준다.
   - `ust31490`으로 주문가능수량을 조회해 여유금(`ACCOUNT1_ORDER_RESERVE_BPS`, 기본 100bps)을
     적용한 수량을 넘으면 차단한다. 100bps는 수수료율이 아니라 운영 여유금이다.
   - 저널에 `PREPARED`로 기록, 120초 뒤 만료. 주문은 보내지 않는다.
2. **전송** `POST /api/scanner-orders/buy/{id}/execute {confirmation_ticker}`
   - 사용자가 종목코드를 직접 입력해야 한다.
   - `PREPARED → SENDING`을 조건부 UPDATE로 커밋한 뒤에만 진행한다(중복 클릭·다중 worker 방지).
   - 같은 종목에 `SENDING`/`UNKNOWN`이 있으면 유일 인덱스로 거부한다.
   - 전송 직전 같은 종목·지정가의 `ust31490`을 다시 확인한다. 실패하거나 수량이 줄면 `BLOCKED`
     (주문 미전송).
   - `ust20000` 지정가 매수 1회. 재시도하지 않는다.
     - 주문번호 수신 → `SUBMITTED` (접수이며 체결이 아님)
     - 명시적 거절 → `REJECTED`
     - 타임아웃·응답 유실·형식 오류 → `UNKNOWN` (접수됐을 수 있음, 재전송 금지)
3. **체결 확인** `POST /buy/{id}/refresh`: `ust21150` 주문내역에서 주문번호·종목·방향·수량·가격·
   전송시각이 모두 일치하는 행만 채택해 `broker_state`(OPEN/PARTIAL/FILLED/CANCELLED/…)를 기록한다.
4. **수동 정리** `POST /buy/{id}/resolve`: `UNKNOWN`(또는 120초 지난 `SENDING`)을 사용자가
   키움 앱에서 확인한 뒤에만 `RESOLVED_MANUAL`로 바꿔 종목 잠금을 푼다. 자동 정리하지 않는다.

## 설정

| 변수 | 기본 | 의미 |
|---|---|---|
| `ACCOUNT1_ORDER_ENABLED` | `false` | account1 전송 스위치. 미리보기는 꺼져 있어도 동작한다. |
| `KIWOOM_TRADING_ENABLED` | `false` | 기존 실주문 공통 스위치. 둘 다 `true`여야 전송한다. |
| `KIWOOM_WEB_ENABLED` | `false` | account1 실계좌 조회 연결. 미리보기에도 필요하다. |
| `ACCOUNT1_ORDER_JOURNAL_PATH` | 없음 | 영속 로컬 디스크의 전용 SQLite 절대경로. 모든 worker가 같은 파일을 쓴다. account2 저널과 다른 파일을 쓴다. |
| `ACCOUNT1_ORDER_RESERVE_BPS` | `100` | 주문가능수량 계산 여유금 |

운영 VM은 이미 `KIWOOM_TRADING_ENABLED=true`(account2 재분배)이므로, 이 코드를 배포해도
`ACCOUNT1_ORDER_ENABLED`를 켜기 전에는 account1 주문이 나가지 않는다.
프로필 파일의 account1이 `mode=real`이 아니면 어댑터 생성이 거부된다.

## 검증 상태 (2026-10-21)

검증한 것: 가짜 브로커와 임시 SQLite로 한 격리 테스트(`tests/test_account1_buy_orders.py`),
그리고 목업 API에 붙인 화면 흐름. 키움 서버와 운영 DB는 호출하지 않았다.

**아직 검증하지 않은 것** — 실주문 전에 확인한다:

1. account1 실계좌에서 `ust31490`을 호출한 적이 없다. account2 실계좌에서만 계약을 확인했다.
2. `usa20101`에 틀린 거래소를 넣었을 때의 응답(오류인지, 다른 거래소로 응답하는지)을 확인하지 않았다.
   이 코드는 종목·거래소 일치 응답이 정확히 하나일 때만 진행하므로 모호하면 차단된다.
3. account1에서 `ust20000`을 보낸 적이 없다. 첫 주문은 1주, 현재가보다 낮은 지정가로 보내
   접수·`체결 확인`·키움 앱 취소까지 확인할 것을 권한다.
4. 장 운영시간을 검사하지 않는다. 장외 접수 여부는 키움이 판단하며, 시세가 120초보다 오래되면 경고만 한다.

## 하지 않는 것

- 취소·정정·매도: 화면에 없다. 키움 앱에서 처리한다.
- 체결 후 보유종목/거래일지/손절가 자동 기록: 하지 않는다. 체결 여부는 `체결 확인`으로만 본다.
- 국내(account4) 주문: 연결하지 않았다. KR 카드의 매수 버튼은 비활성이다.
