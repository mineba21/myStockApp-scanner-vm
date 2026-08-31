import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
SLACK_WEBHOOK_URL  = os.getenv("SLACK_WEBHOOK_URL", "")

# ── Core MA / Scan ───────────────────────────────────────────────
SCAN_LOOKBACK_DAYS  = int(os.getenv("SCAN_LOOKBACK_DAYS", "7"))
MA_PERIOD           = int(os.getenv("MA_PERIOD", "150"))       # Weinstein 30주(150일) MA
MA_SLOPE_PERIOD     = int(os.getenv("MA_SLOPE_PERIOD", "10"))
VOLUME_SURGE_RATIO  = float(os.getenv("VOLUME_SURGE_RATIO", "1.5"))  # legacy (used as fallback)
VOLUME_AVG_PERIOD   = int(os.getenv("VOLUME_AVG_PERIOD", "20"))

# ── BREAKOUT (돌파) ─────────────────────────────────────────────
# pivot/base 기반 돌파: 최근 base 기간 고점을 거래량과 함께 돌파
BREAKOUT_BASE_LOOKBACK_DAYS = int(os.getenv("BREAKOUT_BASE_LOOKBACK_DAYS", "60"))
BREAKOUT_MIN_BASE_DAYS      = int(os.getenv("BREAKOUT_MIN_BASE_DAYS", "15"))
BREAKOUT_VOLUME_RATIO       = float(os.getenv("BREAKOUT_VOLUME_RATIO", "1.5"))
# Step 2 — legacy v3(_find_breakout_signal)와 v4(detect_stage2_breakout) 가
# 이 값을 공유한다. 15.0 → 25.0 (실측: MRK 28%, SJM 23.7% 등 정상 돌파가
# 과열로 잘리고 있었다).
BREAKOUT_MAX_EXTENDED_PCT   = float(os.getenv("BREAKOUT_MAX_EXTENDED_PCT", "25.0"))
REQUIRE_PRICE_ABOVE_MA50    = os.getenv("REQUIRE_PRICE_ABOVE_MA50", "true").lower() == "true"

# ── RE_BREAKOUT (재돌파) ────────────────────────────────────────
# Stage2 조정 후 연속 돌파: 단기 base 돌파로 추세 지속 확인
REBREAKOUT_BASE_LOOKBACK_DAYS   = int(os.getenv("REBREAKOUT_BASE_LOOKBACK_DAYS", "30"))
REBREAKOUT_MAX_PULLBACK_PCT     = float(os.getenv("REBREAKOUT_MAX_PULLBACK_PCT", "15.0"))
REBREAKOUT_VOLUME_RATIO         = float(os.getenv("REBREAKOUT_VOLUME_RATIO", "1.5"))
REBREAKOUT_REQUIRE_VOLUME_DRYUP = os.getenv("REBREAKOUT_REQUIRE_VOLUME_DRYUP", "false").lower() == "true"

# ── REBOUND (눌림목 반등) ───────────────────────────────────────
# MA50 지지 후 반등: Stage2에서 MA50으로 눌림 → 반등 확인
REBOUND_MA_PERIOD            = int(os.getenv("REBOUND_MA_PERIOD", "50"))
REBOUND_TOUCH_PCT            = float(os.getenv("REBOUND_TOUCH_PCT", "3.0"))    # MA50 ±3% 이내
REBOUND_CONFIRM_PCT          = float(os.getenv("REBOUND_CONFIRM_PCT", "2.0"))  # 저점 대비 +2% 반등
REBOUND_MAX_PULLBACK_PCT     = float(os.getenv("REBOUND_MAX_PULLBACK_PCT", "12.0"))
REBOUND_REQUIRE_VOLUME_DRYUP = os.getenv("REBOUND_REQUIRE_VOLUME_DRYUP", "false").lower() == "true"
REBOUND_ALLOW_PIVOT_RETEST   = os.getenv("REBOUND_ALLOW_PIVOT_RETEST", "true").lower() == "true"
# v4 게이트: REBOUND 시 직전 base 재테스트 OR 주봉 30-SMA 터치+회복 강제 여부.
# 운영에서 시그널 수가 너무 줄면 False 로 토글 가능 (단 strategy invariant 위반).
REBOUND_REQUIRE_BASE_RETEST  = os.getenv("REBOUND_REQUIRE_BASE_RETEST", "true").lower() == "true"

