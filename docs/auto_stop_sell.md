# account1 미국 S01 자동 손절 매도

2026-11-24 작성. 사용자 승인 범위는 **개발**이며, 실계좌 자동 매도(LIVE) 활성화는 별도 결정이다.

## 무엇을 하나

- 사용자가 자유투자(account1) 미국 보유종목을 스캐너 신호(Strict 통과, `stop_loss` 있음)와 연결해 등록한다.
- 등록 시점의 Scanner 손절가(S01)를 고정한다. 이후 재스캔으로 바뀌지 않는다.
- 정규장에서 유효한 당일 시세가 손절가 이하이면, 사람 확인 없이 지정가로 매도한다.
  - 기준가: 최신 현재가
  - 지정가: 기준가 × (1 − 0.5%), 센트 내림
- 미체결이면 다음 순서로 잔여 수량만 다시 주문한다. 이 동작은 `AUTO_STOP_US_REPLACE_ENABLED`를 켰을 때만 한다.
  1. 5분 뒤 취소를 요청한다.
  2. 취소 확정과 최종 체결 수량을 확인한다.
  3. 잔고와 매도가능수량을 다시 조회한다.
  4. 최신 시세로 잔여 수량만 다시 주문한다.

### 하지 않는 것

- 자동매수, Breakout 실패 매도, 트레일링, 부분 익절
- 국내주식, 다른 계좌
- 새 주문 유형. `ust20001` 지정가(`trde_tp=00`)와 `ust20003` 취소만 쓴다.
- 화면에서 주문을 직접 보내는 기능. 전송은 스케줄러 tick만 한다.
- 일반 상태 초기화 API

## 구성

| 파일 | 역할 |
|---|---|
| `trading/auto_stop/settings.py` | 환경변수, 실거래 게이트(`live_order_block_reason`, `live_cancel_block_reason`) |
| `trading/auto_stop/us_calendar.py` | 규칙 기반 NYSE 캘린더: 휴장일, 13:00 조기폐장, 서머타임, env 임시휴장. 2020~2040 밖이면 닫힘 |
| `trading/auto_stop/quotes.py` | 시세 검증, 기준가와 지정가 계산(Decimal) |
| `trading/auto_stop/store.py` | `SCANNER_ORDER_JOURNAL_PATH` SQLite 테이블, 조건부 전이, 선점, fill ledger, lease, 알림 outbox |
| `trading/auto_stop/engine.py` | 상태 머신(tick) |
| `trading/auto_stop/brokers.py` | `LiveStopSellBroker`(account1 실계좌 전용), `ShadowBroker`(가상 주문) |
| `trading/auto_stop/service.py` | 등록, 일시중지, 해제, LIVE 전환, UNKNOWN 증거 해제, 헬스 |
| `trading/auto_stop/runner.py` | 스케줄러 진입점, 실 브로커 조립, Slack/Telegram 알림 |
| `web/auto_stop_api.py` | `/api/auto-stop/*` (`SITES_API_KEY` 인증) |
| `scheduler.py` | 30초 `IntervalTrigger` 작업 1개(`auto_stop_us_tick`, `max_instances=1`, coalesce) |

- 테이블은 Alembic 밖 저널 SQLite에 모듈이 직접 만든다.
  - 테이블: `auto_stop_positions`, `auto_stop_exit_events`, `auto_stop_orders`, `auto_stop_fill_ledger`, `auto_stop_runtime`, `auto_stop_alerts`, `auto_stop_audit`
  - 운영 DB와 Alembic은 변경하지 않는다.
  - 저널을 삭제·초기화하거나 과거 사본으로 덮어쓰지 않는다. 덮어쓰면 중복 전송 방지가 깨진다.

## tick (30초)

1. lease를 획득한다. 실패하면 종료한다.
2. 잔여 SENDING은 UNKNOWN으로, CANCEL_SENDING은 CANCEL_UNKNOWN으로 바꾼다. 재전송하지 않는다.
3. 미확정 주문 전체를 대조한다. 이 단계는 **모든 모드에서, 주문 행의 mode 기준으로** 한다.
   - LIVE 주문은 OFF나 SHADOW 모드에서도 실계좌 주문내역으로 대조한다.
   - SHADOW 주문은 가상 브로커로만 대조한다.
4. 세션을 이월한다. 이월 횟수는 현재 모드의 이벤트에만 센다.
5. SHADOW/LIVE 모드이고 정규장 시간이면 아래를 실행한다. 연속 오류 보류 중이면 조회 회복 확인만 한다.
   - 잔고 동기화
   - 손절 감시
   - 청산 진행(신규 주문, 취소, 재주문)
