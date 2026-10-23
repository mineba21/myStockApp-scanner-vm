"""Read-only brokerage snapshots for B. No orders, holdings writes, or delivery.

Broker identities are source-scoped; the integer references are diagnostic, not
foreign keys into the manual portfolio. Runtime identity uses the full key hash.
"""
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path

from database.models import AgentScanCapture
from .schemas import Observation, DataQuality, HoldingStatus, canonical_json

logger = logging.getLogger(__name__)
TARGETS = {'account1': 'US', 'account4': 'KR'}


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def load_snapshot():
    """Fresh required balances only; never turn a broker error into an empty list."""
    from trading.kiwoom_readonly import load_profile_configs, KiwoomReadOnlyClient, KiwoomError
    from web.kiwoom_holdings import (
        _get_token, _is_invalid_token_error, map_kiwoom_holding, map_kiwoom_overseas_holding,
    )
    configs = load_profile_configs()
    if not set(TARGETS).issubset(configs):
        raise ValueError('broker snapshot profiles incomplete')
    rows = []
    for profile, market in TARGETS.items():
        config = configs[profile]
        client = KiwoomReadOnlyClient(config)
        fetch = client.get_overseas_account_balance if market == 'US' else client.get_account_balance
        try:
            report = fetch(_get_token(profile, config, client))
        except KiwoomError as exc:
            if not _is_invalid_token_error(exc):
                raise
            report = fetch(_get_token(profile, config, client, force=True))
        if report.get('complete') is not True or not isinstance(report.get('holdings'), list):
            raise ValueError('broker snapshot holdings missing')
        mapper = map_kiwoom_overseas_holding if market == 'US' else map_kiwoom_holding
        stamp = datetime.now(timezone.utc).isoformat()
        for item in report['holdings']:
            # UI mappers tolerate missing/non-numeric amounts by showing zero.
            # An observation inventory must instead distinguish bad input from absence.
            quantity_key, price_key = ('poss_qty', 'frgn_stk_book_uv') if market == 'US' else ('rmnd_qty', 'pur_pric')
            quantity = float(str(item.get(quantity_key, '')).replace(',', '').strip())
            if not math.isfinite(quantity) or quantity < 0:
                raise ValueError('invalid raw broker quantity')
            if quantity > 0:
                price = float(str(item.get(price_key, '')).replace(',', '').strip())
                if not math.isfinite(price) or price <= 0:
                    raise ValueError('invalid raw broker cost basis')
            rows.append(mapper(profile, item, updated_at=stamp))
    return rows


