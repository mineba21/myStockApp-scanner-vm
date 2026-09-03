"""스캔 엔진 - 전체 스캔 오케스트레이션"""
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ── Strict Weinstein 필터 통합 ────────────────────────────────────
# Phase 4 — analyze_stock 결과 dict 에 ``apply_strict_filter`` 의
# (passed, reasons) 결과를 채우고, ``STRICT_WEINSTEIN_MODE=True`` 면
# strict-pass 만 _save / notify 로 전파한다.
#
# - STRICT_PERSIST_REJECTED=True 토글 시 거부 시그널도 DB 에 영속화 →
#   백테스트/QA 데이터 확보. 알림은 *모든* 모드에서 strict-pass 만 발송.
# - STRICT_NOTIFY_INCLUDE_REASONS=True 토글 시 알림에 거부 사유 표시
#   (그 자체는 strict-pass 시그널이라 normally 빈 리스트지만 legacy 모드
#    에서는 모든 시그널이 통과 표기되므로 무의미; 디버그 도움용).

def _evaluate_strict_filter(signal: dict,
                            market_condition: Optional[str],
                            benchmark_close) -> Tuple[bool, list]:
    """analyze_stock 결과에 strict 필터를 적용하고 (passed, reasons) 를 반환.

    signal dict 에 ``strict_filter_passed`` / ``filter_reasons`` 도 in-place
    로 기록하여 _save() / _notify() 가 그 값을 그대로 영속/표시할 수 있게 한다.

    섹터(Gate 2)는 ``_gics_sector``(유니버스 목록에서 실려온 GICS 원문)를
    표시 라벨과 주봉 Stage 로 풀어 ``sector_name`` / ``sector_stage`` 에 기록한다.
    매핑이 없는 종목(국내 전체, S&P500 밖의 미국 종목)은 둘 다 None 으로 남고,
    ``STRICT_REQUIRE_SECTOR_STAGE2`` 가 꺼져 있는 동안에는 판정에 영향이 없다.
    """
    from scanner.market_analysis import get_sector_stage
    from scanner.strict_filter import apply_strict_filter

    sector_name, sector_stage = get_sector_stage(
        signal.get("_gics_sector"), signal.get("market"),
    )
    signal["sector_name"]  = sector_name
    signal["sector_stage"] = sector_stage

    ctx = {
        "market_condition":  market_condition,
        "sector_stage":      sector_stage,
        "benchmark_present": benchmark_close is not None,
    }
    passed, reasons = apply_strict_filter(signal, ctx)
    signal["strict_filter_passed"] = passed
    signal["filter_reasons"]       = reasons
    return passed, reasons

# ── 스캔 퍼널 집계 (진단 계측) ────────────────────────────────────
# analyze_stock 의 diag out-parameter 로 수집한 탈락 사유를 종목 단위로
# 집계한다. 매수 후보가 0개일 때 "어느 단계에서 전부 죽는지" 를 보기 위한
# 관측 전용 코드 — 스캔 판정 로직에는 영향을 주지 않는다.
#
# funnel 구조:
#   total_scanned  — analyze_stock 을 호출한 종목 수 (= 기존 count)
#   rejects        — analyze_stock 이 None 을 반환한 사유별 카운트
#                    (weinstein.REJECT_* 키). 종목당 1건이며, 세 탐지기
#                    가운데 "파이프라인을 가장 멀리 통과한" 사유가 대표로
#                    기록된다.
#   rejects_by_detector — 같은 탈락을 탐지기별로 쪼갠 카운트. 대표 사유가
#                    다른 경로에 가려지지 않도록 BREAKOUT / RE_BREAKOUT /
#                    REBOUND 각각의 사유를 그대로 센다 (종목당 3건).
#   strict_rejects — 시그널은 생성됐으나 market/strict 필터에서 거부된
#                    사유별 카운트 (strict_filter reason enum, rs_below_zero
#                    등). 한 시그널이 복수 게이트에서 걸리면 각각 카운트.
#   signals        — 알림 대상까지 통과한 시그널의 signal_type 별 카운트
#   week_basis     — 주봉 거래량 산출 기준(CURRENT_COMPLETE / CURRENT_NORMALIZED
#                    / PREVIOUS_COMPLETE) 별 종목 수 (Step 1)
#   weekly_vol_ratio_stats — BREAKOUT 탐지기가 실제로 임계값과 비교한 종목의
#                    주봉 거래량 비율 분포 (median / p90 / max / n). Step 2
#                    임계값 재설정 근거로 쓴다.
#   weekly_gate_cut_but_would_pass_daily — 주봉 게이트(BREAKOUT_WEEKLY_VOL_AS_GATE)
#                    가 실제로 차단한 종목 중, 일봉 거래량 조건은 이미
#                    충족했을 종목 수 (Step 2). 0 이 아니면 주봉 게이트의
#                    "성능 목적 사전 컷" 임계값(0.5)이 실질 후보까지 자르고
#                    있다는 뜻 — 더 낮춰야 한다.
#   base_mode      — 이번 스캔이 쓴 BASE_MODE ("v1" | "v2", Step 2).
#   base_stats     — BREAKOUT 시그널의 base_width_pct / tight_width_pct /
#                    contraction_ratio 중앙값 (v2 전용 필드 — v1 시그널은
#                    tight_width_pct/contraction_ratio 가 None 이라 자동 제외).
#   stop_pct_stats — BREAKOUT 시그널의 (진입가-손절가)/진입가 분포
#                    (median/p90/n). Van Tharp 포지션 사이징 근거.
#                    진입가는 **strict_price**(signal_date 시점 종가) 사용
#                    — stop_loss 도 signal_date 시점 지표로 계산되므로
#                    (Codex 리뷰 P2). strict_price 가 없는 신호(주봉 데이터
#                    부족 등)는 표본에서 제외된다.
#   stop_pct_at_current_price — 같은 계산을 res["price"](최신 종가, 최대
#                    SCAN_LOOKBACK_DAYS 일 지난 시점일 수 있음) 로 한 참고
#                    치. stop_pct_stats 와 크게 벌어지면 알림 시점에 이미
#                    가격이 손절선에 가까워졌다는(추격 리스크) 뜻이다.

MARKET_FILTER_BLOCKED = "market_filter_blocked"

# funnel 내부 누적용 (JSON 출력 전 _finalize_funnel 이 통계로 접어 제거)
_WVR_SAMPLES_KEY         = "_wvr_samples"
_BASE_WIDTH_SAMPLES_KEY  = "_base_width_samples"
_TIGHT_WIDTH_SAMPLES_KEY = "_tight_width_samples"
_CONTRACTION_SAMPLES_KEY = "_contraction_samples"
_STOP_PCT_SAMPLES_KEY    = "_stop_pct_samples"
_STOP_PCT_CURRENT_SAMPLES_KEY = "_stop_pct_current_samples"
_ENTRY_BASELINE_COUNT_KEY     = "_entry_baseline_count"


def _new_funnel() -> dict:
    from scanner.weinstein import BASE_MODE
    return {
        "total_scanned": 0,
        "rejects": {},
        "rejects_by_detector": {"BREAKOUT": {}, "RE_BREAKOUT": {}, "REBOUND": {}},
        "strict_rejects": {},
        "signals": {},
        "week_basis": {},
        "weekly_gate_cut_but_would_pass_daily": 0,
        "base_mode": BASE_MODE,
        "entry_control_shadow": {
            "pivot_ext_would_cut": 0,
            "upthrust_would_cut": 0,
            "upthrust_pending": 0,
            "alert_freshness_would_cut": 0,
            "any_would_cut": 0,
            "would_remain": 0,
        },
        _WVR_SAMPLES_KEY: [],
        _BASE_WIDTH_SAMPLES_KEY: [],
        _TIGHT_WIDTH_SAMPLES_KEY: [],
        _CONTRACTION_SAMPLES_KEY: [],
        _STOP_PCT_SAMPLES_KEY: [],
        _STOP_PCT_CURRENT_SAMPLES_KEY: [],
        _ENTRY_BASELINE_COUNT_KEY: 0,
    }