6. 종료를 판정한다. 관리 잔여가 0이고 미확정 주문이 없으면 SOLD로 바꾼다. NEEDS_REVIEW 상태에서도 같다.
7. 장 종료 후 마감 요약을 보내고, heartbeat를 기록하고, 알림 outbox를 전송한 뒤 lease를 해제한다.

내부 주기는 다음과 같다.

| 작업 | 주기 |
|---|---|
| 시세 | 60초 |
| 주문 상태 | 30초 |
| 재주문 판단 | 주문 후 300초 |
| 잔고 | 300초, 그리고 매 전송 직전 |

## 상태 머신

### 포지션

```
ACTIVE ──(유효 시세 ≤ 손절가)──> EXITING ──(관리 잔여 0, 미확정 0)──> SOLD
ACTIVE ──(보유 0, 미확정 0)──> EXTERNAL_CLOSED   (재매수해도 복구 안 함)
ACTIVE/EXITING ──(원인 불명 수량 감소, 분할 의심, 급변 미확인)──> NEEDS_REVIEW
ACTIVE/EXITING/NEEDS_REVIEW ──(사용자 해제, 미확정 0)──> DISABLED
NEEDS_REVIEW ──(관리 잔여 0, 미확정 0)──> SOLD
```

- `auto_paused=1`은 상태와 별개다. 트리거, 신규 매도, 취소, 재주문만 멈추고 대조와 체결 반영은 계속한다.
- 이미 접수된 증권사 주문은 취소하지 않는다. 취소가 필요하면 키움 앱에서 한다.

### 청산 이벤트 (포지션당 열린 이벤트 1개)

```
트리거(cutoff 전) → OPEN                 트리거(cutoff 후) → CARRY_OVER
OPEN ──(장 종료, 모든 주문 종료·체결 확정)──> CARRY_OVER
OPEN ──(장 종료, 주문 미확정)──> OPEN 유지 (추적 계속)
CARRY_OVER/OPEN ──(다음 정규장, carry_over_count+1 ≤ 한도)──> OPEN (session_replace_count=0)
    · 미확정 주문이 남아 있으면: 새 주문 없이 추적만 (overnight 알림)
    · 주문 이력 없음 → 다음 주문 ORIGINAL, 이력 있음 → CARRY_OVER
carry_over_count 초과 / 재주문 한도 / 신규 거부 / 취소 거부 후 미체결 / 상한 초과 / 외부 미체결 주문
    / 매도가능 0 / 체결 수량 이상 / 외부 취소 / $1 미만 → NEEDS_REVIEW (신규 전송 중단, 대조 계속)
관리 잔여 0 + 미확정 0 → DONE
```

### 주문 (이벤트당 미확정 1개: DB 부분 유니크 인덱스)

```
claim(트랜잭션: lease, 스위치 재검증, 이벤트 OPEN, 직전 주문 FILLED/CANCELLED, 같은 (mode,ticker) 미확정 없음,
      주문 종류 일치, 재주문 한도) → SENDING
SENDING ─접수→ SUBMITTED ─조회→ PARTIAL_FILLED ─→ FILLED
SENDING ─OrderRejected→ REJECTED (이벤트 NEEDS_REVIEW)
SENDING ─OrderUnknown/예외/재시작→ UNKNOWN (재전송 없음, URGENT)
SENDING ─입력검증 실패(증권사 미전송)→ NOT_SENT (NEEDS_REVIEW)
SUBMITTED/PARTIAL ─(300초, cutoff 전, 재주문 스위치, 한도 남음)→ claim_cancel → CANCEL_SENDING
CANCEL_SENDING ─접수→ CANCEL_PENDING ─조회 CANCELLED(잔여 0)→ CANCELLED → 다음 주문 REPLACE
CANCEL_SENDING ─OrderRejected→ 원상태 복원 + cancel_rejected=1 → 즉시 재조회:
                 FILLED면 진행, 여전히 미체결이면 NEEDS_REVIEW (같은 주문 자동 취소 다시 안 함)
CANCEL_SENDING ─OrderUnknown/재시작→ CANCEL_UNKNOWN (재시도 없음, URGENT) ─조회 CANCELLED/FILLED→ 종료
UNKNOWN ─(주문번호 있음, 주문내역 대조 OPEN/PARTIAL)→ SUBMITTED/PARTIAL_FILLED (자동)
UNKNOWN/CANCEL_UNKNOWN ─(주문번호 없음)→ 사람이 주문번호를 입력해 증거 대조(resolve) → 이벤트 NEEDS_REVIEW
외부에서 취소된 주문: 장중이면 NEEDS_REVIEW, 주문 세션 마감 후면 '소멸'로 보고 이월 규칙 적용 (WARN 알림)
```

