"""P7 위험 예산 시뮬레이터 테스트 (합성 데이터)."""
import numpy as np

from backtest.entry_strategy.src import portfolio as PF
from backtest.entry_strategy.src import risk_portfolio as RP

COSTS = {"buy_commission": 0.0, "sell_commission": 0.0, "sell_tax": 0.0, "slippage_bp": 0}
N = 40
SECT = {"AAA": "Tech", "BBB": "Tech", "CCC": "Energy"}


def _si(tickers, dec, stop, planned, fill_px, fill_ci, exit_ci=None, exit_px=None):
    k = len(tickers)
    return PF.SimInput(
        ticker=np.array(tickers), stop=np.array(stop, float), decision_ci=np.array(dec),
        planned_px=np.array(planned, float), status_filled=np.ones(k, bool), fill_ci=np.array(fill_ci),
        fill_px=np.array(fill_px, float), expire_ci=np.array(fill_ci), exit_ci=np.array(exit_ci or [-1] * k),
        exit_px=np.array(exit_px or [np.nan] * k, float), exit_reason=np.array([""] * k),
        placeable=np.ones(k, bool), skip_open=np.zeros(k, bool))


def _prof(**kw):
    base = dict(equity=50000, RISK_PCT=0.5, MAX_TOTAL_HEAT_PCT=2.0, MAX_POSITION_PCT=10.0)
    base.update(kw)
    return RP.RiskProfile(**base)


def _winner():
    """AAA 는 100 에 사서 200 까지 상승(손절 없음), 20일째 BBB 신호."""
    si = _si(["AAA", "BBB"], [1, 20], [90, 45], [101, 50.5], [100, 50], [2, 21])
    closes = {"AAA": np.r_[np.full(3, 100.0), np.linspace(100, 200, N - 3)], "BBB": np.full(N, 50.0)}
    return si, closes


def _run(si, closes, prof, top_n=999, manual=None):
    return RP.run(si, closes, 0, N - 1, np.ones(len(si.ticker), bool), np.arange(len(si.ticker), dtype=float),
                  top_n, prof, COSTS, SECT, manual_series=manual)


def test_current_heat_blocks_after_winner_but_entry_heat_does_not():
    si, closes = _winner()
    cur = _run(si, closes, _prof(heat_def="current"))
    ent = _run(si, closes, _prof(heat_def="entry"))
    assert [t["ticker"] for t in cur["open"]] == ["AAA"] and cur["block"].get("heat_limit_exceeded") == 1
    assert sorted(t["ticker"] for t in ent["open"]) == ["AAA", "BBB"]
    # Entry Heat 는 체결가 기준 고정, Current Heat 는 평가이익을 포함
    assert ent["rec"]["heat_entry"][-1] < ent["rec"]["heat_current"][-1]


def test_pending_order_risk_is_reserved_same_day():
    si = _si(["AAA", "CCC"], [1, 1], [90, 45], [101, 50.5], [100, 50], [2, 2])
    closes = {"AAA": np.full(N, 100.0), "CCC": np.full(N, 50.0)}
    res = _run(si, closes, _prof(MAX_TOTAL_HEAT_PCT=0.49))       # AAA 예약 $242 → 남은 $3 < CCC 1주 위험
    assert len(res["open"]) == 1 and res["block"].get("heat_limit_exceeded") == 1


def test_min_cash_reserve_blocks():
    si, closes = _winner()
    res = _run(si, closes, _prof(min_cash_pct=0.999))
    assert not res["open"] and res["block"].get("min_cash_reserve") == 2


def test_current_heat_block_limit():
    si, closes = _winner()
    res = _run(si, closes, _prof(current_block=0.01, MAX_TOTAL_HEAT_PCT=6.0))
    assert [t["ticker"] for t in res["open"]] == ["AAA"] and res["block"].get("current_heat_block") == 1


def test_unstopped_manual_holding_risk_is_not_zero():
    si, closes = _winner()
    spy = np.full(N, 400.0)
    zero = _run(si, closes, _prof(MAX_TOTAL_HEAT_PCT=4.0, manual_pct=0.3, manual_risk_pct=0.0), manual=spy)
    m15 = _run(si, closes, _prof(MAX_TOTAL_HEAT_PCT=4.0, manual_pct=0.3, manual_risk_pct=15.0), manual=spy)
    assert len(zero["open"]) == 2
    assert not m15["open"] and m15["block"].get("heat_limit_exceeded") == 2       # 30%×15% = 4.5% > 4%
    assert m15["rec"]["heat_entry"][0] > 0.04


def test_stop_breached_open_position_blocks_new_buys_and_keeps_risk():
    si = _si(["AAA", "BBB"], [1, 20], [90, 45], [101, 50.5], [100, 50], [2, 21])
    closes = {"AAA": np.r_[np.full(10, 100.0), np.full(N - 10, 85.0)], "BBB": np.full(N, 50.0)}
    res = _run(si, closes, _prof(MAX_TOTAL_HEAT_PCT=6.0))
    assert res["block"].get("stop_breached_open_position") == 1 and res["breached_days"] > 0
    assert res["rec"]["heat_current"][-1] > 0                                   # 이탈해도 위험 0 아님


def test_fill_at_or_below_stop_is_rejected():
    si = _si(["AAA"], [1], [90], [101], [90], [2])
    res = _run(si, {"AAA": np.full(N, 95.0)}, _prof())
    assert not res["open"] and res["counts"]["unfilled"] == 1


def test_sector_concentration_recorded():
    si, closes = _winner()
    res = _run(si, closes, _prof(heat_def="entry"))
    assert res["rec"]["max_sector_w"][-1] >= res["rec"]["max_pos_w"][-1] > 0