def _percentile(sorted_vals: list, q: float) -> float:
    """선형보간 백분위 (numpy 의존 없이)."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo  = int(pos)
    hi  = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def _wvr_stats(samples: list) -> dict:
    """주봉 거래량 비율 분포 요약."""
    if not samples:
        return {"median": None, "p90": None, "max": None, "n": 0}
    vals = sorted(float(v) for v in samples)
    return {
        "median": round(_percentile(vals, 0.50), 3),
        "p90":    round(_percentile(vals, 0.90), 3),
        "max":    round(vals[-1], 3),
        "n":      len(vals),
    }


def _median(samples: list) -> Optional[float]:
    if not samples:
        return None
    return round(_percentile(sorted(float(v) for v in samples), 0.50), 3)


def _base_stats(width_samples: list, tight_samples: list,
                contraction_samples: list) -> dict:
    """BREAKOUT 시그널의 v2 base 폭/tight 폭/수축비 중앙값 요약 (Step 2).

    n 은 base_width_pct 표본 수 — v1 시그널도 이 필드는 채우므로 v1/v2 가
    섞인 스캔에서는 n 이 tight_width/contraction 표본 수보다 클 수 있다
    (v1 은 tight_width_pct/contraction_ratio 가 None 이라 자동 제외됨).
    """
    return {
        "base_width_median":        _median(width_samples),
        "tight_width_median":       _median(tight_samples),
        "contraction_ratio_median": _median(contraction_samples),
        "n": len(width_samples),
    }


def _stop_pct_stats(samples: list) -> dict:
    """BREAKOUT 시그널의 (진입가-손절가)/진입가 분포 — Van Tharp 사이징 근거."""
    if not samples:
        return {"median": None, "p90": None, "n": 0}
    vals = sorted(float(v) for v in samples)
    return {
        "median": round(_percentile(vals, 0.50), 4),
        "p90":    round(_percentile(vals, 0.90), 4),
        "n":      len(vals),
    }


def _bump(counter: dict, key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def _funnel_record(funnel: Optional[dict], res: Optional[dict],
                   diag: Optional[dict], notified: bool) -> None:
    """종목 1개의 스캔 결과를 funnel 에 반영."""
    if funnel is None:
        return

    # 결과(시그널/탈락)와 무관하게 먼저 기록 — Step 1/2 진단
    basis = (diag or {}).get("week_basis")
    if basis:
        _bump(funnel["week_basis"], basis)
    breakout_diag = ((diag or {}).get("detectors") or {}).get("BREAKOUT") or {}
    if "wvr" in breakout_diag:
        funnel[_WVR_SAMPLES_KEY].append(breakout_diag["wvr"])
    if breakout_diag.get("would_pass_daily_volume") is True:
        # 주봉 게이트가 차단했지만(diag 는 이 키를 게이트가 실제로 막을 때만
        # 기록한다) 일봉 거래량 조건은 충족했을 종목 — Step 2 성능 컷 검증.
        funnel["weekly_gate_cut_but_would_pass_daily"] += 1

    if res is not None and res.get("signal_type") == "BREAKOUT":
        bw = res.get("base_width_pct")
        tw = res.get("tight_width_pct")
        cr = res.get("contraction_ratio")
        if bw is not None:
            funnel[_BASE_WIDTH_SAMPLES_KEY].append(bw)
        if tw is not None:
            funnel[_TIGHT_WIDTH_SAMPLES_KEY].append(tw)
        if cr is not None:
            funnel[_CONTRACTION_SAMPLES_KEY].append(cr)

        stop = res.get("stop_loss")
        if stop is not None:
            # 진입가 기준 — strict_price(signal_date 시점 종가). stop_loss
            # 도 signal_date 시점 지표로 계산되므로 같은 시점끼리 맞춰야
            # 손절폭이 왜곡되지 않는다 (Codex 리뷰 P2). strict_price 가
            # 없으면(주봉 데이터 부족 등) 이 신호는 표본에서 제외한다.
            strict_price = res.get("strict_price")
            if strict_price and strict_price > 0:
                funnel[_STOP_PCT_SAMPLES_KEY].append((strict_price - stop) / strict_price)

            # 참고치 — 최신 종가(res["price"], signal_date 로부터 최대
            # SCAN_LOOKBACK_DAYS 일 지났을 수 있음) 기준. stop_pct_stats 와
            # 크게 벌어지면 알림 시점에 이미 가격이 손절선에 가까워졌다는
            # (추격 리스크) 뜻 — signal_date 손절폭만으로는 안 보인다.
            price = res.get("price")
            if price and price > 0:
                funnel[_STOP_PCT_CURRENT_SAMPLES_KEY].append((price - stop) / price)

    # Step 4 shadow — 기존 strict 8게이트를 통과했을 후보를 모집단으로 삼아
    # 세 통제를 모두 켰을 때의 교집합 효과를 종목당 한 번만 센다.
    if res is not None and res.get("_pre_entry_control_passed") is True:
        import config
        funnel[_ENTRY_BASELINE_COUNT_KEY] += 1
        shadow = funnel["entry_control_shadow"]
        is_breakout = res.get("signal_type") == "BREAKOUT"
        pivot_cut = bool(
            is_breakout
            and res.get("pivot_ext_pct") is not None
            and res["pivot_ext_pct"] > config.MAX_PIVOT_EXT_PCT
        )
        upthrust_cut = bool(
            is_breakout
            and (res.get("upthrust_failed") is True
                 or res.get("_upthrust_cooldown_active") is True)
        )
        pending = bool(is_breakout and res.get("upthrust_failed") is None)
        freshness_cut = bool(
            is_breakout and res.get("_alert_freshness_would_cut") is True)
        if pivot_cut:
            shadow["pivot_ext_would_cut"] += 1
        if upthrust_cut:
            shadow["upthrust_would_cut"] += 1
        if pending:
            shadow["upthrust_pending"] += 1
        if freshness_cut:
            shadow["alert_freshness_would_cut"] += 1
        if pivot_cut or upthrust_cut or freshness_cut:
            shadow["any_would_cut"] += 1

    if res is None:
        from scanner.weinstein import REJECT_NO_SIGNAL
        _bump(funnel["rejects"], (diag or {}).get("reject") or REJECT_NO_SIGNAL)
        for det, sub in ((diag or {}).get("detectors") or {}).items():
            if det in funnel["rejects_by_detector"]:
                _bump(funnel["rejects_by_detector"][det],
                      (sub or {}).get("reject") or REJECT_NO_SIGNAL)
        return
    if notified:
        _bump(funnel["signals"], res.get("signal_type") or "UNKNOWN")
        return
    if res.get("_alert_suppressed") is True:
        # Strict-pass 상태로 DB에는 기록됐지만 알림 신선도 gate가 전송만 막음.
        return
    # 시그널은 났지만 market filter / strict filter 에서 탈락
    reasons = res.get("filter_reasons") or [MARKET_FILTER_BLOCKED]
    for reason in reasons:
        _bump(funnel["strict_rejects"], reason)


def _write_funnel_file(market: str, funnel: dict) -> Optional[str]:
    """logs/funnel_{market}_{YYYYMMDD_HHMM}.json 으로 스냅샷 저장."""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(root, "logs")
    path = os.path.join(
        log_dir, f"funnel_{market}_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(funnel, f, ensure_ascii=False, indent=2, sort_keys=True)
    except OSError as e:
        logger.warning(f"[{market}] funnel JSON 기록 실패: {e}")
        return None
    return path


def _finalize_funnel(market: str, funnel: Optional[dict], count: int) -> None:
    """스캔 종료 시 total_scanned 확정 + 로그 + JSON 파일 기록."""
    if funnel is None:
        return
    funnel["total_scanned"] = count
    funnel["weekly_vol_ratio_stats"] = _wvr_stats(funnel.pop(_WVR_SAMPLES_KEY, []))
    funnel["base_stats"] = _base_stats(
        funnel.pop(_BASE_WIDTH_SAMPLES_KEY, []),
        funnel.pop(_TIGHT_WIDTH_SAMPLES_KEY, []),
        funnel.pop(_CONTRACTION_SAMPLES_KEY, []),
    )
    funnel["stop_pct_stats"] = _stop_pct_stats(funnel.pop(_STOP_PCT_SAMPLES_KEY, []))
    funnel["stop_pct_at_current_price"] = _stop_pct_stats(
        funnel.pop(_STOP_PCT_CURRENT_SAMPLES_KEY, []))
    baseline_count = funnel.pop(_ENTRY_BASELINE_COUNT_KEY, 0)
    shadow = funnel["entry_control_shadow"]
    shadow["would_remain"] = max(0, baseline_count - shadow["any_would_cut"])
    logger.info(f"[{market}] funnel: {json.dumps(funnel, ensure_ascii=False, sort_keys=True)}")
    path = _write_funnel_file(market, funnel)
    if path:
        logger.info(f"[{market}] funnel JSON: {path}")


scan_status = {
    "is_running": False, "market": "",
    "progress": 0, "total": 0,
    "current_stock": "", "started_at": None,
}


def _prog(cur, tot, msg=""):
    scan_status.update(progress=cur, total=tot, current_stock=msg)


# ── 시장 필터 ─────────────────────────────────────────────────────

def _get_market_filter_decision(market_condition: Optional[str],
                                signal_type: str) -> Tuple[bool, Optional[str]]:
    """
    시장 상태에 따라 BUY 시그널 허용 여부를 결정합니다.

    반환값: (allow: bool, flag_msg: str | None)
      - allow=False → 시그널을 저장/알림하지 않음
      - flag_msg    → 허용되지만 주의 메시지 있음 (CAUTION 상황)
    """
    try:
        from config import ENABLE_MARKET_FILTER, BLOCK_NEW_BUYS_IN_BEAR, CAUTION_MODE
    except ImportError:
        return True, None

    if not ENABLE_MARKET_FILTER or market_condition is None:
        return True, None

    if BLOCK_NEW_BUYS_IN_BEAR and market_condition == "BEAR":
        return False, "BEAR 장세 필터"

    if market_condition == "CAUTION":
        if CAUTION_MODE == "block_breakout" and signal_type == "BREAKOUT":
            return False, "CAUTION: 돌파 차단"
        elif CAUTION_MODE == "allow_with_flag":
            return True, "⚠️ CAUTION 장세"
        # allow_all: 아무것도 차단하지 않음

    return True, None


# ── 등급 계산 ─────────────────────────────────────────────────────

def _grade(signal: dict) -> str:
    """
    S / A / B 종합 등급.

    점수 기준:
      signal_quality STRONG=3 / MODERATE=2 / WEAK=1
      signal_type    BREAKOUT +1
      base_quality   STRONG   +1
      rs             ≥1.5 +1  / ≥1.0 +0.5
      시장 조건      BULL +1  / BEAR -2

    등급: S(≥6) / A(≥4) / B(나머지)
    """
    qual         = signal.get("signal_quality", "WEAK")
    signal_type  = signal.get("signal_type", "")
    base_quality = signal.get("base_quality", "N/A")
    rs           = signal.get("rs")
    mkt          = signal.get("market_condition", "")

    score = {"STRONG": 3, "MODERATE": 2, "WEAK": 1}.get(qual, 1)

    if signal_type == "BREAKOUT":    score += 1
    if base_quality == "STRONG":     score += 1
    if rs is not None:
        if rs >= 1.5:   score += 1
        elif rs >= 1.0: score += 0.5

    if mkt == "BULL":   score += 1
    elif mkt == "BEAR": score -= 2

    if score >= 6: return "S"
    if score >= 4: return "A"
    return "B"


# ── 메인 스캔 ─────────────────────────────────────────────────────

def run_scan(market: str = "ALL", universe: str = None,
             triggered_by: str = "manual") -> dict:
    if scan_status["is_running"]:
        return {"status": "already_running"}

    scan_status.update(is_running=True, market=market,
                       progress=0, total=0, started_at=datetime.now().isoformat())

    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from database.models import SessionLocal, ScanResult, ScanLog, Holding
    from scanner.weinstein import analyze_stock, check_sell_signal
    from notifications.telegram import send_telegram_message
    from notifications.slack import send_slack_message
    from config import US_UNIVERSE

    # universe 파싱: KR 유니버스(kospi/kosdaq/kospi+kosdaq) vs US 유니버스 구분
    KR_UNIVERSES = {"kospi", "kosdaq", "kospi+kosdaq"}
    if universe and universe.lower() in KR_UNIVERSES:
        kr_universe = universe.lower()
        us_universe = US_UNIVERSE
    else:
        kr_universe = "kospi+kosdaq"
        us_universe = universe if universe else US_UNIVERSE

    db  = SessionLocal()
    log = ScanLog(market=market, triggered_by=triggered_by, status="RUNNING")
    db.add(log); db.commit(); db.refresh(log)

    buy_signals, total_scanned = [], 0
    funnels: dict = {}
    funnel_counts: dict = {}

    try:
        # 시장 지수 상태 로드 (Forest to Trees)
        from scanner.market_analysis import get_market_stages, get_benchmark_close
        market_stages = get_market_stages()
        kr_condition  = market_stages.get("KR_condition")
        us_condition  = market_stages.get("US_condition")

        # 벤치마크 로드 (RS 계산용)
        holding_markets = {
            row[0] for row in db.query(Holding.market).filter(
                Holding.is_active == True,
                Holding.quantity > 0,
            ).distinct().all()
        }
        kr_bench = get_benchmark_close("KR") if market in ("KR", "ALL") or "KR" in holding_markets else None
        us_bench = get_benchmark_close("US") if market in ("US", "ALL") or "US" in holding_markets else None

        if market in ("KR", "ALL"):
            funnels["KR"] = _new_funnel()
            sigs, cnt = _scan_kr(db, kr_bench, kr_condition, kr_universe,
                                 funnel=funnels["KR"])
            funnel_counts["KR"] = cnt
            buy_signals.extend(sigs); total_scanned += cnt

        if market in ("US", "ALL"):
            funnels["US"] = _new_funnel()
            sigs, cnt = _scan_us(db, us_universe, us_bench, us_condition,
                                 funnel=funnels["US"])
            funnel_counts["US"] = cnt
            buy_signals.extend(sigs); total_scanned += cnt

        holding_signals = _check_holdings(
            db,
            kr_bench=kr_bench,
            us_bench=us_bench,
            kr_condition=kr_condition,
            us_condition=us_condition,
        )
        sell_signals = _check_watchlist(db, kr_bench=kr_bench, us_bench=us_bench)

        # Step 4 — 실제 알림 전송 직전에 현재가 기반 값을 다시 계산/저장한다.
        # gate가 켜진 경우 DB에는 남기고 notify 목록에서만 제외한다.
        buy_signals, suppressed = _prepare_alert_candidates(db, buy_signals)
        for signal in suppressed:
            funnel = funnels.get(signal.get("market"))
            if funnel is None:
                continue
            signal_type = signal.get("signal_type") or "UNKNOWN"
            remaining = funnel["signals"].get(signal_type, 0) - 1
            if remaining > 0:
                funnel["signals"][signal_type] = remaining
            else:
                funnel["signals"].pop(signal_type, None)

        for funnel_market, funnel in funnels.items():
            _finalize_funnel(
                funnel_market,
                funnel,
                funnel_counts.get(funnel_market, 0),
            )

        if buy_signals or sell_signals or holding_signals:
            _notify(
                buy_signals,
                sell_signals,
                holding_signals,
                send_telegram_message,
                send_slack_message,
            )

        log.finished_at   = datetime.utcnow()
        log.total_scanned = total_scanned
        log.signals_found = len(buy_signals)
        log.status        = "DONE"
        db.commit()

        return {"status": "done", "total_scanned": total_scanned,
                "signals_found": len(buy_signals),
                "sell_signals": len(sell_signals),
                "holding_signals": len([
                    signal for signal in holding_signals
                    if signal.get("signal_type") == "SELL"
                ]),
                "funnel": funnels}

    except Exception as e:
        logger.error(f"스캔 오류: {e}", exc_info=True)
        log.status    = "ERROR"
        log.error_msg = str(e)
        log.finished_at = datetime.utcnow()
        db.commit()
        return {"status": "error", "message": str(e), "funnel": funnels}

    finally:
        scan_status["is_running"] = False
        db.close()


def _append_entry_warning(signal: dict, message: str) -> None:
    for key in ("entry_warnings", "warning_flags"):
        values = list(signal.get(key) or [])
        if message not in values:
            values.append(message)
        signal[key] = values


def _prepare_alert_candidates(db, signals: list) -> Tuple[list, list]:
    """Recompute current-price risk immediately before notify and apply opt-in gate."""
    from config import ALERT_FRESHNESS_AS_GATE
    from scanner.entry_control import annotate_alert_freshness

    _attach_position_sizing(db, signals)

    eligible, suppressed = [], []
    for signal in signals:
        annotate_alert_freshness(signal)
        _save(db, signal)
        if (ALERT_FRESHNESS_AS_GATE
                and signal.get("_alert_freshness_would_cut") is True):
            signal["_alert_suppressed"] = True
            suppressed.append(signal)
        else:
            eligible.append(signal)
    return eligible, suppressed


def _sizing_config(market: str) -> dict:
    """현재 config에서 시장별 사이징 값을 읽어 순수 함수에 전달한다."""
    from config import market_param

    defaults = {
        "RISK_PCT": 1.0,
        "MAX_POSITION_PCT": 20.0,
        "MAX_TOTAL_HEAT_PCT": 6.0,
        "MIN_R_PCT": 3.0,
        "MAX_R_PCT": 15.0,
    }
    return {name: float(market_param(name, market, default))
            for name, default in defaults.items()}


def _attach_position_sizing(db, signals: list) -> None:
    """알림 후보에 시장별 최신 자산/heat 기준 사이징 스냅샷을 붙인다."""
    from database.models import AccountEquity, Holding
    from scanner.sizing import calculate_open_risk, calculate_position

    holdings = db.query(Holding).filter(
        Holding.is_active == True,
        Holding.quantity > 0,
    ).all()
    contexts = {}
    for market in {str(s.get("market", "")).upper() for s in signals}:
        if market not in {"KR", "US"}:
            continue
        equity = db.query(AccountEquity).filter(
            AccountEquity.market == market,
        ).order_by(
            AccountEquity.recorded_at.desc(),
            AccountEquity.id.desc(),
        ).first()
        contexts[market] = {
            "equity": equity,
            "heat": calculate_open_risk(holdings, market),
            "cfg": _sizing_config(market),
        }

    for signal in signals:
        market = str(signal.get("market", "")).upper()
        context = contexts.get(market)
        equity_row = context.get("equity") if context else None
        heat = context.get("heat") if context else {
            "open_risk_sum": 0.0, "warnings": [],
        }
        signal["_heat_warnings"] = list(heat.get("warnings") or [])

        if equity_row is None or not equity_row.total_equity or equity_row.total_equity <= 0:
            signal["sizing"] = None
            signal["suggested_qty"] = None
            signal["r_per_share"] = None
            signal["risk_amount"] = None
            signal["position_pct"] = None
            signal["sizing_constrained_by"] = None
            signal["equity_snapshot"] = None
            signal["_sizing_status"] = "equity_missing"
            continue

        result = calculate_position(
            entry=signal.get("strict_price"),
            stop=signal.get("stop_loss"),
            equity=equity_row.total_equity,
            cash=equity_row.cash_balance,
            open_risk_sum=heat["open_risk_sum"],
            market=market,
            cfg=context["cfg"],
        )
        signal["sizing"] = result
        signal["suggested_qty"] = result["qty"]
        signal["r_per_share"] = result["r_per_share"]
        signal["risk_amount"] = result["risk_amount"]
        signal["position_pct"] = result["position_pct"]
        signal["sizing_constrained_by"] = result["constrained_by"]
        signal["equity_snapshot"] = float(equity_row.total_equity)
        signal["_sizing_status"] = "calculated"
        signal["_heat_before_pct"] = round(
            heat["open_risk_sum"] / equity_row.total_equity * 100.0, 4)
        signal["_heat_after_pct"] = round(
            (heat["open_risk_sum"] + result["risk_amount"])
            / equity_row.total_equity * 100.0, 4)
        signal["_remaining_heat_pct"] = max(
            0.0,
            context["cfg"]["MAX_TOTAL_HEAT_PCT"] - signal["_heat_before_pct"],
        )


def _sync_upthrust_cooldown(db, signal: dict) -> None:
    """Record confirmed failures and annotate a later signal's active cooldown."""
    signal["_upthrust_cooldown_active"] = False
    if signal.get("signal_type") != "BREAKOUT":
        return

    from database.models import UpthrustCooldown
    from config import UPTHRUST_COOLDOWN_DAYS

    now = datetime.utcnow()
    source_date = signal.get("signal_date") or ""
    active = db.query(UpthrustCooldown).filter(
        UpthrustCooldown.market == signal.get("market"),
        UpthrustCooldown.ticker == signal.get("ticker"),
        UpthrustCooldown.source_signal_date != source_date,
        UpthrustCooldown.expires_at > now,
    ).order_by(UpthrustCooldown.expires_at.desc()).first()
    if active is not None:
        signal["_upthrust_cooldown_active"] = True
        _append_entry_warning(
            signal,
            f"돌파 실패 쿨다운 중 ({active.expires_at.strftime('%Y-%m-%d')}까지)",
        )

    failed_date = signal.get("_upthrust_failed_date")
    if signal.get("upthrust_failed") is not True or not failed_date:
        return
    failed_at = datetime.strptime(failed_date, "%Y-%m-%d")
    expires_at = failed_at + timedelta(days=UPTHRUST_COOLDOWN_DAYS)
    row = db.query(UpthrustCooldown).filter(
        UpthrustCooldown.market == signal.get("market"),
        UpthrustCooldown.ticker == signal.get("ticker"),
        UpthrustCooldown.source_signal_date == source_date,
    ).first()
    if row is None:
        db.add(UpthrustCooldown(
            market=signal.get("market"),
            ticker=signal.get("ticker"),
            source_signal_date=source_date,
            failed_date=failed_date,
            expires_at=expires_at,
        ))
    else:
        # 같은 실패를 재스캔해도 최초 실패일 기반 만료일은 밀리지 않는다.
        row.failed_date = failed_date
        row.expires_at = expires_at
    db.flush()