### 주문 종류와 카운터

| 종류 | 언제 | 재주문 카운터 |
|---|---|---|
| ORIGINAL | 이벤트 첫 주문, `attempt_no=0` | 증가 없음 |
| REPLACE | 같은 세션에서 취소 확정 후, 또는 FILLED인데 관리 잔여가 남은 경우 | `session_replace_count`, `event_replace_count` +1 |
| CARRY_OVER | 다음 정규장의 첫 주문 | 증가 없음 (`carry_over_count`는 세션 진입 때 +1) |

- `event_replace_count`는 재시작이나 거래일 변경으로 초기화하지 않는다.
- 모든 한도 검사는 선점 트랜잭션 안에서 한다.

## 시세 규칙

- `usa20101` 현재가를 쓴다. 시각은 `dt`+`bid_tm`(분 단위, ET)이다. 다음 조건을 모두 통과해야 한다.
  - 당일 정규장 거래일
  - 개장 이후 시각
  - 120초 이내
  - 양수
- 시각이 없는 응답은 쓰지 않는다. `usa20100` 대체 응답이 여기에 해당한다.
- 직전 유효값 대비 ±25% 이상 변했으면 즉시 재조회한다. 두 값이 2% 이내로 일치하는지 확인하고 잔고를 다시 확인한다.
  - 일치하면 손절을 진행한다.
  - 불일치, 재조회 실패, 분할 의심이면 NEEDS_REVIEW로 바꾼다.
- `bid_tm` 갱신만으로 실시간이나 거래 가능 여부를 확정하지 않는다.
- 1호가 필드는 미검증이다.
  - `KIWOOM_US_ORDERBOOK_BID_FIELD`와 `KIWOOM_US_ORDERBOOK_ASK_FIELD`(account2와 공용 env)를 함께 설정하면 `min(현재가, 매수1호가)`를 쓴다.
  - **LIVE에서는** 추가로 `AUTO_STOP_US_VERIFIED_CONTRACTS`에 `USA20101_BID`가 있어야 매수1호가를 쓴다. 없으면 env가 설정돼 있어도 현재가만 쓴다. SHADOW는 env만으로 사용한다.
  - 응답이 비정상이면 주문하지 않는다.
- 기준가가 $1 미만이면 NEEDS_REVIEW로 바꾼다. 주문 클라이언트가 센트 단위만 지원하기 때문이다.

## 동시성과 중복 방지

- lease
  - `auto_stop_runtime.lease`에 owner와 만료 시각을 두고 `BEGIN IMMEDIATE`로 처리한다. 만료는 90초다.
  - 모든 증권사 호출 전후에 갱신한다.
  - 갱신에 실패하면 heartbeat와 알림을 포함한 이후 모든 쓰기를 멈춘다.
- 선점
  - 주문·취소 직전에 한 트랜잭션에서 lease 소유와 실거래 스위치를 다시 확인하고 SENDING/CANCEL_SENDING을 기록한다.
- 재시작
  - tick 시작 시 남아 있는 SENDING/CANCEL_SENDING은 이전 작업자의 것으로 보고 UNKNOWN 계열로 바꾼다.
- 늦은 응답
  - 다른 작업자가 이미 UNKNOWN으로 바꿨으면 상태를 바꾸지 않는다. `details.late_response`에 증거만 남긴다.
- 주문번호 유니크
  - 범위는 `(mode, account_profile, exchange, order_date(NY), order_number)`다.
- 체결 반영
  - 증권사 누적 체결과의 차이만 반영한다. ledger PK는 `(order_id, cum_filled_qty)`다.
  - 누적값이 감소하면 이상으로 보고 NEEDS_REVIEW로 바꾼다.
- 다른 주문과 충돌 방지
  - 같은 SQLite의 `scanner_buy_orders`에 account1 같은 종목의 미확정·미체결 매수가 있으면 LIVE 매도를 보류한다.
  - 같은 종목에 우리 것이 아닌 미체결 주문(수동 주문 포함)이 주문내역에 있으면 NEEDS_REVIEW로 바꾼다.
- 단일 uvicorn 프로세스를 가정한다. 다중 프로세스에서도 lease와 유니크 인덱스로 이중 전송을 막는다.

## 등록 검증 (`POST /api/auto-stop/positions`)

