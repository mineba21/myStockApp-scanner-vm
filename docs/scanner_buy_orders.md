# 스캐너 후보 지정가 매수 (미국 → account1, 국내 → account4)

- 2026-10-21: 미국 후보 → 자유투자(account1) 연결.
- 2026-10-22: 국내 후보 → ISA(account4) 연결.

스캐너 카드의 매수 버튼으로 해당 시장의 대상 계좌에 지정가 매수 1건을 보낸다. 대상 계좌는
`web/kiwoom_sizing.TARGET_ACCOUNT_BY_MARKET`(제안 수량 계산)과 같다.
account2 ETF 차액 재분배 흐름과는 코드·저널·설정 스위치가 모두 분리되어 있다.

## 구성

- `trading/scanner_buy_orders.py`: 영속 SQLite 저널(`scanner_buy_orders` 테이블), 상태 전이,
  `Account1LiveBroker`(미국), `Account4LiveBroker`(국내).
- `web/scanner_order_api.py`: `/api/scanner-orders/*`. 기존 `SITES_API_KEY` 인증을 그대로 쓴다.
  `scanner:read`·`portfolio:read` 토큰은 이 경로에 접근할 수 없다(기존 미들웨어 범위 유지).
- `ExecutionEvidence(account="account1")` 허용(기본값은 계속 account2),
  `DomesticExecutionEvidence.orderable_cash()` 추가, `KiwoomReadOnlyClient.get_domestic_quote()`(ka10001) 추가.
- 화면: 스캔 카드의 `🛒 매수 · 자유투자` / `🛒 매수 · ISA` → 주문 티켓.

## 흐름과 안전장치 (두 시장 공통)

1. **미리보기** `POST /api/scanner-orders/buy/preview {result_id, quantity, limit_price}`
   - 종목·시장·손절가·Strict 결과는 브라우저 값이 아니라 DB의 스캔 결과에서 읽는다.
     시장이 계좌를 정한다. 브라우저가 계좌를 고를 수 없다.
   - Strict 거부(`strict_filter_passed=false`)면 차단한다.
   - 지정가가 키움 현재가보다 10% 넘게 높으면 오입력으로 보고 차단, 10% 넘게 낮으면 경고.
   - 지정가 ≤ 손절가면 차단. 손절가가 있으면 주당·총 손절 손실을 보여준다.
   - 주문가능수량을 넘으면 차단(아래 시장별). 여유금 `SCANNER_ORDER_RESERVE_BPS`(기본 100bps)는
     수수료율이 아니라 운영 여유금이다.
   - 같은 계좌·종목에 최근 24시간 안에 `SUBMITTED`된 주문이 있으면 경고한다(차단하지 않음).
     주문창에도 최근 접수 주문을 함께 보여준다.
   - 저널에 `PREPARED`로 기록, 120초 뒤 만료. 주문은 보내지 않는다.
2. **전송** `POST /api/scanner-orders/buy/{id}/execute {confirmation_ticker}`
   - 사용자가 종목코드를 직접 입력해야 한다. 해당 시장의 전송 스위치가 꺼져 있으면 503.
   - `PREPARED → SENDING`을 조건부 UPDATE로 커밋한 뒤에만 진행한다(중복 클릭·다중 worker 방지).
   - 같은 계좌·종목에 `SENDING`/`UNKNOWN`이 있으면 유일 인덱스로 거부한다.
   - 전송 직전 주문가능수량을 다시 확인한다. 실패하거나 수량이 줄면 `BLOCKED`(주문 미전송).
   - 그 사이 사용자가 이 요청을 수동 정리했다면(`SENDING`이 아님) 전송하지 않는다.
     정리 후의 새 주문과 중복되지 않게 하기 위해서다.
   - 접수 후 `SUBMITTED` 저장이 실패하면 주문번호와 함께 재주문 금지를 안내한다. 행은 `SENDING`으로
     남아 종목 잠금이 유지된다.
   - 지정가 매수 1회. 재시도하지 않는다.
     - 주문번호 수신 → `SUBMITTED` (접수이며 체결이 아님)
     - 명시적 거절 → `REJECTED`
     - 타임아웃·응답 유실·형식 오류 → `UNKNOWN` (접수됐을 수 있음, 재전송 금지)
   - 예상하지 못한 오류에서는 "주문이 접수되었을 수 있다"고 안내한다. "보내지 않았다"는 미리보기
     단계에서만 말한다.
   - 화면은 전송 전에 주문 ID를 보관한다. 응답이 유실되면 `GET /buy/{id}`로 저널 기록을 확인해
     결과를 보여주고, 확인 전에는 새 주문 입력으로 돌아가지 않는다. 늦게 도착한 응답은 요청을 보낸
     주문창에만 반영한다(다른 종목 창은 토스트만).
3. **체결 확인** `POST /buy/{id}/refresh`: 주문내역에서 주문번호·종목·방향·수량·가격이 일치하는
   행 하나만 채택해 `broker_state`(OPEN/PARTIAL/FILLED/…)를 기록한다.
4. **수동 정리** `POST /buy/{id}/resolve`: `UNKNOWN`(또는 120초 지난 `SENDING`)을 사용자가
   키움 앱에서 확인한 뒤에만 `RESOLVED_MANUAL`로 바꿔 종목 잠금을 푼다. 자동 정리하지 않는다.

