"""Strict Weinstein optimal-buy filter — gate decision functions.

본 모듈은 외부 데이터 fetch 를 일절 하지 않는다. 사전에 계산된 signal dict
+ ctx dict 만 입력으로 받아 거부 사유 리스트(reason enum 문자열) 를
in-place 로 채우거나 ``apply_strict_filter`` 가 ``(passed, reasons)`` 를
반환한다.

이 분리는 weinstein.py 의 *순수 계산 → DB / 알림 결정* 경로를 깨끗하게
유지하기 위함이다 (CLAUDE.md "Strategy logic should be testable with
synthetic pandas data without calling external data sources").

## Phasing

* **Phase 2**: ``_check_rs`` + RS reason 상수.
* **Phase 3 (현재)**: 나머지 7개 게이트(``_check_market``,
  ``_check_sector``, ``_check_weekly_stage``, ``_check_base``,
  ``_check_volume``, ``_check_extension``, ``_check_stop_loss``) 와
  엔트리포인트 ``apply_strict_filter`` 추가.
* **Phase 4**: scan_engine.py 가 ``apply_strict_filter`` 를 호출해
  ``signal["strict_filter_passed"]`` / ``signal["filter_reasons"]`` 채움
  + STRICT_PERSIST_REJECTED 분기 + notify 가드.

Phase 3 머지 시점에는 아직 ``apply_strict_filter`` 의 호출자가 없어
스캐너 동작에 영향 없음 (no-op).

## Reason 상수 안정성

DB(``scan_results.filter_reasons`` JSON) 와 모니터링 대시보드가 이 enum
문자열에 의존하므로, **상수 값을 바꾸면 changelog 의무**. 새 enum 값을
추가하는 것은 안전하지만, 기존 값을 변경/삭제하면 forward-compat 가
깨진다.

## signal dict 입력 규약 (no-look-ahead)

각 게이트는 두 부류의 키를 구분해 소비한다.

* **공개 필드** (``price``/``ma150``/``sma30w``/``volume``/``stage`` 등)
  은 last-bar 기준이라 stale 신호에서 신호일 *이후* 데이터를 본 결과다.
  strict gate 는 이 필드를 직접 읽지 **않는다**.
* **``strict_*`` 스냅샷** (``strict_price``/``strict_ma150``/
  ``strict_sma30w``/``strict_slope30w``/``strict_weekly_stage``/
  ``strict_weekly_volume_ratio`` 등) 은 weinstein.analyze_stock 가
  ``signal_date`` 까지 슬라이스한 indicator 로 계산. **Gate 3/5/7/8 입력은
  반드시 strict_* 만 사용**.

이 분리 덕분에 알림/UI/DB persistence 는 last-bar "현재가" 의미를 유지
하면서, strict 평가는 신호 시점 진실값으로 결정성을 보장한다.
"""
from typing import Any, Dict, List, Tuple

from config import (
    BASE_MIN_WEEKS,
    BREAKOUT_DAILY_VOL_RATIO,
    BREAKOUT_WEEKLY_VOL_RATIO,
    BREAKOUT_MAX_EXTENDED_PCT,
    STRICT_WEINSTEIN_MODE,
    STRICT_REQUIRE_MARKET_CONFIRMATION,
    STRICT_BLOCK_CAUTION_BREAKOUTS,
    STRICT_REQUIRE_SECTOR_STAGE2,
    STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA,
    STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA,
    STRICT_REQUIRE_BREAKOUT_VOLUME,
    STRICT_REQUIRE_RS_POSITIVE,
    STRICT_REQUIRE_RS_RISING,
    STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT,
    STRICT_REQUIRE_STOP_LOSS,
)


# ── Reason enum 상수 ───────────────────────────────────────────────
# Gate 1 — Market
MARKET_BEAR              = "market_bear"
MARKET_UNKNOWN           = "market_unknown"
MARKET_CAUTION_BREAKOUT  = "market_caution_breakout"

# Gate 2 — Sector (스텁; sector 매핑은 후속 plan)
SECTOR_STAGE4            = "sector_stage4"
SECTOR_NOT_STAGE2        = "sector_not_stage2"