- `holding_id`는 `kiwoom:account1:US:{ticker}`여야 하고, 확인 티커를 다시 입력해야 한다.
- 스캔 결과(DB 값)는 다음 조건을 모두 충족해야 한다.
  - `market=US`이고 티커가 일치한다.
  - `strict_filter_passed is True`
  - `stop_loss > 0`
- 신호가 30일보다 오래됐으면 `acknowledge_stale=true`가 필요하다.
- 등록 모드는 현재 `AUTO_STOP_US_MODE`와 같아야 한다. LIVE로 등록하려면 실거래 게이트가 모두 켜져 있어야 한다.
- 잔고는 완전 조회(`complete=True`)여야 하고, 같은 종목 행은 하나여야 한다.
- 관리 수량은 보유 수량 이하여야 한다. 추가매수분은 관리하지 않는다.
- 세 거래소(ND/NY/NA) 중 정확히 하나에서만 시세가 일치해야 한다.
  - 장중에는 시세 검증을 통과해야 한다.
  - 현재가가 손절가보다 높아야 한다. 이미 이탈한 종목은 등록하지 않는다.
- 다음 조건이면 등록하지 않는다.
  - 1회 주문 상한(수량, 금액) 초과
  - 등록 종목 수 한도 초과
  - 같은 모드·종목의 ACTIVE/EXITING/NEEDS_REVIEW 포지션이 이미 있음
  - 같은 종목에 미확정 주문이 있음
- 신호 스냅샷(JSON)을 보존한다.

## 환경변수

| 변수 | 기본 | 설명 |
|---|---|---|
| `AUTO_STOP_US_MODE` | `OFF` | `OFF` / `SHADOW` / `LIVE` |
| `AUTO_STOP_US_REPLACE_ENABLED` | `false` | 미체결 자동 취소·재주문. 꺼져 있으면 알림만 보낸다 |
| `AUTO_STOP_US_TICK_SEC` | 30 | 스케줄러 주기. 재시작해야 반영된다 |
| `AUTO_STOP_US_PRICE_INTERVAL_SEC` / `_STATUS_INTERVAL_SEC` / `_REPLACE_AFTER_SEC` / `_BALANCE_SYNC_SEC` | 60 / 30 / 300 / 300 | 내부 주기 |
| `AUTO_STOP_US_MAX_REPLACES_PER_SESSION` / `_PER_EVENT` | 3 / 6 | |
| `AUTO_STOP_US_MAX_CARRY_OVER_SESSIONS` | 1 | |
| `AUTO_STOP_US_SLIP_BPS` | 50 | 지정가 = 기준가 × (1 − bps) |
| `AUTO_STOP_US_SESSION_START_OFFSET_MIN` / `_CUTOFF_MIN` | 0 / 5 | |
| `AUTO_STOP_US_QUOTE_MAX_AGE_SEC` / `_QUOTE_JUMP_PCT` | 120 / 25 | |
| `AUTO_STOP_US_MAX_CONSECUTIVE_ERRORS` | 5 | 종류별(시세/잔고/주문상태/주문내역) 연속 오류. 초과하면 신규 진입을 보류한다 |
| `AUTO_STOP_US_LEASE_SEC` | 90 | |
| `AUTO_STOP_US_MAX_ORDER_QTY` / `_MAX_ORDER_NOTIONAL_USD` | 1000 / 20000 | 초과하면 NEEDS_REVIEW |
| `AUTO_STOP_US_MAX_ACTIVE_POSITIONS` | 20 | |
| `AUTO_STOP_US_STALE_SIGNAL_DAYS` | 30 | |
| `AUTO_STOP_US_EXTRA_CLOSED_DATES` / `_EXTRA_EARLY_CLOSE_DATES` | 없음 | `YYYY-MM-DD,...` 임시 휴장, 조기폐장 |
| `AUTO_STOP_US_SHADOW_FILL_MODEL` | `NEXT_QUOTE` | `IMMEDIATE` / `NEXT_QUOTE` / `STRESS`. SHADOW 가상 체결 방식 |
| `AUTO_STOP_US_VERIFIED_CONTRACTS` | 없음 | 운영자가 검증을 마친 API 계약 이름 목록(쉼표). 아래 "API 계약 게이트" |
| `KIWOOM_TIMEOUT_SECONDS` | 15 | 기존 키움 HTTP timeout. 자동 손절 주문 브로커는 30초를 넘으면 생성을 거부한다 (lease 90초 안에 응답 판정) |

LIVE 전송은 다음을 **모두** 충족해야 한다. 주문·취소 직전마다 다시 확인한다.

