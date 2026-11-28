"""자동매수 SHADOW 저널 스키마 v4 추가 DDL (리비전 ab0004). ab0001~ab0003 은 공개됐으므로 바꾸지 않고 트리거만 더한다.

원장 불변성 (ab0003 run fencing 보완):
- run 단위 표의 UPDATE 는 NEW 뿐 아니라 **OLD** 의 run 도 BUILDING·CURRENT 여야 한다 (종료 run 행을 활성 run 으로 옮기는 우회 차단).
  ``run_id`` 는 바꿀 수 없다.
- run 단위 표·체결 원장·run·감사 이벤트의 **DELETE 는 모두 거부**한다 (원장은 추가만 한다. 코드에 삭제 경로 없음).
- ``ab_fill_ledger``: 없는 주문을 가리키는 INSERT 거부, UPDATE 는 OLD·NEW 주문의 run 이 BUILDING·CURRENT 일 때만,
  ``order_id`` 변경 거부.
- ``ab_runs``: 종료 run(FAILED·SUPERSEDED)은 어떤 열도 바꿀 수 없다 (감사 정보 포함). ``run_id`` 변경 거부.
- ``ab_events``(감사 이력): UPDATE·DELETE 거부.
- 존재하지 않는 run 을 가리키는 INSERT·UPDATE 는 ab0003 fencing 이 이미 거부한다 (상태가 NULL → BUILDING·CURRENT 아님).
"""

_RUN_TABLES = ("ab_account_runs", "ab_decisions", "ab_orders", "ab_reservations", "ab_positions", "ab_stop_events",
               "ab_daily", "ab_days")
_ACTIVE = "('BUILDING','CURRENT')"
_FENCED = "SELECT RAISE(ABORT, 'run fenced: writes allowed only for BUILDING or CURRENT runs')"


def _triggers():
    out = []
    for t in _RUN_TABLES:
        out.append(f"""CREATE TRIGGER {t}_fence_update_old BEFORE UPDATE ON {t}
        WHEN (SELECT status FROM ab_runs WHERE run_id = OLD.run_id) IS NULL
          OR (SELECT status FROM ab_runs WHERE run_id = OLD.run_id) NOT IN {_ACTIVE}
        BEGIN {_FENCED}; END""")
        out.append(f"""CREATE TRIGGER {t}_run_id_immutable BEFORE UPDATE OF run_id ON {t}
        WHEN NEW.run_id IS NOT OLD.run_id
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: run_id cannot change'); END""")
        out.append(f"""CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t}
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: delete not allowed'); END""")
    out.append("""CREATE TRIGGER ab_fill_ledger_order_exists BEFORE INSERT ON ab_fill_ledger
        WHEN NOT EXISTS (SELECT 1 FROM ab_orders WHERE id = NEW.order_id)
        BEGIN SELECT RAISE(ABORT, 'run fenced: fill references unknown order'); END""")
    out.append(f"""CREATE TRIGGER ab_fill_ledger_fence_update BEFORE UPDATE ON ab_fill_ledger
        WHEN (SELECT r.status FROM ab_orders o JOIN ab_runs r ON r.run_id = o.run_id WHERE o.id = OLD.order_id) IS NULL
          OR (SELECT r.status FROM ab_orders o JOIN ab_runs r ON r.run_id = o.run_id WHERE o.id = OLD.order_id)
             NOT IN {_ACTIVE}
          OR (SELECT r.status FROM ab_orders o JOIN ab_runs r ON r.run_id = o.run_id WHERE o.id = NEW.order_id) IS NULL
          OR (SELECT r.status FROM ab_orders o JOIN ab_runs r ON r.run_id = o.run_id WHERE o.id = NEW.order_id)
             NOT IN {_ACTIVE}
        BEGIN {_FENCED}; END""")
    out.append("""CREATE TRIGGER ab_fill_ledger_order_immutable BEFORE UPDATE OF order_id ON ab_fill_ledger
        WHEN NEW.order_id IS NOT OLD.order_id
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: order_id cannot change'); END""")
    out.append("""CREATE TRIGGER ab_fill_ledger_no_delete BEFORE DELETE ON ab_fill_ledger
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: delete not allowed'); END""")
    out.append("""CREATE TRIGGER ab_runs_terminal_immutable BEFORE UPDATE ON ab_runs
        WHEN OLD.status IN ('FAILED','SUPERSEDED')
        BEGIN SELECT RAISE(ABORT, 'run status is terminal: terminal runs are immutable'); END""")
    out.append("""CREATE TRIGGER ab_runs_id_immutable BEFORE UPDATE OF run_id ON ab_runs
        WHEN NEW.run_id IS NOT OLD.run_id
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: run_id cannot change'); END""")
    out.append("""CREATE TRIGGER ab_runs_no_delete BEFORE DELETE ON ab_runs
        BEGIN SELECT RAISE(ABORT, 'ledger immutable: delete not allowed'); END""")
    out.append("""CREATE TRIGGER ab_events_no_update BEFORE UPDATE ON ab_events
        BEGIN SELECT RAISE(ABORT, 'audit immutable: events cannot change'); END""")
    out.append("""CREATE TRIGGER ab_events_no_delete BEFORE DELETE ON ab_events
        BEGIN SELECT RAISE(ABORT, 'audit immutable: events cannot be deleted'); END""")
    return tuple(out)


DDL_V4 = _triggers()
