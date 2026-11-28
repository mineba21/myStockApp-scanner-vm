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


_UNSET = object()


def parse_chart(payload, ticker: str, day: str, response_sha256: Optional[str] = None, *, settings=None,
                session=_UNSET) -> tuple:
    """Yahoo v8 chart 원본 JSON → (Bar | None, 근거 dict). 순수 함수 (테스트에서 원본 응답을 그대로 주입).

    Bar 가 None 이면 OHLC 를 쓸 수 없는 응답이다 (결측과 같이 처리: 마감 전 대기, 마감 후 MISSING).
    OHLC 는 정상이고 거래량만 미확인이면 Bar(volume=None) 를 돌려준다 (마감 후 INVALID/UNKNOWN).

    **일봉 응답 계약** — 하나라도 확인할 수 없으면 정상 응답으로 가정하지 않는다 (Bar 없음 → 결측·복구 대상):
    - ``chart.error`` 없음, ``result[0]`` 과 ``meta`` 가 객체
    - ``meta.dataGranularity`` 가 **문자열 "1d"** (분봉·시간봉·주봉·누락·다른 자료형 거부 → 그 응답의 volume:0 도 쓰지 않음)
    - ``meta.symbol`` 이 있고 요청 종목과 같음
    - ``meta.exchangeTimezoneName`` 이 있고 거래소 시간대(America/New_York)와 같음 (대체 시간대로 가정하지 않음)
    - 요청한 날이 정규장 거래일이고, 타임스탬프가 모두 정수·증가 순서·요청 구간 안이며, 그날 봉이 정확히 1개이고
      그 시각이 그날 정규장 시간(조기폐장 반영) 안
    - OHLC 배열 길이가 타임스탬프와 같고, 값이 양수이며 고가·저가가 시가·종가를 감쌈
    거래량 배열 길이·값은 기존 정책 (누락·부분·null·음수 = 미확인, 0 = 이 원본 경로에서만 확정 0).

    **거래소 세션**은 runner 와 같은 캘린더·설정(추가 휴장일·조기폐장일)을 쓴다. 운영 경로는 ``ChartFetcher`` 가 job 의
    ``settings`` 로 구한 그날 세션(``session``)을 넘긴다. ``session`` 을 주지 않으면 ``settings`` 로 직접 구한다
    (``settings`` 도 없으면 기본 NYSE 규칙 — 단독 호출·테스트용)."""
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
        meta = res.get("meta")
        if not isinstance(meta, dict):
            return fail("META_MISSING")
        gran = meta.get("dataGranularity")
        ev["data_granularity"] = gran if gran is None or isinstance(gran, (str, int, float)) else type(gran).__name__
        if gran is None:
            return fail("GRANULARITY_MISSING")
        if not isinstance(gran, str):
            return fail("GRANULARITY_INVALID_TYPE")
        if gran != "1d":
            return fail("GRANULARITY_NOT_DAILY")
        sym = meta.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            return fail("SYMBOL_MISSING")
        if sym.strip().upper() != ticker.upper():
            ev["meta_symbol"] = sym.strip().upper()[:20]
            return fail("SYMBOL_MISMATCH")
        tzname = meta.get("exchangeTimezoneName")
        if not isinstance(tzname, str) or not tzname:
            return fail("TZ_MISSING")
        ev["exchange_tz"] = tzname[:40]
        if tzname != EXCHANGE_TZ:
            return fail("TZ_MISMATCH")
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(EXCHANGE_TZ)
        ts = res.get("timestamp")
        quotes = (res.get("indicators") or {}).get("quote") or []
        q = quotes[0] if quotes and isinstance(quotes[0], dict) else None
        if not isinstance(ts, list) or not ts or q is None:
            return fail("EMPTY_OR_PARTIAL_RESPONSE")
        if not all(isinstance(t, int) and not isinstance(t, bool) for t in ts):
            return fail("TIMESTAMP_INVALID")
        if any(b <= a for a, b in zip(ts, ts[1:])):
            return fail("TIMESTAMP_NOT_INCREASING")
        lo_b, hi_b = _day_bounds(day)
        if any(t < lo_b or t >= hi_b for t in ts):
            return fail("TIMESTAMP_OUT_OF_RANGE")
        if session is _UNSET:
            from trading.auto_buy.shadow import calendar as CAL
            session = CAL.session(date.fromisoformat(day), settings)
        sess = session
        if sess is None:
            return fail("DAY_NOT_TRADING_SESSION")
        ev["session_close_et"] = sess.close_at.astimezone(tz).strftime("%H:%M")
        idx = [i for i, t in enumerate(ts) if datetime.fromtimestamp(t, tz).date().isoformat() == day]
        if not idx:
            return fail("DAY_NOT_IN_RESPONSE")
        if len(idx) > 1:
            return fail("DUPLICATE_DAY")
        i = idx[0]
        ev["timestamp"] = ts[i]
        if not (sess.open_at.timestamp() - 60 <= ts[i] <= sess.close_at.timestamp()):
            ev["bar_time_et"] = datetime.fromtimestamp(ts[i], tz).strftime("%H:%M")
            return fail("BAR_TIME_OUTSIDE_SESSION")
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
        tol = 1e-9 * raw["high"]
        if raw["high"] + tol < max(raw["open"], raw["close"], raw["low"]) or \
                raw["low"] - tol > min(raw["open"], raw["close"]):
            return fail("OHLC_INCONSISTENT")
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


