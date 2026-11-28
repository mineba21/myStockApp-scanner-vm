"""시장별 정규장 마감 후 Weinstein 자동 스캔."""
import logging
import pytz
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import KR_SCHEDULE_TIMES, US_SCHEDULE_TIMES

logger  = logging.getLogger(__name__)
KST     = pytz.timezone("Asia/Seoul")
NEW_YORK = pytz.timezone("America/New_York")
_sched  = None


def _run(market: str):
    from scanner.scan_engine import run_scan
    logger.info("[스케줄러] %s 자동 스캔 시작", market)
    try:
        run_scan(market=market, triggered_by="scheduler")
    except Exception as e:
        logger.error("[스케줄러] %s 오류: %s", market, e, exc_info=True)


def _run_monthly_allocation():
    """Prior month close data, once before the first US session of the month."""
    from web.asset_allocation_api import (
        _claim_allocation, _latest_completed_month_end, _refresh_asset_allocation,
    )
    requested = _latest_completed_month_end(datetime.now(NEW_YORK).date())
    for profile in ("easy", "original"):
        if not _claim_allocation(profile, requested):
            logger.info("[스케줄러] 자산배분 계산이 이미 진행 중이라 건너뜀: %s %s", profile, requested)
            continue
        logger.info("[스케줄러] 자산배분 자동 계산 시작: %s %s", profile, requested)
        _refresh_asset_allocation(profile, requested)


def _add_market_jobs(scheduler, market: str, times: list[str], timezone) -> None:
    for raw_time in times:
        scan_time = raw_time.strip()
        if not scan_time:
            continue
        try:
            hour, minute = scan_time.split(":")
            scheduler.add_job(
                _run,
                CronTrigger(
                    day_of_week="mon-fri",
                    hour=int(hour),
                    minute=int(minute),
                    timezone=timezone,
                ),
                args=[market],
                id=f"scan_{market.lower()}_{scan_time.replace(':', '')}",
                name=f"Weinstein {market} 스캔 {scan_time} {timezone.zone}",
                replace_existing=True,
                max_instances=1,
                misfire_grace_time=300,
            )
            logger.info("스케줄 등록: %s %s %s", market, scan_time, timezone.zone)
        except (TypeError, ValueError) as exc:
            logger.error("스케줄 등록 실패 (%s %s): %s", market, scan_time, exc)


def start_scheduler():
    global _sched
    if _sched and _sched.running:
        return
    _sched = BackgroundScheduler(timezone=KST)
    _add_market_jobs(_sched, "KR", KR_SCHEDULE_TIMES, KST)
    _add_market_jobs(_sched, "US", US_SCHEDULE_TIMES, NEW_YORK)
    _sched.add_job(
        _run_monthly_allocation,
        CronTrigger(day="1", hour=8, minute=0, timezone=NEW_YORK),
        id="asset_allocation_monthly",
        name="자산배분 월 1회 계산 · 매월 1일 08:00 ET",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    _add_auto_stop_job(_sched)
    try:
        _add_auto_buy_shadow_job(_sched)
    except Exception as exc:  # SHADOW 등록 실패가 공용 스케줄러(S01 포함) 시작을 막지 않게 한다
        logger.error("자동매수 SHADOW 등록 실패 (S01·스캔 일정은 그대로 시작): %s", exc)
    _sched.start()
    logger.info("스케줄러 시작")


AUTO_STOP_JOB_ID = "auto_stop_us_tick"


def _run_auto_stop():
    from trading.auto_stop.runner import auto_stop_us_tick
    auto_stop_us_tick()


def _add_auto_stop_job(scheduler) -> None:
    """account1 미국 S01 자동 손절 tick. OFF여도 등록한다 (기존 주문 대조·종료 판정용, 신규 주문 없음)."""
    from apscheduler.triggers.interval import IntervalTrigger
    from trading.auto_stop.settings import Settings
    scheduler.add_job(
        _run_auto_stop,
        IntervalTrigger(seconds=Settings.from_env().tick_sec, timezone=NEW_YORK),
        id=AUTO_STOP_JOB_ID,
        name="미국 자동 손절 감시 (account1 S01)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=20,
    )


AUTO_BUY_SHADOW_JOB_ID = "auto_buy_us_shadow"


def _run_auto_buy_shadow():
    from trading.auto_buy.shadow.runner import run_shadow_job
    run_shadow_job()


def _job_minute_expr(minutes: int) -> str:
    """N분 간격의 cron minute 식. 60분은 매시 정각("0")이다 (APScheduler 는 ``*/60`` 을 거부한다)."""
    minutes = min(max(int(minutes), 5), 60)
    return "0" if minutes >= 60 else f"*/{minutes}"


def _add_auto_buy_shadow_job(scheduler) -> bool:
    """자동매수 SHADOW (가상 계좌 전용, 실주문 없음). AUTO_BUY_US_MODE=SHADOW 일 때만 등록한다 (재시작해야 반영).

    미국 정규장 마감 후 17:00~23:59 ET 사이 N분마다 실행한다. 작업은 멱등이라 스캔·일봉이 늦어도 다음 실행에서 이어간다.
    """
    from trading.auto_buy.settings import AutoBuySettings
    s = AutoBuySettings.from_env()
    if not s.shadow_enabled:
        if s.config_error:
            logger.warning("자동매수 SHADOW 미등록: %s", s.config_error)
        return False
    scheduler.add_job(
        _run_auto_buy_shadow,
        CronTrigger(day_of_week="mon-sat", hour="17-23", minute=_job_minute_expr(s.job_minutes), timezone=NEW_YORK),
        id=AUTO_BUY_SHADOW_JOB_ID,
        name="미국 자동매수 SHADOW (가상 계좌)",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=600,
    )
    return True


def stop_scheduler():
    global _sched
    if _sched and _sched.running:
        _sched.shutdown(wait=False)


def get_next_run_times() -> list:
    if not _sched or not _sched.running:
        return []
    result = []
    jobs = sorted(
        (job for job in _sched.get_jobs()
         if job.next_run_time and job.id not in (AUTO_STOP_JOB_ID, AUTO_BUY_SHADOW_JOB_ID)),
        key=lambda job: job.next_run_time,
    )
    for job in jobs:
        if job.next_run_time:
            result.append({
                "name":     job.name,
                "market":   job.args[0] if job.args else None,
                "next_run": job.next_run_time.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            })
    return result
