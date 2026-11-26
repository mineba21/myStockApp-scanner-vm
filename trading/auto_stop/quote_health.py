"""시세 장애 격리: 오류 분류, 종목별 상태 머신, 감시 슬롯. 순수 함수만 둔다 (DB·증권사 호출 없음).

배경: 2026-11-25 SHADOW 첫날, 종목 공통 시세 오류 카운터와 '첫 포지션만 반복 조회'하는 회복 확인 때문에
전체 신규 손절 진입이 장 종료까지 보류됐다. 최초 QuoteUnavailable 의 상세 원인은 기록되지 않아 미확인이다.
그래서 모든 오류를 종목별 장애로 보지 않는다: 범위를 확정할 수 없는 오류는 전체(Global) 카운터에도 센다.

오류 범위 (docs/auto_stop_sell.md "시세 장애 격리와 복구")
- SYSTEM : 인증(HTTP 401/403), 호출 제한(429 재시도 소진), 서버 오류(5xx), 네트워크·timeout,
           응답 형식(JSON 아님 등), 브로커 생성(토큰 발급) 실패. → 종목 카운터 + 전체 카운터
- UNKNOWN: 키움 응답 코드 오류(return_code), 그 밖의 HTTP 상태, 분류되지 않은 예외.
           → 종목 카운터 + 전체 카운터 (범위 미확정이므로 전체 장애 가능성을 배제하지 않는다)
- SYMBOL : 응답은 왔지만 그 종목 시세를 쓸 수 없음 (종목·거래소 불일치, 가격 해석 실패, 1호가 필드 이상,
           검증 실패: 시각 없음·당일 아님·개장 전·120초 초과 등). → 종목 카운터만.
           단 여러 종목이 동시에 장애면 '광범위 조회 실패'로 전체 보류한다 (engine._hold_reasons).
"""
from __future__ import annotations

import math
import re

SYSTEM, SYMBOL, UNKNOWN = "SYSTEM", "SYMBOL", "UNKNOWN"

SYSTEM_CATEGORIES = frozenset({"auth", "rate_limit", "http_5xx", "network", "timeout", "response_format",
                               "broker_init"})
VALIDATION_CODES = frozenset({"symbol_mismatch", "exchange_mismatch", "price_invalid", "no_quote_time",
                              "not_trading_day", "not_today", "pre_open", "future_time", "stale"})
SYMBOL_CATEGORIES = frozenset({"price_parse", "orderbook_field"}) | VALIDATION_CODES

OK, FAULT, RECOVERING = "OK", "FAULT", "RECOVERING"
RECOVERY_CONFIRMATIONS = 2          # 서로 다른 유효 시세 시각으로 연속 2회 정상이면 종목 장애 해제
GLOBAL_RECOVERY_ROUNDS = 2          # 전체 시세 보류 해제: 서로 다른 tick 2회 연속, 새 유효 시세가 있고 SYSTEM 오류 없음
BROAD_FAULT_MIN = 2                 # 광범위 조회 실패: 장애 종목 2개 이상이면서
BROAD_FAULT_RATIO = 0.5             # 감시 종목의 절반 이상
SLOT_TOLERANCE_SEC = 3.0            # 30초 tick 의 미세한 앞당김을 같은 슬롯으로 본다
MAX_BACKOFF_FACTOR = 4              # 429 반복 시 회복 조회 간격 상한 (감시 주기 × 4)
RECENT_ERRORS_KEEP = 50


def scope_of(category: str) -> str:
    if category in SYSTEM_CATEGORIES:
        return SYSTEM
    if category in SYMBOL_CATEGORIES:
        return SYMBOL
    return UNKNOWN


# ── 민감정보 제거 ────────────────────────────────────────────────
_BEARER = re.compile(r"(?i)bearer\s+\S+")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-\.=+/]{24,}")
_DIGITS = re.compile(r"\d{8,}")


