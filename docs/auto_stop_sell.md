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
| `quote_system` | 시세 공통 카운터 5회 (SYSTEM/UNKNOWN) | 아래 "시세 장애(incident) 해제 조건" |
| `quote_broad` | 감시 종목(일시중지 제외) 중 장애(FAULT·RECOVERING) 종목이 2개 이상이면서 절반 이상 | 아래 "시세 장애(incident) 해제 조건" (종목 추가로 비율이 낮아져도 해제하지 않음) |

보류 구간은 감사 기록(`global_hold`)으로 남기고 일일 보고에서 합계 시간을 보여 준다.

### 시세 장애(incident) 보존과 해제 조건 (2026-11-27 Codex 리뷰 P1 반영)

- 시세 보류는 `auto_stop_runtime.quote_global_hold` 에 **원인(`triggers`, `reason`)·영향 범위(`affected`)·대상별 복구 증거**와 함께 보존한다. tick 사이, 프로세스 재시작 사이에 유지된다. 카운터·streak·incident 갱신은 한 트랜잭션(`AutoStopStore.runtime_txn`)으로 한다.
- 영향 대상
  - `quote_system`: 임계값에 도달한 연속 오류 구간(`quote_error_streak`)에서 SYSTEM/UNKNOWN 오류를 낸 포지션 전부. 보류 중 새로 SYSTEM/UNKNOWN 오류를 낸 포지션도 추가되고, 이미 대상이면 증거를 처음부터 다시 모은다(백오프가 끝난 뒤 다시 실패한 경우 포함).
  - `quote_broad`: 발생 시점의 장애(FAULT·RECOVERING) 종목. 이후 정상 종목이 추가돼 비율이 낮아져도 대상은 빠지지 않는다.
  - 기존 카운터 이행(`legacy_common_counter`): 이행 시점에 감시 중이던 모든 포지션.
  - 종목 추가·일시중지·관리 종료(DISABLED·SOLD·EXTERNAL_CLOSED·NEEDS_REVIEW)는 대상을 빼지 않는다.
- 대상별 복구 증거 (조회하지 않은 tick 은 증거가 아니다)

| 대상 원인 | 필요한 증거 |
|---|---|
| `system` (SYSTEM/UNKNOWN 오류, legacy) | 요청한 종목·거래소와 일치하고 시세 시각이 전진하는 **유효 시세를 서로 다른 tick 에서 2회**. 그 사이 그 대상에서 SYSTEM/UNKNOWN 오류가 나면 처음부터 |
| `broad` (관리 중 ACTIVE·EXITING) | 위 유효 시세 2회 + 그 종목의 회복 상태 OK |
| `broad` (관리 종료) | 위 유효 시세 2회 |

- 2026-11-27 2차 리뷰 반영: 응답 수신·유효 시세 회복·전체 장애 해제를 따로 센다 (`affected.*.responses`, 통계 `incident_response` / `affected.*.evidence_ticks`, 통계 `incident_evidence` / 통계 `incident_release`). 종목 단위(SYMBOL) 응답 — 오래된 시세, 같은 시세 시각 반복, 종목·거래소 불일치, 무효 가격, 시세 시각 누락, 당일 정규장이 아닌 시세 — 은 **응답 수신으로만** 기록하고 복구 증거로 쓰지 않는다.
- 새 SYSTEM 또는 UNKNOWN 오류가 나면 같은 트랜잭션에서 그 대상의 증거 초기화, 공통 정상 라운드 초기화(`rounds_ok=0`), 원인·발생 시각 갱신(`last_error`, `last_system_error_at`)을 한다.

- 해제 = **모든 영향 대상의 증거 충족** + **공통 시세 경로 2회 연속 정상 라운드**(서로 다른 tick, 각 tick 에 새 유효 시세 1건 이상, SYSTEM·UNKNOWN 오류 0건. 실패만 있는 tick 은 처음부터, 같은 시세 반복만 있는 tick 은 중립). 특정 종목의 정상 응답만으로, 또는 다른 종목의 정상 조회만으로 해제하지 않는다.
- 같은 tick 에 정상 응답과 SYSTEM 또는 UNKNOWN 오류가 섞이면 그 tick 은 정상 라운드가 아니다.
- 조회 상태 구분 (종목 레코드 `last_observation`, 대상 `last_status`): `ok`(정상 조회), `failed`(조회 실패·검증 실패), `skipped_backoff`(429 백오프로 조회하지 않음), `skipped_budget`(tick 시간 예산 초과로 조회하지 않음), `paused`(종목 일시중지), `recovering`(복구 확인 중), `recovered`(복구 완료). `skipped_*`·`paused` 는 증거가 아니다. 시간 예산으로 미룬 종목은 다음 tick 에 예정 시각 순으로 먼저 조회한다.
- 일반 감시 대상이 아닌 영향 대상(일시중지, 관리 종료, 다른 모드)은 **읽기 전용 대체 확인 경로**(`_incident_probe`)로 계속 조회한다. 일반 감시와 같은 `validate_detail`(종목·거래소 일치, 양수 가격, 시세 시각 존재, 당일 정규장, 개장 이후, 120초 이내)을 쓰고, 증거 규칙도 같다(시각 전진 유효 시세, 서로 다른 tick 2회). 트리거·주문은 없다. 일시중지 종목은 종목 회복 상태도 함께 갱신한다 (일시중지는 트리거·주문만 멈춘다).
- 검증 범위 차이: 주문·취소 근거 시세(`_confirmed_quote`)는 위 검증에 더해 직전 유효가 대비 ±25% 급변 시 재조회·잔고 확인을 한다. 읽기 전용 대체 경로는 거래에 쓰지 않으므로 급변 재조회·잔고 확인을 하지 않는다 (복구 증거로만 쓰며, 그 종목이 다시 거래 판단에 들어갈 때는 일반 감시의 전체 검증을 거친다).
- **운영자 조치 절차 (증거를 얻을 수 없는 경우)**: 영향 대상 종목이 상장폐지 등으로 더 이상 시세를 낼 수 없으면 incident 는 해제되지 않는다(안전 쪽). 운영자 조치만으로 미검증 장애를 정상으로 보지 않으며, 이 기능에는 수동 해제 API 가 없다.
  1. health 의 `quote_incident_unresolved` 와 `recent_quote_errors`(category·http_status·return_code)로 원인을 확인하고 기록한다.
  2. 조회 전용 스크립트(`scripts/auto_stop_readonly_probe.py quote`)로 해당 종목과 다른 보유 종목의 시세를 확인한다 (주문 없음).
  3. 자동 손절 보호가 필요하면 키움 앱에서 직접 관리하고, 자동 손절은 `AUTO_STOP_US_MODE=OFF` 로 끈다 (기존 주문 대조는 계속).
  4. 저널의 `quote_global_hold` 를 손으로 지우거나 고치지 않는다. 코드로 해제 조건을 바꿔야 하면 별도 리뷰를 거친다.

### 종목별 상태 머신 (`auto_stop_runtime.quote_health:{mode}`, 포지션 ID 기준)

