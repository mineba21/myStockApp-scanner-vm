# account1 미국 S01 자동 손절 매도

2026-11-24 작성. 사용자 승인 범위는 **개발**이며, 실계좌 자동 매도(LIVE) 활성화는 별도 결정이다.

## 무엇을 하나

- 사용자가 자유투자(account1) 미국 보유종목을 스캐너 신호(Strict 통과, `stop_loss` 있음)와 연결해 등록한다.
- 등록 시점의 Scanner 손절가(S01)를 고정한다. 이후 재스캔으로 바뀌지 않는다.
- 정규장에서 유효한 당일 시세가 손절가 이하이면, 사람 확인 없이 지정가로 매도한다.
  - 기준가: 최신 현재가
  - 지정가: 기준가 × (1 − 0.5%), 센트 내림
- 미체결이면 다음 순서로 잔여 수량만 다시 주문한다. 이 동작은 LIVE에서는 `AUTO_STOP_US_REPLACE_ENABLED`, SHADOW(가상)에서는 `AUTO_STOP_US_SHADOW_REPLACE_ENABLED`를 켰을 때만 한다.
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
| `trading/auto_stop/quotes.py` | 시세 검증(`validate_detail`: 일반 감시·회복 조회·주문 직전 공용), 기준가와 지정가 계산(Decimal) |
| `trading/auto_stop/quote_health.py` | 시세 오류 분류(SYSTEM/SYMBOL/UNKNOWN), 민감정보 제거, 종목별 장애 상태 머신, 감시 슬롯 (순수 함수) |
| `trading/auto_stop/store.py` | `SCANNER_ORDER_JOURNAL_PATH` SQLite 접근(DDL 없음, `mode=rw`), 조건부 전이, 선점, fill ledger, lease, 알림 outbox |
| `trading/auto_stop/schema.py` | 런타임 스키마 검증(읽기 전용), 저널 전용 Migration 진입점 |
| `trading/auto_stop/journal_migrations/` | 저널 전용 Alembic 환경 (`as0001`, 버전 표 `auto_stop_alembic_version`) |
| `scripts/auto_stop_journal_migrate.py` | 배포 작업: 백업·사전 점검·upgrade·사후 검증 |
| `trading/auto_stop/engine.py` | 상태 머신(tick) |
| `trading/auto_stop/brokers.py` | `LiveStopSellBroker`(account1 실계좌 전용), `ShadowBroker`(가상 주문) |
| `trading/auto_stop/service.py` | 등록, 일시중지, 해제, LIVE 전환, UNKNOWN 증거 해제, 헬스 |
| `trading/auto_stop/runner.py` | 스케줄러 진입점, 실 브로커 조립, Slack/Telegram 알림 |
| `web/auto_stop_api.py` | `/api/auto-stop/*` (`SITES_API_KEY` 인증) |
| `scheduler.py` | 30초 `IntervalTrigger` 작업 1개(`auto_stop_us_tick`, `max_instances=1`, coalesce) |

- 테이블은 저널 SQLite에 **저널 전용 Migration으로만** 만든다 (아래 "저널 스키마와 Migration"). 런타임은 DDL을 실행하지 않는다.
  - 테이블: `auto_stop_positions`, `auto_stop_exit_events`, `auto_stop_orders`, `auto_stop_fill_ledger`, `auto_stop_runtime`, `auto_stop_alerts`, `auto_stop_daily_stats`, `auto_stop_audit`
  - 운영 DB와 운영 Alembic(`alembic/`)은 변경하지 않는다.
  - 저널을 삭제·초기화하거나 과거 사본으로 덮어쓰지 않는다. 덮어쓰면 중복 전송 방지가 깨진다.

## tick (30초)

1. lease를 획득한다. 실패하면 종료한다.
2. 잔여 SENDING은 UNKNOWN으로, CANCEL_SENDING은 CANCEL_UNKNOWN으로 바꾼다. 재전송하지 않는다.
3. 미확정 주문 전체를 대조한다. 이 단계는 **모든 모드에서, 주문 행의 mode 기준으로** 한다.
   - LIVE 주문은 OFF나 SHADOW 모드에서도 실계좌 주문내역으로 대조한다.
   - SHADOW 주문은 가상 브로커로만 대조한다.
