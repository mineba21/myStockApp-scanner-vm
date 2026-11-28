"""자동매수 RT1 저널 스키마 v1 DDL (동결). 이 파일은 수정하지 않는다.

리비전 abu0001 이 실행하는 문장 그대로다. 스키마를 바꿀 때는 새 리비전과 새 DDL 모듈을 추가한다.
금액·가격은 Decimal 문자열(TEXT), 시각은 epoch 초(REAL), 날짜는 YYYY-MM-DD(TEXT).
"""

_IMMUTABLE = ("abu_signal_days", "abu_signals", "abu_decisions", "abu_order_events", "abu_fills", "abu_quotes",
              "abu_daily_perf")


def _no_change_triggers(table: str) -> tuple:
    return tuple(
        f"CREATE TRIGGER {table}_no_{op.lower()} BEFORE {op} ON {table} "
        f"BEGIN SELECT RAISE(ABORT, 'abu immutable'); END"
        for op in ("UPDATE", "DELETE"))


DDL_V1 = (
    "CREATE TABLE abu_signal_days (session_day TEXT PRIMARY KEY, "
    "status TEXT NOT NULL CHECK(status IN ('CAPTURED','NO_SIGNAL_SCAN','MISSED')), "
    "scan_log_id INTEGER, captured_at REAL)",
    "CREATE TABLE abu_signals (id INTEGER PRIMARY KEY AUTOINCREMENT, session_day TEXT NOT NULL, "
    "scan_log_id INTEGER NOT NULL, scan_result_id INTEGER, ticker TEXT NOT NULL, name TEXT, signal_type TEXT, "
    "signal_date TEXT, signal_key TEXT NOT NULL, grade TEXT, signal_quality TEXT, price REAL, pivot REAL, "
    "stop_loss REAL, market_condition TEXT, strict INTEGER, first_detected_at TEXT, "
    "is_first_detection INTEGER NOT NULL, first_strict_pass INTEGER, first_pass_basis TEXT, ext_pct REAL, "
    "stop_pct REAL, sector_name TEXT, entry_warnings TEXT, raw_json TEXT NOT NULL, "
    "strategy_version TEXT NOT NULL, captured_at REAL NOT NULL, UNIQUE(scan_log_id, ticker))",
    "CREATE INDEX abu_signals_key_day ON abu_signals(signal_key, session_day)",
    "CREATE TABLE abu_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, "
    "session_day TEXT NOT NULL, signal_id INTEGER NOT NULL REFERENCES abu_signals(id), ticker TEXT NOT NULL, "
    "exchange TEXT, verdict TEXT NOT NULL, reasons TEXT, qty INTEGER, decision_price TEXT, limit_price TEXT, "
    "stop_price TEXT, planned_risk REAL, reserved_cash REAL, entry_heat REAL, live_block_reason TEXT, "
    "strategy_version TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(strategy, session_day, ticker))",
    "CREATE TABLE abu_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, "
    "ticker TEXT NOT NULL, exchange TEXT, qty INTEGER NOT NULL, entry_price TEXT NOT NULL, "
    "initial_stop TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('OPEN','EXITING','CLOSED','NEEDS_REVIEW')), opened_at REAL NOT NULL, "
    "opened_day TEXT NOT NULL, closed_at REAL, exit_reason TEXT, exit_price TEXT, "
    "session_replace_count INTEGER NOT NULL DEFAULT 0, carry_over_count INTEGER NOT NULL DEFAULT 0, "
    "last_valid_price TEXT, last_valid_at REAL)",
    "CREATE UNIQUE INDEX abu_position_live ON abu_positions(strategy, ticker) WHERE state != 'CLOSED'",
    "CREATE TABLE abu_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, strategy TEXT NOT NULL, "
    "side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), "
    "kind TEXT NOT NULL CHECK(kind IN ('ENTRY','ORIGINAL','REPLACE','CARRY_OVER')), "
    "decision_id INTEGER REFERENCES abu_decisions(id), position_id INTEGER REFERENCES abu_positions(id), "
    "ticker TEXT NOT NULL, exchange TEXT, qty INTEGER NOT NULL, limit_price TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('PLANNED','WORKING','FILLED','EXPIRED','EXPIRED_UNOBSERVED',"
    "'NOT_STARTED','SKIPPED_OPEN','BLOCKED','CANCELLED')), "
    "executor TEXT NOT NULL CHECK(executor IN ('SHADOW','LIVE')), fill_model TEXT, open_check_basis TEXT, "
    "created_at REAL NOT NULL, created_quote_id INTEGER, created_quote_as_of REAL, session_day TEXT, "
    "expires_at REAL, broker_ref TEXT, ended_at REAL, end_reason TEXT)",
    "CREATE UNIQUE INDEX abu_order_open ON abu_orders(strategy, ticker, side) "
    "WHERE state IN ('PLANNED','WORKING')",
    "CREATE TRIGGER abu_orders_terminal_no_update BEFORE UPDATE ON abu_orders "
    "WHEN OLD.state NOT IN ('PLANNED','WORKING') BEGIN SELECT RAISE(ABORT, 'abu immutable'); END",
    "CREATE TRIGGER abu_orders_terminal_no_delete BEFORE DELETE ON abu_orders "
    "WHEN OLD.state NOT IN ('PLANNED','WORKING') BEGIN SELECT RAISE(ABORT, 'abu immutable'); END",
    "CREATE TABLE abu_order_events (order_id INTEGER NOT NULL REFERENCES abu_orders(id), seq INTEGER NOT NULL, "
    "from_state TEXT, to_state TEXT NOT NULL, at REAL NOT NULL, evidence_json TEXT, PRIMARY KEY(order_id, seq))",
    "CREATE TABLE abu_fills (order_id INTEGER NOT NULL REFERENCES abu_orders(id), cum_qty INTEGER NOT NULL, "
    "qty INTEGER NOT NULL, price TEXT NOT NULL, at REAL NOT NULL, quote_id INTEGER, model TEXT, basis TEXT, "
    "PRIMARY KEY(order_id, cum_qty))",
    "CREATE TABLE abu_sleeves (strategy TEXT PRIMARY KEY, initial_cash TEXT NOT NULL, initial_basis_json TEXT, "
    "cash TEXT NOT NULL, realized_pnl TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE TABLE abu_quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, exchange TEXT, "
    "requested_at REAL NOT NULL, received_at REAL NOT NULL, as_of REAL, price TEXT, source TEXT, "
    "valid INTEGER NOT NULL, invalid_reason TEXT)",
    "CREATE INDEX abu_quotes_ticker_received ON abu_quotes(ticker, received_at)",
    "CREATE TABLE abu_account_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, "
    "net_asset_usd TEXT, orderable_cash_usd TEXT, holdings_count INTEGER, complete INTEGER NOT NULL, reason TEXT)",
    "CREATE TABLE abu_daily_perf (strategy TEXT NOT NULL, day TEXT NOT NULL, equity TEXT, cash TEXT, "
    "open_value TEXT, unpriced INTEGER NOT NULL, entries INTEGER NOT NULL, closed INTEGER NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('COMPLETE','INCOMPLETE')), created_at REAL, PRIMARY KEY(strategy, day))",
    "CREATE TABLE abu_system_events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, "
    "level TEXT NOT NULL CHECK(level IN ('INFO','WARN','URGENT')), kind TEXT NOT NULL, details_json TEXT, "
    "notified INTEGER NOT NULL DEFAULT 0)",
    # 알림 발송 표시(notified 0→1)만 허용하고 나머지 UPDATE·모든 DELETE 는 거부한다.
    "CREATE TRIGGER abu_system_events_no_update BEFORE UPDATE ON abu_system_events "
    "WHEN NOT (OLD.notified = 0 AND NEW.notified = 1 AND NEW.id IS OLD.id AND NEW.at IS OLD.at "
    "AND NEW.level IS OLD.level AND NEW.kind IS OLD.kind AND NEW.details_json IS OLD.details_json) "
    "BEGIN SELECT RAISE(ABORT, 'abu immutable'); END",
    "CREATE TRIGGER abu_system_events_no_delete BEFORE DELETE ON abu_system_events "
    "BEGIN SELECT RAISE(ABORT, 'abu immutable'); END",
    "CREATE TABLE abu_api_calls (minute TEXT NOT NULL, api_id TEXT NOT NULL, outcome TEXT NOT NULL, "
    "n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(minute, api_id, outcome))",
    "CREATE TABLE abu_runtime (key TEXT PRIMARY KEY, value TEXT)",
) + tuple(s for t in _IMMUTABLE for s in _no_change_triggers(t))
