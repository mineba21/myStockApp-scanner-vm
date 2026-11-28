# Kiwoom LIVE-Oriented SHADOW (account1 US auto-buy) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** account1 미국 Scanner 신호로 B00·B00-N·B01 세 전략을 실제 키움 시세 기반 가상 주문·가상 체결·가상 손절로 장기간 운영할 수 있는 SHADOW 시스템을 만든다. LIVE Executor 는 인터페이스만 두고 구현·활성화하지 않는다.

**Architecture:** 기존 uvicorn 프로세스의 APScheduler 30초 tick 하나가 전용 SQLite 저널의 유일한 작업자다. 키움 호출은 트랜잭션 밖에서 SHADOW 전용 호출 예산을 거쳐 하고, 결과를 받은 뒤 짧은 `BEGIN IMMEDIATE` 트랜잭션에 주문·체결·포지션·현금·손절 상태를 함께 저장한다. 판단은 순수 함수(`strategy`, `sizing`, `decision`)이고 Executor 만 SHADOW/LIVE 로 갈린다.

**Tech Stack:** Python 3.11, sqlite3, Alembic(저널 전용), requests, APScheduler, SQLAlchemy 1.4(운영 DB 읽기), pytest. 기존 모듈 재사용: `trading/auto_stop/{quotes,us_calendar,brokers,settings}.py`, `trading/kiwoom_readonly.py`, `web/kiwoom_holdings._get_token`, `scanner/sizing.calculate_position`.

**Spec:** `docs/superpowers/specs/2026-11-28-kiwoom-live-oriented-shadow-design.md` (이하 "설계", § 번호는 설계 문서 기준). 실행 전 설계를 함께 읽는다. 보고는 한국어, 커밋 메시지는 영문.

## Global Constraints