```
OK ──(이 종목 연속 오류 5회)──> FAULT ──(유효 시세 1회)──> RECOVERING ──(더 새로운 시세 시각의 유효 시세)──> OK
RECOVERING ──(오류)──> FAULT          RECOVERING ──(같은·더 오래된 시세 시각)──> RECOVERING (복구 근거 아님)
```

- 일반 감시·회복 조회·주문 직전 시세는 모두 `_confirmed_quote` 하나로 검증한다: 종목·거래소 일치, 양수 가격, 당일 정규장, 개장 이후, 120초 이내, 급변(±25%) 재조회·잔고 확인. `bid_tm` 갱신만으로 거래 가능을 확정하지 않는다 (거래정지 필드 미확인 → 시세 나이로 감지).
- 시세를 얻지 못했거나 검증에 실패한 tick 에서는 임계값 전이라도 그 종목 시세를 트리거·주문에 쓰지 않는다.
- FAULT·RECOVERING 종목: 트리거, 신규 주문, 자동 취소(재주문 목적)를 하지 않는다. 이미 걸린 주문은 대조만 한다 (취소하면 재주문 근거 시세가 없어 보호를 잃는다).
- 저장된 종목 상태 OK 는 주문·취소의 필요조건일 뿐이다. 주문 직전과 재주문 목적 취소 직전에는 새 시세를 같은 `_confirmed_quote` 로 검증한다 (아래 "재주문 목적 취소 전 검증").

### 공통 실행 게이트 (2026-11-27 Codex 2차 리뷰 P1 반영)

신규 매도·재주문(`claim_sell`)과 재주문 목적 취소(`claim_cancel`)는 같은 불변 조건을 지킨다.

| 불변 조건 | 확인 위치 |
|---|---|
| 마감 cutoff 이후 신규 매도·재주문 목적 취소 금지 (정규장 종료 포함) | 선점 트랜잭션 게이트 |
| 유효하지 않은 lease 로 선점 금지 (만료, 다른 작업자 보유) | 선점 트랜잭션 (`_check_lease`, 잠금 획득 후 시각) |
| 일시중지 종목의 신규 매도·자동 취소 금지 | 게이트 (`auto_paused`) |
| 미확정 주문이 있으면 중복 매도 금지 | `claim_sell` (같은 모드·종목 미확정, 직전 주문 상태) |
| 사전 검증 이후 주문·체결·관리 수량이 바뀌면 그 검증을 쓰지 않음 | 게이트 스냅샷 비교 |
| 유효하지 않은·유효기간이 지난 시세를 주문 근거로 쓰지 않음 | `_confirmed_quote` + 게이트 시세 신선도 |
| 재주문 불가능이 사전에 확인된 주문은 취소하지 않음 | 취소 전 검증 + 게이트(재주문 스위치·한도) |
| 확인되지 않은 시스템 장애를 자동 해제하지 않음 | incident 해제 조건 |
| 결과 불명이면 자동 재전송하지 않음 | 기존 UNKNOWN / CANCEL_UNKNOWN 정책 |

- 게이트는 SQLite 쓰기 잠금(BEGIN IMMEDIATE)을 얻은 **뒤** 주입된 시계(`Engine.clock`, 테스트는 FakeClock)로 현재 시각을 다시 읽어 판단한다. 사전 검증 때 얻은 `now`·market clock 을 재사용하지 않는다. 잠금 대기 동안 흐른 시간도 반영된다.
- 같은 트랜잭션에서 다시 확인하는 것: lease 소유·만료, 정규장·거래일·cutoff, 현재 운영 모드, 긴급 중지, 전체 보류(잔고·주문상태·주문내역·시세 연속 오류, 시세 incident), 종목 시세 상태, 실거래·SHADOW 재주문 스위치와 API 계약(`guard`), 포지션 상태·`auto_paused`, 청산 이벤트 상태, 사전 검증 스냅샷과 최신 DB 일치(포지션 관리 잔여·누적 체결·최초 수량, 이벤트 상태·다음 주문 종류·재주문 횟수, 취소면 주문 상태·누적 체결·미체결 잔량·주문 수량·주문번호·취소 거부 이력), 사전 검증 시세의 당일 정규장 여부와 신선도(120초), 취소면 재주문 한도.
- 스냅샷이 하나라도 다르면 선점을 거부하고 기존 주문 상태를 유지한다 (같은 PARTIAL_FILLED 여도 누적 체결·잔량이 바뀌면 거부). 다음 tick 에 주문 대조부터 다시 하고 새로 검증한다.
- 트랜잭션 안에서는 네트워크 호출을 하지 않는다 (DB 읽기·쓰기와 env 재확인만).

### 주문 종류별 안전 조건 매트릭스 (2026-11-27 Codex 3차 리뷰 반영)

주문 종류: `ORIGINAL`(첫 매도), `REPLACE`(취소 확정 후 잔여 재주문), `CARRY_OVER`(다음 정규장 첫 매도), `CANCEL_FOR_REPLACE`(재주문 목적 취소). 게이트는 호출부의 주문 종류와 이벤트의 실제 다음 주문 종류가 같은지 확인하고(취소 제외), 알 수 없는 종류·최신 설정 조회 실패는 거부한다(fail closed). 최신 설정은 선점 트랜잭션 안에서 **한 번** 읽고, `claim_*` 의 재주문 한도·lease 판단도 같은 설정 객체를 쓴다.

✓ = 적용(사전 검증 후 바뀌면 거부), – = 해당 없음, 정책 = 기존 정책 유지. 매개변수화 테스트: `tests/test_auto_stop_gate_matrix.py`.

| 조건 | ORIGINAL S/L | REPLACE S/L | CARRY_OVER S/L | CANCEL_FOR_REPLACE S/L |
|---|---|---|---|---|
| lease·현재 시각 (잠금 획득 후 시각) | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 정규장·cutoff | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 운영 모드 = 주문 모드 | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| Kill Switch | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 주문 활성화 스위치 (KIWOOM_TRADING_ENABLED·ACCOUNT1_ORDER_ENABLED 등) | –/✓ | –/✓ | –/✓ | –/✓ |
| 재주문 스위치 (LIVE `REPLACE_ENABLED` / SHADOW `SHADOW_REPLACE_ENABLED`) | –/– | ✓/✓ | –/– | ✓/✓ |
| 세션·이벤트 재주문 한도 | –/– | ✓/✓ | –/– | ✓/✓ |
| 이월 정책 (이월 횟수 한도, 오늘 세션 이월, LIVE `POST_CLOSE_EXPIRY`) | –/– | –/– | 정책/정책 | –/– |
| API Contract Gate (`LIVE_REQUIRED`, 취소는 `UST20003_PARTIAL_CANCEL` 추가) | –/✓ | –/✓ | –/✓ | –/✓ |
| 전체·종목 장애 보류 | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 포지션·이벤트 상태 | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 일시중지 | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 주문·체결·잔량·관리 수량 변경 (스냅샷) | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 최신 주문 수량·금액 상한, LIVE 수량 제한 (취소는 계획된 재주문 기준) | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| 시세 유효성 (당일 정규장, 120초) | ✓/✓ | ✓/✓ | ✓/✓ | ✓/✓ |
| Scanner 미확정 매수 (같은 account1·종목) | –/✓ | –/✓ | –/✓ | –/✓ |