4. 세션을 이월한다. 이월 횟수는 현재 모드의 이벤트에만 센다.
5. SHADOW/LIVE 모드이고 정규장 시간이면 아래를 실행한다 (상세: "시세 장애 격리와 복구").
   - 전체 보류 사유를 계산한다 (잔고·주문상태·주문내역 연속 오류, 시세 공통 오류, 광범위 조회 실패).
   - 잔고 동기화 (잔고 보류 중이면 잔고 회복 조회만)
   - 시세 감시: 종목마다 예정 시각에 조회한다. 정상 종목은 일반 감시(트리거 가능), 장애 종목과 전체 보류 중인 종목은
     회복 조회(같은 검증, 트리거·주문 없음). 한 종목의 예외는 다음 종목 조회를 막지 않는다.
   - 청산 진행(신규 주문, 취소, 재주문): 전체 보류가 없을 때만. 시세 장애 종목은 주문·취소하지 않는다.
6. 종료를 판정한다. 관리 잔여가 0이고 미확정 주문이 없으면 SOLD로 바꾼다. NEEDS_REVIEW 상태에서도 같다.
7. 장 종료 후 마감 요약을 보내고, heartbeat를 기록하고, 알림 outbox를 전송한 뒤 lease를 해제한다.

내부 주기는 다음과 같다.

| 작업 | 주기 |
|---|---|
| 시세 | 60초 (종목별 예정 시각 슬롯. 실패하면 같은 슬롯에서 다음 tick 1회 재시도) |
| 시세 회복 조회 | 60초 (종목별. 429 반복 시 120·240초) |
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
| `AUTO_STOP_US_REPLACE_ENABLED` | `false` | **LIVE 전용** 미체결 자동 취소·재주문. 꺼져 있으면 알림만 보낸다. SHADOW에는 영향 없음 |
| `AUTO_STOP_US_SHADOW_REPLACE_ENABLED` | `false` | **SHADOW 전용** 가상 취소·재주문. LIVE에는 영향 없음. SHADOW 브로커는 주문 클라이언트가 없어 어떤 조합에서도 실주문·실취소가 나가지 않는다 |
| `AUTO_STOP_US_TICK_SEC` | 30 | 스케줄러 주기. 재시작해야 반영된다 |
| `AUTO_STOP_US_PRICE_INTERVAL_SEC` / `_STATUS_INTERVAL_SEC` / `_REPLACE_AFTER_SEC` / `_BALANCE_SYNC_SEC` | 60 / 30 / 300 / 300 | 내부 주기 |
| `AUTO_STOP_US_MAX_REPLACES_PER_SESSION` / `_PER_EVENT` | 3 / 6 | |
| `AUTO_STOP_US_MAX_CARRY_OVER_SESSIONS` | 1 | |
| `AUTO_STOP_US_SLIP_BPS` | 50 | 지정가 = 기준가 × (1 − bps) |
| `AUTO_STOP_US_SESSION_START_OFFSET_MIN` / `_CUTOFF_MIN` | 0 / 5 | |
| `AUTO_STOP_US_QUOTE_MAX_AGE_SEC` / `_QUOTE_JUMP_PCT` | 120 / 25 | |
| `AUTO_STOP_US_MAX_CONSECUTIVE_ERRORS` | 5 | 연속 오류 임계값. 잔고/주문상태/주문내역/시세 공통 카운터는 도달하면 전체 신규 진입 보류, 종목별 시세 카운터는 도달하면 그 종목만 장애(FAULT) |
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
- 가상 주문마다 만든 시점의 `fill_model`과 `shadow_replace_enabled`를 기록한다(`details.shadow`). 운영 중 모델을 바꿔도 기존 주문은 만든 모델로 판정·집계한다.
- 모델·SHADOW 재주문 스위치가 바뀌면 첫 tick에서 감사 기록(`auto_stop_audit`, action `shadow_config`)과 WARN 알림을 남긴다.
- **NEXT_QUOTE와 STRESS 결과를 합쳐 성과로 평가하지 않는다.** 일일 보고는 모델별로 따로 집계한다.

### STRESS 검증 절차 (선택, NEXT_QUOTE 5거래일 운영과 분리)