- 브랜치: `origin/main` 기반 새 브랜치에서만 작업한다. PR #14(`feature/auto-buy-shadow`) 이력·코드를 가져오지 않는다 (cherry-pick·merge 금지). 브랜치 생성·Push 는 사용자 승인 후 (Task 0).
- 기존 S01(`trading/auto_stop/`), `trading/kiwoom_*.py`, `scanner/`, `web/kiwoom_holdings.py` 는 **수정하지 않는다**. import 만 한다. 수정 대상 기존 파일은 `scheduler.py`(작업 1개 추가), `tests/conftest.py`(env 기본값), `CLAUDE.md`, `AGENTS.md`(진입점 한 줄) 뿐이다.
- 새 패키지 어디에서도 `trading.kiwoom_orders` 를 import 하지 않는다. 주문 TR(`ust20000`/`ust20001`/`ust20003`)을 호출하지 않는다.
- 모드 값은 `OFF`, `SHADOW` 만. `LIVE` 및 그 밖의 값은 OFF 로 동작하고 사유를 남긴다. 기본 OFF.
- env 접두사는 `AUTO_BUY_US_RT_` (PR #14 의 `AUTO_BUY_US_MODE` 와 충돌 방지). 저널 경로 `AUTO_BUY_US_RT_JOURNAL_PATH` — S01 저널(`SCANNER_ORDER_JOURNAL_PATH`)과 다른 파일이어야 한다 (같으면 거부).
- 저널 표 접두사 `abu_`, 버전 표 `abu_alembic_version`, 첫 리비전 `abu0001`. 런타임(store·engine·scheduler)에서 DDL·파일 생성 금지. 스키마는 `scripts/auto_buy_us_journal_migrate.py` 로만 만든다.
- 운영 DB 는 읽기만 (`ScanLog`, `ScanResult`). 표 추가 없음.
- 네트워크 호출을 DB 쓰기 트랜잭션 안에 넣지 않는다.
- 가격·지정가·체결가는 `Decimal`, 저널에는 TEXT. 수량 계산은 `scanner.sizing.calculate_position` (float).
- 고정 값(설계 §8.3·§9.3·§10·§18): 지정가 = 판단가 × 1.01 센트 내림, 결정 스캔 마감 22:00 ET, 개장 검사 대기 30분, 관측 공백 300초, 시세 유효 120초, 가상 자금 = 시작 시 account1 USD 순자산 × 0.30 (세 전략 같은 값, 재계산 없음), 위험 0.75% / 전체 열 4% / 종목 15% / R 3~15% / 섹터 35%, 가상 매도 slip 50bp·재가격 300초·세션 3회·이월 1회·마감 5분 전 cutoff, 호출 예산 SHADOW 전체 초당 1회·TR별 초당 1회·tick 당 20회·감시 40종목·KST 09:00~10:00 호출 0·429 즉시 정지 후 60초 쿨다운, 연속 오류 5회 보류, lease 90초.
- 비교 비용은 PR #14 와 같은 `COSTS = {"buy_commission": 0.0025, "sell_commission": 0.0025, "sell_tax": 0.0000278, "slippage_bp": 10}`.
- `STRATEGY_VERSION = "RT1"`, `FILL_MODEL = "CUR_V1"`, `OPEN_CHECK_BASIS = "FIRST_QUOTE"` 를 모든 결정·주문 행에 기록한다.
- 판정 유지: NOT_RECOMMENDED, B01 = NEED_MORE_DATA. 보고서는 기간·건수 충족을 LIVE 승인으로 표시하지 않는다 (`live_approval: False` 고정).
- 테스트는 임시 SQLite·가짜 키움 세션만 쓴다. 지정 환경(Python 3.11 + pandas 1.5.3) 결과와 호환 환경 결과를 구분해 보고한다.
- 수량은 정수. 1주 미만·$1 미만 판단가는 계획하지 않는다 (`PRICE_BELOW_1`).

## Review Focus

1. **조기폐장일·서머타임 경계** — 13:00 ET 조기폐장일에 매수 만료·가상 매도 cutoff 가 13:00 기준으로 동작해야 한다 → Task 11 `test_early_close_day_expires_at_1300`.
2. **같은 종목을 세 전략이 동시에 계획** — 시세는 tick 당 한 번만 조회하고 세 주문이 같은 `quote_id` 로 판정돼야 한다 → Task 11 `test_shared_ticker_queried_once_per_tick`.
3. **장중 재시작 (lease 만료 후 다른 owner)** — WORKING 주문을 이어받고, 재시작 공백이 300초를 넘으면 마감 시 `EXPIRED_UNOBSERVED` 여야 한다 → Task 11 `test_restart_gap_marks_unobserved`.
4. **토큰 만료(8005)·인증 오류 응답** — 체결·손절로 해석하지 않고 무효 시세로 기록, 오류 카운트만 증가해야 한다 → Task 8 `test_invalid_token_response_is_invalid_quote`.
5. **`BRK.B` 같은 점 포함 티커·거래소 모호** — 3 거래소 중 정확히 하나가 아니면 `EXCHANGE_UNRESOLVED` 로 계획에서 제외해야 한다 → Task 8 `test_ambiguous_exchange_unresolved`, Task 11 `test_unresolved_exchange_not_planned`.

---

## File Structure

```
trading/auto_buy_us/
  __init__.py
  settings.py          Settings.from_env, 상수(STRATEGIES, RISK, COSTS, 버전), 모드 차단 사유
  schema.py            schema_status(path), upgrade_journal(path, allow_create) — S01 schema.py 패턴
  journal_migrations/
    __init__.py
    env.py             S01 env.py 와 같은 구조, VERSION_TABLE="abu_alembic_version"
    ddl_v1.py          DDL 문자열 목록 (표·인덱스·불변 트리거)
    versions/abu0001_initial.py
  store.py             Store: 연결·tx·read·lease·runtime·event·api 집계
  ledger.py            도메인 트랜잭션: 계획·주문 전이·체결·손절·청산·만료·이월
  signals.py           결정 스캔 확인·신호 수집 (운영 DB 읽기)
  strategy.py          적격 판정·순위 (PR #14 eligibility 이식)
  sizing.py            가상 자금 상태·수량·열 (PR #14 risk 이식, standalone sleeve)
  decision.py          plan_entries, open_check, stop_check, confirm_stop, exit_limit
  limiter.py           CallBudget, ThrottledSession, RateLimited, BudgetExhausted
  market.py            QuoteEvidence, AccountSnapshot, KiwoomMarketData, build_market_data
  fill_model.py        buy_fill, sell_fill, has_gap (CUR_V1)
  executor.py          Observation, SubmitResult, ShadowExecutor, LiveExecutor
  engine.py            Engine.tick (단일 작업자)
  report.py            close_day, metrics, milestones, daily_report
  runner.py            auto_buy_us_tick, notifier, schema gate
scripts/auto_buy_us_journal_migrate.py
scheduler.py                       (수정: _add_auto_buy_us_job)
tests/auto_buy_us_fakes.py         가짜 시계·키움 세션·시세 대본·운영 DB 시드
tests/test_auto_buy_us_settings.py
tests/test_auto_buy_us_schema.py
tests/test_auto_buy_us_store.py
tests/test_auto_buy_us_strategy.py
tests/test_auto_buy_us_sizing.py
tests/test_auto_buy_us_signals.py
tests/test_auto_buy_us_decision.py
tests/test_auto_buy_us_market.py
tests/test_auto_buy_us_ledger.py
tests/test_auto_buy_us_executor.py
tests/test_auto_buy_us_engine.py
tests/test_auto_buy_us_report.py
docs/auto_buy_us_shadow.md
```

---

### Task 0: 브랜치 준비 (사용자 승인 필요)

- [ ] **Step 1:** 사용자에게 브랜치 생성·Push 승인을 받는다. 승인 전에는 이후 Task 를 시작하지 않는다.
- [ ] **Step 2:** `git fetch origin main && git switch -c <승인된 이름> origin/main` (세션 지정 브랜치를 쓰라면 `ccr-c4e498f2-e6cfic` 를 `origin/main` 기준으로 새로 만든다).
- [ ] **Step 3:** 확인: `git log --oneline origin/main..HEAD` 가 비어 있고, `git ls-tree -r HEAD --name-only | grep -c '^trading/auto_buy/'` 가 `0`.
- [ ] **Step 4:** 기준선: `.venv/bin/python -m pytest -q` 결과(통과·skip 수)를 기록한다.

---

### Task 1: settings

**Files:**
- Create: `trading/auto_buy_us/__init__.py`, `trading/auto_buy_us/settings.py`
- Modify: `tests/conftest.py` (env 기본값 블록에 `"AUTO_BUY_US_RT_MODE": "OFF"`, `"AUTO_BUY_US_RT_JOURNAL_PATH": ""` 추가)
- Test: `tests/test_auto_buy_us_settings.py`

**Interfaces:**
- Produces:
  - 상수 `STRATEGY_VERSION="RT1"`, `FILL_MODEL="CUR_V1"`, `OPEN_CHECK_BASIS="FIRST_QUOTE"`, `ACCOUNT_PROFILE="account1"`
  - `STRATEGIES = {"B00": {"pool": "B00", "topn": None}, "B00N": {"pool": "B00", "topn": 2}, "B01": {"pool": "B01", "topn": 2}}`
  - `RISK = {"RISK_PCT": 0.75, "MAX_TOTAL_HEAT_PCT": 4.0, "MAX_POSITION_PCT": 15.0, "MIN_R_PCT": 3.0, "MAX_R_PCT": 15.0}`, `SECTOR_CAP_PCT = 35.0`, `SLEEVE_PCT = Decimal("0.30")`, `LIMIT_PCT = Decimal("0.01")`, `COSTS` (Global Constraints 값)
  - `@dataclass(frozen=True) class Settings` 필드: `mode: str`, `mode_block: str | None`, `journal_path: str`, `kill: bool`, `tick_sec=30`, `quote_interval_sec=60`, `quote_max_age_sec=120`, `signal_cutoff_et="22:00"`, `open_check_wait_min=30`, `gap_max_sec=300`, `slip_bps=50`, `replace_after_sec=300`, `max_replaces_per_session=3`, `max_carry_over_sessions=1`, `session_start_offset_min=0`, `session_cutoff_min=5`, `quote_jump_pct=25`, `quote_confirm_tolerance_pct=2`, `calls_per_sec=1.0`, `per_tr_per_sec=1.0`, `max_calls_per_tick=20`, `max_watch=40`, `cooldown_429_sec=60`, `max_consecutive_errors=5`, `lease_sec=90`, `extra_closed_dates: frozenset[str]`, `extra_early_close_dates: frozenset[str]`
  - `Settings.from_env() -> Settings` — env: `AUTO_BUY_US_RT_MODE`, `AUTO_BUY_US_RT_JOURNAL_PATH`, `AUTO_BUY_US_RT_KILL`, `AUTO_BUY_US_RT_EXTRA_CLOSED_DATES`, `AUTO_BUY_US_RT_EXTRA_EARLY_CLOSE_DATES` (쉼표 구분 ISO 날짜). 다른 값은 상수(env 로 바꾸지 않음).
  - `Settings` 는 S01 `market_clock(now, settings)`·`quotes.validate(q, now, clock, settings)` 가 요구하는 속성(`extra_closed_dates`, `extra_early_close_dates`, `session_start_offset_min`, `session_cutoff_min`, `quote_max_age_sec`, `quote_jump_pct`, `quote_confirm_tolerance_pct`)을 같은 이름으로 가진다.

- [ ] **Step 1: Write the failing tests**

```python
def test_default_mode_off(monkeypatch): ...            # env 없음 → mode == "OFF", mode_block is None
def test_live_rejected(monkeypatch):                    # AUTO_BUY_US_RT_MODE=LIVE
    s = Settings.from_env(); assert s.mode == "OFF" and "LIVE" in s.mode_block
def test_unknown_mode_rejected(monkeypatch): ...        # "shadoww" → OFF, mode_block 문자열
def test_shadow_mode(monkeypatch): ...                  # "shadow" (소문자) → "SHADOW"
def test_journal_must_differ_from_s01(monkeypatch):     # 두 경로 같으면 mode == "OFF", "S01" in mode_block
def test_extra_dates_parsed(monkeypatch): ...           # "2026-12-24,2026-12-26" → frozenset 두 개, 잘못된 날짜는 mode_block
def test_constants_match_spec():
    assert STRATEGIES["B00N"]["topn"] == 2 and STRATEGIES["B00"]["topn"] is None
    assert RISK["RISK_PCT"] == 0.75 and SECTOR_CAP_PCT == 35.0 and SLEEVE_PCT == Decimal("0.30")
def test_settings_compatible_with_s01_clock():
    from trading.auto_stop.us_calendar import market_clock
    assert market_clock(MON_10, Settings.from_env()).is_open      # MON_10 = 2026-11-23 10:00 ET
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_auto_buy_us_settings.py -q` → FAIL (ModuleNotFoundError).
- [ ] **Step 3:** Implement `settings.py` 와 conftest 기본값.
- [ ] **Step 4:** Run 같은 명령 → PASS. `.venv/bin/python -m pytest tests/test_auto_stop_units.py -q` 도 PASS (conftest 영향 없음 확인).
- [ ] **Step 5: Commit** `git add trading/auto_buy_us tests/conftest.py tests/test_auto_buy_us_settings.py && git commit -m "feat(auto-buy-us): settings with OFF/SHADOW modes and spec constants"`

---

### Task 2: 저널 스키마·전용 Migration

**Files:**
- Create: `trading/auto_buy_us/schema.py`, `trading/auto_buy_us/journal_migrations/{__init__,env,ddl_v1}.py`, `trading/auto_buy_us/journal_migrations/versions/abu0001_initial.py`, `scripts/auto_buy_us_journal_migrate.py`
- Test: `tests/test_auto_buy_us_schema.py`

**Interfaces:**
- Consumes: `Settings.journal_path`
- Produces:
  - `schema.EXPECTED_SCHEMA_VERSION="abu0001"`, `VERSION_TABLE="abu_alembic_version"`, `OWNED_PREFIX="abu_"`
  - `schema_status(path: str | None) -> dict` 키: `schema_ready: bool`, `schema_version: str | None`, `schema_error: str | None` (읽기 전용 연결 `mode=ro`, 파일이 없으면 생성하지 않음)
  - `upgrade_journal(path: str, *, allow_create: bool = False) -> str` (테스트·배포 스크립트 전용)
  - `scripts.auto_buy_us_journal_migrate.main(argv) -> int` 옵션 `--journal`(기본 env), `--backup-dir`(필수), `--check`, `--allow-create`. 종료 코드 0 성공, 2 사전 점검 실패, 3 Migration 실패(롤백), 4 사후 검증 실패.
- 표 (모든 이름 `abu_` 접두사, 열은 설계 §12 그대로, 아래는 추가 결정 사항):
  - `abu_signal_days(session_day TEXT PK, status TEXT CHECK IN ('CAPTURED','NO_SIGNAL_SCAN','MISSED'), scan_log_id INT, captured_at REAL)`
  - `abu_signals(... UNIQUE(scan_log_id, ticker))` + 열 `signal_key, is_first_detection INT, first_strict_pass INT NULL, first_pass_basis, ext_pct REAL, stop_pct REAL, sector_name, entry_warnings TEXT, strict INT NULL, raw_json`
  - `abu_decisions(id, strategy, session_day, signal_id, ticker, exchange, verdict, reasons TEXT(JSON), qty INT, decision_price TEXT, limit_price TEXT, stop_price TEXT, planned_risk REAL, reserved_cash REAL, entry_heat REAL, live_block_reason TEXT, strategy_version, created_at, UNIQUE(strategy, session_day, ticker))`
  - `abu_orders(id, strategy, side CHECK IN ('BUY','SELL'), kind CHECK IN ('ENTRY','ORIGINAL','REPLACE','CARRY_OVER'), decision_id, position_id, ticker, exchange, qty, limit_price, state, executor CHECK IN ('SHADOW','LIVE'), fill_model, open_check_basis, created_at, created_quote_id, created_quote_as_of, session_day, expires_at, broker_ref, ended_at, end_reason)`; 부분 유니크 인덱스: `(strategy, ticker, side)` WHERE state IN ('PLANNED','WORKING') — 같은 전략·종목·방향 미종료 주문 1개.
  - `abu_order_events(order_id, seq, from_state, to_state, at, evidence_json, PK(order_id, seq))`
  - `abu_fills(order_id, cum_qty, qty, price, at, quote_id, model, basis, PK(order_id, cum_qty))`
  - `abu_positions(id, strategy, ticker, exchange, qty, entry_price, initial_stop, state CHECK IN ('OPEN','EXITING','CLOSED','NEEDS_REVIEW'), opened_at, opened_day, closed_at, exit_reason, exit_price, session_replace_count INT DEFAULT 0, carry_over_count INT DEFAULT 0, last_valid_price TEXT, last_valid_at REAL)`; 부분 유니크 `(strategy, ticker)` WHERE state != 'CLOSED'
  - `abu_sleeves(strategy PK, initial_cash TEXT, initial_basis_json, cash TEXT, realized_pnl TEXT, created_at)`
  - `abu_quotes(id, ticker, exchange, requested_at, received_at, as_of, price TEXT, source, valid INT, invalid_reason)` + 인덱스 `(ticker, received_at)`
  - `abu_account_snapshots`, `abu_daily_perf(strategy, day, ..., PK(strategy, day))`, `abu_system_events`, `abu_api_calls(minute TEXT, api_id TEXT, outcome TEXT, n INT, PK(minute, api_id, outcome))`, `abu_runtime(key PK, value TEXT)`
  - 불변 트리거: `abu_signals`, `abu_decisions`, `abu_order_events`, `abu_fills`, `abu_quotes`, `abu_system_events`, `abu_signal_days` 에 UPDATE·DELETE 시 `RAISE(ABORT, 'abu immutable')`. `abu_orders` 는 종료 상태(`FILLED, EXPIRED, EXPIRED_UNOBSERVED, NOT_STARTED, SKIPPED_OPEN, BLOCKED, CANCELLED`) 행의 UPDATE 거부 트리거.

- [ ] **Step 1: Write the failing tests**

```python
def test_status_missing_file_does_not_create(tmp_path):
    p = tmp_path / "j.sqlite"; st = schema_status(str(p))
    assert st["schema_ready"] is False and not p.exists()
def test_upgrade_creates_expected_tables(tmp_path):
    p = str(tmp_path / "j.sqlite"); assert upgrade_journal(p, allow_create=True) == "abu0001"
    assert schema_status(p)["schema_ready"]
def test_immutable_tables_reject_update(tmp_path): ...   # abu_fills/abu_quotes/abu_order_events UPDATE·DELETE → sqlite3.IntegrityError 'abu immutable'
def test_terminal_order_rejects_update(tmp_path): ...    # state='FILLED' 행 UPDATE → IntegrityError
def test_one_open_order_per_strategy_ticker_side(tmp_path): ...  # 두 번째 WORKING 삽입 → IntegrityError
def test_runtime_store_never_runs_ddl(): ...             # store.py·engine.py·runner.py 소스에 "CREATE TABLE"·"upgrade_journal" 없음
def test_migrate_script_check_mode_no_change(tmp_path): ...   # --check: 종료 0, 파일 mtime·내용 불변
def test_migrate_script_backup_and_exit_codes(tmp_path): ...  # 성공 0 + 백업 파일 존재, 사후 검증 실패 주입 시 4
def test_s01_journal_untouched(tmp_path): ...            # S01 저널 파일에 abu_ 표 없음 (upgrade 는 지정 경로만)
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_auto_buy_us_schema.py -q` → FAIL.
- [ ] **Step 3:** Implement. `env.py` 는 S01 `journal_migrations/env.py` 와 같은 구조(BEGIN IMMEDIATE, `transactional_ddl=True`, config·database import 금지). `schema_status` 는 `sqlite_master` 의 `abu_` 객체 목록·정의를 `ddl_v1` 기대값과 비교한다 (S01 `structure_diff` 와 같은 방식).
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): journal schema abu0001 and dedicated migration script`

---

### Task 3: store (연결·트랜잭션·lease·runtime·이벤트·API 집계)

**Files:**
- Create: `trading/auto_buy_us/store.py`
- Test: `tests/test_auto_buy_us_store.py`

**Interfaces:**
- Consumes: Task 2 스키마
- Produces:
  - `class LeaseLost(Exception)`
  - `class Store(path: str, clock=time.time)`:
    - `connect() -> sqlite3.Connection` (`isolation_level=None`, `row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, `busy_timeout=30000`; 파일이 없으면 `FileNotFoundError` — 생성 금지, `uri=True` `mode=rw`)
    - `tx()` 컨텍스트 매니저: `BEGIN IMMEDIATE` … COMMIT, 예외 시 ROLLBACK 후 재발생. 연결 객체를 돌려준다.
    - `read()` 컨텍스트 매니저: `BEGIN` (읽기 스냅샷) … COMMIT
    - `acquire_lease(owner: str, now: float, lease_sec: int) -> bool`, `renew_lease(owner, now, lease_sec) -> None` (실패 시 `LeaseLost`), `release_lease(owner) -> None`
    - `get_runtime(db, key) -> Any`, `set_runtime(db, key, value) -> None` (JSON)
    - `event(db, level: str, kind: str, now: float, **details) -> None` (`abu_system_events` 삽입)
    - `count_api(db, now: float, api_id: str, outcome: str, n: int = 1) -> None` (outcome ∈ `ok, err, http_429, timeout, budget_skip`), 분 키 `YYYY-MM-DDTHH:MM` UTC