해당 없음의 이유
- 주문 활성화 스위치·API Contract Gate·Scanner 미확정 매수 (SHADOW): SHADOW 는 실계좌 주문 클라이언트가 없고(가상 주문만), 실계좌 주문 상태와 충돌하지 않는다. 기존 SHADOW 정책과 같다.
- 재주문 스위치·재주문 한도 (ORIGINAL, CARRY_OVER): 재주문이 아니다. 재주문 카운터를 올리지 않는 기존 정책(주문 종류와 카운터 표)과 같다.
- 이월 정책 (CARRY_OVER 외): 이월 주문에만 해당한다. `_rollover` 가 이월 시 확인하는 조건(이월 횟수, LIVE `POST_CLOSE_EXPIRY`)을 선점 때 다시 확인한다. 이월 주문의 적용 조건: 직전 정규장에 이벤트의 모든 주문이 종료·체결 확정되어 CARRY_OVER 대기였고, 다음 정규장에 이월 횟수가 한도 이내이며, 이전 주문 이력이 있을 것 (없으면 ORIGINAL).

### Scanner 미확정 매수 확인

- Scanner 매수의 미확정 정의(`trading/scanner_buy_orders.py`, `store.SCANNER_IN_FLIGHT_SQL`): SENDING, UNKNOWN, 그리고 SUBMITTED 이면서 브로커 최종 상태(FILLED·CANCELLED·REJECTED)가 확인되지 않은 주문(미체결 OPEN·부분체결 PARTIAL·미조회 포함). PREPARED(미전송)·BLOCKED·REJECTED·ABANDONED·RESOLVED_MANUAL 은 증권사에 살아 있는 주문이 아니다.
- LIVE `claim_sell`·`claim_cancel` 의 선점 트랜잭션 안에서 같은 `db` 객체로 확인한다 (`AutoStopStore.scanner_buy_in_flight_db`, 새 연결·중첩 트랜잭션 없음). 사전 검사(`_send`, 취소 전 검증)도 같은 SQL 을 쓴다.
- **객체 판정 (2026-11-27 Codex 4차 리뷰 P1 반영)** — 선점 트랜잭션과 같은 `db` 로 확인하며 스키마를 바꾸지 않는다.
  - 이름은 대소문자 구분 없이 찾는다 (`main.sqlite_master` 에서 `name = 'scanner_buy_orders' COLLATE NOCASE`, SQLite 식별자 규칙과 동일). 유형으로 미리 거르지 않는다.
  - `main` 스키마를 명시한다. 미확정 조회도 `main.scanner_buy_orders` 로 하므로 같은 연결의 TEMP 객체가 실제 표를 가릴 수 없다.
  - `main` 에 그 이름의 객체가 **하나도 없을 때만** "실제 부재 = 미확정 없음"이다. Scanner 매수 모듈은 첫 사용 때 같은 저널에 표를 만들므로, 없다는 것은 그 저널에서 Scanner 매수가 한 번도 준비되지 않았다는 뜻이다. 선점 트랜잭션이 쓰기 잠금을 쥐고 있는 동안 다른 연결은 표를 만들거나 행을 넣을 수 없다.
  - `main` 에 객체가 없는데 TEMP 에만 있으면 신뢰하지 않고 거부한다.
  - 객체 유형이 표 하나가 아니면(VIEW, 같은 이름의 INDEX·TRIGGER 등) 거부한다.
  - 메타데이터 조회 실패, 표가 있는데 미확정 조회 실패(열 누락 등 스키마 불일치, SQLite 오류)는 "부재"로 보지 않고 거부한다.
  - 거부는 모두 `ScannerJournalUnreadable` 이며 LIVE ORIGINAL·REPLACE·CARRY_OVER 매도와 CANCEL_FOR_REPLACE 취소의 선점을 막는다 (sell/cancel 전송 0회). SHADOW 는 증권사로 보내지 않으므로 이 확인을 하지 않는다.
- **읽기 실패 후속 처리 (2026-11-28 Codex 5차 리뷰 P1 반영)** — `ScannerJournalUnreadable` 은 엔진 치명 오류가 아니라 예상 가능한 주문 안전 차단 사유다.
  - 사전 검사(`_send`, `_replace_precheck`)와 최종 선점 게이트가 같은 함수(`Engine.scanner_block_db`)와 같은 사유 문구를 쓴다: `scanner_buy_in_flight: …` / `scanner_journal_unreadable: …`. "미확정 없음(False)"으로 바꾸지 않는다.
  - 이 함수는 `ScannerJournalUnreadable` 만 잡는다. LeaseLost, 저널 자체 손상 등 다른 예외는 기존 정책대로 올라간다 (포괄 `except Exception` 없음). 전송 결과 불명은 기존 UNKNOWN·CANCEL_UNKNOWN 정책 그대로다.
  - 최초·이월 매도 전: 전송하지 않고 이벤트·트리거·잔여를 유지하며 `auto_stop_runtime.order_block:{event_id}`(대기 시작 시각·종류·사유)를 남긴다. 저널 읽기 실패면 URGENT (시간당 1회), Scanner 미확정 매수면 기존 WARN (하루 1회).
  - 재주문 목적 취소 전: cancel 을 보내지 않고 기존 매도 주문을 유지한다. 사전 검사 실패와 최종 선점 거부 모두 `cancel_held` WARN 알림(시간당 1회)과 통계를 남긴다. 기존 주문 대조는 tick 시작의 `_reconcile_all` 이 계속한다.
  - 원주문 취소 확정 후: 대체 매도를 보내지 않고 이벤트·트리거·잔여를 유지하며 `replace_waiting:{event_id}` 에 **실제 사유**를 남기고 URGENT 알림(시간당 1회, 기존 억제 정책)을 보낸다. 반복 tick·재시작에서도 대기 시작 시각(`since`)을 유지한다.
  - 한 이벤트의 차단은 그 이벤트만 건너뛴다. 다른 이벤트의 주문 대조·종료 판정·마감 처리는 계속된다. 저널 읽기 실패는 저널 공통 장애이므로 각 이벤트가 자기 검사에서 똑같이 차단된다 (신규 주문 0).
  - 저널이 정상화되면 다음 tick 에 모든 안전 조건(사전 검사 + 최종 게이트)을 다시 확인해 같은 이벤트로 재주문한다. 장애 상태 수동 초기화나 새 손절 이벤트 생성은 없다.
