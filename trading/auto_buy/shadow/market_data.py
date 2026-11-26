"""SHADOW 입력 공급자 (읽기 전용). 테스트는 가짜 공급자를 주입한다.

- 일봉: Yahoo v8 chart **원본 JSON** 을 종목마다 한 번 받아(yfinance 의 세션·쿠키만 사용) 직접 파싱한다
  (``yfinance_bars`` → ``fetch_chart`` + ``parse_chart``). 요청한 거래일과 날짜가 정확히 같은 봉만 쓴다.
  키움 API 를 쓰지 않으므로 S01 의 키움 호출량에 영향이 없다.
  - 왜 원본인가: yfinance 는 DataFrame 으로 바꾸면서 원본 ``volume: null`` 을 0 으로 채운다
    (yfinance 1.7.0 ``history.py`` ``df['Volume'].fillna(0)``, repair 시에도 0). 변환 결과의 0 은 원본 0 인지 null 인지
    알 수 없으므로, OHLC·거래량을 **같은 응답**에서 원본 그대로 읽는다 (별도 조회 결합 없음).
  - 가격 조정은 yfinance ``auto_adjust`` 와 같은 식: 비율 = adjclose / close, 시가·고가·저가 × 비율, 종가 = adjclose.
    adjclose 가 없으면 비율 1 (근거에 기록).
  - 거래량: 원본 정수 ≥ 0 → 값 그대로 (0 = 확정 0, 출처 ``yahoo_chart_raw``). ``null``·필드 없음·배열 길이 불일치·
    음수·숫자 아님 → ``None`` (미확인). 판정은 ``vfills.volume_state`` / ``inputs._store_bar``.
  - 근거(``Bar.evidence``): 원본 OHLC·adjclose·volume 값(null 보존), 타임스탬프·거래소 시간대, 조정 비율, 응답 SHA-256,
    필드 존재 여부·이상 사유. ``ab_bars.source_evidence`` 에 거래일별로 저장한다 (스키마 ab0003).
  - ``bars_from_download`` (yfinance DataFrame 파서)는 호환·비교용으로 남기며 출처를 ``yfinance_dataframe`` 으로 표시한다.
    이 출처의 0 은 확정 0 으로 보지 않는다 (UNKNOWN).
- 계좌(OPS 전용): 기존 읽기 전용 잔고 조회(``web.kiwoom_holdings``, 60초 캐시)를 **미국 정규장 마감 후 하루 1회**만 부른다.
  장중에는 호출하지 않는다 (S01 자동손절 API 우선). 조회 실패·불완전이면 None → OPS 신규 가상 매수 차단.
  계좌번호·원 응답은 저장하지 않고 평가금액·현금·종목별 평가액만 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from trading.auto_buy.shadow.vfills import SOURCE_YAHOO_CHART_RAW, SOURCE_YF_DATAFRAME, Bar

logger = logging.getLogger(__name__)


YF_PROVIDER = "yfinance_daily_auto_adjust"          # (이전) yfinance DataFrame 경로
RAW_PROVIDER = "yahoo_chart_raw_v8_auto_adjust"      # 원본 JSON 경로 (ab_bars.provider)
CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
EXCHANGE_TZ = "America/New_York"


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
                out[t] = Bar(*vals, volume=_volume(r), volume_source=SOURCE_YF_DATAFRAME)
        except Exception:
            continue
    return out


def _volume(row):
    """거래량 값 (미확인이면 None). 0 은 그대로 0."""
    if "Volume" not in row.index:
        return None
    try:
        v = float(row["Volume"])
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v >= 0 else None


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _day_bounds(day: str) -> tuple:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(EXCHANGE_TZ)
    d = date.fromisoformat(day)
    a = datetime(d.year, d.month, d.day, tzinfo=tz)
    return int(a.timestamp()), int((a + timedelta(days=1)).timestamp())


def chart_params(day: str) -> dict:
    p1, p2 = _day_bounds(day)
    return {"period1": p1, "period2": p2, "interval": "1d", "includePrePost": "false", "events": "div,splits"}


def parse_chart(payload, ticker: str, day: str, response_sha256: Optional[str] = None) -> tuple:
    """Yahoo v8 chart 원본 JSON → (Bar | None, 근거 dict). 순수 함수 (테스트에서 원본 응답을 그대로 주입).

    Bar 가 None 이면 OHLC 를 쓸 수 없는 응답이다 (결측과 같이 처리: 마감 전 대기, 마감 후 MISSING).
    OHLC 는 정상이고 거래량만 미확인이면 Bar(volume=None) 를 돌려준다 (마감 후 INVALID/UNKNOWN)."""
    ev = {"source": SOURCE_YAHOO_CHART_RAW, "ticker": ticker, "day": day, "response_sha256": response_sha256,
          "issues": []}

    def fail(code):
        ev["issues"].append(code)
        return None, ev
    try:
        chart = (payload or {}).get("chart") or {}
        if chart.get("error"):
            ev["chart_error"] = str(chart.get("error"))[:200]
            return fail("CHART_ERROR")
        res = (chart.get("result") or [None])[0]
        if not isinstance(res, dict):
            return fail("NO_RESULT")
        meta = res.get("meta") or {}
        sym = str(meta.get("symbol") or "").upper()
        if sym and sym != ticker.upper():
            ev["meta_symbol"] = sym
            return fail("SYMBOL_MISMATCH")
        ts = res.get("timestamp")
        quotes = (res.get("indicators") or {}).get("quote") or []
        q = quotes[0] if quotes and isinstance(quotes[0], dict) else None
        if not isinstance(ts, list) or not ts or q is None:
            return fail("EMPTY_OR_PARTIAL_RESPONSE")
        from zoneinfo import ZoneInfo
        tzname = meta.get("exchangeTimezoneName") or EXCHANGE_TZ
        try:
            tz = ZoneInfo(tzname)
        except Exception:
            tz = ZoneInfo(EXCHANGE_TZ)
            ev["issues"].append("TZ_FALLBACK")
        ev["exchange_tz"] = str(tz)
        idx = [i for i, t in enumerate(ts) if _num(t) and datetime.fromtimestamp(t, tz).date().isoformat() == day]
        if not idx:
            return fail("DAY_NOT_IN_RESPONSE")
        if len(idx) > 1:
            return fail("DUPLICATE_DAY")
        i = idx[0]
        ev["timestamp"] = ts[i]
        raw = {}
        for k in ("open", "high", "low", "close"):
            arr = q.get(k)
            if not isinstance(arr, list) or len(arr) != len(ts):
                return fail(f"{k.upper()}_ARRAY_PARTIAL")
            raw[k] = arr[i]
        adj = None
        adj_list = (res.get("indicators") or {}).get("adjclose") or []
        if adj_list and isinstance(adj_list[0], dict) and isinstance(adj_list[0].get("adjclose"), list):
            arr = adj_list[0]["adjclose"]
            if len(arr) == len(ts):
                adj = arr[i]
            else:
                ev["issues"].append("ADJCLOSE_ARRAY_PARTIAL")
        else:
            ev["issues"].append("ADJCLOSE_ABSENT")
        raw["adjclose"] = adj
        if "volume" not in q:
            raw["volume"] = None
            ev["volume_field"] = "ABSENT"
            ev["issues"].append("VOLUME_FIELD_ABSENT")
        elif not isinstance(q.get("volume"), list) or len(q["volume"]) != len(ts):
            raw["volume"] = None
            ev["volume_field"] = "PARTIAL"
            ev["issues"].append("VOLUME_ARRAY_PARTIAL")
        else:
            raw["volume"] = q["volume"][i]
            ev["volume_field"] = "PRESENT"
        ev["raw"] = raw
        if not all(_num(raw[k]) and raw[k] > 0 for k in ("open", "high", "low", "close")):
            return fail("OHLC_NULL_OR_INVALID")
        ratio = 1.0
        if _num(adj) and adj > 0:
            ratio = adj / raw["close"]
        elif adj is not None:
            ev["issues"].append("ADJCLOSE_INVALID")
        ev["adjust_ratio"] = ratio
        o, h, lo = raw["open"] * ratio, raw["high"] * ratio, raw["low"] * ratio
        c = adj if _num(adj) and adj > 0 else raw["close"]
        v = raw["volume"]
        if v is None:
            vol = None
            if ev.get("volume_field") == "PRESENT":
                ev["issues"].append("VOLUME_NULL")
        elif _num(v) and v >= 0:
            vol = float(v)
        else:
            vol = None
            ev["issues"].append("VOLUME_INVALID")
        return Bar(float(o), float(h), float(lo), float(c), volume=vol, volume_source=SOURCE_YAHOO_CHART_RAW,
                   evidence=ev), ev
    except Exception as exc:                               # 형식이 다른 응답 → 쓰지 않는다
        return fail(f"PARSE_ERROR:{type(exc).__name__}")


def _http_get(url: str, params: dict, timeout: int = 15):
    """yfinance 의 세션(쿠키·crumb)으로 원본 응답을 받는다 (DataFrame 변환을 거치지 않음). 테스트는 이 함수를 바꾼다."""
    from yfinance.data import YfData
    return YfData().get(url=url, params=params, timeout=timeout)


def fetch_chart(ticker: str, day: str, http_get: Optional[Callable] = None) -> tuple:
    """원본 응답 1건 → (payload | None, sha256 | None, 오류 코드 | None)."""
    get = http_get or _http_get
    try:
        resp = get(CHART_URL.format(ticker=ticker), chart_params(day))
        body = resp.content if hasattr(resp, "content") else resp.text.encode()
        status = getattr(resp, "status_code", 200)
        sha = hashlib.sha256(body).hexdigest()
        if status != 200 and not body:
            return None, sha, f"HTTP_{status}"
        return json.loads(body.decode("utf-8")), sha, (None if status == 200 else f"HTTP_{status}")
    except Exception as exc:
        return None, None, f"FETCH_ERROR:{type(exc).__name__}"


def yfinance_bars(tickers: list, day: str, http_get: Optional[Callable] = None) -> dict:
    """{ticker: Bar | None}. 종목마다 원본 응답 1건에서 OHLC·거래량을 함께 읽는다 (복수 종목도 서로 섞지 않음)."""
    out = {}
    for t in sorted(set(tickers)):
        payload, sha, err = fetch_chart(t, day, http_get)
        if payload is None:
            logger.warning("자동매수 SHADOW 일봉 원본 조회 실패 %s: %s", t, err)
            out[t] = None
            continue
        bar, _ev = parse_chart(payload, t, day, sha)
        out[t] = bar
    return {t: out.get(t) for t in tickers}


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