# Gate 3 — Stock weekly stage
WEEKLY_DATA_MISSING        = "weekly_data_missing"
BELOW_WEEKLY_30MA          = "below_weekly_30ma"
BELOW_DAILY_150MA          = "below_daily_150ma"
STAGE_STAGE3               = "stage_stage3"
STAGE_STAGE4               = "stage_stage4"
WEEKLY_30MA_SLOPE_NEGATIVE = "weekly_30ma_slope_negative"

# Gate 4 — Base / Pivot
BASE_INSUFFICIENT          = "base_insufficient"
BASE_TOO_WIDE              = "base_too_wide"
REBOUND_NO_RETEST          = "rebound_no_retest"

# Gate 5 — Volume
BREAKOUT_DAILY_VOLUME      = "breakout_daily_volume"
BREAKOUT_WEEKLY_VOLUME     = "breakout_weekly_volume"

# Gate 6 — Mansfield Relative Strength (Phase 2)
RS_BELOW_ZERO              = "rs_below_zero"
RS_FALLING                 = "rs_falling"
RS_BENCHMARK_MISSING       = "rs_benchmark_missing"
RS_NO_ZERO_CROSS           = "rs_no_zero_cross"

# Gate 7 — Extension (over-extended above MA150 / 30W)
EXTENDED_ABOVE_MA150       = "extended_above_ma150"
EXTENDED_ABOVE_30W         = "extended_above_30w"

# Gate 8 — Stop-loss
STOP_LOSS_MISSING          = "stop_loss_missing"
STOP_LOSS_ABOVE_PRICE      = "stop_loss_above_price"


# ── Gate 1 — Market ────────────────────────────────────────────────
def _check_market(signal: Dict[str, Any],
                  ctx: Dict[str, Any],
                  reasons: List[str]) -> None:
    """Gate 1 — Market.

    BEAR 시장은 무조건 fail. UNKNOWN 은 ``STRICT_REQUIRE_MARKET_CONFIRMATION``
    토글, CAUTION 은 ``STRICT_BLOCK_CAUTION_BREAKOUTS`` 토글에 따라
    BREAKOUT/RE_BREAKOUT 만 fail.

    Args:
        signal: analyze_stock 결과 dict. 필요 키: signal_type.
        ctx:    상위 컨텍스트. 필요 키: market_condition (str | None).
        reasons: in-place fail 사유 append.
    """
    market = ctx.get("market_condition")
    sig_type = signal.get("signal_type")

    if market == "BEAR":
        reasons.append(MARKET_BEAR)
        return

    if market in (None, "UNKNOWN"):
        if STRICT_REQUIRE_MARKET_CONFIRMATION:
            reasons.append(MARKET_UNKNOWN)
        return

    if market == "CAUTION":
        if (STRICT_BLOCK_CAUTION_BREAKOUTS
                and sig_type in ("BREAKOUT", "RE_BREAKOUT")):
            reasons.append(MARKET_CAUTION_BREAKOUT)


# ── Gate 2 — Sector (stub) ─────────────────────────────────────────
def _check_sector(signal: Dict[str, Any],
                  ctx: Dict[str, Any],
                  reasons: List[str]) -> None:
    """Gate 2 — Sector (stub).

    종목당 sector 매핑은 별도 plan(``strict-weinstein-sector-mapping.md``)
    으로 분리되어 본 plan 범위에서는 ``ctx.sector_stage`` 가 항상 None.
    ``STRICT_REQUIRE_SECTOR_STAGE2`` 기본 False 라 noop. 매핑 구현 후
    True 로 토글하면 활성화된다.

    Args:
        ctx: 필요 키: sector_stage (str | None).
    """
    if not STRICT_REQUIRE_SECTOR_STAGE2:
        return
    sector_stage = ctx.get("sector_stage")
    if sector_stage == "STAGE4":
        reasons.append(SECTOR_STAGE4)
    elif sector_stage != "STAGE2":
        # STAGE1 / STAGE3 / UNKNOWN / None 모두 fail 로 통일
        reasons.append(SECTOR_NOT_STAGE2)


