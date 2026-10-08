"""Committed observation replay; no notification or broker imports."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging

from sqlalchemy import or_, and_

from database.models import AgentScanCapture, AgentObservation, AgentRuntimeState, ScanLog
from .event_engine import RuntimeStore, ConcurrencyConflict, StaleObservation, IdempotencyConflict
from .schemas import EventInput, EventType, Severity, RuntimeSettings, canonical_json, utc_naive

logger = logging.getLogger(__name__)


def classify(current, previous=None, *, gaps=(), last_valid=None):
    """Classify persisted judgments, never recompute the investment strategy."""
    payload = json.loads(current.payload_json)
    causes = []
    if previous:
        if previous.strategy_version != current.strategy_version:
            causes.append('STRATEGY_CHANGED')
        if previous.input_version != current.input_version:
            causes.append('INPUT_CHANGED')
    if gaps:
        causes.append('GAP_RESET')
    evidence = dict(observation_id=current.id, scan_id=current.scan_sequence,
                    previous_status=previous.status if previous else None,
                    current_status=current.status, reason=payload.get('reason'),
                    data_as_of=current.data_as_of.isoformat() if current.data_as_of else None,
                    strategy_version=current.strategy_version, input_version=current.input_version,
                    data_quality=current.data_quality, causes=causes, gap_scan_ids=list(gaps),
                    intermediate_changes_may_be_missing=bool(gaps),
                    last_valid_status=last_valid.status if last_valid else None,
                    last_valid_data_as_of=last_valid.data_as_of.isoformat() if last_valid and last_valid.data_as_of else None,
                    comparison_spans_unavailable_data=bool(previous and previous.status is None),
                    comparison=payload.get('comparison', {}))
    severity = Severity(payload.get('severity') or 'INFO')
    facts = []
    def add(key, kind, level=severity):
        facts.append(EventInput(key, kind, level, evidence))
    if causes:
        add('baseline', EventType.BASELINE_RESET)
    elif previous and current.status is not None:
        prior_status = previous.status if previous.status is not None else (last_valid.status if last_valid else None)
        if prior_status is not None and current.status != prior_status:
            add('state', EventType.HOLDING_STATE_CHANGED)
    if (previous and previous.data_quality != current.data_quality) or (
            not previous and current.data_quality != 'AVAILABLE'):
        add('quality', EventType.DATA_QUALITY_CHANGED, Severity.INFO)
    comparison = payload.get('comparison', {})
    if comparison.get('predicted_sell_due') or comparison.get('predicted_missing_stop_included'):
        add('alert', EventType.INITIAL_ALERT if previous is None else EventType.HOLDING_ALERT_DUE)
    return tuple(facts)


class ShadowStore(RuntimeStore):
    def __init__(self, session_factory, settings=None):
        super().__init__(session_factory, replace(settings or RuntimeSettings.from_config(),
                                                delivery_enabled=False))

    def begin_capture(self, scan_id, expected, *, now):
        if not self.settings.enabled:
            return
        body = canonical_json({'items': expected})
        with self._transaction() as session:
            existing = session.get(AgentScanCapture, scan_id)
            if existing:
                if existing.expected_json != body:
                    raise IdempotencyConflict('capture inventory changed')
                return
            session.add(AgentScanCapture(scan_id=scan_id, expected_json=body,
                                         status='CAPTURING', created_at=utc_naive(now),
                                         audit_json='{}'))

    def finish_capture(self, scan_id, *, now):
        if not self.settings.enabled:
            return
        with self._transaction() as session:
            capture = session.get(AgentScanCapture, scan_id)
            if capture is None:
                raise ValueError('capture inventory missing')
            expected = json.loads(capture.expected_json)['items']
            scan = session.get(ScanLog, scan_id)
            if scan and scan.status != 'DONE':
                raise ValueError('source scan has not committed DONE')
            ids = {r.id for r in session.query(AgentObservation.id).filter_by(scan_sequence=scan_id)}
            capture.status = 'COMPLETE' if all(e['id'] in ids for e in expected) else 'GAP'
            capture.finished_at = utc_naive(now)
            audit = json.loads(capture.audit_json)
            for observation in session.query(AgentObservation).filter_by(scan_sequence=scan_id):
                state = session.get(AgentRuntimeState, observation.episode_id)
                if state and (observation.scan_sequence, observation.revision) < (state.scan_sequence, state.observation_revision):
                    audit[observation.id] = 'STALE_CURSOR'
            capture.audit_json = canonical_json(audit)

    def reset_gap(self, scan_id, reason, *, now):
        """Explicit operator acknowledgment; missing evidence remains missing."""
        if not self.settings.enabled:
            return
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
            raise ValueError('a reset reason of 1..200 characters is required')
        with self._transaction() as session:
            capture = session.get(AgentScanCapture, scan_id)
            if capture is None or capture.status == 'COMPLETE':
                raise ValueError('capture does not have a gap')
            scan = session.get(ScanLog, scan_id)
            if scan and scan.status == 'RUNNING':
                raise ValueError('cannot reset a running scan; reconcile its status first')
            if capture.reset_at is not None:
                return
            capture.reset_at = utc_naive(now)
            capture.reset_reason = reason.strip()
            capture.status = 'GAP'

    def _inventory(self):
        with self._transaction() as session:
            manifests = []
            for capture in session.query(AgentScanCapture).order_by(AgentScanCapture.scan_id):
                scan = session.get(ScanLog, capture.scan_id)
                manifests.append(dict(scan_id=capture.scan_id, status=capture.status,
                                      source_status=scan.status if scan else 'UNKNOWN',
                                      reset=capture.reset_at is not None,
                                      expected=json.loads(capture.expected_json)['items'],
                                      audit=json.loads(capture.audit_json)))
            return manifests

    def _mark_stale(self, observation):
        with self._transaction() as session:
            row = session.query(AgentScanCapture).filter_by(scan_id=observation.scan_sequence).with_for_update().one()
            audit = json.loads(row.audit_json)
            audit[observation.id] = 'STALE_DATA'
            row.audit_json = canonical_json(audit)

    def recover_captures(self):
        """Recover a crash after observation commits but before the completion marker."""
        if not self.settings.enabled:
            return
        with self._transaction() as session:
            rows = session.query(AgentScanCapture.scan_id, ScanLog.status).join(
                ScanLog, ScanLog.id == AgentScanCapture.scan_id,
            ).filter(AgentScanCapture.status != 'COMPLETE').all()
        for scan_id, status in rows:
            if status == 'DONE':
                self.finish_capture(scan_id, now=datetime.now(timezone.utc))
            elif status == 'ERROR':
                with self._transaction() as session:
                    session.query(AgentScanCapture).filter_by(scan_id=scan_id).update(
                        {'status': 'GAP'}, synchronize_session=False)

    def replay(self, *, limit=200, now=None):
        if not self.settings.enabled:
            return {'enabled': False}
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be 1..1000')
        now = now or datetime.now(timezone.utc)
        self.recover_captures()
        manifests = self._inventory()
        by_scan = {m['scan_id']: m for m in manifests}
        result = dict(processed=0, blocked=0, stale=0, conflicts=0, failed=0)
        with self._transaction() as session:
            rows = session.query(AgentObservation).outerjoin(
                AgentRuntimeState, AgentRuntimeState.episode_id == AgentObservation.episode_id,
            ).filter(or_(AgentRuntimeState.episode_id.is_(None),
                         AgentObservation.scan_sequence > AgentRuntimeState.scan_sequence,
                         and_(AgentObservation.scan_sequence == AgentRuntimeState.scan_sequence,
                              AgentObservation.revision > AgentRuntimeState.observation_revision)),
            ).order_by(AgentObservation.scan_sequence, AgentObservation.revision).all()
            session.expunge_all()
        attempted = 0
        for row in rows:
            manifest = by_scan.get(row.scan_sequence)
            if manifest is None:
                result['blocked'] += 1
                continue
            if row.id in manifest['audit']:
                result['stale'] += 1
                continue
            relevant = [m for m in manifests if m['scan_id'] <= row.scan_sequence and
                        any(e['episode_id'] == row.episode_id for e in m['expected'])]
            if any(m['status'] != 'COMPLETE' and not m['reset'] for m in relevant):
                result['blocked'] += 1
                continue
            if attempted >= limit:
                break
            attempted += 1
            try:
                state = self.get_state(row.episode_id)
                with self._transaction() as session:
                    previous = session.get(AgentObservation, state['observation_id']) if state else None
                    valid = session.query(AgentObservation).filter(
                        AgentObservation.episode_id == row.episode_id,
                        AgentObservation.scan_sequence <= state['scan_sequence'],
                        AgentObservation.data_quality == 'AVAILABLE',
                    ).order_by(AgentObservation.scan_sequence.desc(), AgentObservation.revision.desc()).first() if state else None
                    if valid and valid is not previous:
                        session.expunge(valid)
                    if previous:
                        session.expunge(previous)
                gaps = [m['scan_id'] for m in relevant if m['reset'] and
                        (not state or m['scan_id'] > state['scan_sequence'])]
                self.apply_observation(row.id, expected_version=state['version'] if state else 0,
                                       events=classify(row, previous, gaps=gaps, last_valid=valid), now=now)
                result['processed'] += 1
            except StaleObservation:
                self._mark_stale(row)
                result['stale'] += 1
            except ConcurrencyConflict:
                result['conflicts'] += 1
                break
            except Exception as exc:
                result['failed'] += 1
                logger.warning('Runtime DEGRADED replay scan=%s error=%s', row.scan_sequence, type(exc).__name__)
                # Do not skip a failed intermediate observation of the same episode.
                break
        result['pending'] = len(rows) - result['processed'] - result['stale']
        result['degraded'] = bool(result['pending'] or
                                  any(m['status'] != 'COMPLETE' and not m['reset'] for m in manifests))
        return result

    def report(self):
        if not self.settings.enabled:
            return {'enabled': False}
        manifests = self._inventory()
        with self._transaction() as session:
            rows = session.query(AgentObservation).all()
            ids = {r.id for r in rows}
            comparisons = [json.loads(r.payload_json).get('comparison', {}) for r in rows]
            states = {s.episode_id: (s.scan_sequence, s.observation_revision)
                      for s in session.query(AgentRuntimeState)}
            audited = {key for m in manifests for key in m['audit']}
            pending = sum(r.id not in audited and (r.scan_sequence, r.revision) >
                          states.get(r.episode_id, (0, 0)) for r in rows)
            missing = {str(m['scan_id']): [e['id'] for e in m['expected'] if e['id'] not in ids]
                       for m in manifests}
            return dict(captures=len(manifests), observations=len(rows),
                        pending=pending,
                        degraded=bool(pending or any(m['status'] != 'COMPLETE' and not m['reset'] for m in manifests)),
                        mismatches=sum(c.get('matches') is False for c in comparisons),
                        gaps=[dict(scan_id=m['scan_id'], status=m['status'], reset=m['reset'],
                                   source_status=m['source_status'], missing=missing[str(m['scan_id'])])
                              for m in manifests if m['status'] != 'COMPLETE'],
                        stale_audit=sum(len(m['audit']) for m in manifests))