1. NEXT_QUOTE 5거래일 보고를 먼저 확정한다.
2. 장 시작 전에 `AUTO_STOP_US_SHADOW_FILL_MODEL=STRESS`, `AUTO_STOP_US_SHADOW_REPLACE_ENABLED=true`로 재시작한다. `AUTO_STOP_US_REPLACE_ENABLED`(LIVE)는 `false` 그대로 둔다.
3. 그날 보고의 `[STRESS]` 행에서 부분체결 → 가상 취소(90초 지연) → 재주문 → 종료 흐름을 확인한다.
4. 장 마감 후 `NEXT_QUOTE`, `SHADOW_REPLACE_ENABLED=false`로 되돌린다.
5. STRESS로 진행된 SHADOW 포지션은 손절 진행·종료 상태라 LIVE로 전환할 수 없다(ACTIVE만 전환 가능). 전환하더라도 Shadow 주문·이벤트는 복사하지 않는다.

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

## 저널 스키마와 Migration

- 자동 손절 테이블·인덱스는 `SCANNER_ORDER_JOURNAL_PATH` SQLite에만 있고, **저널 전용 Alembic**(`trading/auto_stop/journal_migrations`, 리비전 `as0001`, 버전 표 `auto_stop_alembic_version`)으로만 만든다. 운영 DB의 `alembic/`, `alembic_version`, `init_db()`와 설정·이력을 공유하지 않는다.
- **런타임(스케줄러 tick, 웹 API, `AutoStopStore` 생성자)은 DDL을 실행하지 않는다.** 스키마 확인은 저널을 `mode=ro`로 열어 버전·객체만 보고, 저널 파일이 없으면 만들지 않는다. store 연결은 `mode=rw`라 파일이 없으면 실패한다.
- Migration은 앱 시작·스케줄러·API에서 자동 실행하지 않는다. 배포 작업으로만 실행한다.

| 스키마 상태 | OFF | SHADOW | LIVE |
|---|---|---|---|
| 준비됨 (`as0001`) | 기존 주문 대조·잔고 동기화 | 정상 | 정상 (다른 LIVE 조건 충족 시) |
| 없음 (자동 손절 객체 0) | 건너뜀, health에 Migration 필요. Scanner 정상 | 감시·가상 주문 없음, WARN 알림 (하루 1회) | 모든 신규 매도·취소·재주문 없음, URGENT 알림 |
| 버전 불일치·버전 없음, 구조는 대조 가능 | 기존 주문 대조만, 신규 주문 없음 (LIVE 미확정 주문이 있으면 URGENT) | 대조만, WARN | 대조만, URGENT |
| 구조 손상 (대조 불가) | URGENT: 키움 앱에서 직접 확인 | 같음 | 같음 |

LIVE 전송 조건(`live_order_block_reason`)에도 스키마 확인이 들어 있어, 주문·취소 선점 직전에 다시 검사한다.

### Migration 실행 (배포 작업, 앱 정지 상태)

```bash
python -m scripts.auto_stop_journal_migrate --backup-dir /var/backups/mystockapp --check   # 사전 점검만 (변경 없음)
python -m scripts.auto_stop_journal_migrate --backup-dir /var/backups/mystockapp           # 실행
```

- 사전: 대상 경로 확인(파일이 없으면 중단, 새 저널은 `--allow-create`일 때만), 무결성 검사, 테이블별 건수·내용 해시, 미확정 주문(`scanner_buy_orders` SENDING/UNKNOWN, 자동 손절 미확정) 확인, 기존 스키마 기록. 미확정 주문이 있으면 증권사 주문내역을 확인한 뒤 `--allow-unresolved`로만 진행한다.
- 백업: `scripts/auto_stop_journal_backup`으로 읽기 전용 원본에서 사본을 만들고 무결성을 검사한다 (권한 600).
- upgrade: 전체가 `BEGIN IMMEDIATE` 트랜잭션 하나다. 실패하면 DDL과 버전 기록이 함께 롤백되고 원본은 그대로다.
  - 자동 손절 객체가 없으면 v1 DDL을 실행한다.
  - 이전 자체 DDL 방식으로 이미 만들어진 저널은 객체 목록·정의(`sqlite_master`)가 v1과 정확히 같고 무결성·외래키 검사를 통과할 때만 버전을 기록한다. 하나라도 다르면 중단한다. 구조 검증 없는 `alembic stamp`는 하지 않는다.
  - 재실행은 아무것도 바꾸지 않는다. downgrade는 지원하지 않는다 (주문 기록 삭제가 되므로).