# ── Market Filter (시장 필터) ────────────────────────────────────
ENABLE_MARKET_FILTER   = os.getenv("ENABLE_MARKET_FILTER", "true").lower() == "true"
BLOCK_NEW_BUYS_IN_BEAR = os.getenv("BLOCK_NEW_BUYS_IN_BEAR", "true").lower() == "true"
# CAUTION_MODE 옵션: "block_breakout" | "allow_with_flag" | "allow_all"
CAUTION_MODE           = os.getenv("CAUTION_MODE", "allow_with_flag")

# ── v4 Weinstein (Weekly Stage Analysis) ───────────────────────
# 주봉 기반 Stage 판정 (원전 충실)
WEEKLY_MA_LONG    = int(os.getenv("WEEKLY_MA_LONG", "30"))   # 주봉 30-SMA (원전 기준)
WEEKLY_MA_SHORT   = int(os.getenv("WEEKLY_MA_SHORT", "10"))  # 주봉 10-SMA (추세 확인)
DAILY_MA_FAST     = int(os.getenv("DAILY_MA_FAST", "50"))    # 일봉 MA50
DAILY_MA_SLOW     = int(os.getenv("DAILY_MA_SLOW", "150"))   # 일봉 MA150 (주봉 30 ≈ 일봉 150)

# 거래량 확인 (주봉/일봉 모두)
# Step 2 — 주봉 게이트는 "확실히 거래량 없는 종목만 조기 제외"하는 성능
# 컷으로 재정의 (Step 1 A/B 실측: 게이트를 완전히 없애도 KR/US 실제 통과
# 종목이 늘지 않음 — 병목은 base 정의였다). 2.0 → 0.5.
BREAKOUT_WEEKLY_VOL_RATIO = float(os.getenv("BREAKOUT_WEEKLY_VOL_RATIO", "0.5"))
# Step 2 — 실효 breakout 의 거래량 비율이 실측상 1.54x 부근(Weinstein 원전
# 수준)이라 3.0 은 과했다. 1.5 로 완화.
BREAKOUT_DAILY_VOL_RATIO  = float(os.getenv("BREAKOUT_DAILY_VOL_RATIO",  "1.5"))

# Mansfield RS & Base
RS_LOOKBACK_WEEKS    = int(os.getenv("RS_LOOKBACK_WEEKS",    "52"))  # Mansfield RS 평균 기간
BASE_MIN_WEEKS       = int(os.getenv("BASE_MIN_WEEKS",       "5"))   # 최소 base 기간 (주) — v1 전용
PIVOT_LOOKBACK_WEEKS = int(os.getenv("PIVOT_LOOKBACK_WEEKS", "26"))  # pivot 탐색 최대 기간 — v1 전용

