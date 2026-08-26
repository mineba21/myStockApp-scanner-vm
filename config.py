import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
SLACK_WEBHOOK_URL  = os.getenv("SLACK_WEBHOOK_URL", "")

# ── Strategy rollout ────────────────────────────────────────────
# Phase 1 baseline: the v1 engine is not wired into scan execution yet.
# Keeping the switch OFF guarantees that introducing the rollout scaffold
# does not change legacy_v4 scan, persistence, or notification behavior.
STRATEGY_VERSION   = os.getenv("STRATEGY_VERSION", "legacy_v4").strip() or "legacy_v4"
WEINSTEIN_V1_MODE  = os.getenv("WEINSTEIN_V1_MODE", "off").strip().lower() or "off"

if STRATEGY_VERSION not in {"legacy_v4", "weinstein_breakout_v1"}:
    raise ValueError(
        "STRATEGY_VERSION must be one of: legacy_v4, weinstein_breakout_v1"
    )

if WEINSTEIN_V1_MODE not in {"off", "shadow", "primary"}:
    raise ValueError(
        "WEINSTEIN_V1_MODE must be one of: off, shadow, primary"
    )

# ── Core MA / Scan ───────────────────────────────────────────────
SCAN_LOOKBACK_DAYS  = int(os.getenv("SCAN_LOOKBACK_DAYS", "7"))
MA_PERIOD           = int(os.getenv("MA_PERIOD", "150"))       # Weinstein 30주(150일) MA
MA_SLOPE_PERIOD     = int(os.getenv("MA_SLOPE_PERIOD", "10"))
VOLUME_SURGE_RATIO  = float(os.getenv("VOLUME_SURGE_RATIO", "1.5"))  # legacy (used as fallback)
VOLUME_AVG_PERIOD   = int(os.getenv("VOLUME_AVG_PERIOD", "20"))
FINAL_BAR_DELAY_MINUTES = int(os.getenv("FINAL_BAR_DELAY_MINUTES", "15"))

# ── BREAKOUT (돌파) ─────────────────────────────────────────────
# pivot/base 기반 돌파: 최근 base 기간 고점을 거래량과 함께 돌파
BREAKOUT_BASE_LOOKBACK_DAYS = int(os.getenv("BREAKOUT_BASE_LOOKBACK_DAYS", "60"))
BREAKOUT_MIN_BASE_DAYS      = int(os.getenv("BREAKOUT_MIN_BASE_DAYS", "15"))
BREAKOUT_VOLUME_RATIO       = float(os.getenv("BREAKOUT_VOLUME_RATIO", "1.5"))
BREAKOUT_MAX_EXTENDED_PCT   = float(os.getenv("BREAKOUT_MAX_EXTENDED_PCT", "15.0"))
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
BREAKOUT_WEEKLY_VOL_RATIO = float(os.getenv("BREAKOUT_WEEKLY_VOL_RATIO", "2.0"))
BREAKOUT_DAILY_VOL_RATIO  = float(os.getenv("BREAKOUT_DAILY_VOL_RATIO",  "3.0"))

# Mansfield RS & Base
RS_LOOKBACK_WEEKS    = int(os.getenv("RS_LOOKBACK_WEEKS",    "52"))  # Mansfield RS 평균 기간
BASE_MIN_WEEKS       = int(os.getenv("BASE_MIN_WEEKS",       "5"))   # 최소 base 기간 (주)
PIVOT_LOOKBACK_WEEKS = int(os.getenv("PIVOT_LOOKBACK_WEEKS", "26"))  # pivot 탐색 최대 기간

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

# Persistence / Notification (debug & opt-in)
STRICT_PERSIST_REJECTED                     = os.getenv("STRICT_PERSIST_REJECTED", "false").lower() == "true"
STRICT_NOTIFY_INCLUDE_REASONS               = os.getenv("STRICT_NOTIFY_INCLUDE_REASONS", "false").lower() == "true"

# ── Schedule / Infra ────────────────────────────────────────────
SCHEDULE_TIMES = os.getenv("SCHEDULE_TIMES", "09:00,14:00,22:00").split(",")

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
