"""SHADOW 입력 공급자 (읽기 전용). 테스트는 가짜 공급자를 주입한다.

- 일봉: Scanner 와 같은 yfinance(auto_adjust) 일봉. 요청한 거래일과 날짜가 정확히 같은 봉만 쓴다.
  키움 API 를 쓰지 않으므로 S01 의 키움 호출량에 영향이 없다.
- 계좌(OPS 전용): 기존 읽기 전용 잔고 조회(``web.kiwoom_holdings``, 60초 캐시)를 **미국 정규장 마감 후 하루 1회**만 부른다.
  장중에는 호출하지 않는다 (S01 자동손절 API 우선). 조회 실패·불완전이면 None → OPS 신규 가상 매수 차단.
  계좌번호·원 응답은 저장하지 않고 평가금액·현금·종목별 평가액만 쓴다.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timedelta, timezone

from trading.auto_buy.shadow.vfills import Bar

logger = logging.getLogger(__name__)


YF_PROVIDER = "yfinance_daily_auto_adjust"


OHLC = ("Open", "High", "Low", "Close")


def ticker_frame(raw, ticker: str):
    """yfinance download 결과에서 한 종목의 OHLC 프레임을 꺼낸다 (없으면 None).

    단일·복수 종목 모두 처리한다. yfinance 버전에 따라 단일 종목도 ``(Ticker, Price)`` 또는 ``(Price, Ticker)``
    다중 열로 오고, 예전 버전은 단일 종목이면 평평한 열(Open/High/...)로 온다.
    """
    if raw is None or getattr(raw, "empty", True):
        return None
    cols = raw.columns
    if getattr(cols, "nlevels", 1) > 1:
        for level in range(cols.nlevels):
            if ticker in cols.get_level_values(level):
                df = raw.xs(ticker, axis=1, level=level)
                return df if all(c in df.columns for c in OHLC) else None
        return None
    return raw if all(c in cols for c in OHLC) else None


def bars_from_download(raw, tickers: list, day: str) -> dict:
    """download 결과 → {ticker: Bar | None}. 요청한 거래일과 날짜가 같은 봉만 쓴다 (순수 함수, 테스트 가능)."""
    out = {t: None for t in tickers}
    uniq = sorted(set(tickers))
    d = date.fromisoformat(day)
    for t in uniq:
        try:
            df = ticker_frame(raw, t)
            if df is None or (getattr(raw.columns, "nlevels", 1) == 1 and len(uniq) > 1):
                continue
            df = df[[ix.date() == d for ix in df.index]]
            if df.empty:
                continue
            r = df.iloc[-1]
            vals = [float(r[c]) for c in OHLC]
            if all(math.isfinite(v) and v > 0 for v in vals):
                out[t] = Bar(*vals)
        except Exception:
            continue
    return out


def yfinance_bars(tickers: list, day: str) -> dict:
    if not tickers:
        return {}
    try:
        import yfinance as yf
        d = date.fromisoformat(day)
        raw = yf.download(sorted(set(tickers)), start=d.isoformat(), end=(d + timedelta(days=1)).isoformat(),
                          auto_adjust=True, group_by="ticker", progress=False, threads=False)
    except Exception as exc:
        logger.warning("자동매수 SHADOW 일봉 조회 실패: %s", type(exc).__name__)
        return {t: None for t in tickers}
    return bars_from_download(raw, tickers, day)


RECONCILE_ABS_USD = 5.0          # 보유 평가합 vs 원장 평가금액 허용 오차 (절대)
RECONCILE_REL = 0.005            # 상대 0.5%


def _unavailable(reason: str) -> dict:
    return {"status": "UNAVAILABLE", "reason": reason, "source": "kiwoom_readonly",
            "as_of": datetime.now(timezone.utc).isoformat()}


def kiwoom_account_snapshot(sector_lookup=None) -> dict:
    """account1 미국 순자산·주문가능현금·수동 보유 (손절가 미확인 → 미확정 위험).

    반환 ``status``: OK / UNAVAILABLE (+ ``reason``). 다음이면 UNAVAILABLE 로 OPS 신규 가상 매수를 막는다.
    - 조회 실패·원장 평가 합계 미확인(``error``)·값 비정상(현금 > 순자산 등)
    - 보유 종목 통화가 USD 가 아님
    - 보유 평가합과 원장 평가금액(같은 원장 ust21070)의 차이가 max($5, 0.5%) 초과 (잔고 불일치)
    - 보유 행과 계좌 요약의 조회 시각이 다름 (서로 다른 시점의 값)
    계좌번호·원 응답은 저장하지 않고 평가금액·현금·종목별 평가액만 쓴다.
    """
    if os.getenv("KIWOOM_WEB_ENABLED", "false").strip().lower() != "true":
        return _unavailable("KIWOOM_WEB_ENABLED=false")
    try:
        from web.kiwoom_holdings import get_kiwoom_account_summaries, get_kiwoom_holdings
        from web.kiwoom_sizing import account_context
        summaries = {str(s.get("account_profile")): s for s in get_kiwoom_account_summaries(holdings_basis=True)}
        rows = [h for h in get_kiwoom_holdings() if h.get("account_profile") == "account1" and h.get("market") == "US"
                and float(h.get("quantity") or 0) > 0]
    except Exception as exc:
        logger.warning("자동매수 SHADOW 계좌 조회 실패: %s", type(exc).__name__)
        return _unavailable(f"조회 실패: {type(exc).__name__}")
    return build_account_snapshot(summaries.get("account1"), rows, sector_lookup, account_context)


def build_account_snapshot(summary, rows, sector_lookup, account_context) -> dict:
    """조회 결과 → 스냅샷 (순수 함수, 테스트 가능). 대조 실패 시 UNAVAILABLE."""
    if not summary:
        return _unavailable("account1 요약 없음")
    if summary.get("error"):
        return _unavailable(f"원장 평가 미확인: {summary.get('error')}")
    try:
        equity, cash = account_context(summary, "US")
    except Exception:
        return _unavailable("순자산·현금 계산 불가")
    if not (math.isfinite(equity) and math.isfinite(cash)) or equity <= 0 or cash < 0 or cash > equity:
        return _unavailable("순자산·현금 값 비정상")
    holdings, total = [], 0.0
    for h in rows:
        cur = str(h.get("currency") or "USD").strip().upper()
        if cur != "USD":
            return _unavailable(f"보유 통화 {cur} (USD 아님)")
        t = str(h.get("ticker") or "").upper()
        try:
            v = float(h.get("eval_amount") or 0) or float(h.get("current_price") or 0) * float(h.get("quantity") or 0)
        except (TypeError, ValueError):
            return _unavailable(f"{t} 평가금액 비정상")
        if not math.isfinite(v) or v < 0:
            return _unavailable(f"{t} 평가금액 비정상")
        total += v
        holdings.append({"ticker": t, "value": v, "sector": sector_lookup(t) if sector_lookup else None})
    ov = summary.get("overseas") or {}
    try:
        ledger = float(ov.get("evaluation_amount"))
    except (TypeError, ValueError):
        return _unavailable("원장 평가금액 없음")
    tol = max(RECONCILE_ABS_USD, RECONCILE_REL * max(ledger, total))
    if abs(total - ledger) > tol:
        return _unavailable(f"잔고 불일치: 보유 평가합 {total:.2f} vs 원장 평가금액 {ledger:.2f} USD")
    ts = {str(h.get("price_updated_at")) for h in rows if h.get("price_updated_at")}
    if ts and summary.get("updated_at") and ts != {str(summary.get("updated_at"))}:
        return _unavailable("보유·요약 조회 시점 불일치")
    return {"status": "OK", "reason": None, "equity": equity, "cash": cash, "holdings": holdings,
            "holdings_total": total, "ledger_evaluation": ledger,
            "as_of": summary.get("updated_at") or datetime.now(timezone.utc).isoformat(), "source": "kiwoom_readonly"}