- [ ] **Step 1: Write the failing tests**

```python
def test_connect_does_not_create_file(tmp_path): ...       # FileNotFoundError, 파일 없음 유지
def test_tx_rolls_back_on_error(store): ...                # tx 안 event 삽입 후 raise → 행 0
def test_lease_exclusive(store):
    assert store.acquire_lease("a", 100, 90) and not store.acquire_lease("b", 150, 90)
    assert store.acquire_lease("b", 191, 90)               # 만료 후 획득
def test_renew_after_takeover_raises(store): ...           # a 획득 → 만료 → b 획득 → a.renew → LeaseLost
def test_count_api_accumulates(store): ...                 # 같은 분·TR·outcome 두 번 → n == 2
def test_runtime_roundtrip(store): ...                     # set {"x":1} → get == {"x":1}
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_auto_buy_us_store.py -q` → FAIL.
- [ ] **Step 3:** Implement. lease 는 `abu_runtime` key `lease` = `{"owner", "until"}` 를 `tx()` 안에서 조건부 갱신.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): journal store with transactions, lease, runtime, api counters`

---

### Task 4: strategy (적격 판정·순위)

**Files:**
- Create: `trading/auto_buy_us/strategy.py`
- Test: `tests/test_auto_buy_us_strategy.py`

**Interfaces:**
- Consumes: `settings.STRATEGIES`
- Produces:
  - `BO = ("BREAKOUT", "RE_BREAKOUT")`, `GRADE_ORDER = {"S": 0, "A": 1, "B": 2}`, `QUALITY_ORDER = {"STRONG": 0, "MODERATE": 1, "WEAK": 2}`
  - `common_reasons(sig: dict) -> list[str]`, `b01_reasons(sig: dict) -> list[str]`, `evaluate(sig: dict) -> dict[str, tuple[bool, list[str]]]` (키 `"B00"`, `"B01"`), `grade_rank(sig) -> int`, `ordered(sigs: Iterable[dict]) -> list[dict]` (정렬 키 `(grade_rank, ticker, signal_key)`)
  - 입력 `sig` 는 `abu_signals` 행 dict. 사용 키: `strict`, `first_strict_pass`, `first_pass_basis`, `decision_price`(=`price`), `stop_loss`, `is_first_detection`, `market_condition`, `signal_type`, `ext_pct`, `entry_warnings`, `stop_pct`, `grade`, `signal_quality`, `ticker`, `signal_key`.
- 규칙 (PR #14 `eligibility.py` 정의 그대로, 단 결정 스캔만 수집하므로 `decision_role`·`scan_status`·`intraday` 사유는 없음):
  - common: `strict is None` → `STRICT_UNASSESSED`; `strict == 0` → `NOT_STRICT`; strict 1 이고 `first_strict_pass is None` → `FIRST_PASS_UNKNOWN`; `first_strict_pass == 0` → `ALREADY_PASSED`; 판단가 결측·≤0 → `INVALID_PRICE`; 손절 결측·≤0·≥판단가 → `INVALID_STOP`; 판단가 < 1 → `PRICE_BELOW_1`.
  - B01 추가: `is_first_detection` 거짓 → `LATE_STRICT_PASS`; `market_condition != "BULL"` → `MARKET_NOT_BULL`; BO 이고 `ext_pct < 0` → `BELOW_PIVOT`; 경고에 `"추격"` 포함 → `CHASE_WARNING`; `stop_pct` 결측 또는 > 15 → `STOP_GT_15`.

- [ ] **Step 1: Write the failing tests** (테이블 기반, 각 사유 하나씩 + 통과 케이스)

```python
@pytest.mark.parametrize("patch,pool,reason", [
    ({"strict": None}, "B00", "STRICT_UNASSESSED"), ({"strict": 0}, "B00", "NOT_STRICT"),
    ({"first_strict_pass": None}, "B00", "FIRST_PASS_UNKNOWN"), ({"first_strict_pass": 0}, "B00", "ALREADY_PASSED"),
    ({"stop_loss": 12.0}, "B00", "INVALID_STOP"), ({"price": 0.9, "stop_loss": 0.8}, "B00", "PRICE_BELOW_1"),
    ({"is_first_detection": 0}, "B01", "LATE_STRICT_PASS"), ({"market_condition": "CAUTION"}, "B01", "MARKET_NOT_BULL"),
    ({"ext_pct": -0.5}, "B01", "BELOW_PIVOT"), ({"entry_warnings": '["추격 주의"]'}, "B01", "CHASE_WARNING"),
    ({"stop_pct": 15.01}, "B01", "STOP_GT_15")])
