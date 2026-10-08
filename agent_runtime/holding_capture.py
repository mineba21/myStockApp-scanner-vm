"""Optional, failure-isolated adapter for the existing holdings evaluation."""
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from database.models import AgentScanCapture, Transaction
from .schemas import Observation, DataQuality, HoldingStatus, canonical_json
from .shadow import ShadowStore

logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def strategy_version():
    from scanner import weinstein
    import config
    root = Path(__file__).resolve().parents[1]
    sources = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
               for name in ('scanner/weinstein.py', 'scanner/scan_engine.py')}
    # Loaded strategy constants only. Never inspect general config (tokens/DB URLs).
    settings = {name: value for name, value in vars(weinstein).items()
                if name.isupper() and type(value) in (str, int, float, bool)}
    settings['HOLDING_ALERT_REPEAT_HOURS'] = config.HOLDING_ALERT_REPEAT_HOURS
    return digest(dict(sources=sources, settings=settings))


def episode_anchor(transactions):
    """Match the ledger's date/id order; additions do not create new episodes."""
    quantity, anchor = 0., None
    for item in transactions:
        if item.tx_type == 'BUY' and item.quantity and item.price:
            if quantity <= 0:
                anchor = item.id
            quantity += item.quantity
        elif item.tx_type == 'SELL' and item.quantity:
            quantity = max(0., quantity - item.quantity)
    return anchor if quantity > 0 else None