def _process_signal(db, res: dict, market_label: str,
                    market_condition: Optional[str],
                    benchmark_close) -> bool:
    """analyze_stock 결과를 받아 legacy 시장 필터 + strict 필터 + persist/notify
    분기를 한 곳에서 처리하고, 알림 대상이면 True 를 반환.

    흐름:
      1. legacy ``_get_market_filter_decision`` (CAUTION 표기 + BEAR fast-path).
         BEAR fast-path 는 STRICT_PERSIST_REJECTED 일 때만 strict 평가까지
         넘기고, 그 외 모드에서는 비용 절약 차원에서 즉시 drop.
      2. ``apply_strict_filter`` 평가 → signal dict 에 strict_filter_passed /
         filter_reasons 기록.
      3. STRICT_WEINSTEIN_MODE=True 면 strict-pass 만 _save / notify.
         False 면 legacy 호환 — 모두 _save / notify (단, market filter 로
         이미 차단된 BEAR 시그널은 여전히 drop).
      4. STRICT_PERSIST_REJECTED=True 면 거부 시그널도 _save 하되 notify
         리스트에는 포함하지 않음 (debug-only).
    """
    from config import STRICT_WEINSTEIN_MODE, STRICT_PERSIST_REJECTED
    from scanner.entry_control import annotate_alert_freshness
    from scanner.strict_filter import (
        PIVOT_EXTENSION_TOO_HIGH,
        UPTHRUST_FAILED,
        UPTHRUST_COOLDOWN,
    )

    ticker = res["ticker"]

    # 1) legacy market filter — CAUTION 표시용. BEAR fast-path 는 비용 절약.
    allow, flag = _get_market_filter_decision(market_condition, res["signal_type"])
    if not allow and not STRICT_PERSIST_REJECTED:
        logger.debug(f"[{market_label}] {ticker} legacy market filter: {flag}")
        return False
    if flag:
        res["_market_flag"] = flag

    # Step 4 관측은 gate 토글과 무관하게 항상 수행/저장한다. 현재가 재검증은
    # 이 신호를 알림 큐에 넣을지 결정하기 직전에 수행한다.
    _sync_upthrust_cooldown(db, res)
    annotate_alert_freshness(res)

    # 2) strict 필터 평가 (기존 8게이트 + opt-in entry gate)
    passed, reasons = _evaluate_strict_filter(res, market_condition, benchmark_close)

    # 3) persist/notify 분기
    if passed:
        _save(db, res)
        logger.info(f"[{market_label}] {ticker} {res['name']}: "
                    f"{res['signal_type']} Q={res.get('signal_quality','?')} "
                    f"strict=PASS")
        return True

    entry_reasons = {
        PIVOT_EXTENSION_TOO_HIGH, UPTHRUST_FAILED, UPTHRUST_COOLDOWN,
    }
    entry_rejected = any(r in entry_reasons for r in reasons)

    # Entry gate는 legacy strict-mode 토글과 독립된 명시적 opt-in이다.
    if entry_rejected:
        _save(db, res)
        logger.debug(f"[{market_label}] {ticker} entry-control reject: {reasons}")
        return False

    # 거부 — strict 모드 ON
    if STRICT_WEINSTEIN_MODE:
        if STRICT_PERSIST_REJECTED:
            _save(db, res)
        logger.debug(f"[{market_label}] {ticker} strict reject: {reasons}")
        return False

    # STRICT_WEINSTEIN_MODE=False (legacy 호환) — 통과 처리
    _save(db, res)
    return True