# ── Gate 3 — Stock weekly stage ────────────────────────────────────
def _check_weekly_stage(signal: Dict[str, Any],
                        reasons: List[str]) -> None:
    """Gate 3 — Stock weekly stage.

    주봉 30MA 위 상승 진행(STAGE2 + slope>0) + (BREAKOUT 만)일봉 MA150 위.

    **모든 입력은 signal-date 스냅샷 필드(``strict_*``) 사용.** 공개 필드
    ``price``/``ma150``/``sma30w``/``weekly_stage``/``slope30w`` 는 last-bar
    기준이라 stale 신호에서 look-ahead 됨.

    검사 항목:
      1. 주봉 데이터 자체 없음                      → "weekly_data_missing"
      2. STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA
         strict_price < strict_sma30w                → "below_weekly_30ma"
      3. BREAKOUT + STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA
         strict_price < strict_ma150                 → "below_daily_150ma"
      4. strict_weekly_stage in {STAGE3, STAGE4}     → "stage_stage3" / "stage_stage4"
      5. strict_weekly_stage == STAGE2 + strict_slope30w <= 0
                                                    → "weekly_30ma_slope_negative"

    Args:
        signal: 필요 키: signal_type, strict_price, strict_ma150,
                strict_sma30w, strict_slope30w, strict_weekly_stage.
    """
    sma30w = signal.get("strict_sma30w")
    if sma30w is None:
        # 주봉 시리즈 자체 없음 — 후속 비교 의미 없음, 이 사유만 기록.
        reasons.append(WEEKLY_DATA_MISSING)
        return

    price = signal.get("strict_price")

    # 2) 주봉 30MA 아래
    if (STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA
            and price is not None and price < sma30w):
        reasons.append(BELOW_WEEKLY_30MA)

    # 3) BREAKOUT 한정 — 일봉 MA150 아래
    sig_type = signal.get("signal_type")
    if (sig_type == "BREAKOUT"
            and STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA):
        ma150 = signal.get("strict_ma150")
        if ma150 is not None and price is not None and price < ma150:
            reasons.append(BELOW_DAILY_150MA)

    # 4) Stage 3/4
    weekly_stage = signal.get("strict_weekly_stage")
    if weekly_stage == "STAGE3":
        reasons.append(STAGE_STAGE3)
    elif weekly_stage == "STAGE4":
        reasons.append(STAGE_STAGE4)

    # 5) STAGE2 인데 30W slope 음수 → 진짜 상승 아님
    if weekly_stage == "STAGE2":
        slope = signal.get("strict_slope30w")
        if slope is not None and slope <= 0:
            reasons.append(WEEKLY_30MA_SLOPE_NEGATIVE)


# ── Gate 4 — Base / Pivot ──────────────────────────────────────────
def _check_base(signal: Dict[str, Any],
                reasons: List[str]) -> None:
    """Gate 4 — Base / Pivot.

    BREAKOUT 은 detect_stage2_breakout 가 이미 hard-block 하지만
    sanity 차원 재검증. REBOUND 는 v4 retest gate 통과 여부 확인.

    검사 항목 (BREAKOUT):
      - pivot_price is None or base_weeks < BASE_MIN_WEEKS → "base_insufficient"
      - base_quality_v4 == "WIDE"                          → "base_too_wide"
    검사 항목 (REBOUND):
      - v4_gate not in {BASE_RETEST, 30W_RETEST}           → "rebound_no_retest"

    Args:
        signal: 필요 키: signal_type, pivot_price, base_weeks,
                base_quality_v4, v4_gate.
    """
    sig_type = signal.get("signal_type")

    if sig_type == "BREAKOUT":
        pivot      = signal.get("pivot_price")
        base_weeks = signal.get("base_weeks")
        if pivot is None or base_weeks is None or base_weeks < BASE_MIN_WEEKS:
            reasons.append(BASE_INSUFFICIENT)
        if signal.get("base_quality_v4") == "WIDE":
            reasons.append(BASE_TOO_WIDE)

    elif sig_type == "REBOUND":
        if signal.get("v4_gate") not in ("BASE_RETEST", "30W_RETEST"):
            reasons.append(REBOUND_NO_RETEST)