- 사후: 버전 `as0001`, 무결성, 기존 테이블 건수·해시·정의 불변, 신규 테이블·인덱스 구조, `scanner_buy_orders` 읽기, 런타임 `schema_status` 준비 확인. 보고서 JSON(권한 600)을 백업 폴더에 남긴다 (행 내용 없음).
- 종료 코드: 0 성공, 2 사전 점검 실패, 3 Migration 실패(롤백), 4 사후 검증 실패. 0이 아니면 운영 배포·SHADOW 전환을 하지 않는다.
- **실패해도 백업으로 자동 복원하지 않는다.** 실제 주문 기록이 있을 수 있는 저널을 과거 사본으로 덮지 않는다. 원인을 확인하고 사람이 판단한다.
- 기존 `scanner_buy_orders` 테이블은 이전과 같이 Scanner 매수 모듈이 처음 쓸 때 스스로 만든다 (이번 변경 범위 밖, 정의 불변).

## 시세 장애 격리와 복구 (2026-11-26)

### 배경: 2026-11-25 SHADOW 첫날 FAIL

- 미국 정규장 tick 780/780(최대 간격 30.8초)은 정상이었지만 유효 시세 감시는 73/734회였다 (CINF 53/390, EVRG 20/344). 일반 감시 시세 실패 6회 뒤 00:46:57 KST부터 전체 신규 손절 진입이 보류됐고 장 종료까지 풀리지 않았다 (회복 확인 실패 246회). 자동 손절 주문·이벤트는 0건.
- 재현된 코드 문제 (배포 커밋 `6f196f5`):
  1. 시세 연속 오류 카운터가 종목 공통이었다. 두 종목의 실패가 합쳐져 임계값에 도달했다.
  2. 회복 확인(`_probe`)이 ID 기준 첫 포지션(CINF)만 반복 조회했다. CINF가 계속 실패하면 EVRG의 정상화를 확인할 방법이 없었다.
  3. 회복 확인은 `broker.quote()` 반환만으로 카운터를 0으로 만들었다 (시각·나이·종목 검증 없음).
  4. 시세 감시 다음 시각이 '마지막 성공 완료 시각 + 60초'라 30초 tick·응답 지연과 겹치면 간격이 약 90초로 늘었다.
  5. `QuoteUnavailable` 로그에 예외 이름만 남아 최초 원인을 알 수 없었다. 예외로 끝난 호출의 HTTP 429 는 집계되지 않을 수 있었다. 회복 확인 실패는 일일 보고에 들어가지 않아 장애 규모가 작게 보였다.
- **최초 키움 API 장애의 원인은 아직 확인되지 않았다.** 그래서 모든 오류를 종목별 장애로 보지 않는다.
  - 코드 경로로 좁힌 범위 (원인 확정 아님): 기록된 예외 이름이 `QuoteUnavailable` 이므로 `LiveStopSellBroker.quote()` 안에서 났다. 키움 HTTP 오류·응답 코드 오류·JSON 오류를 감싼 '시세 조회 실패'이거나, 응답 해석 실패(종목·거래소 불일치, 현재가, 1호가 필드)다. 당시 코드에서 requests 네트워크 예외와 브로커 생성(토큰 발급) 실패는 다른 예외 이름으로 남으므로 해당하지 않는다.
  - 후보 중 하나: 자동 손절 조회 경로(`runner.live_reader`)는 응답 코드 8005(토큰 무효)를 받아도 캐시 토큰을 강제 재발급하지 않는다 (보유 화면 경로 `web/kiwoom_holdings` 는 재발급한다). 이번 수정은 8005 를 `auth`(SYSTEM)로 분류·기록만 하고 재발급 동작은 바꾸지 않았다. SHADOW 재검증의 `return_code` 기록으로 확인한 뒤 별도로 결정한다.

### 오류 범위 분류 (`quote_health.classify`)

