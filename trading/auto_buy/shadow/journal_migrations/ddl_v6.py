"""자동매수 SHADOW 저널 스키마 v6 (리비전 ab0006): **테이블별 데이터 불변성 계약**과 그 계약에서 만든 트리거.

ab0001~ab0005 는 공개됐으므로 바꾸지 않는다. 이 리비전은 트리거를 더하고, 정책이 바뀐 ab0005 트리거 1개
(``ab_eligibility_no_replace_insert``: 충돌을 모두 조용히 무시)만 교체한다.

개별 SQL 연산·컬럼에 보호 조건을 하나씩 붙이는 대신, 표마다 아래를 ``CONTRACT`` 로 정의하고 트리거를 그 정의에서 만든다.
테스트(``tests/test_auto_buy_shadow_review7_contract.py``)가 실제 SQLite 스키마의 표·컬럼·인덱스와 계약을 대조하며,
분류되지 않은 표나 컬럼이 있으면 실패한다.
- 분류 (``category``): 완전 불변 입력 · 추가 전용 감사 기록 · 최초 1회 입력 허용 · 활성 run 에서만 변경 · 런타임 상태 갱신 허용
- 행 식별 방식, PK·UNIQUE(부분 포함) 제약, **변경 허용 컬럼(허용 목록)**, 허용된 상태 전환, 신규 INSERT 조건,
  기존 행 변경·삭제 정책, 충돌 처리 방식

이 리비전에서 더하는 보호 (보호 대상 표 전체, 연결 설정·recursive_triggers 와 무관):
1. **숨은 rowid 교체 차단.** 정수 PK 별칭이 아닌 표(복합 PK·TEXT PK)는 선언된 키와 별도로 SQLite rowid 가 있다.
   ``INSERT OR REPLACE INTO t(rowid, …)`` 로 기존 rowid 에 새 키를 넣거나 ``UPDATE OR REPLACE t SET rowid=…`` 로 다른 행의
   rowid 를 가져가면, 선언된 키는 충돌하지 않아도 기존 행이 내부 삭제된다.
   - ``BEFORE INSERT``: 같은 rowid 의 행이 이미 있으면 거부. 자동 생성 rowid 는 BEFORE INSERT 에서 아직 정해지지 않아
     ``NEW.rowid`` 가 -1 로 보인다 (SQLite 3.53 확인). 그래서
   - ``AFTER INSERT``: 확정된 rowid 가 0 이하이면 거부 → 0 이하 rowid 행은 생길 수 없고, 자동 생성(-1)은 기존 행과
     충돌하지 않는다. 명시적 음수 rowid 도 거부된다.
   - ``BEFORE UPDATE``: rowid(= ``oid``·``_rowid_``·정수 PK 별칭)는 바꿀 수 없다.
2. **변경 허용 목록.** 표마다 계약의 ``mutable`` 에 없는 컬럼과 PK·UNIQUE 키 컬럼은 최초 INSERT 이후 바꿀 수 없다.
   (변경 금지 목록이 아니라 허용 목록이다. 새 컬럼을 추가하면 계약에 분류하기 전까지 테스트가 실패한다.)
3. **상태 전환·최초 1회 입력.** ab_runs·ab_orders·ab_positions·ab_reservations 의 상태 전환, ab_day_inputs 의 계좌 입력
   (최초 1회, 허용된 상태·필드 조합만), ab_scan_captures 의 수집 건수 (최초 1회, 불변 스냅샷 건수와 일치할 때만).
4. **ab_eligibility 충돌 정책.** 같은 키에 **같은 판단**(eligible·reasons·decision_role·logic_hash)을 다시 넣는 경우만
   멱등 무시한다. 판단이 다르면 ``INSERT OR IGNORE`` 여도 ``eligibility conflict`` 로 거부한다 (기존 행 유지).
   ``created_at`` 은 기록 시각 메타데이터라 비교하지 않는다.

보호 범위 밖 (기존과 같음): DB 관리자·파일 소유자의 임의 DDL(트리거 DROP·``writable_schema``)과 파일 직접 변경.
"""

FROZEN_INPUT = "완전 불변 입력"
APPEND_ONLY_AUDIT = "추가 전용 감사 기록"
SET_ONCE = "최초 1회 입력 허용"
ACTIVE_RUN = "활성 run 에서만 변경"
RUNTIME = "런타임 상태 갱신 허용"
CATEGORIES = (FROZEN_INPUT, APPEND_ONLY_AUDIT, SET_ONCE, ACTIVE_RUN, RUNTIME)

