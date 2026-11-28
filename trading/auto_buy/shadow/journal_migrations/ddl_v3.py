"""자동매수 SHADOW 저널 스키마 v3 추가 DDL (리비전 ab0003). ab0001·ab0002 는 공개됐으므로 바꾸지 않고 열 추가만 한다.

- ``ab_bars.volume_source``: 거래량 출처. ``yahoo_chart_raw``(Yahoo v8 chart 원본 JSON, null 과 0 구분)만 0 을
  확정할 수 있다. ``yfinance_dataframe`` 등 변환 출처의 0 은 미확인으로 저장한다. v3 이전 행은 NULL (출처 미확인).
- ``ab_bars.source_evidence``: 원본 근거 JSON — 원본 OHLC·adjclose·volume 값(null 보존), 타임스탬프·거래소 시간대,
  조정 비율, 응답 SHA-256, 필드 존재 여부·이상 사유. 거래일별 입력 리비전과 함께 불변으로 남는다 (기존 불변 트리거).
- **run fencing 트리거** (재계산 실행권 상실 후 이전 작업자 쓰기 차단): run 단위 표(계좌 상태·판단·주문·예약·체결·
  포지션·손절·일일·거래일 요약)에 대한 INSERT·UPDATE 는 그 run 이 BUILDING 또는 CURRENT 일 때만 허용한다.
  FAILED·SUPERSEDED run 에는 쓸 수 없다. FAILED·SUPERSEDED 는 다른 상태로 되돌릴 수 없고, CURRENT 는 동시에 하나만
  있을 수 있다. 실행권을 잃은 작업자가 나중에 다시 실행돼도 새 CURRENT 를 덮어쓰거나 자기 run 을 되살릴 수 없다.
  실행권 식별자(pid·호스트·boot·프로세스 시작 식별자·토큰)는 기존 ``ab_runtime`` JSON 에 저장하므로 열 추가는 없다.
"""

_RUN_TABLES = ("ab_account_runs", "ab_decisions", "ab_orders", "ab_reservations", "ab_positions", "ab_stop_events",
               "ab_daily", "ab_days")
_FENCE = ("(SELECT status FROM ab_runs WHERE run_id = NEW.run_id) IS NOT 'BUILDING' "
          "AND (SELECT status FROM ab_runs WHERE run_id = NEW.run_id) IS NOT 'CURRENT'")


def _fence_triggers():
    out = []
    for t in _RUN_TABLES:
        for op in ("INSERT", "UPDATE"):
            out.append(f"""CREATE TRIGGER {t}_fence_{op.lower()} BEFORE {op} ON {t}
        WHEN {_FENCE}
        BEGIN SELECT RAISE(ABORT, 'run fenced: writes allowed only for BUILDING or CURRENT runs'); END""")
    out.append("""CREATE TRIGGER ab_fill_ledger_fence_insert BEFORE INSERT ON ab_fill_ledger
        WHEN (SELECT r.status FROM ab_orders o JOIN ab_runs r ON r.run_id = o.run_id WHERE o.id = NEW.order_id)
             NOT IN ('BUILDING','CURRENT')
        BEGIN SELECT RAISE(ABORT, 'run fenced: writes allowed only for BUILDING or CURRENT runs'); END""")
    out.append("""CREATE TRIGGER ab_runs_terminal BEFORE UPDATE OF status ON ab_runs
        WHEN OLD.status IN ('FAILED','SUPERSEDED') AND NEW.status IS NOT OLD.status
        BEGIN SELECT RAISE(ABORT, 'run status is terminal'); END""")
    out.append("""CREATE TRIGGER ab_runs_single_current_update BEFORE UPDATE OF status ON ab_runs
        WHEN NEW.status = 'CURRENT' AND EXISTS (SELECT 1 FROM ab_runs WHERE status = 'CURRENT' AND run_id != NEW.run_id)
        BEGIN SELECT RAISE(ABORT, 'only one CURRENT run'); END""")
    out.append("""CREATE TRIGGER ab_runs_single_current_insert BEFORE INSERT ON ab_runs
        WHEN NEW.status = 'CURRENT' AND EXISTS (SELECT 1 FROM ab_runs WHERE status = 'CURRENT'
                                                 AND run_id IS NOT NEW.run_id)
        BEGIN SELECT RAISE(ABORT, 'only one CURRENT run'); END""")
    return tuple(out)


DDL_V3 = (
    "ALTER TABLE ab_bars ADD COLUMN volume_source TEXT",
    "ALTER TABLE ab_bars ADD COLUMN source_evidence TEXT",
) + _fence_triggers()