| 범위 | 분류(category) | 종목 카운터 | 전체(공통) 카운터 |
|---|---|---|---|
| SYSTEM | `auth`(HTTP 401/403, 키움 응답 코드 8005 '토큰 무효'), `rate_limit`(429 재시도 소진), `http_5xx`, `network`, `timeout`, `response_format`(JSON 아님·형식 오류), `broker_init`(토큰 발급 등 브로커 생성 실패) | +1 | +1 |
| UNKNOWN | `kiwoom_api_error`(return_code≠0, 코드 기록), `http_error`(그 밖의 HTTP 상태), `quote_unavailable`(원인 정보 없음), `unexpected` | +1 | +1 (범위 미확정) |
| SYMBOL | `symbol_mismatch`, `exchange_mismatch`, `price_parse`, `orderbook_field`, 검증 실패 `price_invalid`·`no_quote_time`·`not_trading_day`·`not_today`·`pre_open`·`future_time`·`stale` | +1 | 0, 응답이 왔으므로 공통 카운터를 0으로 (보류 중 제외) |

- 공통 카운터는 기존 `auto_stop_runtime.api_errors.quote` 를 그대로 쓴다. 유효 시세나 종목 단위 응답(검증 실패 포함)이 오면 0이 되므로, 다른 종목이 정상 응답하는 동안의 한 종목 반복 실패는 전체 장애로 번지지 않는다. 연속 5회면 전체 시세 보류(`quote_global_hold`)에 들어간다.
- 실제 응답에 없는 정보는 만들지 않는다. 키움 오류의 HTTP 상태·응답 코드는 `KiwoomError` 속성(`kind`, `http_status`, `return_code`)으로 전달된다 (메시지 형식은 기존과 같다).

### 전체 보류 (Global Safety Gate)

다음 중 하나면 모든 종목의 신규 손절 진입·주문·취소·재주문을 보류한다. 기존 주문 대조, 잔고 동기화(잔고 보류가 아니면), 모든 종목의 회복 조회는 계속한다. Kill Switch·API 계약 게이트·LIVE 차단은 이와 별개로 그대로 동작한다.

| 사유 | 조건 | 해제 |
|---|---|---|
| `balance` / `status` / `orders` | 잔고·주문 상태·주문내역 연속 오류 5회 | 해당 조회 성공 (잔고·주문내역은 주기 60초 회복 조회, 주문내역은 종목 순환) |
| `quote_system` | 시세 공통 카운터 5회 (SYSTEM/UNKNOWN) | 서로 다른 tick **2회 연속**, 각 tick 에 새 유효 시세(포지션·시세 시각 쌍)가 1건 이상 있고 SYSTEM 오류가 없을 때. 실패만 있는 tick 은 처음부터 다시, 같은 시세 반복만 있는 tick 은 중립. 특정 종목의 정상 응답 1건으로는 해제하지 않는다 |
| `quote_broad` | 감시 종목(일시중지 제외) 중 장애(FAULT·RECOVERING) 종목이 2개 이상이면서 절반 이상 | 종목별 회복으로 조건이 풀릴 때 |

보류 구간은 감사 기록(`global_hold`)으로 남기고 일일 보고에서 합계 시간을 보여 준다.

### 종목별 상태 머신 (`auto_stop_runtime.quote_health:{mode}`, 포지션 ID 기준)

```
OK ──(이 종목 연속 오류 5회)──> FAULT ──(유효 시세 1회)──> RECOVERING ──(더 새로운 시세 시각의 유효 시세)──> OK
RECOVERING ──(오류)──> FAULT          RECOVERING ──(같은·더 오래된 시세 시각)──> RECOVERING (복구 근거 아님)
```

- 일반 감시·회복 조회·주문 직전 시세는 모두 `_confirmed_quote` 하나로 검증한다: 종목·거래소 일치, 양수 가격, 당일 정규장, 개장 이후, 120초 이내, 급변(±25%) 재조회·잔고 확인. `bid_tm` 갱신만으로 거래 가능을 확정하지 않는다 (거래정지 필드 미확인 → 시세 나이로 감지).
- 시세를 얻지 못했거나 검증에 실패한 tick 에서는 임계값 전이라도 그 종목 시세를 트리거·주문에 쓰지 않는다.
- FAULT·RECOVERING 종목: 트리거, 신규 주문, 자동 취소(재주문 목적)를 하지 않는다. 이미 걸린 주문은 대조만 한다 (취소하면 재주문 근거 시세가 없어 보호를 잃는다).
- 회복을 확정한 시세는 정상 감시 성공으로 세지 않고 손절 판단에도 쓰지 않는다. 다음 감시 슬롯부터 일반 감시로 판단한다.
- 감시 공백 동안의 과거 가격으로 소급 주문하지 않는다. 보류 중 손절가 이하 시세가 관측되면 `held_trigger` 로 세고 WARN 알림만 보낸 뒤, 보류가 풀리면 최신 시세로 다시 판단한다.
- 기존 손절 이벤트(OPEN/CARRY_OVER)는 장애·복구 과정에서 삭제하거나 초기화하지 않는다. EXITING 종목이 장애면 회복 조회만 하고, 회복 후 `_send` 가 최신 시세로 진행한다.
- 레코드는 프로세스 재시작 후에도 유지된다 (저널 SQLite).