# ── Gate 5 — Volume ────────────────────────────────────────────────
def _check_volume(signal: Dict[str, Any],
                  reasons: List[str]) -> None:
    """Gate 5 — Breakout volume.

    BREAKOUT 한정 — 일봉 ≥ ``BREAKOUT_DAILY_VOL_RATIO``,
    주봉 ≥ ``BREAKOUT_WEEKLY_VOL_RATIO``. 주봉 비율이 None(데이터 부족)
    이면 차단하지 않음 (Gate 3 의 weekly_data_missing 으로 흡수).

    ``volume_ratio`` 는 detect_* 가 신호 시점 비율을 반환하므로 그대로 사용.
    ``weekly_volume_ratio`` 는 last-bar 의 공개 필드 vs signal-date 스냅샷이
    다르므로 ``strict_weekly_volume_ratio`` 사용.

    Args:
        signal: 필요 키: signal_type, volume_ratio (sig 값),
                strict_weekly_volume_ratio.
    """
    if not STRICT_REQUIRE_BREAKOUT_VOLUME:
        return
    if signal.get("signal_type") != "BREAKOUT":
        return

    vol_ratio = signal.get("volume_ratio")
    if vol_ratio is not None and vol_ratio < BREAKOUT_DAILY_VOL_RATIO:
        reasons.append(BREAKOUT_DAILY_VOLUME)

    wvr = signal.get("strict_weekly_volume_ratio")
    if wvr is not None and wvr < BREAKOUT_WEEKLY_VOL_RATIO:
        reasons.append(BREAKOUT_WEEKLY_VOLUME)


# ── Gate 6 — Mansfield Relative Strength (Phase 2) ─────────────────
def _check_rs(signal: Dict[str, Any],
              ctx: Dict[str, Any],
              reasons: List[str]) -> None:
    """Gate 6 — Mansfield Relative Strength.

    검사 항목:
      1. STRICT_REQUIRE_RS_POSITIVE
         - benchmark 자체 없음 또는 RS 산출 실패 → "rs_benchmark_missing"
         - rs_value < 0                          → "rs_below_zero"
      2. STRICT_REQUIRE_RS_RISING
         - rs_trend == "FALLING"                 → "rs_falling"
      3. STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT (BREAKOUT 만)
         - rs_zero_crossed != True               → "rs_no_zero_cross"

    Args:
        signal: analyze_stock 결과 dict. 필요 키:
                signal_type, rs_value, rs_trend, rs_zero_crossed.
        ctx:    상위 컨텍스트. 필요 키: benchmark_present (bool).
        reasons: in-place 로 fail 사유를 append 받을 리스트.
    """
    sig_type = signal.get("signal_type")

    # 1) RS 양수 강제
    if STRICT_REQUIRE_RS_POSITIVE:
        if not ctx.get("benchmark_present"):
            reasons.append(RS_BENCHMARK_MISSING)
        else:
            rs_value = signal.get("rs_value")
            if rs_value is None:
                # 벤치마크는 있지만 RS 산출 실패 (데이터 부족 등) — 동일 사유로 통일
                reasons.append(RS_BENCHMARK_MISSING)
            elif rs_value < 0.0:
                reasons.append(RS_BELOW_ZERO)

    # 2) RS 상승 추세 강제
    if STRICT_REQUIRE_RS_RISING:
        if signal.get("rs_trend") == "FALLING":
            reasons.append(RS_FALLING)

    # 3) BREAKOUT 한정 — 최근 RS 0선 음→양 전환
    if (STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT
            and sig_type == "BREAKOUT"):
        # rs_zero_crossed 가 None(미산출) 이거나 False 이면 fail
        if not signal.get("rs_zero_crossed"):
            reasons.append(RS_NO_ZERO_CROSS)


# ── Gate 7 — Extension ─────────────────────────────────────────────
# BREAKOUT 시 30주 SMA 대비 과대 연장 임계 (CLAUDE.md 명시 30%)
_EXT_30W_LIMIT_PCT = 30.0


