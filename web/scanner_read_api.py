"""Explicit scanner-only projection. No brokerage, scheduling or mutations."""
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from database.models import ScanResult, ScanLog, get_db

router = APIRouter(prefix='/api/scanner-read', tags=['scanner-read'])


def utc(value):
    return value.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z') if value else None


def reasons(raw):
    try:
        result = json.loads(raw or '[]')
        return [item[:500] for item in result[:30] if isinstance(item, str)] if isinstance(result, list) else []
    except (ValueError, TypeError):
        return []


def serialize(row):
    # Expose the saved recommendation only; never query accounts or brokerage here.
    fields = ('id', 'market', 'ticker', 'name', 'signal_type', 'signal_date', 'stage',
              'price', 'ma150', 'pivot_price', 'stop_loss', 'grade', 'signal_quality',
              'volume_ratio', 'weekly_volume_ratio', 'weekly_volume_ratio_4w',
              'weekly_volume_quality_passed', 'weekly_volume_quality_threshold',
              'strict_filter_passed', 'sector_name', 'sector_stage', 'rs_value', 'rs_trend',
              'upthrust_failed', 'pivot_ext_pct', 'cur_ext_pct', 'cur_stop_pct',
              'suggested_qty')
    result = {field: getattr(row, field) for field in fields}
    result = {k: None if isinstance(v, float) and not math.isfinite(v) else v for k, v in result.items()}
    result.update(scan_time=utc(row.scan_time), first_detected_at=utc(row.first_detected_at),
                  filter_reasons=reasons(row.filter_reasons),
                  entry_warnings=reasons(row.entry_warnings), price_basis='stored_scan_snapshot',
                  strict_assessment='legacy_unassessed' if row.strict_filter_passed is None else 'passed',
                  event_key=f'{row.id}:{utc(row.scan_time)}',
                  signal_key=f'{row.market}:{row.ticker}:{row.signal_type}:{row.signal_date}')
    return result


def visible(db):
    return db.query(ScanResult).filter(or_(ScanResult.strict_filter_passed.is_(None),
                                         ScanResult.strict_filter_passed.is_(True)))


def parse_time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, '시간은 시간대가 포함된 ISO8601이어야 합니다.') from exc


@router.get('/status')
def status(db: Session = Depends(get_db)):
    logs = []
    for market in ('KR', 'US'):
        row = db.query(ScanLog).filter(ScanLog.market == market).order_by(ScanLog.id.desc()).first()
        logs.append({'market': market, 'status': row.status if row else 'NO_DATA',
                     'started_at': utc(row.started_at) if row else None,
                     'finished_at': utc(row.finished_at) if row else None,
                     'total_scanned': row.total_scanned if row else None,
                     'signals_found': row.signals_found if row else None})
    return {'observed_at': utc(datetime.utcnow()), 'scans': logs, 'source': 'stored_scan_logs'}


@router.get('/signals')
def signals(market: Literal['ALL', 'KR', 'US'] = 'ALL',
            signal_type: Literal['ALL', 'BREAKOUT', 'RE_BREAKOUT', 'REBOUND'] = 'ALL',
            after: Optional[str] = None, after_id: int = Query(0, ge=0),
            through: Optional[str] = None, limit: int = Query(50, ge=1, le=100),
            db: Session = Depends(get_db)):
    now = datetime.utcnow()
    start = parse_time(after) if after else now - timedelta(days=7)
    end = parse_time(through) if through else now
    if start > end or end > now or start < now - timedelta(days=366):
        raise HTTPException(422, '조회 범위는 최근 366일 이내이며 미래일 수 없습니다.')
    q = visible(db).filter(ScanResult.scan_time <= end,
        or_(ScanResult.scan_time > start, and_(ScanResult.scan_time == start, ScanResult.id > after_id)))
    if market != 'ALL': q = q.filter(ScanResult.market == market)
    if signal_type != 'ALL': q = q.filter(ScanResult.signal_type == signal_type)
    rows = q.order_by(ScanResult.scan_time.asc(), ScanResult.id.asc()).limit(limit + 1).all()
    more, selected = len(rows) > limit, rows[:limit]
    last = selected[-1] if selected else None
    cursor = {'after': utc(last.scan_time), 'after_id': last.id} if more else {'after': utc(end), 'after_id': 0}
    return {'items': [serialize(r) for r in selected], 'has_more': more,
            'next_cursor': cursor, 'through': utc(end), 'market': market, 'signal_type': signal_type,
            'source': 'stored_scan_results', 'coverage': 'current rows, not an immutable event history'}


@router.get('/signals/{result_id}')
def signal(result_id: int, db: Session = Depends(get_db)):
    row = visible(db).filter(ScanResult.id == result_id).first()
    if row is None:
        raise HTTPException(404, '조회 가능한 스캐너 결과가 없습니다.')
    return serialize(row)