### 감시 슬롯 (P2)

- 종목마다 `next_due_at` 을 둔다. 다음 예정 시각 = 현재 슬롯 시작 + 60초로, 조회 시작·완료 시각(응답 지연)과 분리한다. 30초 tick 에서 응답이 2~12초 걸려도 간격은 60초 근처로 유지된다.
- 일반 감시가 실패하면 같은 슬롯에서 다음 tick 에 한 번 더 시도한다 (tick 당 종목별 최대 1회). 놓친 슬롯은 만회하지 않는다: tick 이 늦게 오면 현재 슬롯 1회만 조회하고 다음은 미래 슬롯이다.
- 처리 순서는 예정 시각 순(가장 밀린 종목 먼저)이다. tick 처리 시간이 tick 주기의 1.5배를 넘으면 남은 종목은 다음 tick 으로 미룬다(`slot_deferred`). API timeout(주문 30초 상한), 429 백오프, lease(90초, 호출 전후 갱신) 정책은 그대로다.

### 오류 기록

- 일반·회복·주문 직전 시세 오류마다 다음을 구조화해 로그(JSON)와 `auto_stop_runtime.quote_errors_recent`(최근 50건), 종목 레코드 `last_error`·그날 `first_error` 에 남긴다: ticker, 요청·응답 거래소(응답 값이 있을 때), 목적(watch/recovery/order), 시작·완료 시각, 소요 시간, HTTP 상태, 키움 응답 코드, 민감정보를 지운 메시지·원인 메시지, 시세 시각·지연, 검증 실패 코드·필드, 시도·retry 횟수, 그 호출의 429 횟수.
- 원본 응답 전체, 토큰, 계좌 식별자는 남기지 않는다 (`quote_health.sanitize`: Bearer·긴 토큰·8자리 이상 숫자 제거, 200자 제한).
- HTTP 429 와 HTTP 요청 수는 브로커 세션 단계에서 세고 tick 종료 시 모든 브로커에서 거둔다. 예외로 끝난 호출의 429 도 빠지지 않는다. 호출별 `http_429_in_call` 은 기록용이며 합계에 다시 더하지 않는다.

### 저널 호환성과 기존 카운터 이행

- **스키마 변경 없음**: 새 상태는 기존 `auto_stop_runtime`(key/JSON), 통계는 `auto_stop_daily_stats`(metric/key), 장애·복구·보류 전이는 `auto_stop_audit` 행으로만 쓴다. 저널 Migration `as0001` 은 그대로이며 새 revision 이 필요 없다. 운영 DB·운영 Alembic 변경 없음, 런타임 DDL 없음.
- 첫 tick 에서 한 번 이행한다 (`quote_health_version=1`, 감사 기록 `quote_health_migrate`).
  - 기존 공통 카운터 `api_errors.quote`(첫날 운영 저널: 6)는 **0으로 바꾸지 않는다**. 임계값 이상이면 전체 시세 보류 레코드(`reason=legacy_common_counter`, `legacy_count`)를 만들고, 위 해제 규칙(서로 다른 tick 2회 연속 새 유효 시세)을 통과할 때 0이 된다 (감사 기록 `quote_hold_off.counter_before`).
  - 어느 종목의 오류였는지 알 수 없으므로 감시 중인 모든 종목을 FAULT(`legacy_unattributed`)로 두고 종목별 회복 확인(서로 다른 시세 시각 2회)을 요구한다.
  - 포지션, 등록 손절가, 주문 이력, 감사 기록, 일일 통계는 건드리지 않는다.