# ── Step 2 — 2단(2-tier) Base 구조 (BASE_MODE="v2") ────────────
# v1(detect_base_pivot) 은 base 기간과 pivot 을 한 확장 루프에서 같이
# 계산해 "base 가 길수록 pivot·손절폭도 함께 벌어지는" 구조였다(S&P500
# 실측: 돌파 34건, 평균 손절폭 14.5%). v2 는 자격 구간(폭만 확인)과 tight
# 구간(pivot/손절 기준)을 분리해 손절폭을 8.3% 수준으로 좁힌다(돌파 82건).
#
# market_param() 으로 KR/US 개별 오버라이드 가능 — 아래 값이 그 기본값.
BASE_MODE               = os.getenv("BASE_MODE", "v2")   # "v1" | "v2"
BASE_LOOKBACK_DAYS      = int(os.getenv("BASE_LOOKBACK_DAYS", "25"))
BASE_MAX_WIDTH_PCT      = float(os.getenv("BASE_MAX_WIDTH_PCT", "25.0"))
# US_/KR_ 오버라이드는 3단 조회다: 자기 자신의 env → **공통 env 가 명시적으로
# 설정된 경우만** 그 값 → 시장별 하드코딩 기본값(25.0/30.0). 공통값의
# "설정 안 됐을 때의 기본값"(os.getenv 의 2번째 인자)은 폴백 대상이 아니다
# — 그걸 폴백에 넣으면 KR=30.0 의 의도적 시장별 편차가, 공통값을 한 번도
# 손대지 않은 평범한 배포에서도 25.0 으로 사라져 버린다(Codex 리뷰 P2:
# "BASE_MAX_WIDTH_PCT=20 만 설정해도 US/KR 모두 반영돼야 한다"는 요구와,
# "아무것도 설정 안 하면 KR 은 여전히 30.0"이라는 기존 invariant 를 동시에
# 만족시키려면 이렇게 "명시적으로 설정됐는지"를 구분해야 한다).
_common_base_max_width_env  = os.environ.get("BASE_MAX_WIDTH_PCT")
US_BASE_MAX_WIDTH_PCT   = float(os.getenv("US_BASE_MAX_WIDTH_PCT", _common_base_max_width_env or "25.0"))
KR_BASE_MAX_WIDTH_PCT   = float(os.getenv("KR_BASE_MAX_WIDTH_PCT", _common_base_max_width_env or "30.0"))  # KR 25일 변동폭 실측 중앙값 22.4%
TIGHT_LOOKBACK_DAYS     = int(os.getenv("TIGHT_LOOKBACK_DAYS", "10"))
TIGHT_MAX_WIDTH_PCT     = float(os.getenv("TIGHT_MAX_WIDTH_PCT", "10.0"))
# 위와 동일한 3단 조회 (Codex 리뷰 P2 — TIGHT_MAX_WIDTH_PCT 도 동일 문제)
_common_tight_max_width_env = os.environ.get("TIGHT_MAX_WIDTH_PCT")
US_TIGHT_MAX_WIDTH_PCT  = float(os.getenv("US_TIGHT_MAX_WIDTH_PCT", _common_tight_max_width_env or "10.0"))
KR_TIGHT_MAX_WIDTH_PCT  = float(os.getenv("KR_TIGHT_MAX_WIDTH_PCT", _common_tight_max_width_env or "12.0"))  # 손절폭을 시장 무관하게 유지
TIGHT_CONTRACTION_RATIO = float(os.getenv("TIGHT_CONTRACTION_RATIO", "0.85"))

# ── Step 4 — 진입 시점 통제 (shadow-first) ──────────────────────
# 세 통제는 항상 계산/저장하지만 기본값에서는 경고와 funnel shadow 집계만
# 수행한다. 실제 후보/알림 차단은 각 *_AS_GATE=true 일 때만 활성화된다.
MAX_PIVOT_EXT_PCT       = float(os.getenv("MAX_PIVOT_EXT_PCT", "5.0"))
PIVOT_EXT_AS_GATE       = os.getenv("PIVOT_EXT_AS_GATE", "false").lower() == "true"
UPTHRUST_CHECK_DAYS     = int(os.getenv("UPTHRUST_CHECK_DAYS", "3"))
UPTHRUST_AS_GATE        = os.getenv("UPTHRUST_AS_GATE", "false").lower() == "true"
UPTHRUST_COOLDOWN_DAYS  = int(os.getenv("UPTHRUST_COOLDOWN_DAYS", "10"))
ALERT_MAX_CUR_EXT_PCT   = float(os.getenv("ALERT_MAX_CUR_EXT_PCT", "5.0"))
ALERT_MAX_CUR_STOP_PCT  = float(os.getenv("ALERT_MAX_CUR_STOP_PCT", "12.0"))
ALERT_FRESHNESS_AS_GATE = os.getenv("ALERT_FRESHNESS_AS_GATE", "false").lower() == "true"