def test_reason(patch, pool, reason): assert reason in evaluate({**GOOD, **patch})[pool][1]
def test_good_signal_eligible_both(): assert evaluate(GOOD) == {"B00": (True, []), "B01": (True, [])}
def test_rebound_skips_pivot_rule(): ...      # signal_type REBOUND, ext_pct -3 → B01 eligible
def test_ordered_by_grade_quality_ticker(): ... # [B/STRONG "AAA", S/WEAK "ZZZ", S/STRONG "MMM"] → MMM, ZZZ, AAA
```
`GOOD = {"strict": 1, "first_strict_pass": 1, "price": 10.0, "stop_loss": 9.0, "is_first_detection": 1, "market_condition": "BULL", "signal_type": "BREAKOUT", "ext_pct": 1.0, "entry_warnings": "[]", "stop_pct": 10.0, "grade": "A", "signal_quality": "STRONG", "ticker": "AAA", "signal_key": "US:AAA:BREAKOUT:2026-11-20"}`

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): port B00/B01 eligibility and grade ranking`

---

### Task 5: sizing (가상 자금 상태·수량·열)

**Files:**
- Create: `trading/auto_buy_us/sizing.py`
- Test: `tests/test_auto_buy_us_sizing.py`

**Interfaces:**
- Consumes: `settings.RISK`, `SECTOR_CAP_PCT`, `LIMIT_PCT`, `COSTS`; `scanner.sizing.calculate_position(entry, stop, equity, cash, open_risk_sum, "US", cfg) -> dict(qty, reasons, constrained_by, r_pct)`
- Produces:
  - `UNKNOWN_SECTOR = "UNKNOWN"`, `sector_of(name) -> str`
  - `limit_price(decision_price: Decimal) -> Decimal` — `(decision_price * (1 + LIMIT_PCT)).quantize(Decimal("0.01"), ROUND_FLOOR)`
  - `unit_cost(px: Decimal) -> float`, `unit_proceeds(px: Decimal) -> float` (PR #14 식, COSTS 사용)
  - `@dataclass class SleeveState`: `cash: float`, `equity: float`, `positions_value: float`, `reserved_cash: float`, `entry_heat_usd: float`, `current_heat_usd: float | None`, `sector_values: dict[str, float]`, `unpriced: list[str]`
  - `sleeve_state(cash: float, positions: list[dict], open_buys: list[dict]) -> SleeveState` — positions 키 `ticker, sector, qty, entry_price, initial_stop, last_price(None=시세 없음)`; open_buys 키 `ticker, sector, reserved_cash, planned_risk`. Entry Heat = Σ max(entry−stop,0)×qty + Σ planned_risk. Current Heat = Σ max(last−stop,0)×qty + Σ planned_risk; last 가 없는 포지션이 하나라도 있으면 `current_heat_usd=None`, `unpriced` 에 티커. equity = cash + Σ(qty × (last 또는 entry)) + reserved.
  - `size_order(st: SleeveState, decision_price: Decimal, stop: Decimal, sector: str) -> tuple[int, str | None, dict]` — 순서: 섹터 불명 → `(0, "SECTOR_UNKNOWN")`; 지정가 1주 비용 > 사용 가능 현금(`cash − reserved_cash`) → `INSUFFICIENT_CASH`; 섹터 여유(`0.35 × equity − sector_values[sec]`) < 1주 비용 → `SECTOR_CAP`; 그 외 `calculate_position(limit, stop, equity, min(cash_avail, sector_room)/(1+slip)/(1+bc), entry_heat_usd, "US", RISK)`; qty 0 이면 R 범위 사유는 `R_BAND`, 나머지는 첫 사유 대문자. info 키 `limit_price, planned_risk(qty×(limit−stop)), reserved_cash(qty×unit_cost(limit)), r_pct, constrained_by`.
  - 최소 현금 하한은 두지 않는다 (가상 sleeve, 설계 §18-4: 계좌 현금 하한은 실계좌 표시로만).

- [ ] **Step 1: Write the failing tests**

```python
def test_limit_price_floor(): assert limit_price(Decimal("10.07")) == Decimal("10.17")   # 10.1707 → 10.17
def test_limit_price_exact_cent(): assert limit_price(Decimal("20.00")) == Decimal("20.20")
def test_sector_unknown_blocks(): assert size_order(st(15000), D("50"), D("46"), None)[1] == "SECTOR_UNKNOWN"
def test_risk_pct_limits_qty():
    q, r, info = size_order(st(15000), D("50"), D("46"), "Tech")
    assert r is None and info["planned_risk"] <= 15000 * 0.0075 + 1e-6
def test_heat_cap_blocks():   # entry_heat_usd = 0.04*equity 이미 사용 → qty 0, reason 문자열에 "HEAT"
def test_r_band(): assert size_order(st(15000), D("50"), D("49"), "Tech")[1] == "R_BAND"   # R 2%
def test_sector_cap(): ...    # sector_values["Tech"] = 0.35*equity → "SECTOR_CAP"
def test_unpriced_position_current_heat_none(): ...   # last_price None → current_heat_usd is None, unpriced == ["AAA"]
def test_reserved_cash_reduces_available(): ...       # reserved 로 남은 현금 < 1주 → INSUFFICIENT_CASH
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): sleeve state, heat and order sizing`

---

### Task 6: signals (결정 스캔 확인·신호 수집)

**Files:**
- Create: `trading/auto_buy_us/signals.py`
- Test: `tests/test_auto_buy_us_signals.py`, `tests/auto_buy_us_fakes.py` (운영 DB 시드 헬퍼 `seed_scan(session, started_utc, finished_utc, rows, triggered_by="scheduler", status="DONE", market="US")`)

**Interfaces:**
- Consumes: `Store`, `Settings`, `us_calendar.session_for`, `database.models.ScanLog/ScanResult`, SQLAlchemy `session_factory` (기존 `database.models.SessionLocal` 과 같은 호출 형태)
- Produces:
  - `signal_key(ticker, signal_type, signal_date) -> str` = `f"US:{ticker}:{signal_type}:{signal_date}"`
  - `capture_day(store: Store, session_factory, day: date, now: float, settings) -> str` 반환 `"CAPTURED" | "NO_SIGNAL_SCAN" | "MISSED" | "PENDING"`. 이미 `abu_signal_days` 행이 있으면 그 status 를 돌려주고 아무것도 쓰지 않는다. `"PENDING"` 이면 쓰지 않는다.
- 규칙 (설계 §8.3):
  - 결정 스캔: `market IN ('US','ALL')`, `triggered_by == 'scheduler'`, `status == 'DONE'`, `started_at`(UTC naive → aware) ≥ D 정규장 `close_ts`, `finished_at` ≤ D 22:00 ET. 조건을 만족하는 것 중 가장 이른 것.
  - `MISSED`: 결정 스캔 이후 `now` 이전에 시작한 다른 US/ALL ScanLog 가 있음 (행 덮어쓰기 가능).
  - `NO_SIGNAL_SCAN`: `now` ≥ D 22:00 ET 이고 결정 스캔 없음. 그 전이면 `PENDING`.
  - 신호 행: `ScanResult.market == 'US'` 이고 `started_at ≤ scan_time ≤ finished_at`. strict 값은 그대로(None/0/1) 저장, 판단은 strategy 에서.
  - `is_first_detection = first_detected_at 의 뉴욕 날짜 == D`.
  - `first_strict_pass`: strict 1 이고 같은 `signal_key` 의 strict 1 행이 이전 `session_day` 에 있으면 0 (`PRIOR_STRICT`); 아니면 `is_first_detection` 이면 1 (`FIRST_DETECTION`); 아니면 `first_detected_at` 이 `runtime.collection_start` 이전 또는 None → None (`BEFORE_COLLECTION`); `first_detected_at` 날짜 ~ D 사이 거래일 중 `abu_signal_days.status != 'CAPTURED'` 또는 행 없음이 하나라도 → None (`CAPTURE_GAP`); 그 외 1 (`NO_PRIOR_STRICT`).
  - `ext_pct = (price/pivot − 1)×100`, `stop_pct = (1 − stop/price)×100` (값 결측이면 None). `raw_json` = 행 전체 열 JSON (`sort_keys=True`).
  - 첫 `CAPTURED` 쓰기 때 `runtime.collection_start` 가 없으면 now 로 설정 (같은 트랜잭션).
  - `signal_days` + `signals` 를 한 `tx()` 에서 쓴다.

- [ ] **Step 1: Write the failing tests**

```python
def test_captures_scheduler_scan_rows(env): ...          # 16:30 ET 시작 DONE 스캔, 행 2개 → "CAPTURED", abu_signals 2행
def test_manual_scan_ignored(env): ...                   # triggered_by="manual" 만 있음, 22:00 이후 → "NO_SIGNAL_SCAN"
def test_before_cutoff_pending_writes_nothing(env): ...  # 21:00, 스캔 없음 → "PENDING", abu_signal_days 0행
def test_later_scan_started_marks_missed(env): ...       # 결정 스캔 뒤 수동 스캔 시작 → "MISSED", signals 0행
def test_intraday_scan_not_decision(env): ...            # 15:00 ET 시작 스캔 → 결정 스캔 아님
def test_rows_outside_scan_window_excluded(env): ...     # scan_time 이 이전 스캔 시각인 행 제외
def test_idempotent(env): ...                            # 두 번 호출 → 같은 status, 행 수 불변
def test_first_pass_prior_strict(env): ...               # D1 strict 1 수집, D2 같은 key strict 1 → first_strict_pass 0
def test_first_pass_before_collection_unknown(env): ...  # first_detected_at < collection_start → None, basis BEFORE_COLLECTION
def test_first_pass_capture_gap_unknown(env): ...        # 사이 거래일 MISSED → None, basis CAPTURE_GAP
def test_operational_db_read_only(env): ...              # capture 전후 ScanLog/ScanResult 행 수·값 불변
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): capture regular US scan signals with first-pass rules`

---

### Task 7: decision (공통 판단)

**Files:**
- Create: `trading/auto_buy_us/decision.py`
- Test: `tests/test_auto_buy_us_decision.py`

**Interfaces:**
- Consumes: `strategy.evaluate/ordered`, `sizing.sleeve_state/size_order/limit_price`, `settings.STRATEGIES/RISK`, S01 `quotes.Quote`, `quotes.is_jump`, `quotes.consistent`, `quotes.limit_price`
- Produces:
  - `@dataclass(frozen=True) class EntryPlan`: `strategy, session_day: str, signal_id: int, ticker, exchange: str | None, verdict: str, reasons: list[str], qty: int, decision_price: Decimal, limit_price: Decimal | None, stop_price: Decimal, planned_risk: float, reserved_cash: float, entry_heat: float`
    - verdict ∈ `PLANNED, INELIGIBLE, HELD, RANK_CUT, SIZE_ZERO, EXCHANGE_UNRESOLVED`
  - `plan_entries(signals: list[dict], strategy: str, state: SleeveState, held: set[str], exchanges: dict[str, str | None], session_day: str) -> list[EntryPlan]` — 모든 신호에 대해 계획 행 1개. 순서: 풀 적격 아님 → INELIGIBLE(사유); 보유 중 → HELD; 거래소 None → EXCHANGE_UNRESOLVED; `ordered` 정렬 후 topn 초과 → RANK_CUT; 순서대로 `size_order` (PLANNED 가 나올 때마다 state 의 reserved_cash·entry_heat_usd·sector_values 갱신) → PLANNED 또는 SIZE_ZERO(사유). topn 은 PLANNED 개수 기준이 아니라 **적격 정렬 순위** 기준 (PR #14 와 같음).
  - `@dataclass(frozen=True) class OpenVerdict`: `ok: bool`, `reason: str | None` — `open_check(stop: Decimal, price: Decimal) -> OpenVerdict`: `price ≤ stop` → `OPEN_BELOW_STOP`; `(price−stop)/price×100 < RISK["MIN_R_PCT"]` → `OPEN_R_TOO_SMALL`.
  - `@dataclass(frozen=True) class StopVerdict`: `action: Literal["NONE","TRIGGER","CONFIRM","REVIEW"]`, `reason: str | None`
  - `stop_check(stop: Decimal, price: Decimal, prev_price: Decimal | None, settings) -> StopVerdict`: price > stop → NONE; `is_jump(prev, price, settings)` → CONFIRM; else TRIGGER.
  - `confirm_stop(first: Decimal, second: Decimal, stop: Decimal, settings) -> StopVerdict`: `consistent(first, second, settings)` 이고 second ≤ stop → TRIGGER; 일치하지만 second > stop → NONE; 불일치 → REVIEW(`QUOTE_JUMP_UNCONFIRMED`).
  - `exit_limit(price: Decimal, settings) -> Decimal` = S01 `quotes.limit_price(price, settings.slip_bps)`. 기준가 결과가 $1 미만이면 `ValueError` (호출 측이 NEEDS_REVIEW `PRICE_BELOW_1`).

- [ ] **Step 1: Write the failing tests**

```python
def test_b00n_takes_top2_by_grade(): ...      # 적격 3개 (S,A,B) → B00N: PLANNED 2 (S,A) + RANK_CUT 1
def test_b00_has_no_topn(): ...               # 같은 입력 → B00 PLANNED 3 (자금 충분)
def test_b01_filters_before_rank(): ...       # S 등급이 MARKET_NOT_BULL → B01 순위에서 빠지고 A,B 가 PLANNED
def test_held_ticker_not_replanned(): ...     # held={"AAA"} → verdict HELD
def test_sequential_sizing_consumes_heat(): ...  # 열 한도 근처: 두 번째 후보 SIZE_ZERO, reasons 에 HEAT
def test_one_plan_row_per_signal(): ...       # len(plans) == len(signals)
def test_open_check_below_stop(): assert open_check(D("9"), D("9")).reason == "OPEN_BELOW_STOP"
def test_open_check_r_small(): assert open_check(D("9.80"), D("10")).reason == "OPEN_R_TOO_SMALL"   # 2%
def test_open_check_ok(): assert open_check(D("9"), D("10")).ok
def test_stop_jump_requires_confirm(): assert stop_check(D("50"), D("30"), D("55"), S).action == "CONFIRM"
def test_confirm_mismatch_review(): assert confirm_stop(D("30"), D("45"), D("50"), S).action == "REVIEW"
def test_exit_limit_slip(): assert exit_limit(D("100.00"), S) == D("99.50")
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): shared entry planning, open check and stop decisions`

---

### Task 8: limiter + market (키움 조회·호출 예산)

**Files:**
- Create: `trading/auto_buy_us/limiter.py`, `trading/auto_buy_us/market.py`
- Modify: `tests/auto_buy_us_fakes.py` (`FakeKiwoomSession`: api-id 별 대본 응답, 호출 시각 기록, 429·timeout·8005 주입)
- Test: `tests/test_auto_buy_us_market.py`

**Interfaces:**
- Consumes: `Settings`, S01 `brokers.LiveStopSellBroker`, `quotes.validate`, `us_calendar.market_clock`, `kiwoom_readonly.KiwoomConfig/KiwoomReadOnlyClient`, `web.kiwoom_holdings._get_token`, S01 `settings.read_access_block_reason`
- Produces (limiter.py):
  - `class RateLimited(Exception)`, `class BudgetExhausted(Exception)` — **`KiwoomError` 의 하위 클래스가 아니다** (S01 `_read_retry` 의 429 재시도 루프를 타지 않게).
  - `class CallBudget(settings, clock=time.time, sleep=time.sleep)`: `start_tick() -> None`; `acquire(api_id: str) -> None` (쿨다운 중·tick 상한 도달·KST 09:00~10:00 이면 `BudgetExhausted`; 직전 호출(전체)·같은 TR 직전 호출로부터 각각 1초가 지나도록 sleep); `on_response(api_id, status_code) -> None` (429 면 `cooldown_until = now + 60` 후 `RateLimited`); `drain_stats() -> dict[tuple[str, str], int]` (outcome `ok/err/http_429/timeout/budget_skip`).
  - `class ThrottledSession(requests.Session)` `__init__(budget: CallBudget)`; `post(url, *args, headers=None, **kw)` — `api_id = (headers or {}).get("api-id", "oauth")`; acquire → `super().post` → on_response. `requests.Timeout` 은 timeout 으로 집계 후 재발생.
- Produces (market.py):
  - `@dataclass(frozen=True) class QuoteEvidence`: `ticker, exchange, requested_at: float, received_at: float, as_of: float | None, price: Decimal | None, source: str, valid: bool, invalid_reason: str | None`
  - `@dataclass(frozen=True) class AccountSnapshot`: `at: float, net_asset_usd: Decimal | None, orderable_cash_usd: Decimal | None, holdings_count: int | None, complete: bool, reason: str | None` — 순자산 = `ust21110` USD `fc_entra` + `ust21120` USD `evlt_amt` (둘 중 하나라도 없으면 `complete=False`, 0 으로 대체하지 않음). 주문가능 = `ust21110` USD `fc_ord_alowa`.
  - `class KiwoomMarketData(reader, budget: CallBudget, settings, clock=time.time)`:
    - `quote(ticker: str, exchange: str) -> QuoteEvidence` — `reader.quote()` 결과를 `quotes.validate(q, now, market_clock(now, settings), settings)` 로 검사. `QuoteUnavailable`·`KiwoomError` 는 `valid=False` 로 돌려준다(예외 아님). `RateLimited`·`BudgetExhausted` 는 그대로 올린다.
    - `resolve_exchange(ticker: str) -> str | None` — `reader.probe_exchange(ticker)` 성공 시 거래소, `QuoteUnavailable` 이면 None.
    - `account_snapshot() -> AccountSnapshot` — `client.get_overseas_deposit(token)`, `client.get_overseas_currency_valuation(token)`, `reader.holdings()` (`BalanceUnavailable` → `holdings_count=None`, `complete=False`).
  - `build_market_data(settings, budget: CallBudget) -> KiwoomMarketData` — `read_access_block_reason()` 가 있으면 `RuntimeError(사유)`; `KiwoomConfig.from_profile("account1")`; `KiwoomReadOnlyClient(config, session=ThrottledSession(budget))`; `_get_token("account1", config, client)`; `LiveStopSellBroker(config, client=client, token=token, allow_orders=False)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_global_spacing_one_per_sec(fake): ...        # 3회 acquire → 기록된 호출 간격 ≥ 1.0초
def test_per_tr_spacing(fake): ...                    # 같은 TR 연속 → ≥1초, 서로 다른 TR 도 전체 간격 ≥1초
def test_tick_cap_20(fake): ...                       # 21번째 acquire → BudgetExhausted, budget_skip 집계
def test_peak_hours_zero_calls(fake): ...             # KST 09:30 → acquire 즉시 BudgetExhausted, 세션 호출 0
def test_429_stops_and_cools_down(fake): ...          # 429 응답 → RateLimited; 59초 뒤 acquire → BudgetExhausted; 61초 뒤 OK
def test_429_not_retried_by_s01_helper(fake): ...     # reader.quote 에서 429 → RateLimited 전파, 세션 호출 정확히 1회
def test_quote_valid(fake): ...                       # dt/bid_tm = 10:01, now 10:02 → valid, as_of·price 채움
def test_quote_stale_invalid(fake): ...               # 3분 전 시각 → valid False, invalid_reason 에 "초 지났습니다"
def test_quote_premarket_invalid(fake): ...           # 09:20 시각 → invalid
def test_invalid_token_response_is_invalid_quote(fake): ...   # return_code 8005 → valid False, 예외 없음
def test_ambiguous_exchange_unresolved(fake): ...     # ND·NY 둘 다 응답 → None
def test_account_snapshot_incomplete_not_zero(fake): ...      # ust21120 USD 항목 없음 → complete False, net_asset_usd None
def test_no_order_client_import():
    src = pathlib.Path("trading/auto_buy_us").rglob("*.py")
    assert not any("kiwoom_orders" in p.read_text() for p in src)
def test_does_not_patch_s01_session(fake): ...        # build 후 S01 runner 가 쓰는 세션 객체와 다른 세션 (id 비교)
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_auto_buy_us_market.py -q` → FAIL.
- [ ] **Step 3:** Implement. 시각 대본: 가짜 세션 응답의 `dt`/`bid_tm` 은 테스트 시계 기준으로 만든다.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): kiwoom read-only market data with shadow call budget`

---

### Task 9: ledger (원자적 도메인 트랜잭션)

**Files:**
- Create: `trading/auto_buy_us/ledger.py`
- Test: `tests/test_auto_buy_us_ledger.py`

**Interfaces:**
- Consumes: `Store`, `EntryPlan`, `QuoteEvidence`, `sizing.unit_cost/unit_proceeds`, `settings` 상수
- Produces (모든 함수는 `db` = `Store.tx()` 연결을 받는다. 상태 전이는 `UPDATE … WHERE id=? AND state=?`, 영향 0 행이면 `StaleState` 예외 → 호출 측 tx 전체 롤백):
  - `class StaleState(Exception)`
  - `record_quote(db, q: QuoteEvidence) -> int`
  - `init_sleeves(db, snapshot: AccountSnapshot, now) -> Decimal` — `abu_sleeves` 가 비어 있고 `snapshot.complete` 일 때만 세 전략에 `initial_cash = cash = net_asset_usd × 0.30` (센트 내림). 이미 있으면 기존 값 반환. 불완전하면 `ValueError`.
  - `save_plans(db, plans: list[EntryPlan], now, live_block: dict[str, str | None]) -> list[int]` — 모든 계획을 `abu_decisions` 에, PLANNED 만 `abu_orders`(side BUY, kind ENTRY, state PLANNED, `expires_at` = D+1 마감 ts) 에.
  - `transition(db, order_id, frm: str, to: str, now, **evidence) -> None` — `abu_order_events` seq+1 삽입 포함.
  - `start_buy(db, order_id, q_id: int, q: QuoteEvidence, verdict: OpenVerdict, ref: str, now) -> str` — ok → WORKING(`created_at=now`, `created_quote_id`, `created_quote_as_of=q.as_of`, `broker_ref=ref`), 아니면 SKIPPED_OPEN(`end_reason`). 반환 새 상태.
  - `record_buy_fill(db, order_id, price: Decimal, q_id: int, now) -> int` — 한 트랜잭션: `abu_fills`(cum_qty=qty), order FILLED, `abu_positions` OPEN(`initial_stop`=결정 stop, `entry_price`=price), sleeve `cash −= qty × unit_cost(price)`. 반환 position_id. 현금 음수가 되면 `StaleState`.
  - `end_buy(db, order_id, to: Literal["EXPIRED","EXPIRED_UNOBSERVED","NOT_STARTED","BLOCKED"], reason: str, now) -> None`
  - `trigger_stop(db, position_id, q_id, limit: Decimal, quote_as_of: float, kind: str, now) -> int` — position OPEN→EXITING, SELL order WORKING 생성.
  - `record_sell_fill(db, order_id, price, q_id, now) -> None` — fill, SELL FILLED, position CLOSED(`exit_reason="STOP"`, `exit_price`), sleeve `cash += qty × unit_proceeds(price)`, `realized_pnl += qty×unit_proceeds(price) − qty×unit_cost(entry)`.
  - `reprice_sell(db, order_id, limit, q_id, quote_as_of, now) -> int` — 기존 SELL CANCELLED(가상) + 새 SELL WORKING(kind REPLACE), `session_replace_count += 1`.
  - `carry_over(db, session_day, now) -> list[int]` — 마감 시 WORKING SELL: CANCELLED + `carry_over_count += 1`; 한도 초과 포지션은 NEEDS_REVIEW (`CARRY_OVER_LIMIT`). 다음 세션 첫 유효 시세에서 engine 이 kind CARRY_OVER 로 새 주문.
  - `needs_review(db, position_id, reason, now) -> None`
  - `set_last_price(db, position_id, price, at) -> None`
  - `record_account_snapshot(db, snap: AccountSnapshot) -> None` — `abu_account_snapshots` 1행 (불완전도 `complete=0` 으로 기록)
  - 금액 반올림 규칙: sleeve `cash`·`realized_pnl`·체결 금액은 계산 후 `quantize(Decimal("0.01"), ROUND_HALF_UP)` 로 저장한다.

- [ ] **Step 1: Write the failing tests**

```python
def test_buy_fill_atomic(store): ...        # record_buy_fill 중 position 삽입 실패 주입 → fills·order·sleeve 모두 이전 값
def test_double_fill_rejected(store): ...   # 같은 order 두 번 → 두 번째 StaleState, 포지션 1개
def test_cash_math(store): ...              # initial 15000, 10주 @50 → cash == "14498.25" (15000 − 501.75125, HALF_UP)
def test_sell_fill_closes_and_realizes(store): ...
def test_stop_trigger_and_fill_events_sequence(store): ...   # order_events seq 1..n, from/to 연속
def test_end_buy_terminal_immutable(store): ...  # EXPIRED 후 transition → IntegrityError 또는 StaleState
def test_init_sleeves_once(store): ...       # 두 번째 호출 다른 snapshot → 기존 값 유지
def test_init_sleeves_incomplete_raises(store): ...
def test_carry_over_limit_review(store): ...  # carry_over_count 1 인 포지션 다시 이월 → NEEDS_REVIEW
def test_save_plans_writes_all_verdicts(store): ...  # 5 계획(PLANNED 2) → decisions 5행, orders 2행
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement (금액은 Decimal, 저장 시 `str(q.quantize(Decimal("0.01")))`; unit_cost 의 float 결과는 `Decimal(repr(x))` 로 변환). **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): atomic ledger transactions for orders, fills, positions and sleeves`

---

### Task 10: fill_model + executor

**Files:**
- Create: `trading/auto_buy_us/fill_model.py`, `trading/auto_buy_us/executor.py`
- Test: `tests/test_auto_buy_us_executor.py`

**Interfaces:**
- Consumes: `QuoteEvidence`, `abu_orders` 행 dict
- Produces:
  - `buy_fill(order: dict, q: QuoteEvidence) -> Decimal | None` — `q.valid` and `q.as_of ≥ floor_minute(order.created_at) + 60` and `q.received_at > order.created_at` and `q.price ≤ limit` → `limit` (체결가 = 지정가). 주문 생성 근거 시세(`q` 의 id 가 `created_quote_id`)는 항상 None.
  - `sell_fill(order: dict, q: QuoteEvidence) -> Decimal | None` — `q.valid` and `q.as_of > order.created_quote_as_of` and `q.price ≥ limit` → `limit`.
  - `has_gap(valid_received_at: list[float], start: float, end: float, gap_max_sec: int) -> bool` — `[start] + sorted(times) + [end]` 의 인접 간격 중 하나라도 `> gap_max_sec`.
  - `@dataclass(frozen=True) class SubmitResult`: `status: Literal["ACCEPTED","REJECTED","UNKNOWN"]`, `ref: str | None`, `reason: str | None`
  - `@dataclass(frozen=True) class Observation`: `state: Literal["WORKING","FILLED"]`, `price: Decimal | None`, `basis: str`
  - `class ShadowExecutor`: `kind = "SHADOW"`; `submit(order: dict) -> SubmitResult` (`ACCEPTED`, ref `"V" + uuid4().hex[:12]`); `observe(order: dict, q: QuoteEvidence) -> Observation` (BUY → buy_fill, SELL → sell_fill; basis `"CUR_V1:price<=limit"` 등); `cancel(order) -> SubmitResult("ACCEPTED")`.
  - `class LiveExecutor`: `kind = "LIVE"`; `__init__` 에서 `NotImplementedError("LIVE Executor 미구현 — 별도 승인 필요")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_creation_quote_never_fills(): ...           # 생성 시세 price ≤ limit 이어도 None
