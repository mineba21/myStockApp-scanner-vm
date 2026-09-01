"""시장별 정규장 마감 후 Weinstein 자동 스캔."""
import logging
import pytz
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
    _sched.start()
    logger.info("스케줄러 시작")


def stop_scheduler():
    global _sched
    if _sched and _sched.running:
        _sched.shutdown(wait=False)


def get_next_run_times() -> list:
    if not _sched or not _sched.running:
        return []
    result = []
    jobs = sorted(
        (job for job in _sched.get_jobs() if job.next_run_time),
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