def _scan_kr(db, benchmark_close=None, market_condition=None, kr_universe="kospi+kosdaq",
             funnel=None):
    from scanner.kr_stocks import get_all_kr_tickers, get_kr_ohlcv
    from scanner.weinstein import analyze_stock
    import time

    tickers = get_all_kr_tickers(market_filter=kr_universe)
    signals, count = [], 0

    for i, info in enumerate(tickers):
        _prog(i + 1, len(tickers), f"KR [{i+1}/{len(tickers)}] {info['name']}")
        df = get_kr_ohlcv(info["ticker"])
        if df is None:
            continue
        diag: Optional[dict] = {} if funnel is not None else None
        res = analyze_stock(df, info["ticker"], info["name"], "KR",
                            benchmark_close, market_condition, diag=diag)
        count += 1
        notified = bool(res and _process_signal(db, res, "KR",
                                                market_condition, benchmark_close))
        if notified:
            signals.append(res)
        _funnel_record(funnel, res, diag, notified)
        time.sleep(0.05)

    return signals, count


def _scan_us(db, universe, benchmark_close=None, market_condition=None,
             funnel=None):
    from scanner.us_stocks import get_all_us_tickers, get_us_batch
    from scanner.weinstein import analyze_stock

    tickers = get_all_us_tickers(universe)
    results = get_us_batch(tickers, progress_callback=_prog)
    signals, count = [], 0

    for info, df in results:
        if df is None:
            continue
        diag: Optional[dict] = {} if funnel is not None else None
        res = analyze_stock(df, info["ticker"], info["name"], "US",
                            benchmark_close, market_condition, diag=diag)
        if res is not None:
            # Gate 2 입력. analyze_stock 은 순수 계산이라 유니버스 메타데이터를
            # 모르므로 여기서 실어 준다 (S&P500 목록에만 존재).
            res["_gics_sector"] = info.get("sector")
        count += 1
        notified = bool(res and _process_signal(db, res, "US",
                                                market_condition, benchmark_close))
        if notified:
            signals.append(res)
        _funnel_record(funnel, res, diag, notified)

    return signals, count