def test_same_minute_quote_rejected(): ...           # created 10:05:20, as_of 10:05 → None
def test_next_minute_fills_at_limit(): assert buy_fill(o, q(as_of="10:06", price="9.90")) == D("10.10")
def test_price_above_limit_no_fill(): ...
def test_invalid_quote_never_fills(): ...
def test_sell_needs_newer_as_of(): ...               # as_of == created_quote_as_of → None
def test_has_gap_start_edge(): assert has_gap([t0+400], t0, t0+500, 300)
def test_has_gap_none(): assert not has_gap([t0+250, t0+500], t0, t0+700, 300)
def test_live_executor_refuses(): pytest.raises(NotImplementedError, LiveExecutor)
def test_shadow_submit_virtual_ref(): assert ShadowExecutor().submit({}).ref.startswith("V")
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): CUR_V1 fill model, shadow executor and refusing live executor`

---

### Task 11: engine tick + runner + scheduler

**Files:**
- Create: `trading/auto_buy_us/engine.py`, `trading/auto_buy_us/runner.py`
- Modify: `scheduler.py` (`AUTO_BUY_US_JOB_ID = "auto_buy_us_rt_tick"`, `_run_auto_buy_us()`, `_add_auto_buy_us_job(scheduler)` — `IntervalTrigger(seconds=Settings.from_env().tick_sec, timezone=NEW_YORK)`, `max_instances=1`, `coalesce=True`, `replace_existing=True`; `_add_auto_stop_job` 호출 바로 뒤에서 호출)
- Modify: `tests/auto_buy_us_fakes.py` (`FakeMarket`: 종목별 시세 대본, 호출 기록; `make_engine(...)`)
- Test: `tests/test_auto_buy_us_engine.py`

**Interfaces:**
- Consumes: Task 1~10 전부
- Produces:
  - `class Engine(store, market_factory: Callable[[CallBudget], KiwoomMarketData], settings, *, session_factory, clock=time.time, owner: str, executor=None, notifier=None)` — executor 기본 `ShadowExecutor()`.
  - `Engine.tick() -> dict` 키: `status` (`OFF | KILLED | LEASE_BUSY | ERROR_HOLD | OK | LEASE_LOST`), `phase` (`CLOSED | REGULAR | AFTER_CLOSE`), `calls: int`, `actions: dict[str, int]`
  - `runner.auto_buy_us_tick() -> dict` — 프로세스 잠금(`threading.Lock`, non-blocking) + `schema_status` 확인(미준비면 `SCHEMA_NOT_READY`, 모드 SHADOW 면 하루 1회 WARN 알림) + Engine 조립. 예외는 로그 후 `{"status": "ERROR"}`.
  - `runner.notifier(severity, text) -> bool` — `notifications.slack.send_slack_message`, `notifications.telegram.send_telegram_message`, 접두사 `[자동매수 SHADOW]`.
- tick 순서 (설계 §8.2; 모든 키움 호출은 tx 밖, 호출 전후 `renew_lease`):
  1. `mode == "OFF"` → `{"status": "OFF"}` (lease·호출 없음). `kill` → heartbeat 만, `KILLED`.
  2. lease 획득 실패 → `LEASE_BUSY`. 연속 오류 ≥ 5 → 회복 확인용 시세 1회만, 성공 시 카운터 0.
  3. `clock = market_clock(now, settings)`; `budget.start_tick()`.
  4. **AFTER_CLOSE / CLOSED (세션 없는 날 포함)**: (a) 마지막 거래일 D 에 대해 `capture_day`; (b) `CAPTURED` 이고 D 다음 세션의 계획이 없으면: 그날 `account_snapshot` 1회 → `record_account_snapshot`; sleeve 없으면 그 스냅샷으로 `init_sleeves` (불완전 → `SLEEVE_INIT_PENDING` 이벤트, 계획 안 함); 신규 티커 `resolve_exchange` (결과는 `runtime.exchanges` 에 캐시); 전략별 `plan_entries` → `save_plans` (live_block: 그날 account_snapshot 의 `orderable_cash_usd` 가 PLANNED 의 reserved_cash 누계보다 작으면 `"LIVE_INSUFFICIENT_ORDERABLE_CASH"`); (c) 마감 후 첫 tick 에 `report.close_day`. 피크 시간이면 (a)·(c) 의 DB 작업만 하고 호출은 `BudgetExhausted` 로 다음 tick 에 미룬다.
  5. **REGULAR**: 감시 대상 = SELL WORKING 종목 ① → OPEN/EXITING 포지션 ② → BUY WORKING ③ → BUY PLANNED(세션 D+1) ④, 종목 중복 제거, `quote_interval_sec` 이 지난 종목만, 최대 `max_watch`. 각 종목: `quote` → `record_quote` → 같은 tx 에서 그 종목의 모든 주문·포지션 처리:
     - PLANNED: valid 이면 `executor.submit` → `start_buy`. 개장 후 `open_check_wait_min` 지나도 valid 시세가 없으면 `end_buy(NOT_STARTED)`.
     - BUY WORKING: `observe` → FILLED 이면 `record_buy_fill`.
     - 포지션 OPEN: `set_last_price`; `stop_check` → TRIGGER 면 (cutoff 전) `trigger_stop(kind ORIGINAL)`, (cutoff 후) 다음 세션으로 (이벤트만); CONFIRM 이면 같은 tick 안에서 1회 재조회 후 `confirm_stop`; REVIEW → `needs_review`. `exit_limit` 가 ValueError → `needs_review(PRICE_BELOW_1)`.
     - SELL WORKING: `observe` → FILLED 면 `record_sell_fill`; 미체결이고 `replace_after_sec` 경과·cutoff 전·`session_replace_count < 3` → `reprice_sell`. 이월된 포지션(EXITING, 열린 SELL 없음)은 새 세션 첫 valid 시세에서 `trigger_stop(kind CARRY_OVER)`.
     - `RateLimited`/`BudgetExhausted` → 남은 종목은 이번 tick 건너뜀 (`budget_skip`), 오류 카운터는 429 만 증가.
  6. 세션 마감(now ≥ close_ts, 그 세션 처리 전): BUY WORKING → 종목의 valid 시세 `received_at` 목록으로 `has_gap(start=created_at, end=close_ts)` → `EXPIRED` / `EXPIRED_UNOBSERVED`; 아직 PLANNED → `NOT_STARTED`; `carry_over`.
  7. `drain_stats` → `count_api`; heartbeat(`runtime.heartbeat = now`); 새 `system_events` 중 WARN/URGENT 를 notifier 로 전송(실패해도 tick 계속); `release_lease`.

- [ ] **Step 1: Write the failing tests** (가짜 시계·가짜 시장·임시 운영 DB, 2026-11-23(월) 스캔 → 11-24(화) 세션)

```python
def test_off_mode_does_nothing(env): ...                      # status OFF, FakeMarket 호출 0, 저널 행 0
def test_full_day_fill_and_stop(env):
    # 월 16:30 스캔 2종목 → 월 17:00 tick: 계획(B00 2, B00N 2, B01 조건대로)
    # 화 09:31 첫 시세 → WORKING, 09:33 price≤limit → FILLED·포지션, 11:00 price≤stop → EXITING, 11:01 ≥ limit → CLOSED
    # 검증: 각 전이 1회, sleeve 현금 = 식, order_events 연속