# ── Step 5 — Van Tharp R-multiple position sizing ──────────────
# market_param() 으로 KR_/US_ 오버라이드를 지원한다. US와 KR 자산/heat는
# 서로 독립이며 환율 환산을 하지 않는다.
RISK_PCT              = float(os.getenv("RISK_PCT", "1.0"))
MAX_POSITION_PCT      = float(os.getenv("MAX_POSITION_PCT", "20.0"))
MAX_TOTAL_HEAT_PCT    = float(os.getenv("MAX_TOTAL_HEAT_PCT", "6.0"))
MIN_R_PCT             = float(os.getenv("MIN_R_PCT", "3.0"))
MAX_R_PCT             = float(os.getenv("MAX_R_PCT", "15.0"))
R_BAND_AS_GATE        = os.getenv("R_BAND_AS_GATE", "true").lower() == "true"

# ── Step 6 — 보유 종목 매도 알림 ───────────────────────────
HOLDING_ALERT_REPEAT_HOURS = int(os.getenv("HOLDING_ALERT_REPEAT_HOURS", "24"))

for _sizing_name in (
    "RISK_PCT", "MAX_POSITION_PCT", "MAX_TOTAL_HEAT_PCT",
    "MIN_R_PCT", "MAX_R_PCT",
):
    for _sizing_market in ("KR", "US"):
        _sizing_key = f"{_sizing_market}_{_sizing_name}"
        if _sizing_key in os.environ:
            globals()[_sizing_key] = float(os.environ[_sizing_key])


def market_param(name: str, market: "str | None", default):
    """시장별 파라미터 오버라이드 조회.

    조회 순서: ``{MARKET}_{name}`` (이 모듈의 속성) → ``{name}`` (공통값) →
    ``default``. 예: ``market_param("BASE_MAX_WIDTH_PCT", "KR", 25.0)`` 는
    ``KR_BASE_MAX_WIDTH_PCT`` 가 정의돼 있으면 그 값을, 없으면
    ``BASE_MAX_WIDTH_PCT``(공통값)를, 그것도 없으면 ``default`` 를 반환한다.

    항상 이 모듈의 **현재** 속성을 조회하므로(스냅샷 아님), 테스트에서
    ``monkeypatch.setattr(config, "KR_BASE_MAX_WIDTH_PCT", ...)`` 하면
    즉시 반영된다.
    """
    import sys
    mod = sys.modules[__name__]
    if market:
        market_key = f"{str(market).upper()}_{name}"
        if hasattr(mod, market_key):
            return getattr(mod, market_key)
    if hasattr(mod, name):
        return getattr(mod, name)
    return default

# ── Strict Weinstein Optimal Buy Filter ────────────────────────
# CLAUDE.md 8개 mandatory gate를 hard-block 으로 강제. 실패 사유는
# `filter_reasons` 로 추적되며, 실패 게이트는 `warning_flags` 로 강등되지 않는다.
# 자세한 내용: docs/plans/strict-weinstein-optimal-buy-filter.md
STRICT_WEINSTEIN_MODE                       = os.getenv("STRICT_WEINSTEIN_MODE", "true").lower() == "true"

# Gate 1 — Market
STRICT_REQUIRE_MARKET_CONFIRMATION          = os.getenv("STRICT_REQUIRE_MARKET_CONFIRMATION", "true").lower() == "true"
STRICT_BLOCK_CAUTION_BREAKOUTS              = os.getenv("STRICT_BLOCK_CAUTION_BREAKOUTS", "true").lower() == "true"