def _check_watchlist(db, kr_bench=None, us_bench=None):
    """감시목록 매도 시그널 체크.

    Phase 2: 일봉(df) → 주봉(weekly_df)을 derive 해서 check_sell_signal에 전달.
    벤치마크가 주어지면 Mansfield RS 악화 분기까지 평가. 일봉/주봉/벤치마크
    가운데 어느 하나라도 미확보면 해당 분기는 None 폴백으로 graceful 처리.
    """
    from database.models import WatchList
    from scanner.weinstein import check_sell_signal, to_weekly_ohlcv
    from scanner.kr_stocks import get_kr_ohlcv
    from scanner.us_stocks import get_us_ohlcv

    items = db.query(WatchList).filter(WatchList.is_active == True).all()
    sells = []
    for w in items:
        try:
            df = get_kr_ohlcv(w.ticker) if w.market == "KR" else get_us_ohlcv(w.ticker)
            if df is None:
                continue
            weekly_df = to_weekly_ohlcv(df)
            if weekly_df is None or len(weekly_df) == 0:
                weekly_df = None
            bench = kr_bench if w.market == "KR" else us_bench
            sig = check_sell_signal(df, w.ticker, w.name, w.market,
                                    buy_price=w.buy_price, stop_loss=w.stop_loss,
                                    weekly_df=weekly_df, benchmark_close=bench)
            if sig:
                sells.append(sig)
        except Exception as e:
            logger.error(f"감시목록 체크 오류 {w.ticker}: {e}")
    return sells


_HOLDING_SEVERITY_RANK = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
_MISSING_STOP_REASON = "__MISSING_STOP__"


def _clear_holding_alert_state(holding) -> None:
    holding.last_alert_severity = None
    holding.last_alert_reason = None
    holding.last_alert_at = None


def _holding_alert_due(holding, severity: str, reason: str,
                       now: datetime, repeat_hours: int) -> bool:
    """상태 전이/사유 변경/반복 주기에 따라 이번 알림 대상인지 판정한다."""
    previous_severity = holding.last_alert_severity
    previous_reason = holding.last_alert_reason
    previous_at = holding.last_alert_at

    if previous_severity is None or previous_reason is None or previous_at is None:
        return True
    if _HOLDING_SEVERITY_RANK.get(severity, 0) < _HOLDING_SEVERITY_RANK.get(previous_severity, 0):
        _clear_holding_alert_state(holding)
        return False
    if _HOLDING_SEVERITY_RANK.get(severity, 0) > _HOLDING_SEVERITY_RANK.get(previous_severity, 0):
        return True
    if reason != previous_reason:
        return True
    return now - previous_at >= timedelta(hours=repeat_hours)