def _check_extension(signal: Dict[str, Any],
                     reasons: List[str]) -> None:
    """Gate 7 — Extension.

    **모든 입력은 signal-date 스냅샷 필드(``strict_*``) 사용.** 공개
    ``price``/``ma150``/``sma30w`` 는 last-bar 라 stale 신호의 extension
    판단이 신호 *이후* 가격 변동에 오염됨.

    검사 항목:
      - (strict_price - strict_ma150) / strict_ma150 * 100 > BREAKOUT_MAX_EXTENDED_PCT
                                                 → "extended_above_ma150"
      - BREAKOUT 만,
        (strict_price - strict_sma30w) / strict_sma30w * 100 > 30%
                                                 → "extended_above_30w"

    Args:
        signal: 필요 키: signal_type, strict_price, strict_ma150, strict_sma30w.
    """
    price = signal.get("strict_price")

    # MA150 대비 — 모든 signal_type 공통
    ma150 = signal.get("strict_ma150")
    if (price is not None and ma150 is not None and ma150 > 0):
        ext = (price - ma150) / ma150 * 100.0
        if ext > BREAKOUT_MAX_EXTENDED_PCT:
            reasons.append(EXTENDED_ABOVE_MA150)

    # 30W SMA 대비 — BREAKOUT 만
    if signal.get("signal_type") == "BREAKOUT":
        sma30w = signal.get("strict_sma30w")
        if (price is not None and sma30w is not None and sma30w > 0):
            ext_w = (price - sma30w) / sma30w * 100.0
            if ext_w > _EXT_30W_LIMIT_PCT:
                reasons.append(EXTENDED_ABOVE_30W)


# ── Gate 8 — Stop-loss ─────────────────────────────────────────────
def _check_stop_loss(signal: Dict[str, Any],
                     reasons: List[str]) -> None:
    """Gate 8 — Stop-loss.

    검사 항목:
      - stop_loss is None + STRICT_REQUIRE_STOP_LOSS → "stop_loss_missing"
      - stop_loss >= strict_price (sanity, 항상 활성) → "stop_loss_above_price"

    sanity 검사는 STRICT_REQUIRE_STOP_LOSS 와 무관하게 항상 작동 — 잘못
    계산된 stop 으로 매수 진입은 절대 허용 안 됨. ``stop_loss`` 는 Phase 2
    에서 signal-date close 기준으로 산출되므로, 비교 대상도 signal-date
    종가인 ``strict_price`` 여야 일관됨 (last-bar 공개 ``price`` 비교 시
    가격 급변 케이스에서 sanity 가 거짓 음성/양성을 낼 수 있음).

    Args:
        signal: 필요 키: stop_loss, strict_price.
    """
    stop  = signal.get("stop_loss")
    price = signal.get("strict_price")

    if stop is None:
        if STRICT_REQUIRE_STOP_LOSS:
            reasons.append(STOP_LOSS_MISSING)
        return

    # sanity — stop_loss 가 signal 시점 종가 이상이면 의미 없음
    if price is not None and stop >= price:
        reasons.append(STOP_LOSS_ABOVE_PRICE)


# ── Entry-point ────────────────────────────────────────────────────
def apply_strict_filter(signal: Dict[str, Any],
                        ctx: Dict[str, Any]
                        ) -> Tuple[bool, List[str]]:
    """Strict Weinstein optimal-buy 필터 — 8 게이트 평가.

    ``STRICT_WEINSTEIN_MODE=False`` 면 모든 게이트를 우회하고
    ``(True, [])`` 반환 (legacy 호환). True 면 8 게이트를 모두 평가하고
    실패 사유 누적된 ``(passed, reasons)`` 반환.

    게이트 호출 순서는 의미적으로 거시→미시 (Market → Sector → Stock
    Stage → Base → Volume → RS → Extension → Stop-loss). 각 게이트는
    독립적으로 실패 사유를 append 하므로 여러 게이트 동시 실패 시
    *모든* 사유가 누적된다 (early-exit 안 함). 이는 디버깅 / 운영 모니터링
    측면에서 유용.

    Args:
        signal: analyze_stock 결과 dict.
        ctx:    상위 컨텍스트. 필요 키:
                  - market_condition  (str | None)
                  - sector_stage      (str | None)  — Phase 5 까지 None
                  - benchmark_present (bool)

    Returns:
        (passed, reasons): passed=True 면 모든 게이트 통과, reasons=[].
                           passed=False 면 reasons 가 enum 문자열 1개 이상.
    """
    reasons: List[str] = []
    if not STRICT_WEINSTEIN_MODE:
        return True, reasons

    _check_market(signal, ctx, reasons)
    _check_sector(signal, ctx, reasons)
    _check_weekly_stage(signal, reasons)
    _check_base(signal, reasons)
    _check_volume(signal, reasons)
    _check_rs(signal, ctx, reasons)
    _check_extension(signal, reasons)
    _check_stop_loss(signal, reasons)

    return (len(reasons) == 0), reasons
