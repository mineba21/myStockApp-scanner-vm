"""자동매수 SHADOW 저널 스키마 v5 추가 DDL (리비전 ab0005). ab0001~ab0004 는 공개됐으므로 바꾸지 않고 트리거만 더한다.

충돌 교체 차단 (ab0004 보완):
SQLite 의 ``INSERT OR REPLACE``·``REPLACE INTO``·``UPDATE OR REPLACE`` 는 키가 겹치는 기존 행을 지우고 새 행을 넣는다.
이 내부 삭제는 ``PRAGMA recursive_triggers`` 가 꺼져 있으면 DELETE 트리거를 부르지 않아 ab0004 의 삭제 거부를 우회한다.
그래서 연결 설정(recursive_triggers)에만 기대지 않고, 보호 표마다 **PK 와 모든 UNIQUE 제약(부분 UNIQUE 포함)의 기존 키
충돌을 명시적으로 거부**한다.
- ``BEFORE INSERT``: 같은 키의 행이 이미 있으면 거부 → ``INSERT OR REPLACE``·``REPLACE INTO``·``INSERT … ON CONFLICT DO
  UPDATE``·PK 외 UNIQUE 충돌 교체를 모두 막는다 (일반 INSERT 의 중복도 원래처럼 실패한다).
- ``BEFORE UPDATE``: 바꾼 키가 다른 행의 키와 겹치면 거부 → ``UPDATE OR REPLACE`` 로 다른 행을 지우는 우회를 막는다.
- ``ab_eligibility`` 만 ``RAISE(IGNORE)`` (제품이 ``INSERT OR IGNORE`` 로 멱등 기록 — 기존 행 유지, rowcount 0).

추가 불변 보호 (기존에 트리거가 없던 감사 자료):
- ``ab_accounts`` (가상 계좌 설정·해시): UPDATE·DELETE 거부.
- ``ab_scan_captures`` (스캔 수집 이력): DELETE 거부, 수집 직후 채우는 ``rows_captured``·``strict_rows`` 외 열 변경 거부.
- ``ab_eligibility`` (판정 기록): UPDATE·DELETE 거부.
외래키는 모두 NO ACTION (CASCADE·SET NULL 없음)이라 연쇄 변경 경로가 없다. 부모 행 삭제는 각 표의 삭제 거부가 막는다.
"""

_IMM = "SELECT RAISE(ABORT, 'ledger immutable: conflicting key replace not allowed')"

# 표 → [(키 열, 부분 UNIQUE 조건 또는 None)]  — PK 와 UNIQUE 제약 전체 (ab0004 스키마 기준, 테스트로 대조)
KEYS = {
    "ab_runs": [(("run_id",), None)],   # 부분 UNIQUE(status='CURRENT')는 ab0003 single_current 트리거가 INSERT·UPDATE 모두 거부
    "ab_account_runs": [(("run_id", "account_id"), None)],
    "ab_decisions": [(("run_id", "account_id", "snapshot_id"), None)],
    "ab_orders": [(("id",), None), (("run_id", "account_id", "signal_key"), None),
                  (("run_id", "account_id", "ticker"), "state IN ('PENDING','PARTIAL','FILL_UNKNOWN')")],
    "ab_reservations": [(("order_id",), None)],
    "ab_positions": [(("id",), None), (("order_id",), None),
                     (("run_id", "account_id", "ticker"), "state = 'OPEN'")],
    "ab_stop_events": [(("id",), None), (("position_id",), None)],
    "ab_daily": [(("run_id", "account_id", "day"), None)],
    "ab_days": [(("run_id", "day"), None)],
    "ab_fill_ledger": [(("order_id", "cum_filled_qty"), None)],
    "ab_events": [(("id",), None)],
    "ab_signal_snapshots": [(("id",), None), (("scan_log_id", "signal_key"), None)],
    "ab_bars": [(("day", "ticker", "input_rev"), None)],
    "ab_day_inputs": [(("day",), None)],
    "ab_scan_captures": [(("scan_log_id",), None)],
    "ab_accounts": [(("id",), None)],
    "ab_eligibility": [(("snapshot_id", "pool", "strategy_version"), None)],
}
IGNORE_ON_CONFLICT = ("ab_eligibility",)
COVERED_ELSEWHERE = {("ab_runs", ("status",)): "ab_runs_single_current_insert / ab_runs_single_current_update (ab0003)"}
SCAN_CAPTURE_FIXED = ("scan_log_id", "scan_market", "scan_status", "triggered_by", "scan_started_at",
                      "scan_finished_at", "trading_day", "intraday", "source", "complete", "recoverability",
                      "late_after_decision", "signals_found", "affected_rows", "note", "captured_at")


def _match(cols, ref, where):
    m = " AND ".join(f"x.{c} = {ref}.{c}" for c in cols)
    if where:
        m += " AND " + " AND ".join(f"x.{w.strip()}" for w in [where])
    return m


def _cond(t, keys, ref, exclude_self):
    parts = []
    for cols, where in keys:
        sub = f"EXISTS (SELECT 1 FROM {t} x WHERE {_match(cols, ref, where)}"
        if exclude_self:
            sub += " AND x.rowid != OLD.rowid"
        sub += ")"
        if where:                                      # 부분 UNIQUE: 새 행도 조건에 들 때만 충돌
            new_where = where.replace("status", f"{ref}.status") if "status" in where else where
            new_where = new_where.replace("state", f"{ref}.state") if "state" in where else new_where
            sub = f"({new_where} AND {sub})"
        parts.append(sub)
    return "\n          OR ".join(parts)


def _triggers():
    out = []
    for t, keys in KEYS.items():
        action = "SELECT RAISE(IGNORE)" if t in IGNORE_ON_CONFLICT else _IMM
        out.append(f"""CREATE TRIGGER {t}_no_replace_insert BEFORE INSERT ON {t}
        WHEN {_cond(t, keys, "NEW", False)}
        BEGIN {action}; END""")
        out.append(f"""CREATE TRIGGER {t}_no_replace_update BEFORE UPDATE ON {t}
        WHEN {_cond(t, keys, "NEW", True)}
        BEGIN {_IMM}; END""")
    for t, what in (("ab_accounts", "accounts"), ("ab_eligibility", "eligibility")):
        out.append(f"""CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t}
        BEGIN SELECT RAISE(ABORT, 'audit immutable: {what} cannot change'); END""")
        out.append(f"""CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t}
        BEGIN SELECT RAISE(ABORT, 'audit immutable: {what} cannot be deleted'); END""")
    out.append("""CREATE TRIGGER ab_scan_captures_no_delete BEFORE DELETE ON ab_scan_captures
        BEGIN SELECT RAISE(ABORT, 'audit immutable: scan captures cannot be deleted'); END""")
    out.append(f"""CREATE TRIGGER ab_scan_captures_fixed BEFORE UPDATE OF {", ".join(SCAN_CAPTURE_FIXED)} ON ab_scan_captures
        BEGIN SELECT RAISE(ABORT, 'audit immutable: scan capture record is fixed'); END""")
    return tuple(out)


DDL_V5 = _triggers()