def _check_holdings(db, kr_bench=None, us_bench=None,
                    kr_condition=None, us_condition=None):
    """보유 종목 매도 상태를 저장하고, 이번에 발송할 신호 목록을 반환한다."""
    from collections import defaultdict
    from database.models import Holding
    from scanner.weinstein import check_sell_signal, to_weekly_ohlcv
    from scanner.kr_stocks import get_kr_ohlcv
    from scanner.us_stocks import get_us_ohlcv
    from config import MA_PERIOD, HOLDING_ALERT_REPEAT_HOURS

    holdings = db.query(Holding).filter(
        Holding.is_active == True,
        Holding.quantity > 0,
    ).all()
    grouped = defaultdict(list)
    for holding in holdings:
        grouped[(holding.market, holding.ticker)].append(holding)

    signals = []
    missing_stop_rows = []
    status_by_severity = {
        "HIGH": "SELL_REQUIRED",
        "MEDIUM": "REVIEW",
        "LOW": "CAUTION",
    }

    for (market, ticker), rows in grouped.items():
        checked_at = datetime.utcnow()
        try:
            df = get_kr_ohlcv(ticker) if market == "KR" else get_us_ohlcv(ticker)
            if df is None or len(df) < MA_PERIOD + 20:
                raise ValueError("매도 판정에 필요한 시세 데이터가 부족합니다")
            weekly_df = to_weekly_ohlcv(df)
            if weekly_df is None or len(weekly_df) == 0:
                weekly_df = None
            benchmark = kr_bench if market == "KR" else us_bench
            market_condition = kr_condition if market == "KR" else us_condition
            current_price = float(df["Close"].iloc[-1])

            for holding in rows:
                signal = check_sell_signal(
                    df, holding.ticker, holding.name, holding.market,
                    buy_price=holding.entry_price or holding.avg_price,
                    stop_loss=holding.current_stop_loss,
                    weekly_df=weekly_df,
                    benchmark_close=benchmark,
                    market_condition=market_condition,
                )
                if signal:
                    signal.setdefault("signal_type", "SELL")
                    signal.setdefault("ticker", holding.ticker)
                    signal.setdefault("name", holding.name)
                    signal.setdefault("market", holding.market)
                    signal.setdefault("price", current_price)
                status = status_by_severity.get(signal.get("severity"), "CAUTION") if signal else "HOLD"
                holding.current_price = current_price
                holding.price_updated_at = checked_at
                holding.sell_status = status
                holding.sell_severity = signal.get("severity") if signal else None
                holding.sell_reason = signal.get("sell_reason") if signal else None
                holding.sell_checked_at = checked_at

                if holding.current_stop_loss is None:
                    missing_stop_rows.append(holding)

                if signal is None:
                    if holding.last_alert_reason != _MISSING_STOP_REASON:
                        _clear_holding_alert_state(holding)
                    continue

                severity = signal.get("severity") or "LOW"
                reason = signal.get("sell_reason") or "매도 판정"
                if not _holding_alert_due(
                        holding, severity, reason, checked_at,
                        HOLDING_ALERT_REPEAT_HOURS):
                    continue

                entry_price = holding.entry_price or holding.avg_price
                unrealized_r = None
                if (entry_price is not None and holding.initial_r is not None
                        and holding.initial_r > 0):
                    unrealized_r = (current_price - entry_price) / holding.initial_r
                signal.update({
                    "holding_id": holding.id,
                    "is_holding": True,
                    "quantity": holding.quantity,
                    "avg_price": holding.avg_price,
                    "entry_price": entry_price,
                    "initial_stop_loss": holding.initial_stop_loss,
                    "current_stop_loss": holding.current_stop_loss,
                    "initial_r": holding.initial_r,
                    "unrealized_r": (round(unrealized_r, 2)
                                     if unrealized_r is not None else None),
                })
                signals.append(signal)
                holding.last_alert_severity = severity
                holding.last_alert_reason = reason
                holding.last_alert_at = checked_at
        except Exception as exc:
            logger.error(f"보유종목 매도 점검 오류 {market} {ticker}: {exc}")
            for holding in rows:
                holding.sell_status = "CHECK_FAILED"
                holding.sell_severity = None
                holding.sell_reason = "가격 데이터를 확인하지 못했습니다."
                holding.sell_checked_at = checked_at

    # 손절가 미등록은 종목별 알림이 아닌 하나의 정보 섹션으로 묶는다.
    # 매도 신호가 없는 행은 같은 24시간 반복 규칙을 재사용한다.
    missing_due = []
    for holding in missing_stop_rows:
        if holding.sell_severity is not None:
            # 이 행의 실제 매도 알림 상태를 덮어쓰지 않는다.
            continue
        if _holding_alert_due(
                holding, "INFO", _MISSING_STOP_REASON, checked_at,
                HOLDING_ALERT_REPEAT_HOURS):
            missing_due.append(holding)
            holding.last_alert_severity = "INFO"
            holding.last_alert_reason = _MISSING_STOP_REASON
            holding.last_alert_at = checked_at
    if missing_stop_rows and (missing_due or any(
            signal.get("signal_type") == "SELL" for signal in signals)):
        signals.append({
            "signal_type": "HOLDING_STOP_MISSING",
            "holdings": [
                {"ticker": h.ticker, "name": h.name, "market": h.market}
                for h in missing_stop_rows
            ],
        })

    db.flush()
    return signals


def _save(db, signal: dict):
    from database.models import ScanResult
    try:
        existing = db.query(ScanResult).filter(
            ScanResult.ticker      == signal["ticker"],
            ScanResult.signal_date == signal.get("signal_date", ""),
            ScanResult.signal_type == signal["signal_type"],
        ).first()

        grade = _grade(signal)
        signal["_grade"] = grade  # notify에서 재사용

        # filter_reasons 는 list → JSON 문자열로 직렬화 (없으면 None)
        reasons = signal.get("filter_reasons")
        if reasons is None or reasons == []:
            reasons_json = None
        else:
            try:
                reasons_json = json.dumps(reasons)
            except Exception:
                reasons_json = None
        entry_warnings = signal.get("entry_warnings")
        if not entry_warnings:
            entry_warnings_json = None
        else:
            try:
                entry_warnings_json = json.dumps(entry_warnings, ensure_ascii=False)
            except Exception:
                entry_warnings_json = None

        if existing:
            # 최신 가격/품질만 업데이트
            existing.price            = signal["price"]
            existing.ma150            = signal["ma150"]
            existing.volume_ratio     = signal.get("volume_ratio", 0)
            existing.pivot_price      = signal.get("pivot_price")
            existing.support_level    = signal.get("support_level")
            existing.market_condition = signal.get("market_condition")
            existing.signal_quality   = signal.get("signal_quality")
            existing.rs_value         = signal.get("rs_value")
            existing.grade            = grade
            existing.scan_time        = datetime.utcnow()
            # signal-date 당시 스캐너 판정 구간 스냅샷 (차트 API는 재계산 금지)
            existing.base_start_date   = signal.get("base_start_date")
            existing.base_end_date     = signal.get("base_end_date")
            existing.tight_start_date  = signal.get("tight_start_date")
            existing.base_high         = signal.get("base_high")
            existing.base_low          = signal.get("base_range_low", signal.get("base_low"))
            existing.tight_high        = signal.get("tight_high")
            existing.tight_low         = signal.get("tight_low")
            existing.base_width_pct    = signal.get("base_width_pct")
            existing.tight_width_pct   = signal.get("tight_width_pct")
            existing.contraction_ratio = signal.get("contraction_ratio")
            existing.base_mode         = signal.get("base_mode")
            # Step 4 entry-control observations
            existing.pivot_ext_pct     = signal.get("pivot_ext_pct")
            existing.upthrust_failed   = signal.get("upthrust_failed")
            existing.cur_ext_pct       = signal.get("cur_ext_pct")
            existing.cur_stop_pct      = signal.get("cur_stop_pct")
            existing.entry_warnings    = entry_warnings_json
            # Step 5 position-sizing snapshot
            existing.suggested_qty         = signal.get("suggested_qty")
            existing.r_per_share           = signal.get("r_per_share")
            existing.risk_amount           = signal.get("risk_amount")
            existing.position_pct          = signal.get("position_pct")
            existing.sizing_constrained_by = signal.get("sizing_constrained_by")
            existing.equity_snapshot       = signal.get("equity_snapshot")
            # Strict Weinstein filter (Phase 1 scaffold; Phase 4 에서 채워짐)
            existing.stop_loss            = signal.get("stop_loss")
            existing.sector_name          = signal.get("sector_name")
            existing.sector_stage         = signal.get("sector_stage")
            existing.rs_trend             = signal.get("rs_trend")
            existing.rs_zero_crossed      = signal.get("rs_zero_crossed")
            existing.strict_filter_passed = signal.get("strict_filter_passed")
            existing.filter_reasons       = reasons_json
        else:
            db.add(ScanResult(
                scan_time        = datetime.utcnow(),
                market           = signal["market"],
                ticker           = signal["ticker"],
                name             = signal["name"],
                signal_type      = signal["signal_type"],
                stage            = signal.get("stage", "STAGE2"),
                price            = signal["price"],
                ma150            = signal["ma150"],
                volume           = signal.get("volume", 0),
                volume_avg       = signal.get("volume_avg", 0),
                volume_ratio     = signal.get("volume_ratio", 0),
                signal_date      = signal.get("signal_date", ""),
                pivot_price      = signal.get("pivot_price"),
                support_level    = signal.get("support_level"),
                market_condition = signal.get("market_condition"),
                signal_quality   = signal.get("signal_quality"),
                rs_value         = signal.get("rs_value"),
                grade            = grade,
                # Scanner decision snapshot (chart overlay)
                base_start_date   = signal.get("base_start_date"),
                base_end_date     = signal.get("base_end_date"),
                tight_start_date  = signal.get("tight_start_date"),
                base_high         = signal.get("base_high"),
                base_low          = signal.get("base_range_low", signal.get("base_low")),
                tight_high        = signal.get("tight_high"),
                tight_low         = signal.get("tight_low"),
                base_width_pct    = signal.get("base_width_pct"),
                tight_width_pct   = signal.get("tight_width_pct"),
                contraction_ratio = signal.get("contraction_ratio"),
                base_mode         = signal.get("base_mode"),
                # Step 4 entry-control observations
                pivot_ext_pct     = signal.get("pivot_ext_pct"),
                upthrust_failed   = signal.get("upthrust_failed"),
                cur_ext_pct       = signal.get("cur_ext_pct"),
                cur_stop_pct      = signal.get("cur_stop_pct"),
                entry_warnings    = entry_warnings_json,
                # Step 5 position-sizing snapshot
                suggested_qty         = signal.get("suggested_qty"),
                r_per_share           = signal.get("r_per_share"),
                risk_amount           = signal.get("risk_amount"),
                position_pct          = signal.get("position_pct"),
                sizing_constrained_by = signal.get("sizing_constrained_by"),
                equity_snapshot       = signal.get("equity_snapshot"),
                # Strict Weinstein filter (Phase 1 scaffold; Phase 4 에서 채워짐)
                stop_loss            = signal.get("stop_loss"),
                sector_name          = signal.get("sector_name"),
                sector_stage         = signal.get("sector_stage"),
                rs_trend             = signal.get("rs_trend"),
                rs_zero_crossed      = signal.get("rs_zero_crossed"),
                strict_filter_passed = signal.get("strict_filter_passed"),
                filter_reasons       = reasons_json,
            ))
        db.commit()
    except Exception as e:
        logger.error(f"저장 오류: {e}")
        db.rollback()