def sanitize(text, limit: int = 200) -> str:
    """오류 문구에서 토큰·계좌번호처럼 보이는 값을 지운다. 원본 응답 전체는 다루지 않는다."""
    s = str(text or "")
    s = _BEARER.sub("Bearer <redacted>", s)
    s = _LONG_TOKEN.sub("<redacted>", s)
    s = _DIGITS.sub("<digits>", s)
    s = " ".join(s.split())
    return s[:limit]


# ── 오류 분류 ────────────────────────────────────────────────────
def _from_kiwoom(exc) -> dict:
    kind = getattr(exc, "kind", None)
    status = getattr(exc, "http_status", None)
    code = getattr(exc, "return_code", None)
    out = {"http_status": status, "return_code": code}
    if kind == "http":
        if status in (401, 403):
            out["category"] = "auth"
        elif status == 429:
            out["category"] = "rate_limit"
        elif isinstance(status, int) and status >= 500:
            out["category"] = "http_5xx"
        else:
            out["category"] = "http_error"
    elif kind in ("json", "format"):
        out["category"] = "response_format"
    elif kind == "api":
        # 8005 = 'Token이 유효하지 않습니다' (web/kiwoom_holdings._is_invalid_token_error 와 같은 해석)
        out["category"] = "auth" if str(code) == "8005" else "kiwoom_api_error"
    else:
        out["category"] = "kiwoom_error"
    return out


def _network_category(exc) -> str | None:
    try:
        import requests
    except ImportError:          # pragma: no cover
        return None
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.RequestException):
        return "network"
    return None


def classify(exc) -> dict:
    """예외 → {category, scope, error_type, http_status, return_code, message, ...}. 추정 값은 넣지 않는다."""
    from trading.auto_stop.quotes import QuoteUnavailable
    from trading.kiwoom_readonly import KiwoomError

    info = {"error_type": type(exc).__name__, "message": sanitize(exc)}
    cause = exc.__cause__ if isinstance(exc, QuoteUnavailable) else None
    if isinstance(exc, QuoteUnavailable):
        extra = dict(getattr(exc, "info", {}) or {})
        if cause is not None:
            info["cause_type"] = type(cause).__name__
            info["cause_message"] = sanitize(cause)
        if "category" not in extra and cause is not None:
            if isinstance(cause, KiwoomError):
                extra = {**_from_kiwoom(cause), **extra}
            else:
                net = _network_category(cause)
                if net:
                    extra["category"] = net
        info.update({k: v for k, v in extra.items() if v is not None})
        info.setdefault("category", "quote_unavailable")
    elif isinstance(exc, KiwoomError):
        info.update({k: v for k, v in _from_kiwoom(exc).items() if v is not None})
    else:
        info["category"] = _network_category(exc) or "unexpected"
    info["scope"] = scope_of(info["category"])
    return info


def validation_error(code: str, message: str) -> dict:
    return {"category": code, "scope": scope_of(code), "error_type": "Validation", "message": sanitize(message),
            "validation": code}


# ── 종목별 상태 ──────────────────────────────────────────────────
def new_record(now: float, position: dict) -> dict:
    return {"position_id": position["id"], "ticker": position["ticker"], "exchange": position["exchange"],
            "state": OK, "consecutive_errors": 0, "anchor": now, "next_due_at": now,
            "cur_slot": None, "slot_ok": False, "slot_attempts": 0, "slot_held": False, "slot_purpose": None,
            "last_ok_at": None, "last_ok_as_of": None, "fault_started_at": None, "fault_reason": None,
            "recovered_at": None, "confirm_as_of": [], "rate_limit_streak": 0, "last_error": None,
            "first_error": None, "last_attempt": None}


def is_due(rec: dict, now: float) -> bool:
    return now + SLOT_TOLERANCE_SEC >= (rec.get("next_due_at") or 0)


def slot_start(rec: dict, now: float, interval: int) -> float:
    if interval <= 0:
        return now
    anchor = rec.get("anchor") or now
    k = math.floor((now + SLOT_TOLERANCE_SEC - anchor) / interval)
    return anchor + k * interval