_ACTIVE_INSERT = "run 이 BUILDING·CURRENT 일 때만 (ab0003 fence_insert)"
_ACTIVE_CHANGE = "OLD·NEW run 이 BUILDING·CURRENT 일 때 허용 목록 컬럼만 (ab0003·ab0004 fence). 삭제 거부 (ab0004)"
_KEY_CONFLICT = "기존 PK·UNIQUE·rowid 충돌 거부 (REPLACE·UPSERT·UPDATE OR REPLACE 포함, ab0005·ab0006)"


def _t(category, identity, keys, columns, mutable=(), transitions=None, insert="제한 없음 (키 충돌만 거부)",
       change=None, delete="거부", conflict=_KEY_CONFLICT):
    return {"category": category, "identity": identity, "keys": keys, "columns": tuple(columns.split()),
            "mutable": tuple(mutable.split()) if isinstance(mutable, str) else tuple(mutable),
            "transitions": transitions, "insert": insert,
            "change": change or ("변경 불가" if not mutable else "허용 목록 컬럼만"), "delete": delete,
            "conflict": conflict}


# 표 → 계약.  keys: [(키 열, 부분 UNIQUE 조건 또는 None)]  — PK 와 모든 UNIQUE 제약 (테스트가 실제 인덱스와 대조)
CONTRACT = {
    # ── 완전 불변 입력 ──
    "ab_bars": _t(FROZEN_INPUT, "복합 PK + 숨은 rowid", [(("day", "ticker", "input_rev"), None)],
                  "day ticker input_rev status open high low close provider fetched_at purpose detail volume "
                  "volume_status volume_source source_evidence",
                  insert="새 수집 결과는 새 input_rev 행으로만 추가 (기존 행 교체 없음)"),
    "ab_signal_snapshots": _t(FROZEN_INPUT, "정수 PK id (= rowid)",
                              [(("id",), None), (("scan_log_id", "signal_key"), None)],
                              "id scan_log_id scan_result_id signal_key market ticker name signal_type signal_date "
                              "trading_day intraday scan_status scan_completed_at late_after_decision first_detected_at "
                              "is_first_detection strict_passed strict_reasons first_strict_pass first_pass_basis grade "
                              "signal_quality market_condition decision_price pivot_price stop_loss ext_pct stop_pct "
                              "entry_warnings rs_value volume_ratio weekly_volume_ratio sector_name sector_source "
                              "scanner_commit threshold_hash capture_logic_hash row_sha256 row_json capture_source "
                              "created_at"),
    "ab_accounts": _t(FROZEN_INPUT, "TEXT PK id + 숨은 rowid", [(("id",), None)],
                      "id profile strategy seed strategy_version config_hash logic_hash config_json start_day state "
                      "is_virtual created_at"),
    # ── 추가 전용 감사 기록 ──
    "ab_events": _t(APPEND_ONLY_AUDIT, "정수 PK id AUTOINCREMENT (= rowid)", [(("id",), None)],
                    "id at severity kind message details", insert="자동 id 로 추가만"),
    "ab_eligibility": _t(APPEND_ONLY_AUDIT, "복합 PK + 숨은 rowid", [(("snapshot_id", "pool", "strategy_version"), None)],
                         "snapshot_id pool strategy_version eligible reasons decision_role logic_hash created_at",
                         conflict="같은 키 + 같은 판단(eligible·reasons·decision_role·logic_hash)만 멱등 무시, "
                                  "판단이 다르면 거부 (INSERT OR IGNORE 포함). rowid 충돌 거부"),
    # ── 최초 1회 입력 허용 ──
    "ab_day_inputs": _t(SET_ONCE, "TEXT PK day + 숨은 rowid", [(("day",), None)],
                        "day decision_status decision_scan_log_id frozen_at frozen_by signal_set_hash account_status "
                        "account_reason account_snapshot account_fetched_at strategy_version config_hash logic_hash",
                        mutable="account_status account_reason account_snapshot account_fetched_at",
                        transitions="계좌 4필드: 모두 NULL → (OK + JSON 스냅샷 + 조회 시각) 또는 "
                                    "(UNAVAILABLE·LATE + 스냅샷 없음 + 조회 시각), 한 번에 함께, 이후 변경 불가",
                        insert="계좌 4필드 NULL. FROZEN 은 결정 스캔·신호 해시 필수, NO_DECISION_SCAN 은 둘 다 없음",
                        change="계좌 4필드만 최초 1회"),
    "ab_scan_captures": _t(SET_ONCE, "정수 PK scan_log_id (= rowid)", [(("scan_log_id",), None)],
                           "scan_log_id scan_market scan_status triggered_by scan_started_at scan_finished_at "
                           "trading_day intraday source complete recoverability late_after_decision rows_captured "
                           "strict_rows signals_found affected_rows note captured_at",
                           mutable="rows_captured strict_rows",
                           transitions="수집 건수: (0, 0) → 불변 스냅샷의 실제 건수(전체·Strict 통과)와 같은 값으로 1회",
                           change="수집 건수만 최초 1회"),
    # ── 활성 run 에서만 변경 ──
    "ab_runs": _t(ACTIVE_RUN, "정수 PK run_id (= rowid)", [(("run_id",), None)],
                  "run_id status reason parent_run through_day strategy_version config_hash logic_hash created_at "
                  "completed_at summary",
                  mutable="status through_day completed_at summary",
                  transitions="BUILDING→CURRENT·FAILED, CURRENT→SUPERSEDED. FAILED·SUPERSEDED 는 변경 불가 (ab0004)",
                  insert="CURRENT 는 1개만 (ab0003)", change="BUILDING·CURRENT run 의 허용 목록 컬럼만"),
    "ab_account_runs": _t(ACTIVE_RUN, "복합 PK + 숨은 rowid", [(("run_id", "account_id"), None)],
                          "run_id account_id initial_cash cash realized_pnl updated_at",
                          mutable="initial_cash cash realized_pnl updated_at", insert=_ACTIVE_INSERT,
                          change=_ACTIVE_CHANGE),
    "ab_decisions": _t(ACTIVE_RUN, "복합 PK + 숨은 rowid", [(("run_id", "account_id", "snapshot_id"), None)],
                       "run_id account_id snapshot_id day rank_value outcome reason qty details created_at",
                       insert=_ACTIVE_INSERT),
    "ab_orders": _t(ACTIVE_RUN, "정수 PK id (= rowid)",
                    [(("id",), None), (("run_id", "account_id", "signal_key"), None),
                     (("run_id", "account_id", "ticker"), "state IN ('PENDING','PARTIAL','FILL_UNKNOWN')")],
                    "id run_id account_id snapshot_id signal_key ticker sector decision_day target_day decision_price "
                    "limit_price stop_price qty filled_qty avg_fill_price state fill_kind is_virtual fill_model "
                    "created_at updated_at details",
                    mutable="filled_qty avg_fill_price state fill_kind updated_at details",
                    transitions="PENDING→그 외 상태, PARTIAL→PENDING 외 상태. 종결 상태(FILLED·UNFILLED·FILL_UNKNOWN 등)는 "
                                "변경 불가. filled_qty 감소 불가",
                    insert=_ACTIVE_INSERT, change=_ACTIVE_CHANGE),
    "ab_reservations": _t(ACTIVE_RUN, "정수 PK order_id (= rowid)", [(("order_id",), None)],
                          "order_id run_id account_id reserved_cash planned_risk sector state created_day closed_day "
                          "updated_at",
                          mutable="reserved_cash planned_risk state closed_day updated_at",
                          transitions="ACTIVE→ACTIVE·CONVERTED·RELEASED. CONVERTED·RELEASED 는 변경 불가",
                          insert=_ACTIVE_INSERT, change=_ACTIVE_CHANGE),
    "ab_positions": _t(ACTIVE_RUN, "정수 PK id (= rowid)",
                       [(("id",), None), (("order_id",), None), (("run_id", "account_id", "ticker"), "state = 'OPEN'")],
                       "id run_id account_id order_id signal_key ticker sector entry_day entry_price stop_price qty "
                       "cost risk0 last_close last_close_day price_status unresolved_since state exit_day exit_price "
                       "exit_reason proceeds pnl r_multiple is_virtual created_at updated_at zero_volume_days "
                       "last_zero_volume_day",
                       mutable="qty entry_price cost risk0 last_close last_close_day price_status unresolved_since "
                               "state exit_day exit_price exit_reason proceeds pnl r_multiple updated_at "
                               "zero_volume_days last_zero_volume_day",
                       transitions="OPEN→CLOSED (CLOSED 는 변경 불가), price_status OK→UNRESOLVED (되돌림 불가)",
                       insert=_ACTIVE_INSERT, change=_ACTIVE_CHANGE),
    "ab_stop_events": _t(ACTIVE_RUN, "정수 PK id (= rowid)", [(("id",), None), (("position_id",), None)],
                         "id run_id account_id position_id day reason stop_price exit_price qty cancelled_buy_qty "
                         "details is_virtual created_at", insert=_ACTIVE_INSERT),
    "ab_daily": _t(ACTIVE_RUN, "복합 PK + 숨은 rowid", [(("run_id", "account_id", "day"), None)],
                   "run_id account_id day data_status equity cash invested reserved realized_pnl unrealized_pnl "
                   "entry_heat current_heat heat_base current_heat_level sleeve_budget sleeve_used max_pos_w "
                   "max_sector_w n_positions n_pending unresolved_positions unresolved_orders unresolved_value "
                   "stress_full_loss safety_blocks account_level counts created_at", insert=_ACTIVE_INSERT),
    "ab_days": _t(ACTIVE_RUN, "복합 PK + 숨은 rowid", [(("run_id", "day"), None)],
                  "run_id day status decision_status candidates bars_requested bars_missing account_status notes "
                  "processed_at", insert=_ACTIVE_INSERT),
    "ab_fill_ledger": _t(ACTIVE_RUN, "복합 PK + 숨은 rowid", [(("order_id", "cum_filled_qty"), None)],
                         "order_id cum_filled_qty applied_delta price day basis bar_open bar_high bar_low bar_close "
                         "bar_provider bar_fetched_at bar_input_rev is_virtual applied_at bar_volume",
                         insert="주문이 있고 그 run 이 BUILDING·CURRENT 일 때만 (ab0003·ab0004)"),
    # ── 런타임 상태 갱신 허용 (보호 대상 아님) ──
    "ab_runtime": _t(RUNTIME, "TEXT PK key + 숨은 rowid", [(("key",), None)], "key value updated_at",
                     mutable="value updated_at", insert="제한 없음", change="upsert 허용 (운영 상태 kv)",
                     delete="허용 (제품 경로 없음)", conflict="ON CONFLICT(key) DO UPDATE (제품 upsert)"),
    "ab_alembic_version": _t(RUNTIME, "TEXT PK version_num", [(("version_num",), None)], "version_num",
                             mutable="version_num", insert="Alembic 전용", change="Alembic 전용", delete="Alembic 전용",
                             conflict="Alembic 전용"),
    "sqlite_sequence": _t(RUNTIME, "SQLite 내부 (AUTOINCREMENT 순번)", [], "name seq", mutable="name seq",
                          insert="SQLite 내부", change="SQLite 내부", delete="SQLite 내부", conflict="SQLite 내부"),
}
# ab_runs 의 부분 UNIQUE(status='CURRENT')는 ab0003 단일 CURRENT 트리거가 INSERT·UPDATE 모두 거부한다
COVERED_ELSEWHERE = {("ab_runs", ("status",)): "ab_runs_single_current_insert / ab_runs_single_current_update (ab0003)"}
PROTECTED = tuple(t for t, c in CONTRACT.items() if c["category"] != RUNTIME)
ELIGIBILITY_JUDGEMENT = ("eligible", "reasons", "decision_role", "logic_hash")   # 판단 내용 (created_at 은 메타데이터)