# ── 원본 조회 호출량 안전장치 (한 job 단위) ─────────────────────────────
# Scanner 의 yfinance 수집과 같은 Yahoo 를 쓰므로 SHADOW 가 호출량을 키우지 않도록 묶는다.
REQUEST_TIMEOUT_SEC = 10            # 요청 1건 timeout
MAX_ATTEMPTS = 2                    # 같은 (종목, 거래일) 최대 시도 (재시도 1회, 재시도 가능한 오류만)
JOB_REQUEST_BUDGET = 300            # 한 job 의 **애플리케이션 조회 시도** 상한 (재시도 포함). 넘으면 나머지는 수집하지 않음
                                    # (BUDGET_EXHAUSTED). yfinance 세션이 쿠키·crumb 갱신 등으로 내부에서 보내는 추가 요청은
                                    # 세지 못하므로 실제 네트워크 요청 수의 상한이 아니다 (운영 지표 ``fetch.requests`` 도 같은 뜻)
BACKOFF_BASE_SEC = 2.0              # 429/503 등 재시도 전 대기 (Retry-After 가 있으면 그 값)
RETRY_AFTER_MAX_SEC = 30.0          # Retry-After 가 이보다 길면 기다리지 않고 이번 job 수집 중단(RATE_LIMITED)
JOB_BACKOFF_TOTAL_MAX_SEC = 60.0    # 한 job 의 대기 합계 상한
CIRCUIT_CONSECUTIVE_FAILURES = 5    # 연속 전송 실패(재시도 후) 이 수에 이르면 이번 job 수집 중단(CIRCUIT_OPEN)
RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


def _sleep(sec: float) -> None:
    """재시도 대기 (테스트는 이 함수를 바꿔 실제로 기다리지 않는다)."""
    import time as _t
    _t.sleep(sec)


def _http_get(url: str, params: dict, timeout: int = REQUEST_TIMEOUT_SEC):
    """yfinance 의 세션(쿠키·crumb)으로 원본 응답을 받는다 (DataFrame 변환을 거치지 않음). 테스트는 이 함수를 바꾼다."""
    from yfinance.data import YfData
    return YfData().get(url=url, params=params, timeout=timeout)


def _is_yf_rate_limit(exc: BaseException) -> bool:
    """yfinance 가 HTTP 429 등을 감지하면 응답 대신 ``YFRateLimitError`` 를 던진다 (``YfData.get``). 일반 조회 오류와 구분."""
    try:
        from yfinance.exceptions import YFRateLimitError
    except Exception:                                  # pragma: no cover - yfinance 구버전
        return type(exc).__name__ == "YFRateLimitError"
    return isinstance(exc, YFRateLimitError)