def test_expired_vs_unobserved(env): ...                     # 종목 A 는 매분 시세(미체결) → EXPIRED, 종목 B 는 11:00~11:10 공백 → EXPIRED_UNOBSERVED
def test_not_started_without_quote(env): ...                 # 10:00 까지 valid 없음 → NOT_STARTED
def test_skipped_open(env): ...                              # 첫 시세 ≤ stop → SKIPPED_OPEN
def test_shared_ticker_queried_once_per_tick(env): ...       # 세 전략 같은 종목 → tick 당 quote 1회, 세 주문 같은 created_quote_id
def test_early_close_day_expires_at_1300(env): ...           # 2026-11-27(금, 조기폐장) 세션 → 13:00 직후 tick 에서 만료, 12:55 이후 신규 가상 매도 없음
def test_restart_gap_marks_unobserved(env): ...              # owner A 10:00 까지, lease 만료, owner B 10:20 부터 → 같은 WORKING 이어받고 마감 시 EXPIRED_UNOBSERVED
def test_two_engines_same_tick_no_duplicates(env): ...       # 두 Engine 동시 tick → 한쪽 LEASE_BUSY, 주문·체결 중복 0
def test_lease_lost_stops_writes(env): ...                   # 호출 중 lease 탈취 → LEASE_LOST, 이후 쓰기 0
def test_kill_switch(env): ...                               # AUTO_BUY_US_RT_KILL=true → KILLED, 호출 0, heartbeat 기록
def test_error_hold_after_5_errors(env): ...                 # 연속 5회 invalid(오류) → 신규 계획·주문 시작 없음, 회복 후 재개
def test_429_skips_rest_of_tick(env): ...                    # 2번째 종목 429 → 나머지 종목 이번 tick 미조회, api_calls http_429 == 1
def test_unresolved_exchange_not_planned(env): ...           # probe 모호 → decision EXCHANGE_UNRESOLVED, order 없음
def test_sleeve_init_pending_blocks_planning(env): ...       # account_snapshot 불완전 → 계획 0, SLEEVE_INIT_PENDING 이벤트
def test_gap_down_does_not_assume_stop_price(env): ...       # 첫 시세가 stop 아래 → 체결가는 이후 새 시세 기준 지정가, initial_stop 으로 체결 기록 없음
def test_cutoff_trigger_carries_to_next_session(env): ...    # 15:57 손절 조건 → 가상 매도 없음, 다음 세션 첫 시세에서 CARRY_OVER 주문
def test_scheduler_registers_job():                          # scheduler 모듈 함수 호출 후 job id 존재, max_instances 1
def test_runner_schema_not_ready(env): ...                   # 저널 미Migration → SCHEMA_NOT_READY, 파일 생성 없음
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_auto_buy_us_engine.py -q` → FAIL.
- [ ] **Step 3:** Implement `engine.py`, `runner.py`, `scheduler.py` 수정.
- [ ] **Step 4:** Run → PASS. `.venv/bin/python -m pytest tests/test_auto_stop_engine.py tests/test_scheduler.py -q` 도 PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): single-writer tick engine, runner and scheduler job (default OFF)`