### SHADOW 재검증 절차

1. 배포 전: `git log` 로 대상 커밋 확인, `.venv/bin/python -m pytest -q` 전체 통과, 저널 Migration 상태 `as0001` 그대로(새 Migration 없음), 운영 DB Alembic head 불변 확인.
2. 장외에 재시작한다. 첫 tick 뒤 `GET /api/auto-stop/health` 에서 `quote_global_hold.reason=legacy_common_counter`, `api_errors.quote=6`(유지), `quote_health` 의 CINF·EVRG 가 `FAULT`(legacy_unattributed)인지 본다. 감사 기록 `quote_health_migrate` 1건.
3. 다음 정규장 첫 2~3분: 두 종목 모두 회복 조회가 번갈아 일어나고(`recent_quote_errors` 또는 로그), 정상이면 `RECOVERING` → `OK`, `quote_global_hold=null`, `api_errors.quote=0` 이 되는지 확인한다. 실패하면 `first_error`·`recent_quote_errors` 의 category·http_status·return_code 를 기록한다 (최초 원인 확인용).
4. 장중: `global_hold.reasons` 가 비어 있고 종목별 `last_ok_at` 이 약 60초 간격으로 갱신되는지 본다.
5. 장 마감 후 일일 보고: 종목별 정상 감시율, 누락 분해(보류·실패·스케줄 지연), 회복 조회 시도·성공·실패, 전체 보류 시간, HTTP 요청·429, 오류 원인별 횟수를 확인한다. 스케줄 지연 누락이 0에 가깝고 tick 과 감시를 따로 평가한다.
6. 판정 (제안 기준, 운영자가 확정): 장애가 없던 날은 정상 감시율 ≥ 95%, 전체 보류 0초. 장애가 있던 날은 원인 분류가 기록되고, 한 종목 장애가 다른 종목 감시를 막지 않았으며, 회복 후 감시가 재개됐는지로 판단한다. NEXT_QUOTE 5거래일 기준은 이 수정 배포일부터 다시 센다.
7. 실주문·실취소·LIVE 전환·운영 저널 수동 수정은 이 절차에 없다.

## 운영 절차

VM에서 실행하는 단계별 명령과 승인 지점은 `docs/vm_shadow_runbook.md`를 따른다.

1. **OFF 배포**
   - 기본값 그대로 배포한다 (`AUTO_STOP_US_MODE` 미설정 = OFF). 코드 배포만으로는 저널이 바뀌지 않는다: health는 `schema_ready=false`, `migration_required=true`이고 Scanner·계좌 기능은 그대로 동작한다.
   - 앱을 멈추고 저널 Migration을 실행한 뒤 다시 시작한다 (아래 "저널 스키마와 Migration").
   - `GET /api/auto-stop/health`에서 `mode=OFF`, `schema_ready=true`, `schema_version=as0001`, `kill_switch=null`을 확인한다.
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
| 감시 | 종목 수, 종목별 기대 감시 슬롯 대비 유효 시세 감시 성공(정상 감시율), 누락 = 보류 미실행 + 감시 실패 + 스케줄 지연, 일반 감시 시도·API 오류·검증 실패, 최대 시세 지연, 마지막 정상 감시 |
| 회복 조회 | 종목별·합계 시도/성공/실패/무효/동일 시세 반복. 일반 감시와 따로 센다 |
| 장애·보류 | 종목별 장애 발생·복구 시각과 그날 최초 오류, 현재 시세 상태, 전체 보류 구간과 합계 시간, 보류 중 손절가 이하 관측 수 |
| tick | 기대 tick 수 대비 실제, 최대 tick 간격 (Scheduler heartbeat. 종목별 시세 감시와 따로 평가) |
| API | 오류 합계(일반 감시·회복 조회·주문 직전·잔고·주문상태 모두 포함)와 종류별 횟수, 시세 오류 원인별 횟수, 실제 HTTP 요청 수(api-id별), HTTP 429 횟수 |
| 손절·주문 | 트리거 수, 주문 수(원주문/재주문/이월), 체결·미체결·취소 |
| 가상 체결 | 체결가(지정가 기준) vs 손절가 % |
| 미해결 | 열린 이벤트, 미확정 주문, 확인 필요 |
| 알림·heartbeat | 알림 성공/실패/대기, heartbeat 정상 여부 |