def enter_slot(rec: dict, now: float, interval: int) -> str | None:
    """현재 슬롯으로 이동한다. 이전 일반 감시 슬롯이 시도는 있었으나 성공 없이 끝났으면 'failed'."""
    cur = slot_start(rec, now, interval)
    if rec.get("cur_slot") == cur:
        return None
    closed = None
    if (rec.get("cur_slot") is not None and rec.get("slot_purpose") == "watch"
            and rec.get("slot_attempts") and not rec.get("slot_ok")):
        closed = "failed"
    rec.update(cur_slot=cur, slot_ok=False, slot_attempts=0, slot_held=False, slot_purpose=None)
    return closed


def schedule_next_slot(rec: dict, now: float, interval: int, factor: int = 1) -> None:
    """다음 예정 시각 = 현재 슬롯 시작 + 주기 × factor. 응답 지연과 무관하게 슬롯 기준으로 고정한다.
    놓친 슬롯을 한꺼번에 만회하지 않는다 (지난 슬롯은 건너뛰고 '현재 이후' 첫 슬롯)."""
    if interval <= 0:
        rec["next_due_at"] = now
        return
    base = slot_start(rec, now, interval)
    nxt = base + interval * max(1, factor)
    while nxt + SLOT_TOLERANCE_SEC <= now:
        nxt += interval
    rec["next_due_at"] = nxt


def retry_next_tick(rec: dict, now: float) -> None:
    """정상 상태에서 실패한 일반 감시: 같은 슬롯 안에서 다음 tick 에 1회 더 시도한다 (tick 당 1회 상한)."""
    rec["next_due_at"] = now


def on_success(rec: dict, now: float, as_of: float) -> str | None:
    """유효 시세 1건 반영. 반환: 'confirm' (회복 1차), 'recovered', 'stale_repeat', None (정상 유지)."""
    rec["consecutive_errors"] = 0
    rec["rate_limit_streak"] = 0
    rec["last_ok_at"] = now
    prev_as_of = rec.get("last_ok_as_of")
    rec["last_ok_as_of"] = as_of if prev_as_of is None else max(prev_as_of, as_of)
    state = rec.get("state", OK)
    if state == OK:
        return None
    confirms = list(rec.get("confirm_as_of") or [])
    if confirms and as_of <= confirms[-1]:
        return "stale_repeat"           # 같은(또는 더 오래된) 시세 시각 반복은 복구 근거가 아니다
    confirms.append(as_of)
    rec["confirm_as_of"] = confirms
    if len(confirms) >= RECOVERY_CONFIRMATIONS:
        rec.update(state=OK, recovered_at=now, confirm_as_of=[], fault_reason=None)
        return "recovered"
    rec["state"] = RECOVERING
    return "confirm"


def on_error(rec: dict, now: float, err: dict, threshold: int) -> str | None:
    """오류 1건 반영. 반환: 'fault' (이번에 장애 진입), 'refault' (회복 확인 중 다시 실패), None."""
    rec["consecutive_errors"] = int(rec.get("consecutive_errors") or 0) + 1
    rec["rate_limit_streak"] = (int(rec.get("rate_limit_streak") or 0) + 1
                                if err.get("category") == "rate_limit" else 0)
    rec["last_error"] = err
    state = rec.get("state", OK)
    if state == RECOVERING:
        rec.update(state=FAULT, confirm_as_of=[])
        return "refault"
    if state == OK and rec["consecutive_errors"] >= threshold:
        rec.update(state=FAULT, fault_started_at=now, fault_reason=err.get("category"), confirm_as_of=[],
                   recovered_at=None)
        return "fault"
    return None


def backoff_factor(rec: dict) -> int:
    """회복 조회 간격 배수. 호출 제한(429)이 반복될 때만 2, 4 배로 늘린다."""
    streak = int(rec.get("rate_limit_streak") or 0)
    return min(MAX_BACKOFF_FACTOR, 2 ** min(streak, 2)) if streak else 1


def is_broad(faulted: int, watched: int) -> bool:
    return watched >= 2 and faulted >= BROAD_FAULT_MIN and faulted >= watched * BROAD_FAULT_RATIO