_ID = "SELECT RAISE(ABORT, 'ledger immutable: row identity (rowid) conflict or change not allowed')"


def _immutable_columns(t):
    c = CONTRACT[t]
    keys = {k for cols, _ in c["keys"] for k in cols}
    return [x for x in c["columns"] if x not in c["mutable"] or x in keys]


def _triggers():
    out = ["DROP TRIGGER ab_eligibility_no_replace_insert"]
    for t in PROTECTED:
        out.append(f"""CREATE TRIGGER {t}_v6_rowid_insert BEFORE INSERT ON {t}
        WHEN EXISTS (SELECT 1 FROM {t} x WHERE x.rowid = NEW.rowid)
        BEGIN {_ID}; END""")
        out.append(f"""CREATE TRIGGER {t}_v6_rowid_positive AFTER INSERT ON {t}
        WHEN NEW.rowid <= 0
        BEGIN {_ID}; END""")
        changed = " OR ".join([f"NEW.{x} IS NOT OLD.{x}" for x in _immutable_columns(t)] + ["NEW.rowid IS NOT OLD.rowid"])
        out.append(f"""CREATE TRIGGER {t}_v6_allowlist BEFORE UPDATE ON {t}
        WHEN {changed}
        BEGIN SELECT RAISE(ABORT, 'contract immutable: column is not in the update allow-list'); END""")
    # 상태 전환
    out.append("""CREATE TRIGGER ab_runs_v6_transition BEFORE UPDATE OF status ON ab_runs
        WHEN NEW.status IS NOT OLD.status AND NOT COALESCE(
                (OLD.status IS 'BUILDING' AND NEW.status IN ('CURRENT','FAILED'))
                OR (OLD.status IS 'CURRENT' AND NEW.status IS 'SUPERSEDED'), 0)
        BEGIN SELECT RAISE(ABORT, 'contract transition: run status change not allowed'); END""")
    out.append("""CREATE TRIGGER ab_orders_v6_terminal BEFORE UPDATE ON ab_orders
        WHEN OLD.state NOT IN ('PENDING','PARTIAL')
        BEGIN SELECT RAISE(ABORT, 'contract transition: terminal order is immutable'); END""")
    out.append("""CREATE TRIGGER ab_orders_v6_transition BEFORE UPDATE ON ab_orders
        WHEN (NEW.state IS NOT OLD.state AND NEW.state IS 'PENDING') OR COALESCE(NEW.filled_qty < OLD.filled_qty, 1)
        BEGIN SELECT RAISE(ABORT, 'contract transition: order state or filled qty cannot go back'); END""")
    out.append("""CREATE TRIGGER ab_positions_v6_terminal BEFORE UPDATE ON ab_positions
        WHEN OLD.state = 'CLOSED'
        BEGIN SELECT RAISE(ABORT, 'contract transition: closed position is immutable'); END""")
    out.append("""CREATE TRIGGER ab_positions_v6_transition BEFORE UPDATE ON ab_positions
        WHEN (NEW.state IS NOT OLD.state AND NEW.state IS 'OPEN')
          OR (OLD.price_status IS 'UNRESOLVED' AND NEW.price_status IS NOT 'UNRESOLVED')
        BEGIN SELECT RAISE(ABORT, 'contract transition: position state cannot go back'); END""")
    out.append("""CREATE TRIGGER ab_reservations_v6_terminal BEFORE UPDATE ON ab_reservations
        WHEN OLD.state IN ('CONVERTED','RELEASED')
        BEGIN SELECT RAISE(ABORT, 'contract transition: closed reservation is immutable'); END""")
    # ab_day_inputs: 신규 INSERT 조건, 계좌 4필드 최초 1회
    out.append("""CREATE TRIGGER ab_day_inputs_v6_insert BEFORE INSERT ON ab_day_inputs
        WHEN NEW.account_status IS NOT NULL OR NEW.account_reason IS NOT NULL OR NEW.account_snapshot IS NOT NULL
          OR NEW.account_fetched_at IS NOT NULL
          OR NOT COALESCE((NEW.decision_status IS 'FROZEN' AND NEW.decision_scan_log_id IS NOT NULL
                           AND NEW.signal_set_hash IS NOT NULL)
                       OR (NEW.decision_status IS 'NO_DECISION_SCAN' AND NEW.decision_scan_log_id IS NULL
                           AND NEW.signal_set_hash IS NULL), 0)
        BEGIN SELECT RAISE(ABORT, 'contract insert: day input must start without account and with a valid decision'); END""")
    out.append("""CREATE TRIGGER ab_day_inputs_v6_account_once BEFORE UPDATE ON ab_day_inputs
        WHEN (NEW.account_status IS NOT OLD.account_status OR NEW.account_reason IS NOT OLD.account_reason
              OR NEW.account_snapshot IS NOT OLD.account_snapshot OR NEW.account_fetched_at IS NOT OLD.account_fetched_at)
         AND NOT COALESCE(OLD.account_status IS NULL AND OLD.account_reason IS NULL AND OLD.account_snapshot IS NULL
                  AND OLD.account_fetched_at IS NULL AND NEW.account_fetched_at IS NOT NULL
                  AND ((NEW.account_status IS 'OK' AND NEW.account_snapshot IS NOT NULL
                        AND json_valid(NEW.account_snapshot))
                    OR (NEW.account_status IN ('UNAVAILABLE','LATE') AND NEW.account_snapshot IS NULL)), 0)
        BEGIN SELECT RAISE(ABORT, 'contract set-once: account input is stored once with a valid combination'); END""")
    # ab_scan_captures: 수집 건수 최초 1회, 불변 스냅샷과 일치
    out.append("""CREATE TRIGGER ab_scan_captures_v6_counts_once BEFORE UPDATE ON ab_scan_captures
        WHEN (NEW.rows_captured IS NOT OLD.rows_captured OR NEW.strict_rows IS NOT OLD.strict_rows)
         AND NOT COALESCE(OLD.rows_captured IS 0 AND OLD.strict_rows IS 0
                  AND NEW.rows_captured IS (SELECT COUNT(*) FROM ab_signal_snapshots s
                                            WHERE s.scan_log_id = NEW.scan_log_id)
                  AND NEW.strict_rows IS (SELECT COUNT(*) FROM ab_signal_snapshots s
                                          WHERE s.scan_log_id = NEW.scan_log_id AND s.strict_passed = 1), 0)
        BEGIN SELECT RAISE(ABORT, 'contract set-once: capture counts are set once and must match snapshots'); END""")
    # ab_eligibility: 같은 판단만 멱등 무시, 다른 판단은 거부
    same = " AND ".join(f"x.{c} IS NEW.{c}" for c in ELIGIBILITY_JUDGEMENT)
    key = "x.snapshot_id = NEW.snapshot_id AND x.pool = NEW.pool AND x.strategy_version = NEW.strategy_version"
    out.append(f"""CREATE TRIGGER ab_eligibility_v6_conflict BEFORE INSERT ON ab_eligibility
        WHEN EXISTS (SELECT 1 FROM ab_eligibility x WHERE {key} AND NOT ({same}))
        BEGIN SELECT RAISE(ABORT, 'eligibility conflict: different judgement for the same key'); END""")
    out.append(f"""CREATE TRIGGER ab_eligibility_v6_same BEFORE INSERT ON ab_eligibility
        WHEN EXISTS (SELECT 1 FROM ab_eligibility x WHERE {key} AND {same})
        BEGIN SELECT RAISE(IGNORE); END""")
    return tuple(out)


