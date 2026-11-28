"""auto_buy_us 테스트 보조: 운영 DB(임시 SQLite) 시드 헬퍼."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_naive(aware: datetime) -> datetime:
    """aware datetime → 운영 DB 방식의 naive UTC."""
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def seed_scan(session, started_utc, finished_utc, rows, triggered_by="scheduler", status="DONE", market="US"):
    """ScanLog 1건과 ScanResult 행을 넣고 ScanLog 를 돌려준다. rows 는 dict 목록(ticker 필수).

    scan_time 기본값은 started + 1분, market 기본값은 'US'. 시각은 naive UTC.
    """
    from database.models import ScanLog, ScanResult

    log = ScanLog(started_at=started_utc, finished_at=finished_utc, market=market,
                  status=status, triggered_by=triggered_by)
    session.add(log)
    session.flush()
    for r in rows:
        kw = {"scan_time": started_utc + timedelta(minutes=1), "market": "US", "price": 100.0,
              "pivot_price": 98.0, "stop_loss": 92.0, "signal_type": "BREAKOUT",
              "signal_date": "2026-11-24"}
        kw.update(r)
        session.add(ScanResult(**kw))
    session.commit()
    return log
