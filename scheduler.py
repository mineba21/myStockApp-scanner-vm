"""APScheduler - KST 09:00 / 14:00 / 22:00 자동 스캔"""
import logging
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SCHEDULE_TIMES

logger  = logging.getLogger(__name__)
KST     = pytz.timezone("Asia/Seoul")
_sched  = None


def _run():
    from scanner.scan_engine import run_scan
    logger.info("[스케줄러] 자동 스캔 시작")
    try:
        run_scan(market="ALL", triggered_by="scheduler")
    except Exception as e:
        logger.error(f"[스케줄러] 오류: {e}", exc_info=True)


def start_scheduler():
    global _sched
    if _sched and _sched.running:
        return
    _sched = BackgroundScheduler(timezone=KST)
    for t in SCHEDULE_TIMES:
        t = t.strip()
        try:
            h, m = t.split(":")
            _sched.add_job(_run, CronTrigger(hour=int(h), minute=int(m), timezone=KST),
                           id=f"scan_{t.replace(':','')}",
                           name=f"Weinstein 스캔 {t} KST",
                           replace_existing=True, max_instances=1,
                           misfire_grace_time=300)
            logger.info(f"스케줄 등록: {t} KST")
        except Exception as e:
            logger.error(f"스케줄 등록 실패 ({t}): {e}")
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
    for job in _sched.get_jobs():
        if job.next_run_time:
            result.append({
                "name":     job.name,
                "next_run": job.next_run_time.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST"),
            })
    return result