---

### Task 12: report (성과·운영 보고)

**Files:**
- Create: `trading/auto_buy_us/report.py`
- Test: `tests/test_auto_buy_us_report.py`

**Interfaces:**
- Consumes: Store, 저널 표, `sizing.sleeve_state`
- Produces:
  - `close_day(db, session_day: str, now: float) -> list[dict]` — 전략별 `abu_daily_perf` 1행 (이미 있으면 쓰지 않음). 평가는 포지션 `last_valid_price` 가 그 세션 날짜 것일 때만; 아니면 그 포지션 `unpriced` +1, `status="INCOMPLETE"`. 열: `equity, cash, open_value, unpriced, entries, closed, status`.
  - `metrics(db, strategy: str) -> dict` 키: `cum_return`, `mdd` (daily_perf COMPLETE 일 기준; INCOMPLETE 가 섞이면 `mdd_status="INCOMPLETE_DAYS:<n>"`), `pf_closed`, `avg_r_closed`, `avg_r_incl_open` (R = (평가가 − entry)/(entry − initial_stop)), `entries`, `closed`, `fill_rate` (FILLED / (FILLED+EXPIRED)), `unobserved` (EXPIRED_UNOBSERVED + NOT_STARTED 건수), `entry_heat`, `current_heat` (unpriced 있으면 None + `current_heat_status="UNPRICED"`), `observation_gap_rate`, `quote_freshness_p95_sec`.
  - `milestones(db, strategy) -> dict` = `{"trading_days": n, "reached": [5, 20, 60, 120 중 도달], "entries_40": bool, "verdict": "NOT_RECOMMENDED", "b01": "NEED_MORE_DATA", "live_approval": False}`
  - `daily_report(db, session_day) -> str` — 섹션: 감시(종목 수, 정상 시세 수, 누락, 최대 지연), 주문(상태별 건수), 포지션·손절, API(TR·outcome 별, 429), 오류·이벤트, 전략별 지표·마일스톤. INCOMPLETE·불명이 있으면 첫 줄에 `⚠ 미완료 데이터 포함`.
  - engine 의 마감 처리(Task 11 단계 4c)가 `close_day` 뒤 `daily_report` 를 notifier 로 INFO 전송.

