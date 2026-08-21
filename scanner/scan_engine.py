"""스캔 엔진 - 전체 스캔 오케스트레이션"""
import json
import logging
from datetime import datetime
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
    Phase 5 의 sector 매핑이 들어오기 전까지는 sector_stage 는 항상 None.
    """
    from scanner.strict_filter import apply_strict_filter

    ctx = {
        "market_condition":  market_condition,
        "sector_stage":      None,                          # Phase 5 까지 None
        "benchmark_present": benchmark_close is not None,
    }
    passed, reasons = apply_strict_filter(signal, ctx)
    signal["strict_filter_passed"] = passed
    signal["filter_reasons"]       = reasons
    return passed, reasons

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
            sigs, cnt = _scan_kr(db, kr_bench, kr_condition, kr_universe)
            buy_signals.extend(sigs); total_scanned += cnt

        if market in ("US", "ALL"):
            sigs, cnt = _scan_us(db, us_universe, us_bench, us_condition)
            buy_signals.extend(sigs); total_scanned += cnt

        holding_checks = _check_holdings(db, kr_bench=kr_bench, us_bench=us_bench)
        sell_signals = _check_watchlist(db, kr_bench=kr_bench, us_bench=us_bench)

        if buy_signals or sell_signals:
            _notify(buy_signals, sell_signals, send_telegram_message)

        log.finished_at   = datetime.utcnow()
        log.total_scanned = total_scanned
        log.signals_found = len(buy_signals)
        log.status        = "DONE"
        db.commit()

        return {"status": "done", "total_scanned": total_scanned,
                "signals_found": len(buy_signals),
                "sell_signals": len(sell_signals),
                "holding_checks": holding_checks}

    except Exception as e:
        logger.error(f"스캔 오류: {e}", exc_info=True)
        log.status    = "ERROR"
        log.error_msg = str(e)
        log.finished_at = datetime.utcnow()
        db.commit()
        return {"status": "error", "message": str(e)}

    finally:
        scan_status["is_running"] = False
        db.close()


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

    ticker = res["ticker"]

    # 1) legacy market filter — CAUTION 표시용. BEAR fast-path 는 비용 절약.
    allow, flag = _get_market_filter_decision(market_condition, res["signal_type"])
    if not allow and not STRICT_PERSIST_REJECTED:
        logger.debug(f"[{market_label}] {ticker} legacy market filter: {flag}")
        return False
    if flag:
        res["_market_flag"] = flag

    # 2) strict 필터 평가 (STRICT_WEINSTEIN_MODE=False 면 항상 (True, []) 반환)
    passed, reasons = _evaluate_strict_filter(res, market_condition, benchmark_close)

    # 3) persist/notify 분기
    if passed:
        _save(db, res)
        logger.info(f"[{market_label}] {ticker} {res['name']}: "
                    f"{res['signal_type']} Q={res.get('signal_quality','?')} "
                    f"strict=PASS")
        return True

    # 거부 — strict 모드 ON
    if STRICT_WEINSTEIN_MODE:
        if STRICT_PERSIST_REJECTED:
            _save(db, res)
        logger.debug(f"[{market_label}] {ticker} strict reject: {reasons}")
        return False

    # STRICT_WEINSTEIN_MODE=False (legacy 호환) — 통과 처리
    _save(db, res)
    return True


def _scan_kr(db, benchmark_close=None, market_condition=None, kr_universe="kospi+kosdaq"):
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
        res = analyze_stock(df, info["ticker"], info["name"], "KR",
                            benchmark_close, market_condition)
        count += 1
        if res and _process_signal(db, res, "KR",
                                   market_condition, benchmark_close):
            signals.append(res)
        time.sleep(0.05)

    return signals, count


def _scan_us(db, universe, benchmark_close=None, market_condition=None):
    from scanner.us_stocks import get_all_us_tickers, get_us_batch
    from scanner.weinstein import analyze_stock

    tickers = get_all_us_tickers(universe)
    results = get_us_batch(tickers, progress_callback=_prog)
    signals, count = [], 0

    for info, df in results:
        if df is None:
            continue
        res = analyze_stock(df, info["ticker"], info["name"], "US",
                            benchmark_close, market_condition)
        count += 1
        if res and _process_signal(db, res, "US",
                                   market_condition, benchmark_close):
            signals.append(res)

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


def _check_holdings(db, kr_bench=None, us_bench=None):
    """활성 보유종목을 중복 티커 단위로 조회하고 현재 매도 상태를 저장한다."""
    from collections import defaultdict
    from database.models import Holding
    from scanner.weinstein import check_sell_signal, to_weekly_ohlcv
    from scanner.kr_stocks import get_kr_ohlcv
    from scanner.us_stocks import get_us_ohlcv
    from config import MA_PERIOD

    holdings = db.query(Holding).filter(
        Holding.is_active == True,
        Holding.quantity > 0,
    ).all()
    grouped = defaultdict(list)
    for holding in holdings:
        grouped[(holding.market, holding.ticker)].append(holding)

    counts = {"total": len(holdings), "SELL_REQUIRED": 0, "REVIEW": 0,
              "CAUTION": 0, "HOLD": 0, "CHECK_FAILED": 0}
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
            current_price = float(df["Close"].iloc[-1])

            for holding in rows:
                signal = check_sell_signal(
                    df, holding.ticker, holding.name, holding.market,
                    buy_price=holding.avg_price,
                    weekly_df=weekly_df,
                    benchmark_close=benchmark,
                )
                status = status_by_severity.get(signal.get("severity"), "CAUTION") if signal else "HOLD"
                holding.current_price = current_price
                holding.price_updated_at = checked_at
                holding.sell_status = status
                holding.sell_severity = signal.get("severity") if signal else None
                holding.sell_reason = signal.get("sell_reason") if signal else None
                holding.sell_checked_at = checked_at
                counts[status] += 1
        except Exception as exc:
            logger.error(f"보유종목 매도 점검 오류 {market} {ticker}: {exc}")
            for holding in rows:
                holding.sell_status = "CHECK_FAILED"
                holding.sell_severity = None
                holding.sell_reason = "가격 데이터를 확인하지 못했습니다."
                holding.sell_checked_at = checked_at
                counts["CHECK_FAILED"] += 1

    db.flush()
    return counts


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
        bull = [e["name"] for e in etfs if e["stage"] == "STAGE2"]
        bear = [e["name"] for e in etfs if e["stage"] == "STAGE4"]
        parts = []
        if bull: parts.append(f"강세: {', '.join(bull[:3])}")
        if bear: parts.append(f"약세: {', '.join(bear[:3])}")
        return "📊 " + " | ".join(parts) if parts else ""
    except Exception:
        return ""


def _notify(buys, sells, send_fn):
    """매수/매도 시그널을 Telegram 메시지로 포맷.

    Phase 4 invariant: ``buys`` 는 *strict-pass* 만 들어오므로 본 함수는
    별도의 strict 거부 분기 없이 순수 포맷팅만 담당. 거부 시그널의
    DB persistence 는 ``_process_signal`` 에서 STRICT_PERSIST_REJECTED 토글
    하에 처리된다.

    ``STRICT_NOTIFY_INCLUDE_REASONS=True`` 토글 시 strict 결과 메타(통과 표시
    + 비어있지 않은 reason 리스트) 가 알림에 추가된다. 기본 False — 메시지
    길이/노이즈 방지.
    """
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
                        f"{bq_str}{flag_warn}{strict_str}\n"
                        f"  • 시그널일: {s['signal_date']}\n\n")
            if len(mkt_list) > 10:
                msg += f"  ... 외 {len(mkt_list) - 10}개\n\n"
        send_fn(msg)

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
        send_fn(msg)