SHADOW 보고에는 `shadow` 항목이 붙는다.

| 항목 | 내용 |
|---|---|
| `current` | 현재 `fill_model`, `shadow_replace_enabled` |
| `by_model` | 가상 체결 모델별 주문 수, 원주문/재주문/이월, 체결·취소·미체결, 가상 체결가 vs 손절가 |
| `config_changes` | 그날 모델·스위치 변경 시각과 값 |
| `mixed_models` | 하루에 모델이 둘 이상이면 true. 이 경우 합산 수치로 평가하지 않는다 |

누락·오류·회복 조회 실패·전체 보류·미해결이 있으면 WARN, 없으면 INFO로 보낸다. 휴장일은 `trading_day=false`.

감시 슬롯·조회 시도·HTTP 요청은 서로 다른 단위다. 슬롯은 감시 주기 1칸(성공은 슬롯당 1회만 센다), 시도는 증권사 시세 조회 1회와 검증, HTTP 요청은 실제 요청(429 재시도 포함)이다. 기대 감시 횟수는 장중 등록·트리거 시점을 반영하는 기존 기준을 유지한다.

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

권장 구성 (push 방식, API 키를 외부 서비스에 주지 않음):

1. Healthchecks.io 같은 push 모니터에 체크를 만든다 (주기 1분, 유예 5분). ping URL은 비밀값으로 다룬다.
2. VM crontab에 1분마다 로컬 health를 확인하고 정상일 때만 ping하는 줄을 넣는다. `SITES_API_KEY`와 ping URL은 권한 600 파일(예: `~/.config/mystockapp/heartbeat.env`)에서 읽는다.

   ```cron
   * * * * * . $HOME/.config/mystockapp/heartbeat.env && curl -fsS -m 10 -H "Authorization: Bearer $SITES_API_KEY" http://127.0.0.1:8000/api/auto-stop/health | jq -e '(.market_open | not) or (.heartbeat_age_sec != null and .heartbeat_age_sec < 120 and .unknown_orders == 0 and .needs_review == 0)' >/dev/null && curl -fsS -m 10 "$HC_PING_URL" >/dev/null
   ```

   - 앱이 죽었거나, 장중 heartbeat가 120초를 넘었거나, UNKNOWN·확인 필요가 생기면 ping이 끊겨 모니터가 경보를 보낸다.
   - 장외에는 앱이 살아 있기만 하면 ping한다.

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
| 키움 호출 한도 | 문서 없음 | 조회만 429 백오프(2/4/8초 재시도), 종목 수 상한 20, 회복 조회 429 반복 시 간격 2·4배, 일일 보고에 429·HTTP 요청 수 집계 (예외로 끝난 호출 포함) | SHADOW 관찰 |
| 2026-11-25 최초 `QuoteUnavailable` 원인 | **미확인** (당시 로그에 예외 이름만 남음) | 원인 정보를 `QuoteUnavailable.info`/`__cause__`로 보존하고 구조화 기록. 범위 미확정 오류는 전체 카운터에도 센다 | SHADOW 재검증 시 `first_error`·`recent_quote_errors` 확인 |

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

#### 조회 전용 확인 기록

계좌번호·토큰·원 응답 전문은 적지 않는다. 필드명, 값의 일치 여부, 확인 시각만 기록한다. "판정"이 `확인`인 계약만 `AUTO_STOP_US_VERIFIED_CONTRACTS`에 넣는다.

| 계약 | 확인일(ET) | 근거 (필드·비교 대상) | 판정 |
|---|---|---|---|
| `USA20101_QUOTE_TIME` | | `dt`/`bid_tm` 갱신 간격, 최대 age, `cur_prc` vs 앱 | 미확인 |
| `USA20101_BID` | | 앱 매수1호가와 같은 필드명 | 미확인 |
| `UST21070_SELLABLE` | | 행 수, `sell_alowq` vs 앱 매도가능수량 | 미확인 |
| `UST21150_STATUS` | | 체결/부분/취소 주문별 수량·상태명 vs 앱 | 미확인 |
| 체결 평균가 필드 | | 가격 후보 필드명과 앱 체결가 일치 여부 (계약 이름 없음, 매핑은 별도 승인) | 미확인 |

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