- [ ] **Step 1: Write the failing tests**

```python
def test_unpriced_day_incomplete(db): ...         # 포지션 last_valid_price 전일 것 → status INCOMPLETE, unpriced 1, open_value 에 0 대입 안 함
def test_mdd_excludes_incomplete_flagged(db): ... # mdd_status 에 INCOMPLETE_DAYS
def test_avg_r_split(db): ...                     # 청산 1건(−1R) + 미청산 1건(+2R) → avg_r_closed −1, avg_r_incl_open 0.5
def test_fill_rate_excludes_unobserved(db): ...   # FILLED 1, EXPIRED 1, UNOBSERVED 2 → 0.5, unobserved 2
def test_entries_40_and_no_live_approval(db): ... # 진입 40 → entries_40 True, live_approval False
def test_report_flags_incomplete_first_line(db): ...
def test_close_day_idempotent(db): ...
```

- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Run → PASS.
- [ ] **Step 5: Commit** `feat(auto-buy-us): daily performance, metrics, milestones and operations report`

---

### Task 13: 문서·진입점·전체 검증

**Files:**
- Create: `docs/auto_buy_us_shadow.md`
- Modify: `CLAUDE.md`, `AGENTS.md` (각 한 줄: "account1 미국 자동매수 LIVE-Oriented SHADOW: `docs/auto_buy_us_shadow.md`. 기본 `AUTO_BUY_US_RT_MODE=OFF`, OFF/SHADOW 만, 주문 TR 미호출. 저널은 `scripts.auto_buy_us_journal_migrate` 로만.")

- [ ] **Step 1:** `docs/auto_buy_us_shadow.md` 작성 — 섹션: 무엇을 하나/하지 않는 것, 구성 표(파일별 책임), tick 흐름, 신호·주문 시점 정책(설계 §8.3 표), 상태 머신(설계 §10), 호출 예산(§9.3), 저널 표·Migration 절차(백업·`--check`·종료 코드), env 표, 미검증 API 계약과 확인 전 정책(설계 §5 표), 운영 검증 일정(5/20/60/120, 진입 40건, LIVE 자동 승인 없음), 일일 보고 읽는 법, PR #14 와의 관계(동결·재사용 범위).
- [ ] **Step 2:** 지정 환경 전체: `.venv/bin/python -m pytest -q` → Task 0 기준선 대비 실패 0, 새 테스트 수 증가 확인. 결과 숫자 기록.
- [ ] **Step 3:** 호환 환경(있다면) 전체 실행, 결과를 지정 환경과 **구분해** 기록.
- [ ] **Step 4:** 반복 안정성: `for i in 1 2 3 4 5; do .venv/bin/python -m pytest tests/test_auto_buy_us_engine.py tests/test_auto_buy_us_ledger.py -q || break; done` → 5회 모두 PASS.
- [ ] **Step 5:** 정적 확인: `grep -rn "kiwoom_orders\|ust2000[013]\|CREATE TABLE" trading/auto_buy_us --include=*.py | grep -v journal_migrations` → 출력 없음. `git diff --stat origin/main -- trading/auto_stop trading/kiwoom_readonly.py trading/kiwoom_orders.py scanner web/kiwoom_holdings.py` → 출력 없음.
- [ ] **Step 6: Commit** `docs(auto-buy-us): operations guide and repository entry points`
- [ ] **Step 7:** 사용자에게 보고 (한국어): 테스트 수치(환경 구분), 미검증 계약 목록, 남은 위험. Push·PR 은 사용자 승인 후.

---

## 실행 범위 밖 (별도 승인)

- VM 배포·저널 Migration 실행·SHADOW 활성화 (설계 §14 P6·P7)
- 조회 전용 계약 확인 (`usa20101` 시각 주기·시가·1호가, TR별 한도, account1 `ust31490`)
- S01 공통 호출 관리기, LIVE Executor·S01 자동 등록 (설계 §6.4)