- **재주문 결과 판정**: 주문 행 생성 ≠ 접수. `_send` 는 (blocked, 사유) 또는 (order, 주문 id) 를 돌려주고, 대기 기록은 실제 주문 상태로 처리한다. 주문 상태 머신의 의미는 바꾸지 않았다.
- **대기 기록 정합성 (2026-11-28 Codex 6차 리뷰 P2 반영)** — `service.reconcile_wait_records`
  - 청산 이벤트가 OPEN 인지와 무관하게 모든 `replace_waiting:*`·`order_block:*` 기록을 실제 주문·이벤트·포지션 상태와 맞춘다. 실행 지점: 매 tick 의 주문 대조 직후(긴급 중지·보류 여부와 무관), `_send` 직후, `resolve_unknown()` 직후.
  - 판정 대상 주문 = 같은 청산 이벤트에서 대기 시작(`since`) 이후 생긴 가장 최근 주문. 기록에 남은 `order_id` 가 다른 이벤트의 주문이거나 더 새 주문으로 대체됐으면 쓰지 않는다 (과거 주문 때문에 현재 대기 기록을 지우지 않는다).
  - 키마다 한 쓰기 트랜잭션에서 읽고 판정하고, 바뀐 것이 있을 때만 쓴다. 기록을 닫을 때는 같은 트랜잭션에서 `wait_record_closed` 감사 기록을 남긴다 → 반복 실행·중간 재시작에도 결과가 같다.
  - 읽기·정합성 처리일 뿐이다. 주문을 만들거나 주문·이벤트 상태를 바꾸지 않는다 (NEEDS_REVIEW → OPEN 자동 전환 없음, 수동 해제 정책 유지).

  | 판정 대상 주문 상태 | 대기 기록 처리 |
  |---|---|
  | SENDING / UNKNOWN | 유지 (`status=result_unknown`). 자동 재전송 없음, 긴급 알림은 기존 UNKNOWN 알림 |
  | SUBMITTED / PARTIAL_FILLED / CANCEL_* | 접수 확인 → 닫음. 주문 대조는 계속, 실제 잔여는 포지션 기록 |
  | FILLED | 닫음 (이벤트가 DONE 이 돼도 오래된 unknown 기록을 남기지 않음) |
  | REJECTED / NOT_SENT | 이벤트가 OPEN·CARRY_OVER·NEEDS_REVIEW 이면 확인된 최종 결과로 바꿔 유지 (`status=closed`, `outcome`, 실제 잔여). 자동 재주문 없음. 이벤트가 끝나면 닫음 |
  | CANCELLED | 대체 주문이 접수 후 취소됨 → 이 대기 구간을 닫음 (매도 완료로 보지 않음). 잔여 재주문이 필요하면 다음 진행에서 새 대기 구간으로 기록 |
  | 주문 없음 | 이벤트 OPEN·CARRY_OVER·EXITING·잔여>0 이면 유지 (실제 잔여로 갱신). NEEDS_REVIEW·잔여>0 이면 `status=closed`, `replacement_needed=true`. 그 밖(이벤트 종료·잔여 0)은 닫음 |

  - `resolve_unknown()` 감사 기록에 이벤트 상태·실제 잔여·`replacement_needed`(취소 확정인데 잔여가 남음)를 함께 남긴다.