- `AUTO_STOP_US_MODE=LIVE`
- `KIWOOM_WEB_ENABLED=true`
- `KIWOOM_TRADING_ENABLED=true`
- `ACCOUNT1_ORDER_ENABLED=true`
- `SCANNER_ORDER_JOURNAL_PATH` 설정
- account1 프로필이 `mode=real`이고 `REAL_BASE_URL`
- 전체 긴급 중지가 꺼져 있음
- `AUTO_STOP_US_VERIFIED_CONTRACTS`에 `USA20101_QUOTE_TIME`, `UST21070_SELLABLE`, `UST21150_STATUS`가 모두 있음

자동 취소는 위 조건에 더해 `AUTO_STOP_US_REPLACE_ENABLED=true`와 `UST20003_PARTIAL_CANCEL` 검증이 필요하다.

## API 계약 게이트 (`AUTO_STOP_US_VERIFIED_CONTRACTS`)

검증되지 않은 증권사 응답 해석에 기대는 LIVE 기능은 코드에서 막는다. 운영자가 아래 절차로 확인한 이름만 env에 적는다. 이름이 틀리면 무시된다.

| 이름 | 확인 방법 | 없을 때 LIVE 동작 |
|---|---|---|
| `USA20101_QUOTE_TIME` | 조회 전용 스크립트 `quote` | LIVE 신규 매도 차단 |
| `UST21070_SELLABLE` | 조회 전용 스크립트 `balance` | LIVE 신규 매도 차단 |
| `UST21150_STATUS` | 조회 전용 스크립트 `orders` (과거 수동 주문 내역으로) | LIVE 신규 매도 차단 |
| `USA20101_BID` | 조회 전용 스크립트 `quote` + 앱 호가 비교 | 매수1호가 미사용(현재가 기준) |
| `UST20001_SELL` | 실주문 필요: 1주 실매도 절차 | LIVE 주문 수량 1주 상한 (초과 이벤트는 NEEDS_REVIEW, 등록·LIVE 전환도 1주까지만) + **LIVE 포지션 1개 제한** (ACTIVE·EXITING·NEEDS_REVIEW LIVE 포지션이 있으면 새 LIVE 등록·전환 거부) |
| `UST20003_PARTIAL_CANCEL` | 실주문 필요: 부분체결 후 취소 | 자동 취소·재주문 차단 (알림만) |
| `POST_CLOSE_EXPIRY` | 실주문 필요: 마감까지 미체결 주문 관찰 | 전날 주문이 있던 이벤트의 이월 재주문 대신 NEEDS_REVIEW |

`GET /api/auto-stop/health`의 `missing_live_contracts`, `live_qty_cap`, `live_cancel_block_reason`에서 현재 상태를 본다.

## SHADOW 가상 체결 모델

SHADOW는 LIVE와 같은 상태 머신을 쓰고 브로커만 `ShadowBroker`로 바꾼다. `ShadowBroker`는 주문 클라이언트를 갖지 않으며 실계좌 시세·잔고 조회만 한다.

| 모델 | 체결 판정 |
|---|---|
| `NEXT_QUOTE` (기본) | 주문을 만든 시세보다 **뒤의** 시세(`as_of`가 더 큼)에서 현재가 ≥ 지정가면 전량 체결. 같은 시세로는 체결하지 않는다 |
| `IMMEDIATE` | 주문 시점 시세로 바로 판정 (낙관적, 테스트용) |
| `STRESS` | 원주문은 60초 뒤 절반만 체결, 취소는 요청 90초 뒤에 확정, 재주문은 `NEXT_QUOTE`. 부분체결·취소 지연·재주문 경로를 SHADOW에서 돌려 보기 위한 모델 |

- 가상 체결가는 지정가로 기록한다. 실제 체결가가 아니다.
- **가상 체결 결과는 실제 체결률이나 실현손익으로 보고하지 않는다.** 일일 보고와 화면에 이 문구가 함께 표시된다.

## 전체 긴급 중지 (kill switch)

- 화면의 `전체 긴급 중지` 또는 `POST /api/auto-stop/kill-switch {enabled, confirmation, reason}`.
  - 설정: 확인 문구 `STOP` + 사유 필수. 해제: `RESUME`.
  - 저널 SQLite(`auto_stop_runtime.kill_switch`)에 저장돼 **서버 재시작 후에도 유지**된다. env 변경·재시작 없이 즉시 반영된다.
  - 설정·해제는 감사 기록(`auto_stop_audit`, 요청자 IP 포함)과 URGENT 알림을 남긴다.