def _sector_summary(market: str) -> str:
    """강세/약세 섹터 한 줄 요약 (실패 시 빈 문자열)."""
    try:
        from scanner.market_analysis import get_market_stages
        stages = get_market_stages()
        key    = "US_SECTORS" if market == "US" else "KR_SECTORS"
        etfs   = stages.get(key, [])
        if not etfs:
            return ""
        # Gate 2 와 같은 주봉 기준을 쓴다 — 알림 요약과 게이트가 서로 다른
        # Stage 를 말하지 않도록. 산출 실패분(None)은 자연히 빠진다.
        bull = [e["name"] for e in etfs if e.get("stage_weekly") == "STAGE2"]
        bear = [e["name"] for e in etfs if e.get("stage_weekly") == "STAGE4"]
        parts = []
        if bull: parts.append(f"강세: {', '.join(bull[:3])}")
        if bear: parts.append(f"약세: {', '.join(bear[:3])}")
        return "📊 " + " | ".join(parts) if parts else ""
    except Exception:
        return ""


def _format_currency(value, currency: str) -> str:
    """알림용 통화 포맷. 시장 통화 사이의 환산은 수행하지 않는다."""
    if value is None:
        return "-"
    if currency == "KRW":
        return f"{float(value):,.0f}원"
    return f"${float(value):,.2f}"


def _sizing_notification_lines(signal: dict) -> list[str]:
    """한 신호의 포지션 사이징/heat 안내 문구를 만든다."""
    sizing = signal.get("sizing")
    if signal.get("_sizing_status") == "equity_missing" or sizing is None:
        lines = ["  ℹ️ 자산 미등록 — 설정에서 계좌 자산을 입력하면 권장수량이 표시됩니다"]
        lines.extend(f"  ⚠️ {warning}" for warning in signal.get("_heat_warnings") or [])
        return lines

    currency = sizing["currency"]
    qty = sizing["qty"]
    lines = []
    if qty > 0:
        lines.append(
            f"  • 권장수량 {qty:,}주  {_format_currency(sizing['position_value'], currency)} "
            f"(자산의 {sizing['position_pct']:.1f}%)")
        lines.append(
            f"  • 리스크 {_format_currency(sizing['risk_amount'], currency)} "
            f"({sizing['risk_pct_actual']:.1f}%)  |  현재 heat "
            f"{signal.get('_heat_before_pct', 0):.1f}% → "
            f"{signal.get('_heat_after_pct', 0):.1f}%")
    else:
        reason_labels = {
            "r_pct_below_min": "손절폭이 최소 R 범위보다 좁음",
            "r_pct_above_max": "손절폭이 최대 R 범위를 초과",
            "stop_must_be_below_entry": "손절가가 진입가보다 낮지 않음",
            "risk_budget_too_small": "리스크 예산으로 1주도 매수 불가",
            "position_cap_too_small": "비중 상한으로 1주도 매수 불가",
            "heat_limit_exceeded": "총 heat 한도 초과",
            "insufficient_cash": "현금 부족",
            "invalid_numeric_input": "진입가 또는 손절가 없음",
        }
        reasons = [reason_labels.get(r, r) for r in sizing.get("reasons") or []]
        lines.append(f"  ⚠️ 권장수량 산출 불가 — {', '.join(reasons) or '입력값 확인 필요'}")

    constrained = sizing.get("constrained_by")
    if qty > 0 and constrained == "position_cap":
        lines.append(
            f"  ⚠️ 비중 상한으로 축소 (계산 {sizing['calculated_qty']:,}주 → {qty:,}주)")
    elif qty > 0 and constrained == "heat":
        lines.append(
            f"  ⚠️ heat 한도 잔여 {signal.get('_remaining_heat_pct', 0):.1f}% — 리스크 축소 적용")
    elif qty > 0 and constrained == "cash":
        lines.append(f"  ⚠️ 현금 부족 — 최대 {qty:,}주까지 가능")

    for warning in signal.get("_heat_warnings") or []:
        lines.append(f"  ⚠️ {warning}")
    return lines


def _holding_price(value, market: str) -> str:
    if value is None:
        return "-"
    return f"{value:,.0f}원" if market == "KR" else f"${value:,.2f}"


def _holding_recommendation(signal: dict) -> str:
    quantity = int(signal.get("quantity") or 0)
    severity = signal.get("severity")
    reason = signal.get("sell_reason") or ""
    if severity == "HIGH":
        return f"전량 청산 {quantity:,}주"
    if "Stage3" in reason:
        return f"1/2 축소 검토 (약 {int(quantity / 2):,}주)"
    if "Mansfield" in reason:
        return f"1/3 축소 검토 (약 {int(quantity / 3):,}주)"
    if "BEAR" in reason:
        return "신규 중단 + 약체 축소 검토"
    return "관찰"


def _format_holding_alerts(holding_signals: list) -> str:
    actual = [s for s in holding_signals if s.get("signal_type") == "SELL"]
    missing = next((s for s in holding_signals
                    if s.get("signal_type") == "HOLDING_STOP_MISSING"), None)
    lines = [f"💼 *보유 종목 매도 알림*",
             f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} KST", ""]
    severity_icon = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}
    severity_title = {"HIGH": "매도 신호", "MEDIUM": "매도 검토", "LOW": "매도 관찰"}
    for signal in actual:
        market = signal.get("market") or "US"
        severity = signal.get("severity") or "LOW"
        icon = severity_icon.get(severity, "🔵")
        title = severity_title.get(severity, "매도 관찰")
        quantity = int(signal.get("quantity") or 0)
        avg_price = signal.get("avg_price")
        current = signal.get("price")
        profit_pct = signal.get("profit_pct")
        profit_text = f"{profit_pct:+.1f}%" if profit_pct is not None else "-"
        lines.extend([
            f"{icon} *{title}*  {signal.get('ticker')} ({signal.get('name')})   [보유]",
            (f"보유 {quantity:,}주  평균 {_holding_price(avg_price, market)}  "
             f"현재 {_holding_price(current, market)} ({profit_text})"),
        ])
        unrealized_r = signal.get("unrealized_r")
        if unrealized_r is not None:
            risk_line = f"미실현 {unrealized_r:+.2f}R"
            entry = signal.get("entry_price")
            stop = signal.get("current_stop_loss")
            if entry is not None and stop is not None:
                risk_line += (f"  |  진입 {_holding_price(entry, market)} / "
                              f"손절 {_holding_price(stop, market)}")
            lines.append(risk_line)
        lines.extend([
            "",
            f"⚠️ {severity} — {signal.get('sell_reason')}",
            f"권장: {_holding_recommendation(signal)}",
            "",
        ])

    if missing:
        rows = missing.get("holdings") or []
        labels = [f"{row.get('ticker')} {row.get('name')}".strip() for row in rows]
        lines.extend([
            f"ℹ️ 손절가 미등록 {len(rows)}건 — 설정에서 입력하면 손절 도달 알림을 받을 수 있습니다",
            "   " + " / ".join(labels),
        ])
    return "\n".join(lines).rstrip()