**반대 방향 경쟁 (Scanner 매수 선점 → 자동손절 매도) — 교차 시스템 LIVE 차단 조건 (미해결)**
- 현재 상태: Scanner 매수의 `prepare()` 는 Scanner 미확정만 보고, `execute()` 의 선점 갱신(PREPARED→SENDING)은 아무 교차 확인 없이 진행한다. 자동손절 LIVE 매도·취소가 진행 중인 같은 종목에 Scanner 매수가 나갈 수 있다. 이 PR 은 자동손절 쪽(정방향)만 막으며 Scanner 매수 코드는 바꾸지 않는다.
- 필요한 수정 (별도 작업 제안, 자동매수 PR #14 와 병합하지 않음):
  1. Scanner 매수의 PREPARED→SENDING 선점 갱신과 **같은 쓰기 트랜잭션 안에서** 역방향 확인을 한다.
  2. 범위: 같은 `account_profile='account1'`·같은 ticker (대소문자 정규화 기준).
  3. 차단할 자동손절 상태: 미해결 매도 주문 `SENDING`·`SUBMITTED`·`UNKNOWN`·부분체결, 취소 진행 `CANCEL_SENDING`·`CANCEL_SUBMITTED`·`CANCEL_UNKNOWN`, 열린 청산 이벤트가 재주문 대기인 경우. 자동손절 표·객체를 읽을 수 없으면 Scanner 매수 선점도 거부(fail closed)한다.
  4. 양방향 동시 선점 방지: 두 시스템이 같은 SQLite 저널에서 `BEGIN IMMEDIATE` 쓰기 잠금 안에서 서로의 미확정을 확인하고 자기 선점을 커밋하므로, 한쪽이 커밋하면 다른 쪽은 그 행을 보고 거부한다. 두 확인 중 하나라도 잠금 밖에서 하면 이 보장이 깨진다.
  5. 테스트: 양방향 각 상태별 차단, 동시 선점 경쟁, 객체 부재·VIEW·TEMP·조회 실패의 fail closed.
- **이 역방향 확인이 구현·검증되기 전에는 account1 자동손절 LIVE 를 켜지 않는다.** 그전까지 LIVE 는 다른 게이트(미검증 API 계약, 기본 `AUTO_STOP_US_MODE=OFF`)로도 차단되어 있다.
- 두 시스템의 확인을 각각의 쓰기 트랜잭션 안에 두면 선점 시점의 상호 배제는 보장되지만, SQLite 트랜잭션과 증권사 API 를 원자적으로 묶을 수는 없다 (아래 "보장 범위").

**선점과 외부 전송의 보장 범위 (원자성의 한계)**
- SQLite 트랜잭션과 증권사 HTTP 요청은 하나로 묶을 수 없다. 선점 시점 = 위 조건을 모두 확인하고 SENDING / CANCEL_SENDING 을 커밋한 시점이다. 이 커밋에 `sent_at`(취소는 `cancel_sent_at`)과 근거 증거가 함께 들어가며, 커밋 직후 다른 DB·API 작업 없이 바로 요청을 보낸다.
- 선점 이후 전송 전 단계에서 증권사로 나가지 않았음이 확실한 실패(입력 검증 `KiwoomError`)는 기존대로 NOT_SENT(취소는 원상태 + `cancel_rejected`)로 처리한다.
- 전송 여부가 불확실하면(timeout, 응답 불명, 예외, 프로세스 중단) UNKNOWN / CANCEL_UNKNOWN 으로 두고 증권사 주문내역 증거로만 해제한다. 원래 상태로 되돌리거나 자동 재전송하지 않는다.
- 선점 커밋과 HTTP 요청 사이의 극히 짧은 구간에 cutoff 가 지나거나 긴급 중지가 켜지는 경우는 막지 못한다. 긴급 중지가 이미 전송된 주문의 취소를 보장하지 않는다는 기존 안내가 그대로 적용된다.

### 재주문 목적 취소 전 검증 (2026-11-27 Codex 리뷰 P1 반영)

미체결 주문을 취소하면 걸려 있던 매도 주문을 잃는다. 그래서 `claim_cancel` 과 실제 `cancel()` 전에 다음을 모두 확인하고, 하나라도 확인하지 못하면 취소하지 않는다 (기존 주문 유지, 체결·상태 대조 계속, WARN 알림, 통계 `replace_precheck_fail`).

1. 기존 조건: 주문 상태 SUBMITTED·PARTIAL_FILLED, 주문 후 300초, 마감 cutoff 전, 취소 거부 이력 없음, 취소 스위치·API 계약(`_cancel_guard`), 재주문 횟수 한도.
1-1. (2차 리뷰) 이 주문을 증권사 주문내역으로 **다시 대조**해 추가 체결을 반영한다(체결 증거). 조회에 실패하거나 주문이 아직 보이지 않으면 취소하지 않는다. 대조 결과로 주문·포지션·이벤트 스냅샷을 만든다.
2. 전체 긴급 중지, 전체 보류 사유, 종목 시세 상태(FAULT·RECOVERING 아님).
3. 미체결 잔량 > 0, 주문번호 있음. LIVE: 스캐너 매수 미확정 없음, 같은 종목 외부 미체결 없음(주문내역 조회 실패 시 취소 안 함).
4. 잔고 강제 조회 성공, 포지션 EXITING·일시중지 아님, 잔고에 종목 있음.
5. 보유수량 대조 (2차 리뷰): 체결 증거를 반영한 뒤에도 **브로커 보유 < 관리 잔여**이면 원인 불명으로 보고 취소하지 않는다. URGENT 알림(`position:{id}:holding_mismatch:*`)과 `auto_stop_runtime.holding_mismatch:{id}`(최초·잔여·누적 체결, 주문 미체결, 보유, 매도가능)를 남기고 기존 주문 대조는 계속한다. 보유 10·관리 잔여 10·매도가능 0(기존 매도가 전량 예약), 체결 증거로 설명되는 부분체결, 취소 도중 추가 체결은 정상으로 둔다.
5-1. 매도가능 수량: 취소 후 매도가능 추정 = min(보유, 매도가능 + 이 주문의 미체결 잔량). 이 추정식은 **사전 검토에만** 쓴다. 브로커 매도가능이 미체결 매도분을 이미 뺐는지는 미검증(`UST21070_SELLABLE`)이며, 미검증이면 LIVE 신규 매도는 계속 차단된다. 실제 재주문 수량은 취소 확정 뒤 `_send` 가 브로커의 최신 매도가능수량을 다시 조회해 정한다.
6. 새로 조회한 시세의 `_confirmed_quote` 검증 (종목·거래소, 가격, 당일 정규장, 개장 이후, 120초, 급변 재조회).
7. 재주문 가능 가격: 기준가 ≥ $1, 지정가 > 0, 1회 주문 수량·금액 상한, LIVE 1주 상한(`UST20001_SELL` 미검증 시).
8. 취소 선점은 위 "공통 실행 게이트"를 거친다 (잠금 획득 후 시각으로 cutoff·lease·모드·보류·스냅샷·시세 신선도 재검증). 검증 근거는 주문 `details.replace_precheck`, 선점 근거는 `details.cancel_claim` 에 남긴다.

취소 이후
- 취소 확정(CANCELLED, 잔여 0) 전에는 새 주문을 내지 않는다. 취소 응답 사이의 추가 체결은 누적 체결 증분으로 반영하고, 재주문 수량은 실제 관리 잔여로 다시 계산한다.
- 재주문(`_send`, REPLACE)은 종목 상태·잔고·매도가능·새 유효 시세·주문 안전 조건을 다시 확인한다.
- 취소 확정 뒤 시세 장애·전체 보류·Scanner 저널 읽기 실패 등으로 재주문하지 못하면 청산 이벤트와 트리거는 그대로 두고 `auto_stop_runtime.replace_waiting:{event_id}` 에 실제 대기 사유를 기록하고 URGENT 알림을 보낸다 (시간당 1회). 조건이 확인되면 최신 시세로 재주문하고, 증권사 접수가 확인되면 기록을 지운다 (위 "재주문 결과 판정").
- 취소 결과 불명(CANCEL_UNKNOWN)은 기존대로 자동 재시도·재주문하지 않고 증권사 주문내역 증거로만 해제한다.
- 사용자의 수동 취소(키움 앱)는 이 경로와 별개다. 외부 취소는 기존 규칙(장중 NEEDS_REVIEW, 마감 후 소멸·이월)을 따른다.
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
  - 카운터 보존·incident 생성·관련 종목 FAULT·완료 버전·감사 기록을 **한 SQLite 트랜잭션**(BEGIN IMMEDIATE)으로 쓴다. 도중에 실패하면 모두 롤백되어 부분 변환이 남지 않고, 다음 tick(재시작 후 포함)에 같은 변환을 다시 시도한다. 완료 후 반복 재시작에서는 아무것도 쓰지 않는다 (감사 기록 1건).
  - 기존 공통 카운터 `api_errors.quote`(첫날 운영 저널: 6)는 **0으로 바꾸지 않는다**. 임계값 이상이면 incident(`reason=legacy_common_counter`, `legacy_count`, 영향 대상 = 감시 중인 전 포지션)를 만들고, 위 incident 해제 조건을 통과할 때 0이 된다 (감사 기록 `quote_hold_off.counter_before`).
  - 어느 종목의 오류였는지 알 수 없으므로 감시 중인 모든 종목을 FAULT(`legacy_unattributed`)로 두고 종목별 회복 확인(서로 다른 시세 시각 2회)을 요구한다.
  - 포지션, 등록 손절가, 주문 이력, 감사 기록, 일일 통계는 건드리지 않는다.

### SHADOW 재검증 절차

1. 배포 전: `git log` 로 대상 커밋 확인, `.venv/bin/python -m pytest -q` 전체 통과, 저널 Migration 상태 `as0001` 그대로(새 Migration 없음), 운영 DB Alembic head 불변 확인.
2. 장외에 재시작한다. 첫 tick 뒤 `GET /api/auto-stop/health` 에서 `quote_global_hold.reason=legacy_common_counter`, `api_errors.quote=6`(유지), `quote_health` 의 CINF·EVRG 가 `FAULT`(legacy_unattributed)인지 본다. 감사 기록 `quote_health_migrate` 1건.
3. 다음 정규장 첫 2~3분: 두 종목 모두 회복 조회가 번갈아 일어나고(`recent_quote_errors` 또는 로그), 정상이면 `RECOVERING` → `OK`, `quote_global_hold=null`, `api_errors.quote=0` 이 되는지 확인한다. 실패하면 `first_error`·`recent_quote_errors` 의 category·http_status·return_code 를 기록한다 (최초 원인 확인용).
4. 장중: `global_hold.reasons` 가 비어 있고 종목별 `last_ok_at` 이 약 60초 간격으로 갱신되는지 본다.
5. 장 마감 후 일일 보고: 종목별 정상 감시율, 누락 분해(보류·실패·스케줄 지연), 회복 조회 시도·성공·실패, 전체 보류 시간, HTTP 요청·429, 오류 원인별 횟수를 확인한다. 스케줄 지연 누락이 0에 가깝고 tick 과 감시를 따로 평가한다.
6. 판정: 아래 "SHADOW 운영 통과 기준"을 모두 충족해야 그날을 통과로 본다. NEXT_QUOTE 5거래일 기준은 이 수정 배포일부터 다시 센다.

### SHADOW 운영 통과 기준 (2026-11-26 사용자 확정)

| 항목 | 기준 |
|---|---|
| 정상 감시율 | 목표 100%. 정상 거래일 최소 99% 이상 (종목별 `normal_rate_pct`, 합계 `watch.normal_rate_pct`) |
| 감시 누락 | 설명되지 않는 누락 0건. 누락은 모두 보류·감시 실패·스케줄 지연 중 하나로 분해되고, 각 원인이 오류 기록·감사 기록으로 설명돼야 한다 |
| 감시 공백 | 정상 상태(종목 OK, 전체 보류 없음)에서 같은 종목의 최대 연속 감시 공백 120초 이내 |
| 장애 격리·복구 | 한 종목 장애가 다른 정상 종목의 감시·손절 판단을 막지 않고, 장애 종목은 회복 기준(서로 다른 시세 시각 2회)으로 정상 복구 |
| 주문 | 실제 자동손절 주문·취소 0건 (SHADOW 가상 주문만) |

- 95% 같은 낮은 감시율만으로 PASS 또는 LIVE 전환 가능 상태로 판단하지 않는다. LIVE 전환은 이 기준 통과와 별개의 사용자 승인이 필요하다.
- '최대 연속 감시 공백'은 현재 일일 보고에 별도 지표로 없다. 판정 시 종목별 정상 감시 시각(`last_price_check_at` 갱신 이력·로그)으로 확인하며, 보고서 지표 추가 여부는 리뷰 후 결정한다.
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
| 감시 | 종목 수, position_id 별 완료 슬롯 분류(정상·감시 실패·무효·보류·일시중지·스케줄 누락)와 진행 중 슬롯, 정상 감시율, 목적별 조회 시도·API 오류·검증 실패, 최대 시세 지연, 마지막 정상 감시, 데이터 품질 오류 |
| 감시 공백 | position_id 별 최대 연속 감시 공백, 정상 상태 최대 공백, 120초 초과 공백 수와 각 공백의 시작·종료·원인·상태 전이 |
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

감시 슬롯·조회 시도·HTTP 요청은 서로 다른 단위다. 슬롯은 감시 주기 1칸, 시도는 증권사 시세 조회 1회와 검증(목적별: 일반 감시·회복 조회·주문 전·취소 전·대체 확인), HTTP 요청은 실제 요청(429 재시도 포함)이다.

### 감시 슬롯 저장과 보고서 계산 (2026-11-27 Codex 2차 리뷰 P2 반영)

- 슬롯 식별자 = **거래일 + 모드 + position_id + 슬롯 시작 시각**. `auto_stop_daily_stats` 에 `metric='slot'`, `key='<position_id>:<시작 시각>'`, `value=분류 코드` 한 행으로 저장한다 (기존 표, 스키마 변경 없음). 같은 슬롯 재기록은 덮어써 중복 증가가 없다.
- 완료 슬롯의 최종 분류는 정확히 하나: `ok`(정상 감시) / `watch_fail`(일반 감시 API 오류) / `invalid`(무효 시세) / `held`(종목 장애·전체 보류) / `paused`(일시중지) / `missed`(스케줄 누락). 같은 슬롯에서 여러 일이 있으면 실제 관측 우선: 정상 > 감시 실패·무효 > 보류 > 일시중지 > 누락 (실패 후 일시중지되면 실패로 남는다).
- 같은 슬롯의 재시도는 슬롯이 아니라 목적별 시도 횟수(`pos_attempt_*`)로 센다.
- 슬롯은 다음 슬롯으로 넘어갈 때 확정한다. 처리되지 않고 지나간 슬롯은 직전 상태로 분류한다 (보류 중이면 held, 일시중지면 paused, 정상이면 missed). 손절 트리거·관리 종료·장 마감 후 첫 tick 에서는 진행 중이던 마지막 슬롯을 확정한다.
- 슬롯 격자 기준 시각은 그날 `metric='slot_anchor'` 로 남긴다. 보고서의 기대 슬롯 = 감시 구간 [max(개장, 등록), min(현재, 마감, 트리거, 관리 종료)) 안에서 끝난 격자 슬롯 + 트리거·관리 종료로 일찍 확정된 슬롯. 진행 중 슬롯은 `slots.in_progress` 로 따로 보이고 기대·정상에 넣지 않는다.
- 기대 슬롯마다 기록된 분류를 그대로 쓴다. 누락 합계를 맞추려 자르지 않는다. 기록이 없는 기대 슬롯은 스케줄 누락이며 `missed_unrecorded` 로 따로 보인다 (앱 중단 등). 구간 밖 슬롯 기록은 `data_quality` 오류로 표시한다.
- 정상 감시율 = 정상 슬롯 / 완료 기대 슬롯 (100% 초과 불가). 종목별(position_id별) 합 = 전체 합. 같은 티커 재등록은 position_id 가 달라 통계가 섞이지 않는다. 시도·오류·회복 통계도 `pos_*`(key=position_id)로 센다.
- 2026-11-25 등 이 변경 전의 보고서는 슬롯 기록이 없으므로 복원하거나 고치지 않는다.

### 최대 연속 감시 공백

- 일반 감시의 실제 성공 완료 시각을 `metric='watch_ok_at'`, `key='<position_id>:<시각>'` 로 영속 기록한다 (재시작 후에도 유지). 회복 조회·주문 전 조회는 넣지 않는다.
- 공백 = 감시 시작(등록 또는 개장) → 첫 성공, 성공 → 다음 성공, 마지막 성공 → 감시 종료(관리 종료·트리거·장 마감·현재). 그날 성공이 0회면 감시 구간 전체가 하나의 공백이다.
- 종목·position_id 별로 `gaps.max_sec`(최대 공백), `max_normal_sec`(종목 장애·전체 보류·일시중지가 없던 정상 상태 공백의 최대), `over_120`, `normal_over_120`, 120초 초과 공백마다 시작·종료·지속 시간·정상 상태 여부·원인(`cause`)·공백 안 슬롯 분류·상태 전이(`quote_fault`/`quote_recovered`/`pause`/`resume`/`global_hold_on|off`)를 보여 준다. 전체 합계는 `watch.gaps`.
- Scheduler tick 간격(`ticks.max_gap_sec`), 시세 신선도(`max_quote_age_sec`), 실제 감시 공백(`gaps`)은 서로 다른 지표다.

### 정상 상태 공백 판정 (2026-11-27 Codex 3차 리뷰 반영)

- 정상 여부를 슬롯 분류만으로 판단하지 않는다. 감사 기록 **전체 이력**으로 상태 구간을 복원해 공백과 겹치는지 본다 (장 시작 이전부터 이어진 상태 포함).
  - 일시중지: `pause`/`resume` (대상 position_id)
  - 종목 장애: `quote_fault`/`quote_recovered` (대상 `mode:ticker`, position_id) + 기존 카운터 이행 `quote_health_migrate`. 대체 조회 경로의 장애·복구도 감사 기록을 남긴다.
  - 전체 보류: `global_hold`
- 정상 상태 공백 = 공백 전 구간이 위 어떤 상태와도 겹치지 않고 보류·일시중지 슬롯도 없는 공백. 상태가 섞인 공백은 정상 상태가 아니다. 각 공백에 겹친 상태(`states`), 상태 전이, 원인(`global_hold`/`symbol_fault`/`paused`/`watch_fail`/`invalid`/`schedule_missed`/`interval`)을 붙이고, 감시 시작 시점의 상태(`state_at_start`)를 보인다.
- 감사 기록 없는 상태 변경 확인 (2026-11-27 Codex 4차 리뷰 P2 반영) — 조회 시각에 따라 과거 결과가 바뀌지 않게 한다.
  - **감시 종료 시점 근거 보존 (2026-11-28 Codex 5차 리뷰 P2 반영)**: 포지션의 감시가 끝나는 지점마다 전환 직전 상태(`auto_paused`, 종목 상태)를 감사 기록 복원 결과와 비교해 `metric='state_audit'`(key=position_id, 1=일치, 0=불일치)로 남긴다. 같은 날 여러 번 기록되면 작은 값(불일치)을 유지한다(`stat_min`). 기록 지점(보고서 조회 경로가 아님):
    - 관리 종료(`service.disable`), SHADOW→LIVE 전환(`promote`)
    - 손절 트리거(감시 구간 종료), 확인 필요 전환(`_review_position`, 포지션을 멈추는 `_review_event`), 외부 전량 매도(EXTERNAL_CLOSED)
    - 장 마감 처리: 장 마감까지 감시한 ACTIVE·EXITING 포지션. 끝에 `state_audit_done`(시각)을 남긴다.
  - `state_audit_done` 은 일자 처리 완료 표식일 뿐 개별 포지션 근거가 아니다. 포지션별 `state_audit` 이 없으면 완료 표식이 있어도 `state_evidence_missing` (정상 상태 판정 불가)이다.
  - 과거 날짜는 이 근거만 쓴다. 현재 시각·현재 포지션 상태·다음 날 감사 기록으로 판정을 바꾸지 않는다. `state_audit=0` 이면 `state_unaudited`.
  - 현재 상태로 확인하는 것은 **지금 감시 중인 포지션**(마감 처리 전 당일의 ACTIVE)뿐이다. 이미 감시가 끝난 포지션(관리 종료·트리거·확인 필요)의 상태를 현재 값으로 추정하지 않는다.
  - 감시 구간 끝: 손절 트리거 시각, 또는 ACTIVE·EXITING 이 아닌 포지션(NEEDS_REVIEW 포함, 감시 대상 아님)은 전환 시각. 이전에는 NEEDS_REVIEW 를 장 마감까지 감시한 것으로 계산했다.
  - 슬롯 분류와 복원 구간도 대조한다: 일시중지 구간 밖의 `paused` 슬롯, 장애·보류 구간 밖의 `held` 슬롯이 있으면 `state_unaudited`.
  - 판정 불가 포지션: `gaps.measurable=false`, `max_normal_sec=null`. 실제 전체 공백(`max_sec`, `over_120`)은 그대로 보인다.
- 지표: 전체 실제 최대 공백(`max_sec`), 정상 상태 공백의 최대(`max_normal_sec`), 120초 초과 전체 공백 수(`over_120`), 120초 초과 정상 상태 공백 수(`normal_over_120`).

### 보고서 데이터 형식 (LEGACY / SLOT_V2 / MIXED)

- 엔진은 그 거래일·모드에서 처음 tick 을 돌 때 `metric='slot_v2_since'` 에 시각을 남긴다 (장 전 tick 포함, 한 번만).
- 형식은 **포지션별**로 정한다 (2026-11-27 Codex 4차 리뷰 P2 반영). 집계 단위가 둘이다: 구버전 정상 횟수는 티커 단위(`quote_ok`, key=ticker), 신버전 슬롯은 position_id 단위.
  - `LEGACY`: 표식이 없거나 감시가 표식 전에 끝난 포지션 (예: 2026-11-25 전체). 기대 = 감시 구간 ÷ 감시 주기 내림.
  - `SLOT_V2`: 감시 시작이 표식 이후. 표식 직전 허용 오차(tick 주기 + 3초) 안에서 시작했더라도 아래 잔여가 0일 때만 SLOT_V2 다.
  - `MIXED`: 감시가 표식 전에 시작해 표식 이후까지 이어진 포지션 (장중 버전 전환), 또는 허용 오차 안 시작이지만 구버전 성공이 남아 있는 포지션. 기대 = 표식 전 구간 ÷ 주기 올림 + 표식 이후 슬롯 기대. 슬롯·공백은 표식 이후만 측정(`gaps.partial=true`, `measured_from`).
- 티커별 잔여 = `quote_ok`(티커) − 그 티커 SLOT_V2·MIXED 포지션의 신버전 성공(완료 슬롯 ok + 진행 중 슬롯 성공 1회. 신버전도 같은 `quote_ok` 를 슬롯당 1회 올리므로 빼야 중복이 없다).
  - 잔여를 받을 수 있는 포지션(LEGACY·MIXED)이 하나면 그 포지션에 준다 (MIXED 는 잔여 + 신버전 완료 슬롯 ok).
  - 둘 이상이면 나누지 않는다: 포지션 `ok_checks=null`, `legacy_ticker_shared`, `watch.legacy_ticker_totals` 에 티커 합계(정상·기대)로 한 번만 센다.
  - 잔여를 받을 포지션이 없는데 잔여가 남으면 `unattributed_legacy_ok` 로 보이고 어디에도 더하지 않는다.
  - 잔여 < 0(일계가 상세보다 작음)이면 0 으로 자르지 않고 `ok_checks=null`, `aggregate_mismatch`, 측정 불가로 센다.
  - 정상 > 기대이면 100% 로 자르지 않고 `missed=null`, 감시율 `null`, `aggregate_exceeds_expected`.
- 예: 같은 티커 구버전 2/2 후 관리 종료 → 신버전 재등록 3/3 = 전체 5/5, 누락 0, 100%. 허용 오차 안 전환인데 구버전 성공 1회가 있으면 MIXED 로 그 1회를 보존한다.
- 구버전 몫(일시중지·보류·감시 실패·무효 슬롯 수, 최대 공백)은 측정할 수 없으므로 `null` 이다.

### 측정 불가·부분 측정 표시 (API · Slack · 웹 공통)

- `0` 은 실제로 측정한 0 이다. 측정할 수 없는 값은 `null` 이며 Slack·웹에는 "측정 불가"로 쓴다.
- 누락 원인 분해(보류·일시중지·감시 실패·스케줄 지연)는 측정 가능한 포지션 합만 보이고, 원인을 측정할 수 없는 누락은 `cause_unmeasured_missed` 로 따로 보인다. 모두 측정 불가면 "누락 661 (세부 원인 측정 불가)"처럼 쓰고 `= 0 + 0 + 0 + 0` 식의 가짜 분해를 쓰지 않는다.
- 커버리지: `ok_coverage`(정상 횟수), `gaps.coverage`(실제 공백), `gaps.normal_coverage`(정상 상태 공백) 각각 `full` / `partial` / `none`, 측정·부분 측정·미측정 포지션 수를 함께 보인다. 부분 측정이면 `max_sec`·`max_normal_sec` 은 측정된 포지션 기준의 하한이다. 측정 포지션이 하나도 없으면 `null`.
- **전체 기대·성공 집계 (2026-11-28 Codex 5차 리뷰 P2 반영)** — 기대와 성공의 측정 가능 여부를 따로 본다.
  - 기대(`watch.expected`): 모든 포지션의 확인된 기대를 합한다. 성공을 측정할 수 없다고 기대를 빼지 않는다.
  - 성공·누락·정상률·감시 실패·무효·시도: 측정 불가 포지션이 하나라도 있으면 전체는 `null`. 측정된 범위의 합은 `watch.measured_subtotal`(종목 수·기대·정상)로 따로 보이며 전체 통계가 아니다.
  - 상태 구분 `watch.ok_status`: `no_targets`(감시 대상 없음, 기대·정상 0 / 감시율 null) · `full`(모두 측정, 실제 성공 0 이면 정상 0) · `partial` · `unmeasured`. 포지션별 배분은 안 되지만 티커 합계가 확인된 LEGACY 동일 티커 포지션은 측정으로 보고 `ok_ticker_aggregated_positions` 로 보인다 (그룹 합계는 전체에 정확히 한 번).
  - 예: 유일한 MIXED 포지션 기대 8·정상 null → 전체 기대 8, 정상·누락·정상률 null, `ok_coverage=none`.
- **오류 통계 (LEGACY 복원)**:
  - 일반 감시 API 실패(`watch.api_errors`)·무효(`watch.invalid`): 신버전은 티커 일계(`quote_fail`/`quote_invalid`)와 포지션 상세(`pos_watch_fail`/`pos_watch_invalid`)를 같은 호출에서 함께 올린다 → 같은 범위·같은 의미. 티커 일계 − 신버전 상세 = 구버전 몫이며 정상 횟수와 같은 규칙으로 귀속한다(하나면 귀속, 여럿이면 티커 그룹, 음수면 `null`·`aggregate_mismatch`).
  - 구버전(6f196f5) `quote_fail` 은 일반 감시와 주문 직전 시세 조회 실패를 함께 셌다 (`quote_fail_source=ticker_daily`). 2026-11-25 는 주문이 없어 6회 모두 일반 감시 실패다.
  - 신버전에만 있는 지표 — 일반 감시 시도, 회복 조회, 주문 직전 조회, 보류 중 관측, 지연 처리, HTTP 요청·429, 오류 범주별 수, 잔고·주문 회복 조회 — 는 그 범위를 신버전이 전부 기록했을 때만 값이 있다. 구버전 기간이 섞이면 `null` 이다 (구버전 회복 조회 실패는 저널이 아니라 로그에만 있었으므로 추가하지 않는다).
  - 전체 API 오류(`api.errors`)는 구버전부터 있던 `api_error` 일계 그대로다.
  - 2026-11-25: 정상 73/734, 누락 661, 일반 감시 실패 6 (CINF 2, EVRG 4), 전체 API 오류 6, 일반 감시 시도·회복 조회·HTTP 요청·감시 공백 `null`.
- **개별 집계 무결성 (2026-11-28 Codex 6차 리뷰 P2 반영)** — 집계 순서: position 별 검증 → LEGACY 티커 그룹 검증 → 유효·불일치 구분 → 전체·유효 소계 → 품질 표시.
  - 포지션(`ok_integrity`)이나 티커 그룹(`integrity`)에서 정상 > 기대이면 `exceeds_expected`. 전체 합이 정상 범위여도 숨기지 않는다: 전체 정상·누락·정상률 `null`, `watch.ok_integrity=invalid`, `integrity_errors` 에 단위별 내역.
  - 원시 성공 기록은 `watch.ok_recorded` 로 보존한다 (정상 평가에 쓰지 않음). 유효 소계(`measured_subtotal`: 종목 수·기대·정상·누락·정상률)에는 유효한 포지션·그룹만 넣는다.
  - `ok_coverage` 의미(값이 있는 범위)는 그대로다. "값이 있음"과 "값이 유효함"은 `ok_integrity` 로 구분한다.
  - SLOT_V2 의 정상은 기대 슬롯 중 ok 로 기록된 슬롯이므로 구조적으로 기대를 넘지 않는다 (구간 밖 기록은 `slot_out_of_window`).
  - 예: CINF 391/390 + EVRG 20/344 → 기대 734, 원시 기록 411, 전체 누락·정상률 `null`, 유효 소계 EVRG 20/344(누락 324), CINF `aggregate_exceeds_expected`.
- **보고일 감시 대상 선정 (2026-11-28 Codex 6차 리뷰 P2 반영)** — 그날 정규장 [개장, 마감·현재] 과 포지션의 실제 일반 감시 구간이 겹치는 포지션만 감시 대상이다.
  - 감시 시작 = max(개장, 등록). 감시 끝 = 첫 손절 트리거 / ACTIVE·EXITING 이 아닌 포지션은 전환 시각 / 마감.
  - 일반 감시는 ACTIVE 만 하고 트리거 이후 ACTIVE 로 돌아가는 경로가 없으므로, 그날 감시 시작 전에 트리거된 포지션(전날 SOLD·DONE, 전날 이월 등)은 감시 대상이 아니다. 과거 청산 이벤트가 있다는 이유만으로 포함하지 않는다.
  - 구간 길이 0 은 그날 개장 시각 트리거처럼 당일 이벤트 근거가 있을 때만 포함한다 (기대 0).
  - 전 거래일 청산 이벤트의 당일 주문 대조·이월은 `exit_followups` 로 따로 보이며 감시 통계에 넣지 않는다. 주문 통계(`orders`)·청산 이벤트 이력은 그대로다.
  - 감시 대상이 없는 날: `watched_positions=0`, 기대 0, `ok_status=no_targets`, `ok_coverage=none`, 정상률 `null`.
- 장 마감 요약 알림은 감시 대상이 있는데 정상 횟수 커버리지 또는 정상 상태 공백 커버리지가 `full` 이 아니거나, 집계 무결성 오류가 있거나, 누락이 `null` 이면 INFO 가 아니라 WARN 이다 (측정 품질이 불완전한 날을 정상으로 표시하지 않는다).
- 보고서 생성은 읽기 전용이다. 기존 저널·저장된 보고서를 바꾸지 않는다.

### 저장량과 보고서 성능 (측정)

- 감시 주기 60초·30초 tick 에서 종목·분당 약 2행(`slot` 1, `watch_ok_at` 1). 시험 측정: 10종목 1시간 1,190행, 저널 파일 증가 약 156KB(같은 시간의 다른 기록 포함), 보고서 계산 0.02초.
- 추정: 정규장 6.5시간 기준 종목당 하루 약 780행·약 100KB. 2종목이면 연(약 250거래일) 약 50MB, 최대 20종목이면 연 약 0.5GB. 보고서는 그날 행만 읽는다 (PK `day, mode, metric, key`).
- 저널은 자동 삭제하지 않는다 (AGENTS.md). 장기 보관 정책이 필요하면 별도 승인된 배포 작업으로 다룬다.

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