## 시장별 차이

| | 미국 (account1 자유투자) | 국내 (account4 ISA) |
|---|---|---|
| 시세 | `usa20101`을 ND/NY/NA에 조회, 종목·거래소 일치 응답이 정확히 1건일 때만 채택. 시세 시각(뉴욕) 120초 초과 시 경고 | `ka10001` 현재가·상한가·하한가. 응답 종목코드 일치 필수. 시각 필드가 없어 시각 검사 안 함 |
| 가격 | 센트 단위 | 원 단위 정수 + KRX 주식 호가단위 일치 필수, 상·하한가 밖이면 차단 |
| 주문가능수량 | `ust31490.min_ord_alowq`와 여유금 적용 예산 중 작은 값 | `kt00001.ord_alow_amt × (1 − 여유금)` ÷ 지정가, 내림 |
| 주문 | `ust20000` 지정가 | `kt10000` KRX 보통 지정가(`trde_tp=0`) |
| 체결 대조 | `ust21150` (주문번호·시각까지 대조) | `kt00007` 당일 주문체결 |

ETF·ETN 호가단위(2,000원 미만 1원, 이상 5원)는 같은 가격대 주식 호가단위의 약수라서,
주식 기준으로 맞춘 가격은 ETF·ETN에서도 유효하다.

## 설정

| 변수 | 기본 | 의미 |
|---|---|---|
| `ACCOUNT1_ORDER_ENABLED` | `false` | 미국(account1) 전송 스위치 |
| `ACCOUNT4_ORDER_ENABLED` | `false` | 국내(account4) 전송 스위치. account1과 독립 |
| `KIWOOM_TRADING_ENABLED` | `false` | 기존 실주문 공통 스위치. 시장 스위치와 함께 `true`여야 전송한다 |
| `KIWOOM_WEB_ENABLED` | `false` | 실계좌 조회 연결. 미리보기에도 필요하다 |
| `SCANNER_ORDER_JOURNAL_PATH` | 없음 | 영속 로컬 디스크의 전용 SQLite 절대경로. 모든 worker가 같은 파일. account2 저널과 다른 파일 |
| `SCANNER_ORDER_RESERVE_BPS` | `100` | 주문가능수량 계산 여유금 |

미리보기는 시장 스위치가 꺼져 있어도 동작한다(주문 미전송). 운영 VM은 이미
`KIWOOM_TRADING_ENABLED=true`(account2 재분배)이므로, 이 코드를 배포해도 시장 스위치를 켜기 전에는
주문이 나가지 않는다. 프로필 파일의 해당 계좌가 `mode=real`이 아니면 어댑터 생성이 거부된다.

## 검증 상태 (2026-10-22)

검증한 것: 가짜 브로커와 임시 SQLite로 한 격리 테스트(`tests/test_scanner_buy_orders.py`),
그리고 목업 API에 붙인 화면 흐름. 키움 서버와 운영 DB는 호출하지 않았다.

**아직 검증하지 않은 것** — 실주문 전에 확인한다:

미국(account1)
1. account1 실계좌에서 `ust31490`을 호출한 적이 없다. account2 실계좌에서만 계약을 확인했다.
2. `usa20101`에 틀린 거래소를 넣었을 때의 응답을 확인하지 않았다. 모호하면 차단된다.
3. account1에서 `ust20000`을 보낸 적이 없다.

국내(account4)
4. `ka10001` 응답 필드(`stk_cd`, `cur_prc`, `upl_pric`, `lst_pric`)는 키움 공식 명세 기준이며
   실서버 응답으로 확인하지 않았다. 종목코드가 다르거나 현재가가 비면 차단된다.
5. `kt10000` 주문·`kt00007` 대조·`kt00001` 예약금 반영은 **모의계좌**(2026-09-14)에서만 확인했다.
   실계좌, 특히 ISA 계좌에서는 주문을 보낸 적이 없다.
6. `kt00007`은 주문일자를 비워 조회한다(당일 기준으로 추정, 미확인). 다음 날 `체결 확인`은 주문을
   찾지 못할 수 있다(이때 상태는 바꾸지 않는다).
7. 국내 주문가능금액은 현금 기준이다. 종목별 증거금률·ISA 납입한도·매수 제한 종목은 증권사 판단에 맡긴다
   (거절되면 `REJECTED`).

공통
8. 장 운영시간을 검사하지 않는다. 장외 접수 여부는 키움이 판단한다. 국내 모의계좌는 시간외 주문을
   거절했다(RC4087).

권장 첫 실주문: 계좌별로 1주를 현재가보다 낮은 지정가로 보내 접수 → `체결 확인`(OPEN) → 키움 앱 취소까지
확인한 뒤 실제 사용한다.

## 하지 않는 것

- 취소·정정·매도: 화면에 없다. 키움 앱에서 처리한다.
- 체결 후 보유종목/거래일지/손절가 자동 기록: 하지 않는다. 체결 여부는 `체결 확인`으로만 본다.
- NXT·SOR·시간외 주문: 국내는 KRX 정규 지정가만 쓴다.