- 켜져 있으면:
  - 신규 매도, 자동 취소, 재주문을 하지 않는다. 주문·취소 선점 트랜잭션 안에서 확인하므로 판단 직후 켜져도 전송되지 않는다.
  - 주문 상태 대조와 잔고 동기화는 계속한다.
  - **이미 접수된 주문은 자동으로 취소하지 않는다.** 필요하면 키움 앱에서 취소한다.
- **긴급 중지는 이미 전송 중이거나 증권사에 접수된 주문의 취소를 보장하지 않는다.** 선점 트랜잭션을 지나 HTTP 요청이 나간 주문(SENDING)과 접수된 주문(SUBMITTED·PARTIAL_FILLED·CANCEL_*)은 그대로 체결될 수 있다. 긴급 중지 직후 화면의 진행 중 주문과 키움 앱 주문내역을 확인하고, 멈춰야 할 주문은 앱에서 직접 취소한다.
- 기존 env 스위치와 종목별 `일시중지`는 그대로 동작한다.

## 운영 절차

1. **OFF 배포**
   - 기본값 그대로 배포한다 (`AUTO_STOP_US_MODE` 미설정 = OFF). 앱을 재시작하고 `GET /api/auto-stop/health`에서 `mode=OFF`, `journal=true`, `kill_switch=null`을 확인한다.
   - OFF에서는 등록 폼이 보이지 않고 신규 감시·주문이 없다. 이미 등록된 포지션의 잔고 동기화와 기존 주문 대조는 계속한다.
   - 저널 백업 cron을 건다 (아래 "저널 백업").
2. **SHADOW 시작** (최소 5거래일)
   - `AUTO_STOP_US_MODE=SHADOW` (모델 기본 `NEXT_QUOTE`)로 재시작한다. 실거래 스위치는 건드리지 않는다.
   - 화면에서 account1 미국 보유종목 → Scanner 신호를 골라 등록한다.
   - 매일 미국 마감 후 "일일 운영 보고"를 확인한다 (알림 + 화면 `오늘 운영 보고` + `GET /api/auto-stop/report`).
   - 같은 기간에 조회 전용 계약 확인을 마친다 (아래 "검증 절차").
3. **LIVE 전환** — 별도 사용자 승인 후. 아래 "1주 실매도 검증 절차"부터 한다.
   - SHADOW 포지션은 화면의 `LIVE 전환…`으로 하나씩 새 LIVE 포지션으로 만든다. 확인 패널에서 수량·손절가를 보고 티커를 다시 입력한다. 서버가 보유 수량, 손절가, 시세를 다시 검증한다.
     - 이미 손절가를 이탈했으면 NEEDS_REVIEW로 등록된다. Shadow 주문과 체결은 복사하지 않는다.
   - `AUTO_STOP_US_REPLACE_ENABLED`는 부분체결 취소 계약을 확인한 뒤에 켠다.
4. **비상 중지와 복구**
   - 즉시 전체 중지: 화면 `전체 긴급 중지`(STOP + 사유). 재시작 불필요.
   - 종목별 중지: `일시중지`.
   - 완전 차단: `AUTO_STOP_US_MODE=OFF` 또는 `ACCOUNT1_ORDER_ENABLED=false` 후 재시작 (기존 주문 대조는 계속).
   - 이미 접수된 주문은 **키움 앱에서 직접 취소**한다. 이 기능에는 수동 취소 버튼이 없다.
   - UNKNOWN/CANCEL_UNKNOWN: 키움 앱 주문내역에서 주문번호를 찾아 화면의 `대조`로 증거 확인. 확인 후에도 이벤트는 NEEDS_REVIEW로 남고 자동으로 이어가지 않는다.
   - 복구: 원인 확인 → 증권사 주문·잔고와 화면 상태가 일치하는지 확인 → NEEDS_REVIEW 포지션은 `해제` 후 필요하면 재등록 → `RESUME`으로 긴급 중지 해제.
   - 저널 파일은 지우거나 백업으로 덮어쓰지 않는다.

## SHADOW 일일 운영 보고

미국 정규장 마감 후 첫 tick에서 알림으로 보내고(`session:{day}:{mode}:summary`), 화면과 `GET /api/auto-stop/report?day=YYYY-MM-DD&mode=SHADOW`로 다시 볼 수 있다. 손절이 없는 날에도 감시가 정상이었는지 보여 주는 것이 목적이다.