# Gate 2 — Sector (스텁; 종목당 sector 매핑은 후속 plan)
STRICT_REQUIRE_SECTOR_STAGE2                = os.getenv("STRICT_REQUIRE_SECTOR_STAGE2", "false").lower() == "true"

# Gate 3 — Stock Weekly/Daily Stage
STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA      = os.getenv("STRICT_REQUIRE_PRICE_ABOVE_WEEKLY_30MA", "true").lower() == "true"
STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA      = os.getenv("STRICT_REQUIRE_PRICE_ABOVE_DAILY_150MA", "true").lower() == "true"

# Gate 5 — Breakout Volume
STRICT_REQUIRE_BREAKOUT_VOLUME              = os.getenv("STRICT_REQUIRE_BREAKOUT_VOLUME", "true").lower() == "true"

# Gate 6 — Mansfield RS
STRICT_REQUIRE_RS_POSITIVE                  = os.getenv("STRICT_REQUIRE_RS_POSITIVE", "true").lower() == "true"
STRICT_REQUIRE_RS_RISING                    = os.getenv("STRICT_REQUIRE_RS_RISING", "true").lower() == "true"
STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT   = os.getenv("STRICT_REQUIRE_RS_ZERO_CROSS_FOR_BREAKOUT", "true").lower() == "true"
RS_ZERO_CROSS_LOOKBACK_WEEKS                = int(os.getenv("RS_ZERO_CROSS_LOOKBACK_WEEKS", "8"))

# Gate 8 — Stop-loss
STRICT_REQUIRE_STOP_LOSS                    = os.getenv("STRICT_REQUIRE_STOP_LOSS", "true").lower() == "true"

# 주봉 거래량 게이트 — true: 미달 시 BREAKOUT 차단 / false: warning 만 남기고 통과
# (Step 1 — 코드 변경 없이 env 만 바꿔 두 번 스캔해 효과를 측정하기 위한 스위치)
BREAKOUT_WEEKLY_VOL_AS_GATE                 = os.getenv("BREAKOUT_WEEKLY_VOL_AS_GATE", "true").lower() == "true"

# Persistence / Notification (debug & opt-in)
STRICT_PERSIST_REJECTED                     = os.getenv("STRICT_PERSIST_REJECTED", "true").lower() == "true"
STRICT_NOTIFY_INCLUDE_REASONS               = os.getenv("STRICT_NOTIFY_INCLUDE_REASONS", "false").lower() == "true"

# ── Schedule / Infra ────────────────────────────────────────────
# 각 시장의 정규장 종가가 확정된 뒤 실행한다. 미국 시간대는 DST를 자동 반영한다.
KR_SCHEDULE_TIMES = os.getenv("KR_SCHEDULE_TIMES", "16:10").split(",")
US_SCHEDULE_TIMES = os.getenv("US_SCHEDULE_TIMES", "16:30").split(",")

KIS_APP_KEY         = os.getenv("KIS_APP_KEY", "")
KIS_APP_SECRET      = os.getenv("KIS_APP_SECRET", "")
KIS_ACCOUNT_NO      = os.getenv("KIS_ACCOUNT_NO", "")
KIS_ACCOUNT_PROD_CD = os.getenv("KIS_ACCOUNT_PROD_CD", "01")
KIS_IS_PAPER        = os.getenv("KIS_IS_PAPER", "true").lower() == "true"


DATABASE_URL=os.getenv("DATABASE_URL", "")
DATABASE_DIRECT_URL=os.getenv("DATABASE_DIRECT_URL", "")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./stock_scanner.db"
)

DATABASE_DIRECT_URL = os.getenv(
    "DATABASE_DIRECT_URL",
    DATABASE_URL
)

KR_DATA_PERIOD = "2y"
US_DATA_PERIOD = "2y"
US_UNIVERSE    = os.getenv("US_UNIVERSE", "sp500+nasdaq100")