def _notify(buys, sells, holding_signals_or_send=None, send_fn=None,
            slack_send_fn=None):
    """매수 시그널은 Telegram/Slack, 매도 시그널은 Telegram으로 전송.

    Phase 4 invariant: ``buys`` 는 *strict-pass* 만 들어오므로 본 함수는
    별도의 strict 거부 분기 없이 순수 포맷팅만 담당. 거부 시그널의
    DB persistence 는 ``_process_signal`` 에서 STRICT_PERSIST_REJECTED 토글
    하에 처리된다.

    ``STRICT_NOTIFY_INCLUDE_REASONS=True`` 토글 시 strict 결과 메타(통과 표시
    + 비어있지 않은 reason 리스트) 가 알림에 추가된다. 기본 False — 메시지
    길이/노이즈 방지.
    """
    # 기존 호출 `_notify(buys, sells, telegram, slack)`도 그대로 지원.
    if callable(holding_signals_or_send):
        legacy_send = holding_signals_or_send
        legacy_slack = send_fn
        holding_signals = []
        send_fn = legacy_send
        slack_send_fn = legacy_slack
    else:
        holding_signals = holding_signals_or_send or []

    try:
        from config import STRICT_NOTIFY_INCLUDE_REASONS, STRICT_WEINSTEIN_MODE
    except ImportError:
        STRICT_NOTIFY_INCLUDE_REASONS = False
        STRICT_WEINSTEIN_MODE         = False

    if buys:
        kr  = [s for s in buys if s["market"] == "KR"]
        us  = [s for s in buys if s["market"] == "US"]
        msg = (f"📈 *Weinstein Stage2 매수 시그널*\n"
               f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} KST\n\n")

        grade_icon   = {"S": "🔥", "A": "✅", "B": "📌"}
        signal_icon  = {"BREAKOUT": "🚀", "RE_BREAKOUT": "🔁", "REBOUND": "🔄"}

        for mkt_list, flag in ((kr, "🇰🇷"), (us, "🇺🇸")):
            if not mkt_list:
                continue
            mkt_name = "한국" if flag == "🇰🇷" else "미국"
            sector   = _sector_summary("KR" if flag == "🇰🇷" else "US")
            msg += f"{flag} *{mkt_name} 주식*"
            if sector:
                msg += f"\n{sector}"
            msg += "\n"

            for s in mkt_list[:10]:
                ico   = signal_icon.get(s["signal_type"], "🔹")
                g     = s.get("_grade", "B")
                gbadge = grade_icon.get(g, "📌")
                p     = (f"{s['price']:,.0f}원" if s["market"] == "KR"
                         else f"${s['price']:.2f}")
                flag_warn = f" _{s.get('_market_flag', '')}_" if s.get("_market_flag") else ""
                bq    = s.get("base_quality", "")
                bq_str = f" | 베이스 {bq}" if bq and bq not in ("N/A", "NONE") else ""
                # Strict 결과 메타 (opt-in)
                strict_str = ""
                if STRICT_NOTIFY_INCLUDE_REASONS:
                    if s.get("strict_filter_passed") is True:
                        strict_str = " | 🛡 strict-pass"
                    reasons = s.get("filter_reasons") or []
                    if reasons:
                        # 통상 strict-pass 는 reasons=[] 이지만 legacy 모드/
                        # debug 경로에서 들어올 수 있어 표기 — 최대 3개.
                        joined = ", ".join(reasons[:3])
                        more = "" if len(reasons) <= 3 else f" +{len(reasons)-3}"
                        strict_str += f" | reasons={joined}{more}"
                msg += (f"{ico}{gbadge}[{g}] *{s['name']}* ({s['ticker']})\n"
                        f"  • {s['signal_type']} | {p} | 거래량 {s['volume_ratio']:.1f}x"
                        f"{bq_str}{flag_warn}{strict_str}\n")
                if s.get("signal_type") == "BREAKOUT":
                    def _price(value):
                        if value is None:
                            return "-"
                        return (f"{value:,.0f}원" if s["market"] == "KR"
                                else f"${value:.2f}")

                    entry = s.get("strict_price")
                    stop = s.get("stop_loss")
                    signal_stop_pct = None
                    if entry is not None and entry > 0 and stop is not None:
                        signal_stop_pct = (entry - stop) / entry * 100.0
                    # signal_stop_pct 를 못 구하면(진입가 없음 등) "(--)" 같은
                    # 깨진 표기 대신 손절 정보 자체를 생략한다.
                    stop_clause = (f"  손절 {_price(stop)} (-{signal_stop_pct:.1f}%)"
                                  if signal_stop_pct is not None else "")
                    cur_ext = s.get("cur_ext_pct")
                    cur_stop = s.get("cur_stop_pct")
                    cur_ext_text = (f"{cur_ext:+.1f}%" if cur_ext is not None else "-")
                    cur_stop_text = (f"{cur_stop:.1f}%" if cur_stop is not None else "-")
                    msg += (f"  • 신호일 {s['signal_date'][5:]}  진입 {_price(entry)}{stop_clause}\n"
                            f"  • 현재가 {_price(s.get('price'))}  피벗 대비 {cur_ext_text}  "
                            f"현재 기준 손절폭 {cur_stop_text}\n")
                    for warning in s.get("entry_warnings") or []:
                        msg += f"  ⚠️ {warning}\n"
                    for sizing_line in _sizing_notification_lines(s):
                        msg += sizing_line + "\n"
                    msg += "\n"
                else:
                    msg += f"  • 시그널일: {s['signal_date']}\n"
                    for sizing_line in _sizing_notification_lines(s):
                        msg += sizing_line + "\n"
                    msg += "\n"
            if len(mkt_list) > 10:
                msg += f"  ... 외 {len(mkt_list) - 10}개\n\n"
        # Telegram legacy Markdown treats the underscore in RE_BREAKOUT as an
        # emphasis delimiter and rejects the whole message when it is left
        # unmatched.  Slack accepts the raw label, so escape it only for the
        # Telegram copy.
        telegram_msg = msg.replace("RE_BREAKOUT", r"RE\_BREAKOUT")
        _safe_send(send_fn, telegram_msg, "Telegram")
        _safe_send(slack_send_fn, msg, "Slack")

    if sells:
        severity_icon = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}
        msg = (f"⚠️ *포트폴리오 매도 알림*\n"
               f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M')} KST\n\n")
        for s in sells:
            pl  = f"{s['profit_pct']:+.1f}%" if s.get("profit_pct") is not None else "N/A"
            sev = severity_icon.get(s.get("severity", ""), "🔴")
            msg += (f"{sev} *{s['name']}* ({s['ticker']})\n"
                    f"  • {s['sell_reason']}\n"
                    f"  • 현재가: {s['price']:,.4g} | 수익률: {pl}\n\n")
        _safe_send(send_fn, msg, "Telegram")

    if holding_signals:
        _safe_send(send_fn, _format_holding_alerts(holding_signals), "Telegram")


def _safe_send(send_fn, message: str, channel: str) -> None:
    """한 알림 채널의 실패가 다른 채널이나 스캔을 중단하지 않게 한다."""
    if send_fn is None:
        return
    try:
        send_fn(message)
    except Exception as exc:
        logger.error("%s 알림 오류: %s", channel, exc)