| 항목 | 내용 |
|---|---|
| 감시 | 종목 수, 종목별 기대 감시 횟수 대비 정상 시세 횟수, 누락, 시세 실패·무효, 최대 시세 지연, 마지막 정상 시세 |
| tick | 기대 tick 수 대비 실제, 최대 tick 간격 |
| API | 오류 종류별 횟수, HTTP 429 횟수 |
| 손절·주문 | 트리거 수, 주문 수(원주문/재주문/이월), 체결·미체결·취소 |
| 가상 체결 | 체결가(지정가 기준) vs 손절가 % |
| 미해결 | 열린 이벤트, 미확정 주문, 확인 필요 |
| 알림·heartbeat | 알림 성공/실패/대기, heartbeat 정상 여부 |

누락·오류·미해결이 있으면 WARN, 없으면 INFO로 보낸다. 휴장일은 `trading_day=false`.

## 저널 백업

- `python -m scripts.auto_stop_journal_backup --dest /var/backups/mystockapp` (기본 30개 보관). SQLite backup API로 원본을 읽기 전용으로 열어 일관된 사본을 만들고 무결성 검사를 한다. 원본 폴더로는 백업하지 않는다.
- 권장: 하루 2회 cron (미국 개장 전, 마감 후).
- **백업을 원본 위치에 복원하지 않는다.** 과거 사본으로 덮으면 이미 보낸 주문 기록이 사라져 중복 전송 방지가 깨진다. 백업은 사고 조사용이다.

## 거래소 캘린더

- 자체 규칙 구현(`trading/auto_stop/us_calendar.py`, 2020–2040, 범위 밖은 닫힘)을 유지한다.
- 2026-11-24에 NYSE 공식 일정(nyse.com/markets/hours-calendars)과 2026–2028 휴장·조기폐장을 대조해 모두 일치했다 (`tests/test_us_calendar.py`).
- `exchange_calendars`와 비교: 장점은 커뮤니티 유지보수, 단점은 새 의존성(pandas 등)과 버전 업데이트 의존, 임시 휴장도 결국 사후 반영이라는 점. 공식 일정과 일치하고 의존성이 없으므로 교체하지 않는다.
- 임시 휴장(예: 2025-01-09 국가 애도일)은 어떤 라이브러리도 미리 알 수 없다. NYSE 공지를 보면 `AUTO_STOP_US_EXTRA_CLOSED_DATES` / `_EXTRA_EARLY_CLOSE_DATES`에 넣고 재시작한다.
- 매년 말 다음 해 공식 일정과 테스트 표를 갱신한다.

## 외부 heartbeat

- 알림이 오지 않는 장애에 대비해, 외부 업타임 모니터가 `GET /api/auto-stop/health`(Bearer `SITES_API_KEY`)를 주기적으로 확인하게 한다.
- 정규장 중 다음이면 경보를 보낸다.
  - `heartbeat_age_sec`가 120초 초과
  - `unknown_orders > 0`
  - `needs_review > 0`
- 장 시작 "감시 시작" 알림이 오지 않으면 감시가 멈춘 신호다.

## 미검증 API 계약과 검증 절차

| 항목 | 상태 | 현재 처리 | 검증 |
|---|---|---|---|
| `usa20101` 시세 시각(`dt`+`bid_tm`) 갱신 주기 | 미확인 | LIVE 차단 (`USA20101_QUOTE_TIME`) | 조회 전용 |
| `usa20101` 매수1호가 필드 | 미확정 | 현재가 기준 (`USA20101_BID`) | 조회 전용 |
| `ust21070` `sell_alowq`가 앱 매도가능수량과 같은지, 미체결 매도 차감 | account1 미확인 | LIVE 차단 (`UST21070_SELLABLE`) | 조회 전용 (차감 여부는 미체결 매도가 있을 때만 확인 가능) |
| `ust21150` 주문·체결·잔여·취소 수량과 상태명 | 정규화는 있음, account1 실응답 미대조 | LIVE 차단 (`UST21150_STATUS`) | 조회 전용 (과거 수동 주문 내역) |
| `ust21150` 체결 평균가 | 정규화에 필드 없음 | `avg_fill_price` NULL, 보고는 지정가 기준 | 조회 전용 (필드 존재 확인) |
| account1 실계좌 `ust20001` 매도 | 미전송 | LIVE 1주 상한 (`UST20001_SELL`) | **실주문 필요** |
| `ust20003` 부분체결 후 취소 | 모의 전량 미체결 취소만 확인 | 자동 취소 차단 (`UST20003_PARTIAL_CANCEL`) | **실주문 필요** |
| 장 마감 후 미체결 지정가 처리 | 미확인 | 전날 주문이 있던 이월은 NEEDS_REVIEW (`POST_CLOSE_EXPIRY`) | **실주문 필요** |
| 주문번호의 날짜별 재사용 | 미확인 | 유니크 키에 NY 주문일 포함 | 주문내역 장기 관찰 |
| 거래정지·서킷브레이커 신호 | API 필드 미확인 | 시세 나이로만 감지. 주문 거부 → NEEDS_REVIEW | 발생 시 관찰 |
| 키움 호출 한도 | 문서 없음 | 조회만 429 백오프(2/4/8/16초), 종목 수 상한 20, 일일 보고에 429 집계 | SHADOW 관찰 |