class HoldingCapture:
    def __init__(self, scan_id, session_factory):
        self.scan_id = scan_id
        self.store = ShadowStore(session_factory)
        self.inputs = {}
        self.results = {}
        self.observations = []
        self.ready = False
        self.version = strategy_version()

    def safe(self, method, *args):
        try:
            return getattr(self, method)(*args)
        except Exception as exc:
            logger.warning('Runtime DEGRADED capture scan=%s phase=%s error=%s',
                           self.scan_id, method, type(exc).__name__)
            return None

    def prepare(self, db, holdings):
        from config import HOLDING_ALERT_REPEAT_HOURS
        # A complete active inventory also observes absence for manual holdings.
        with self.store._transaction() as session:
            inventories = [(r.scan_id, json.loads(r.expected_json)['items']) for r in
                           session.query(AgentScanCapture).filter(
                               AgentScanCapture.scan_id < self.scan_id).order_by(AgentScanCapture.scan_id)]
        # All additional queries use a separate session: a DB error must not
        # poison the scanner's transaction, including on PostgreSQL.
        with self.store._transaction() as ledger_session:
            for holding in holdings:
                transactions = ledger_session.query(Transaction).filter_by(
                    account_id=holding.account_id, ticker=holding.ticker, market=holding.market,
                ).order_by(Transaction.trade_date, Transaction.id).all()
                anchor = episode_anchor(transactions)
                absent = max((number for number, items in inventories
                              if not any(e['holding_id'] == holding.id for e in items)), default=0)
                identity = dict(account=holding.account_id, holding=holding.id,
                                created=holding.created_at.isoformat() if holding.created_at else None,
                                entry_transaction=anchor, last_observed_absence=absent)
                inputs = {name: getattr(holding, name) for name in
                          ('quantity', 'avg_price', 'entry_price', 'current_stop_loss',
                           'initial_stop_loss', 'initial_r')}
                # Recalculation/edit evidence changes the input version, not a market cause.
                ledger = [dict(id=t.id, type=t.tx_type, date=t.trade_date,
                               quantity=t.quantity, price=t.price) for t in transactions]
                self.inputs[holding.id] = dict(
                    id=f'scan-{self.scan_id}-holding-{holding.id}-r0',
                    episode_id=digest(identity), holding_id=holding.id,
                    account_id=holding.account_id, market=holding.market, ticker=holding.ticker,
                    input_version=digest(dict(inputs=inputs, ledger=ledger)), inputs=inputs,
                    lifecycle=identity, repeat_hours=HOLDING_ALERT_REPEAT_HOURS,
                    prior_alert=dict(last_alert_severity=holding.last_alert_severity,
                                     last_alert_reason=holding.last_alert_reason,
                                     last_alert_at=holding.last_alert_at))
        expected = [{k: v[k] for k in ('id', 'episode_id', 'holding_id')}
                    for v in self.inputs.values()]
        self.store.begin_capture(self.scan_id, expected, now=datetime.now(timezone.utc))
        self.ready = True

    def group(self, holdings, df, weekly, benchmark, checked_at):
        if not self.ready:
            return
        for holding in holdings:
            failed = holding.sell_status == 'CHECK_FAILED'
            stamp = None
            # Daily timestamps are feed labels, not proof of final/confirmed bars.
            if not failed:
                import pandas as pd
                raw = pd.Timestamp(df.index.max())
                if raw.tzinfo is None:
                    raw = raw.tz_localize(ZoneInfo('Asia/Seoul' if holding.market == 'KR' else 'America/New_York'))
                stamp = raw.tz_convert('UTC').to_pydatetime()
            quality = DataQuality.UNAVAILABLE if failed else (
                DataQuality.DEGRADED if weekly is None or benchmark is None or len(benchmark) == 0
                else DataQuality.AVAILABLE)
            self.results[holding.id] = dict(
                status=None if failed else HoldingStatus(holding.sell_status),
                data_quality=quality, data_as_of=stamp,
                observed_at=checked_at.replace(tzinfo=timezone.utc),
                reason=holding.sell_reason, severity=holding.sell_severity,
                weekly_available=weekly is not None,
                benchmark_available=benchmark is not None and len(benchmark) > 0,
                bar_finality='UNKNOWN', timestamp_semantics='DAILY_FEED_LABEL',
                price=None if failed else holding.current_price)

    def evaluated(self, holdings, signals, missing_due, missing_rows, missing_checked_at):
        if not self.ready:
            return
        from scanner.scan_engine import _holding_alert_due, _MISSING_STOP_REASON
        actual_sell = {s['holding_id'] for s in signals if s.get('signal_type') == 'SELL' and 'holding_id' in s}
        actual_missing = {h.id for h in missing_due}
        included = any(s.get('signal_type') == 'HOLDING_STOP_MISSING' for s in signals)
        missing_ids = {h.id for h in missing_rows}
        predictions = {}
        for holding in holdings:
            result = self.results.get(holding.id)
            if result is None:
                continue
            values = self.inputs[holding.id]
            previous = SimpleNamespace(**values['prior_alert'])
            sell_due = False
            if result['severity'] is not None:
                sell_due = _holding_alert_due(previous, result['severity'] or 'LOW',
                                             result['reason'] or '매도 판정',
                                             result['observed_at'].replace(tzinfo=None), values['repeat_hours'])
            elif result['status'] == HoldingStatus.HOLD and previous.last_alert_reason != _MISSING_STOP_REASON:
                previous.last_alert_severity = previous.last_alert_reason = previous.last_alert_at = None
            missing = holding.id in missing_ids and result['severity'] is None
            missing_due_prediction = missing and _holding_alert_due(
                previous, 'INFO', _MISSING_STOP_REASON, missing_checked_at, values['repeat_hours'])
            predictions[holding.id] = (bool(sell_due), bool(missing_due_prediction))
        any_sell = any(v[0] for v in predictions.values())
        any_missing = any(v[1] for v in predictions.values())
        for holding in holdings:
            if holding.id not in predictions:
                continue
            values, result = self.inputs[holding.id], self.results[holding.id]
            sell_due, stop_due = predictions[holding.id]
            predicted_included = holding.id in missing_ids and (any_sell or any_missing)
            actual_included = holding.id in missing_ids and included
            comparison = dict(predicted_sell_due=sell_due, legacy_sell_due=holding.id in actual_sell,
                              predicted_missing_stop_due=stop_due, legacy_missing_stop_due=holding.id in actual_missing,
                              predicted_missing_stop_included=predicted_included,
                              legacy_missing_stop_included=actual_included)
            comparison['matches'] = (sell_due == (holding.id in actual_sell) and
                                     stop_due == (holding.id in actual_missing) and
                                     predicted_included == actual_included)
            payload = {k: result[k] for k in ('reason', 'severity', 'weekly_available',
                       'benchmark_available', 'bar_finality', 'timestamp_semantics', 'price')}
            payload.update(inputs=values['inputs'], lifecycle=values['lifecycle'], comparison=comparison,
                           source_scan_id=self.scan_id, repeat_hours=values['repeat_hours'],
                           prior_alert={k: v.isoformat() if isinstance(v, datetime) else v
                                        for k, v in values['prior_alert'].items()})
            self.observations.append(Observation(
                **{k: values[k] for k in ('id', 'episode_id', 'account_id', 'holding_id', 'market', 'ticker', 'input_version')},
                scan_sequence=self.scan_id, strategy_version=self.version,
                **{k: result[k] for k in ('status', 'data_quality', 'data_as_of', 'observed_at')},
                payload=payload))

    def finish(self):
        if not self.ready:
            return
        for observation in self.observations:
            try:
                self.store.record_observation(observation)
            except Exception as exc:
                logger.warning('Runtime DEGRADED observation scan=%s holding=%s error=%s',
                               self.scan_id, observation.holding_id, type(exc).__name__)
        self.store.finish_capture(self.scan_id, now=datetime.now(timezone.utc))
        summary = self.store.replay()
        summary['comparison_mismatches'] = sum(not o.payload['comparison']['matches'] for o in self.observations)
        logger.info('Runtime shadow scan=%s summary=%s', self.scan_id, summary)
        if summary.get('degraded') or summary['comparison_mismatches']:
            logger.warning('Runtime DEGRADED scan=%s', self.scan_id)


def create_capture(scan_id, session_factory):
    import config
    if not config.AGENT_RUNTIME_ENABLED:
        return None
    try:
        return HoldingCapture(scan_id, session_factory)
    except Exception as exc:
        logger.warning('Runtime DEGRADED initialization scan=%s error=%s', scan_id, type(exc).__name__)
        return None
