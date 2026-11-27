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
    """현재 슬롯으로 이동한다. 이전 슬롯이 일반 감시 시도만 있고 분류(ok/held/paused) 없이 끝났으면 'failed'.

    슬롯 분류(slot_class)는 배타적이다: 한 슬롯은 정상(ok)·보류(held)·일시중지(paused)·실패(failed)·
    스케줄 누락(시도 없음) 중 정확히 하나로만 센다 (회복 조회·재시도로 중복 집계하지 않는다)."""
    cur = slot_start(rec, now, interval)
    if rec.get("cur_slot") == cur:
        return None
    closed = None
    if (rec.get("cur_slot") is not None and rec.get("slot_purpose") == "watch"
            and rec.get("slot_attempts") and not rec.get("slot_class")):
        closed = "failed"
    rec.update(cur_slot=cur, slot_ok=False, slot_attempts=0, slot_held=False, slot_purpose=None, slot_class=None)
    return closed


def claim_slot_class(rec: dict, cls: str) -> bool:
    """현재 슬롯에 분류를 처음 붙일 때만 True (이미 분류된 슬롯은 다시 세지 않는다)."""
    if rec.get("slot_class"):
        return False
    rec["slot_class"] = cls
    rec["slot_ok"] = cls == "ok"
    rec["slot_held"] = cls == "held"
    return True


def schedule_next_slot(rec: dict, now: float, interval: int, factor: int = 1) -> None:
    """다음 예정 시각 = 현재 슬롯 시작 + 주기 × factor. 응답 지연과 무관하게 슬롯 기준으로 고정한다.
    놓친 슬롯을 한꺼번에 만회하지 않는다 (지난 슬롯은 건너뛰고 '현재 이후' 첫 슬롯)."""
    if interval <= 0:
        rec["next_due_at"] = now
        rec["base_due_at"] = None
        return
    base = slot_start(rec, now, interval)
    nxt = base + interval * max(1, factor)
    while nxt + SLOT_TOLERANCE_SEC <= now:
        nxt += interval
    rec["next_due_at"] = nxt
    # 백오프가 없었다면 조회했을 시각. 이 시각이 지나도 조회하지 않은 tick 은 '백오프로 조회하지 않음'이다.
    rec["base_due_at"] = base + interval if factor > 1 else None


def not_due_status(rec: dict, now: float) -> str:
    base = rec.get("base_due_at")
    if base is not None and now + SLOT_TOLERANCE_SEC >= base:
        return "skipped_backoff"
    return "not_due"


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


# ── 시스템 장애(incident): 발생 원인·영향 범위·대상별 복구 증거 ───────────────
# auto_stop_runtime.quote_global_hold 에 보존한다 (tick·재시작 사이 유지). 종목 추가·일시중지·관리 종료로는
# 영향 대상이 빠지지 않는다. 조회하지 않은 대상(백오프·시간 예산·장외)은 증거가 생기지 않으므로 해제되지 않는다.
CAUSE_SYSTEM = "system"     # SYSTEM/UNKNOWN 오류로 영향 대상이 됨 → 같은 요청 경로의 정상 응답 증거 필요
CAUSE_BROAD = "broad"       # 광범위 조회 실패의 장애 종목 → 종목별 회복(서로 다른 유효 시세 시각 2회) 필요


def incident_new(now: float, trigger: str, category, **extra) -> dict:
    return {"since": now, "reason": category, "triggers": [trigger], "rounds_ok": 0, "seen": [],
            "affected": {}, "last_system_error_at": now if trigger != "quote_broad" else None, **extra}


def incident_add(inc: dict, position: dict, cause: str, category, now: float, *, reset: bool = True) -> dict:
    """영향 대상 추가. reset=True(새 SYSTEM/UNKNOWN 오류)면 그 대상의 증거를 처음부터 다시 모은다."""
    key = str(position["id"])
    affected = inc.setdefault("affected", {})
    a = affected.get(key)
    if a is None:
        a = affected[key] = {"position_id": position["id"], "ticker": position["ticker"],
                             "exchange": position["exchange"], "mode": position["mode"], "causes": [],
                             "category": category, "since": now, "anchor": now, "next_due_at": now,
                             "evidence_ticks": [], "evidence_as_of": [], "resolved": False,
                             "last_status": "failed", "last_at": now, "rate_limit_streak": 0}
        reset = True
    if cause not in a["causes"]:
        a["causes"].append(cause)
        a["resolved"] = False
    if reset:
        a.update(category=category, evidence_ticks=[], evidence_as_of=[], resolved=False,
                 last_status="failed", last_at=now)
        a["rate_limit_streak"] = (int(a.get("rate_limit_streak") or 0) + 1) if category == "rate_limit" else 0
    return a


def incident_observe(inc: dict, position_id, tick_at: float, kind: str, now: float, *, as_of=None,
                     health_state=None, managed=True) -> None:
    """영향 대상의 실제 조회 결과 1건. kind: 'ok'(유효 시세) / 'symbol'(응답은 왔으나 그 종목 시세 무효).

    - 같은 tick 의 두 번째 응답은 증거가 아니다 (서로 다른 tick 필요).
    - 유효 시세는 이전 증거보다 새로운 시세 시각일 때만 증거다 (같은 시세 반복 차단).
    - SYSTEM/UNKNOWN 오류는 incident_add(reset=True)로 들어온다.
    """
    a = (inc.get("affected") or {}).get(str(position_id))
    if a is None:
        return
    a["last_at"] = now
    if tick_at in a["evidence_ticks"]:
        return
    if kind == "ok":
        if a["evidence_as_of"] and as_of is not None and as_of <= max(a["evidence_as_of"]):
            a["last_status"] = "stale_repeat"
            return
        a["evidence_as_of"].append(as_of)
    a["evidence_ticks"].append(tick_at)
    a["rate_limit_streak"] = 0
    refresh_resolved(a, health_state=health_state, managed=managed)


def refresh_resolved(a: dict, *, health_state=None, managed=True) -> bool:
    ok = True
    if CAUSE_SYSTEM in a["causes"]:
        ok = ok and len(a["evidence_ticks"]) >= RECOVERY_CONFIRMATIONS
    if CAUSE_BROAD in a["causes"]:
        # 관리 중(ACTIVE/EXITING)이면 그 종목의 회복 상태(OK)가 필요하다. 관리가 끝난 종목은 거래 판단에 쓰이지
        # 않으므로 같은 요청 경로의 정상 응답 증거(서로 다른 tick 2회)로 대신한다 (관리 종료만으로 해제 안 됨).
        ok = ok and ((health_state == OK) if managed else len(a["evidence_ticks"]) >= RECOVERY_CONFIRMATIONS)
    a["resolved"] = ok
    a["last_status"] = "recovered" if ok else "recovering"
    return ok


def incident_unresolved(inc: dict) -> list:
    return [a for a in (inc.get("affected") or {}).values() if not a.get("resolved")]


def incident_releasable(inc: dict) -> bool:
    """해제 조건: (1) 모든 영향 대상이 원인별 복구 증거를 충족, (2) 공통 시세 경로가 서로 다른 tick 2회 연속
    새 유효 시세를 내고 그 사이 SYSTEM 오류가 없음."""
    return not incident_unresolved(inc) and int(inc.get("rounds_ok") or 0) >= GLOBAL_RECOVERY_ROUNDS