def _retry_after(resp) -> Optional[float]:
    try:
        v = (getattr(resp, "headers", None) or {}).get("Retry-After")
    except Exception:
        return None
    if v is None:
        return None
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, (parsedate_to_datetime(str(v)) - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


def fetch_chart(ticker: str, day: str, http_get: Optional[Callable] = None,
                timeout: float = REQUEST_TIMEOUT_SEC) -> dict:
    """원본 응답 1건 → {"payload", "sha256", "status", "error", "retryable", "retry_after"}.

    - HTTP 상태가 정수 200 일 때만 본문을 JSON 으로 읽는다. 상태를 확인할 수 없는 응답(``status_code`` 없음)은
      성공으로 가정하지 않는다 (``HTTP_STATUS_UNKNOWN``).
    - HTTP 오류(4xx·5xx 등)는 본문이 정상 형식 JSON 이어도 읽지 않는다 → 그 본문의 ``volume: 0`` 은 확정 0 이 될 수 없다.
    - 실패에도 응답 해시(본문이 있으면)·상태·오류 종류를 남긴다."""
    get = http_get or _http_get
    out = {"payload": None, "sha256": None, "status": None, "error": None, "retryable": False, "retry_after": None,
           "rate_limited": False}
    try:
        resp = get(CHART_URL.format(ticker=ticker), chart_params(day), timeout=timeout)
    except Exception as exc:
        name = type(exc).__name__
        if _is_yf_rate_limit(exc):                     # 호출 제한: 재시도하지 않고 이번 job 의 Yahoo 조회를 멈춘다
            out.update(error="YF_RATE_LIMITED", rate_limited=True, exception=name)
        elif "timeout" in name.lower() or "timedout" in name.lower():
            out.update(error="TIMEOUT", retryable=True)
        elif isinstance(exc, (ConnectionError, OSError)) or "connection" in name.lower():
            out.update(error="CONNECTION_ERROR", retryable=True)
        else:
            out.update(error=f"FETCH_ERROR:{name}")
        return out
    try:
        body = resp.content if getattr(resp, "content", None) is not None else str(resp.text).encode()
    except Exception:
        body = b""
    if not isinstance(body, (bytes, bytearray)):
        body = str(body).encode()
    out["sha256"] = hashlib.sha256(body).hexdigest() if body else None
    status = getattr(resp, "status_code", None)
    out["status"] = status if isinstance(status, int) and not isinstance(status, bool) else None
    if out["status"] is None:
        out["error"] = "HTTP_STATUS_UNKNOWN"
        return out
    if status != 200:
        out.update(error=f"HTTP_{status}", retryable=status in RETRYABLE_HTTP, retry_after=_retry_after(resp))
        return out
    if not body:
        out["error"] = "EMPTY_BODY"
        return out
    try:
        out["payload"] = json.loads(bytes(body).decode("utf-8"))
    except Exception:
        out["error"] = "INVALID_JSON"
        return out
    if not isinstance(out["payload"], dict):
        out.update(payload=None, error="INVALID_JSON")
    return out


def _failure(code: str, ticker: str, day: str, res: Optional[dict] = None, attempts: int = 0, **extra):
    from trading.auto_buy.shadow.vfills import BarUnavailable
    ev = {"source": SOURCE_YAHOO_CHART_RAW, "ticker": ticker, "day": day, "error": code, "attempts": attempts,
          "fetch_failed": True}
    if res:
        ev.update(http_status=res.get("status"), response_sha256=res.get("sha256"), retry_after=res.get("retry_after"))
    ev.update(extra)
    return BarUnavailable(code, ev)


class ChartFetcher:
    """한 job 동안 쓰는 원본 일봉 공급자 ``(tickers, day) -> {ticker: Bar | BarUnavailable}``.

    - 같은 (종목, 거래일)은 job 안에서 한 번만 조회한다 (결과 재사용, 실패 포함).
    - 요청 예산 ``JOB_REQUEST_BUDGET`` (재시도 포함). 넘으면 ``BUDGET_EXHAUSTED`` — 정상 거래일로 확정하지 않는다.
    - 요청마다 timeout, 재시도 가능한 오류(429·5xx·timeout·연결 오류)만 ``MAX_ATTEMPTS`` 까지 재시도한다.
      재시도 전 대기는 Retry-After(있으면) 또는 지수 backoff. Retry-After 가 ``RETRY_AFTER_MAX_SEC`` 를 넘거나 대기 합계가
      ``JOB_BACKOFF_TOTAL_MAX_SEC`` 를 넘으면 이번 job 수집을 멈춘다 (``RATE_LIMITED``).
    - 연속 전송 실패가 ``CIRCUIT_CONSECUTIVE_FAILURES`` 에 이르면 이번 job 의 나머지 조회를 하지 않는다 (``CIRCUIT_OPEN``).
    - 원본 응답 내용 오류(chart.error·결과 없음·종목/거래일 불일치·OHLC 오류)는 재시도하지 않고 결측으로 돌려준다.
    실패는 모두 ``BarUnavailable`` (근거 포함) 이며 결측과 같이 처리된다 (마감 후 MISSING → 복구 시 재계산)."""

    def __init__(self, http_get: Optional[Callable] = None, *, budget: Optional[int] = None,
                 sleep: Optional[Callable] = None, settings=None):
        self.http_get = http_get
        self.settings = settings                 # runner 와 같은 거래소 캘린더 설정 (추가 휴장일·조기폐장일)
        self.contract_ok = 0                     # 이번 job 에서 일봉 응답 계약을 통과한 실제 신규 응답 수
        self.budget = JOB_REQUEST_BUDGET if budget is None else budget
        self.sleep = sleep or (lambda sec: _sleep(sec))
        self.cache: dict = {}
        self.requests = 0
        self.consecutive_failures = 0
        self.backoff_total = 0.0
        self.halted: Optional[str] = None
        self.failures: dict = {}

    def stats(self) -> dict:
        return {"requests": self.requests, "contract_ok": self.contract_ok, "budget": self.budget,
                "cached": len(self.cache),
                "backoff_sec": round(self.backoff_total, 1), "halted": self.halted,
                "failures": dict(sorted(self.failures.items()))}

    def __call__(self, tickers: list, day: str) -> dict:
        for t in sorted(set(tickers)):
            if (t, day) not in self.cache:
                self.cache[(t, day)] = self._one(t, day)
                b = self.cache[(t, day)]
                if not isinstance(b, Bar):
                    self.failures[b.code] = self.failures.get(b.code, 0) + 1
        return {t: self.cache[(t, day)] for t in tickers}

    def _one(self, t: str, day: str):
        attempts = 0
        while True:
            if self.halted:
                return _failure(self.halted, t, day, attempts=attempts)
            if self.requests >= self.budget:
                self.halted = self.halted or "BUDGET_EXHAUSTED"
                logger.warning("자동매수 SHADOW 원본 조회 예산 %s건 소진 — 이번 job 나머지 조회 중단", self.budget)
                return _failure("BUDGET_EXHAUSTED", t, day, attempts=attempts)
            self.requests += 1
            attempts += 1
            res = fetch_chart(t, day, self.http_get)
            if res["payload"] is not None:
                self.consecutive_failures = 0
                from trading.auto_buy.shadow import calendar as CAL
                bar, ev = parse_chart(res["payload"], t, day, res["sha256"],
                                      session=CAL.session(date.fromisoformat(day), self.settings))
                if bar is None:                     # 200 이지만 내용이 쓸 수 없는 응답 (재시도하지 않음)
                    code = (ev.get("issues") or ["PARSE_ERROR"])[0]
                    return _failure(code, t, day, res, attempts, issues=ev.get("issues"),
                                    chart_error=ev.get("chart_error"), meta_symbol=ev.get("meta_symbol"),
                                    data_granularity=ev.get("data_granularity"), exchange_tz=ev.get("exchange_tz"),
                                    bar_time_et=ev.get("bar_time_et"), session_close_et=ev.get("session_close_et"))
                bar.evidence["http_status"] = res["status"]
                bar.evidence["attempts"] = attempts
                self.contract_ok += 1                   # 거래량 미확인(volume=None)이어도 계약 통과 응답이면 센다
                return bar
            code = res["error"]
            if res.get("rate_limited"):                 # yfinance YFRateLimitError: 즉시 중단, 다른 종목도 요청하지 않음
                self.halted = "RATE_LIMITED"
                logger.warning("자동매수 SHADOW Yahoo 호출 제한(%s) — 이번 job 나머지 조회 중단", res.get("exception"))
                return _failure(code, t, day, res, attempts, halted="RATE_LIMITED", exception=res.get("exception"))
            if res["retryable"] and attempts < MAX_ATTEMPTS:
                ra = res.get("retry_after")
                if ra is not None and ra > RETRY_AFTER_MAX_SEC:
                    self.halted = "RATE_LIMITED"
                    return _failure(code, t, day, res, attempts, halted="RATE_LIMITED")
                wait = ra if ra is not None else BACKOFF_BASE_SEC * (2 ** (attempts - 1))
                if self.backoff_total + wait > JOB_BACKOFF_TOTAL_MAX_SEC:
                    self.halted = "RATE_LIMITED"
                    return _failure(code, t, day, res, attempts, halted="RATE_LIMITED")
                self.backoff_total += wait
                self.sleep(wait)
                continue
            self.consecutive_failures += 1
            if self.consecutive_failures >= CIRCUIT_CONSECUTIVE_FAILURES:
                self.halted = "CIRCUIT_OPEN"
                logger.warning("자동매수 SHADOW 원본 조회 연속 %s건 실패 — 이번 job 나머지 조회 중단", self.consecutive_failures)
            if code in ("HTTP_429", "HTTP_503") and not self.halted:
                self.halted = "RATE_LIMITED"            # 재시도 후에도 제한 응답 → 더 보내지 않는다
            logger.warning("자동매수 SHADOW 일봉 원본 조회 실패 %s %s: %s", t, day, code)
            return _failure(code, t, day, res, attempts)


def yfinance_bars(tickers: list, day: str, http_get: Optional[Callable] = None, settings=None) -> dict:
    """{ticker: Bar | BarUnavailable}. 종목마다 원본 응답 1건에서 OHLC·거래량을 함께 읽는다 (복수 종목도 서로 섞지 않음).
    호출마다 새 ``ChartFetcher`` (단독 호출용). 운영 job 은 job 단위 ``ChartFetcher`` 하나를 공유한다 (runner)."""
    return ChartFetcher(http_get, settings=settings)(tickers, day)


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
        equity, cash = account_context(summary, "US")         # (순자산 = 총현금 + 원장 평가금액, 주문가능현금)
        total_cash = float((summary.get("overseas") or {}).get("cash"))
    except Exception:
        return _unavailable("순자산·현금 계산 불가")
    if not (math.isfinite(equity) and math.isfinite(cash) and math.isfinite(total_cash)) or equity <= 0 \
            or cash < 0 or total_cash < 0 or cash > equity:
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
    # 필드 구분: total_cash(총현금) · orderable_cash(주문가능현금, 미체결·결제 대기 반영) · holdings_total(보유주식 평가)
    # · equity(순자산). 신규 가상 매수의 현금 판단(cash)은 보수적으로 주문가능현금을 쓴다. 가상 포지션·주문 예약금은
    # 여기 넣지 않고 risk.account_state 가 가상 원장에서 따로 더한다 (실계좌 값과 섞지 않음).
    return {"status": "OK", "reason": None, "equity": equity, "cash": cash, "orderable_cash": cash,
            "total_cash": total_cash, "holdings": holdings, "holdings_total": total, "ledger_evaluation": ledger,
            "as_of": summary.get("updated_at") or datetime.now(timezone.utc).isoformat(), "source": "kiwoom_readonly"}