### 조회 전용 확인 (VM, 정규장 중, 주문 없음)

`scripts/auto_stop_readonly_probe.py`는 주문 모듈을 import하지 않는다 (테스트로 확인).

1. `python -m scripts.auto_stop_readonly_probe quote --ticker <보유종목> --exchange ND --samples 5 --interval 20`
   - `dt`/`bid_tm`이 분 단위로 갱신되고 `age`가 120초 이내, `cur_prc`가 키움 앱과 같으면 `USA20101_QUOTE_TIME`.
   - 필드 목록 중 앱의 매수1호가와 같은 값의 필드를 찾으면 `KIWOOM_US_ORDERBOOK_BID_FIELD`/`_ASK_FIELD`에 넣고 `USA20101_BID`.
2. `python -m scripts.auto_stop_readonly_probe balance --ticker <보유종목>`
   - 행이 정확히 1개이고 `sell_alowq`가 앱 매도가능수량과 같으면 `UST21070_SELLABLE`.
3. `python -m scripts.auto_stop_readonly_probe orders --date <과거 수동 주문이 있던 YYYYMMDD>`
   - 체결·부분체결·취소 주문 각각에서 `ord_qty`/`cntr_qty`/`ord_remnq`/`cncl_qty`/`ord_stat_nm`과 정규화 결과가 앱 주문내역과 같으면 `UST21150_STATUS`. 가격 후보 필드는 기록만 한다.
4. 결과를 이 문서에 날짜와 함께 기록하고 env에 이름을 추가한 뒤 재시작한다.

### 1주 실매도 검증 절차 (별도 사용자 승인 후, 사용자가 직접 수행)

1. 조회 전용 3개 계약이 검증돼 있고 kill switch가 꺼져 있는지 확인한다. `AUTO_STOP_US_REPLACE_ENABLED=false` 유지.
2. 검증 대상은 **포지션 1개, 주문당 1주**로 제한한다 (코드: `UST20001_SELL` 미검증이면 LIVE 포지션이 하나라도 있을 때 새 LIVE 등록·전환을 거부). 잃어도 되는 1주 보유 종목을 고른다. account1에 같은 종목의 수동·Scanner 미체결 주문이 없어야 한다.
3. `AUTO_STOP_US_MODE=LIVE`, `KIWOOM_TRADING_ENABLED=true`, `ACCOUNT1_ORDER_ENABLED=true`로 재시작한다. (`UST20001_SELL` 미검증이라 수량은 자동으로 1주 상한)
4. 그 종목을 관리 수량 1로 등록하되 손절가가 현재가 바로 아래인 실제 Scanner 신호를 쓴다. 손절가를 임의로 만들지 않는다. 적당한 신호가 없으면 자연 발생을 기다린다.
5. 트리거 후 확인: 원주문 SUBMITTED, 주문번호, 지정가 = 현재가 × 0.995 내림, 키움 앱 주문내역 일치, 체결 후 FILLED·SOLD, 잔고 0.
6. 문제가 있으면 즉시 `전체 긴급 중지`, 키움 앱에서 주문 취소.
7. 결과를 이 문서에 기록하고 `UST20001_SELL`을 추가한다. 부분체결 취소(`UST20003_PARTIAL_CANCEL`)와 마감 후 처리(`POST_CLOSE_EXPIRY`)는 같은 방식으로 별도 승인 후 확인한다.

## 백테스트와 실거래 차이

- 백테스트 S01은 일봉 저가가 손절가 이하이면 손절가에 체결된다고 가정한다(갭이면 시가, slippage 10bp).
- 실거래는 다르게 동작한다.
  - 1분 현재가로 감시한다.
  - 현재가 −0.5% 지정가로 주문한다.
  - 미체결이면 재주문하고, 이월은 1회까지다.
- 그래서 실현가는 백테스트보다 불리할 수 있다. 50bp 민감도 결과(`backtest/exit_strategy` 보고서)를 참고한다.
- 분봉 기반 체결률 검증은 표본이 부족해 만들지 않았다. SHADOW 기록(트리거 시세, 가상 지정가)으로 관찰한다.