class BrokerCapture:
    def __init__(self, scan_id, store, version):
        self.scan_id, self.store, self.version = scan_id, store, version
        self.rows = []
        self._observations = None

    def prepare(self):
        if os.getenv('KIWOOM_WEB_ENABLED', 'false').lower() != 'true':
            return [], None
        try:
            return self._prepare()
        except Exception as exc:
            self.rows = []
            logger.warning('Runtime DEGRADED broker snapshot scan=%s error=%s',
                           self.scan_id, type(exc).__name__)
            # A durable missing item blocks replay, including first-run failures.
            # It must not be silently retried using a different historical balance.
            return [dict(id=f'scan-{self.scan_id}-broker-snapshot-unavailable',
                         episode_id='broker-snapshot', holding_id=1, source='kiwoom')], {
                             'status': 'UNAVAILABLE', 'keys': []}

    def _prepare(self):
        raw = load_snapshot()
        with self.store._transaction() as session:
            history = [(r.scan_id, json.loads(r.expected_json)) for r in
                       session.query(AgentScanCapture).filter(
                           AgentScanCapture.scan_id < self.scan_id).order_by(AgentScanCapture.scan_id)]
        known_ids = {}
        for _, body in history:
            for item in body['items']:
                if item.get('broker_key'):
                    known_ids[item['holding_id']] = item['broker_key']
        rows, expected, keys = [], [], set()
        for row in raw:
            profile, market, ticker = row.get('account_profile'), row.get('market'), row.get('ticker')
            if row.get('source') != 'kiwoom' or TARGETS.get(profile) != market:
                raise ValueError('unexpected broker snapshot scope')
            if not isinstance(ticker, str) or not ticker or len(ticker) > 20 or ticker != ticker.strip():
                raise ValueError('invalid broker ticker')
            quantity, price = float(row['quantity']), float(row['avg_price'])
            if not math.isfinite(quantity) or not math.isfinite(price) or quantity < 0 or price < 0:
                raise ValueError('invalid broker balance')
            if quantity == 0:
                continue
            if price == 0:
                raise ValueError('positive broker holding without cost basis')
            key = f'kiwoom:{profile}:{market}:{ticker}'
            if key in keys:
                raise ValueError('duplicate broker holding')
            keys.add(key)
            holding_id = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 2147483646 + 1
            if holding_id in known_ids and known_ids[holding_id] != key:
                raise ValueError('broker diagnostic identity collision')
            known_ids[holding_id] = key
            absent = max((number for number, body in history
                          if body.get('broker_snapshot', {}).get('status') == 'COMPLETE'
                          and key not in body['broker_snapshot']['keys']), default=0)
            lifecycle = dict(source='kiwoom', broker_key=key, last_observed_absence=absent,
                             transaction_history_available=False)
            values = dict(id=f'scan-{self.scan_id}-broker-{digest(dict(key=key))}-r0',
                          episode_id=digest(lifecycle), holding_id=holding_id,
                          account_id=int(profile.removeprefix('account')),
                          market=market, ticker=ticker,
                          input_version=digest(dict(quantity=quantity, avg_price=price,
                                                    stop_loss=None)), lifecycle=lifecycle)
            rows.append((dict(row), values))
            expected.append({**{k: values[k] for k in ('id', 'episode_id', 'holding_id')},
                             'source': 'kiwoom', 'broker_key': key})
        self.rows = sorted(rows, key=lambda item: item[1]['id'])
        expected.sort(key=lambda item: item['id'])
        return expected, dict(status='COMPLETE', keys=sorted(keys))

    def evaluate(self):
        if self._observations is not None:
            return self._observations
        if not self.rows:
            self._observations = []
            return self._observations
        from web.kiwoom_sell_analysis import _analyze_one
        from scanner.market_analysis import get_benchmark_close
        benchmarks = {}
        for row, _ in self.rows:
            market = row['market']
            if market not in benchmarks:
                try:
                    benchmarks[market] = get_benchmark_close(market)
                except Exception:
                    benchmarks[market] = None
        from web import kiwoom_sell_analysis
        version = digest(dict(manual_version=self.version, broker_analysis=
                              hashlib.sha256(Path(kiwoom_sell_analysis.__file__).read_bytes()).hexdigest()))
        observations = []
        for row, values in self.rows:
            try:
                _, result = _analyze_one(row, benchmarks[row['market']], observation_metadata=True)
                failed = result['sell_status'] == 'CHECK_FAILED'
                quality = DataQuality.UNAVAILABLE if failed else (
                    DataQuality.AVAILABLE if result['weekly_available'] and result['benchmark_available']
                    else DataQuality.DEGRADED)
                observations.append(Observation(
                    **{k: v for k, v in values.items() if k != 'lifecycle'},
                    scan_sequence=self.scan_id,
                    strategy_version=version,
                    observed_at=datetime.fromisoformat(result['sell_checked_at']),
                    data_as_of=datetime.fromisoformat(result['data_as_of']) if result['data_as_of'] else None,
                    status=None if failed else HoldingStatus(result['sell_status']),
                    data_quality=quality,
                    payload=dict(source='kiwoom', account_profile=row['account_profile'],
                        lifecycle=values['lifecycle'],
                        inputs=dict(quantity=row['quantity'], avg_price=row['avg_price'], current_stop_loss=None),
                        reason=result['sell_reason'], severity=result['sell_severity'],
                        price=result['observation_price'],
                        balance_observed_at=row.get('price_updated_at'),
                        weekly_available=result['weekly_available'], benchmark_available=result['benchmark_available'],
                        bar_finality='UNKNOWN', timestamp_semantics='DAILY_FEED_LABEL',
                        source_scan_id=self.scan_id,
                        comparison=dict(eligible=False, matches=None,
                                        reason='BROKER_HAS_NO_LEGACY_NOTIFICATION_SELECTION'))))
            except Exception as exc:
                logger.warning('Runtime DEGRADED broker observation scan=%s error=%s',
                               self.scan_id, type(exc).__name__)
                # Expected inventory persists: no observation becomes a visible gap.
        self._observations = observations
        return observations