DDL_V6 = _triggers()


def contract_violations(db) -> list:
    """ab0006 적용 전 기존 데이터가 계약과 맞는지 확인한다 (맞지 않으면 Migration 을 중단하고 원본을 바꾸지 않는다)."""
    bad = []
    for t in PROTECTED:
        n = db.execute(f"SELECT COUNT(*) FROM {t} WHERE rowid <= 0").fetchone()[0]
        if n:
            bad.append(f"{t}: rowid 0 이하 {n}행")
    n = db.execute("""SELECT COUNT(*) FROM ab_day_inputs WHERE NOT COALESCE(
        (account_status IS NULL AND account_reason IS NULL AND account_snapshot IS NULL AND account_fetched_at IS NULL)
        OR (account_fetched_at IS NOT NULL AND ((account_status IS 'OK' AND account_snapshot IS NOT NULL
                                                 AND json_valid(account_snapshot))
                                             OR (account_status IN ('UNAVAILABLE','LATE') AND account_snapshot IS NULL))), 0)
        OR NOT COALESCE((decision_status IS 'FROZEN' AND decision_scan_log_id IS NOT NULL AND signal_set_hash IS NOT NULL)
             OR (decision_status IS 'NO_DECISION_SCAN' AND decision_scan_log_id IS NULL AND signal_set_hash IS NULL), 0)
        """).fetchone()[0]
    if n:
        bad.append(f"ab_day_inputs: 계좌·결정 입력 조합이 계약과 다름 {n}행")
    n = db.execute("""SELECT COUNT(*) FROM ab_scan_captures c WHERE NOT COALESCE(
        (c.rows_captured IS 0 AND c.strict_rows IS 0)
        OR (c.rows_captured IS (SELECT COUNT(*) FROM ab_signal_snapshots s WHERE s.scan_log_id = c.scan_log_id)
            AND c.strict_rows IS (SELECT COUNT(*) FROM ab_signal_snapshots s WHERE s.scan_log_id = c.scan_log_id
                                  AND s.strict_passed = 1)), 0)""").fetchone()[0]
    if n:
        bad.append(f"ab_scan_captures: 수집 건수가 스냅샷과 다름 {n}행")
    return bad
