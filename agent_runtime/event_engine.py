"""Transaction-owning storage primitives. Caller supplies classifications and time.

Every method uses its own short session. Never pass the scanner's live session.
There is no sender, heartbeat, detector, or broker integration here.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Sequence
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from database.models import AgentObservation, AgentRuntimeState, AgentEvent, AgentDelivery
from .schemas import (ApplyResult, DataQuality, DeliveryClaim, ErrorCode, EventInput,
                      Observation, RuntimeSettings, canonical_json, utc_naive)


class IdempotencyConflict(ValueError):
    """A stable input identity was reused for different evidence."""


class ConcurrencyConflict(RuntimeError):
    """Reload committed state before retrying the complete operation."""


class StaleObservation(ValueError):
    """An old input cannot overwrite the current cursor/data timestamp."""


class RuntimeStore:
    def __init__(self, session_factory, settings=None):
        self.session_factory = session_factory
        self.settings = settings if settings is not None else RuntimeSettings.from_config()

    @contextmanager
    def _transaction(self):
        session = self.session_factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    def record_observation(self, observation: Observation):
        if not self.settings.enabled:
            return None
        values = observation.values()
        serializable = {key: value.isoformat() if isinstance(value, datetime) else value
                        for key, value in values.items()}
        fingerprint = hashlib.sha256(canonical_json(serializable).encode('utf-8')).hexdigest()
        try:
            with self._transaction() as session:
                existing = session.get(AgentObservation, observation.id)
                if existing is not None:
                    self._check_fingerprint(existing, fingerprint)
                    return existing.id
                session.add(AgentObservation(**values, fingerprint=fingerprint))
                session.flush()
            return observation.id
        except IntegrityError as exc:
            # A concurrent identical insert may have won; inspect only after rollback.
            with self._transaction() as session:
                existing = session.get(AgentObservation, observation.id)
                if existing is None:
                    raise IdempotencyConflict("observation cursor is already occupied") from exc
                self._check_fingerprint(existing, fingerprint)
                return existing.id

    @staticmethod
    def _check_fingerprint(existing, fingerprint):
        if existing.fingerprint != fingerprint:
            raise IdempotencyConflict("observation identity has different contents")

    def get_state(self, episode_id):
        if not self.settings.enabled:
            return None
        with self._transaction() as session:
            row = session.get(AgentRuntimeState, episode_id)
            return ({column.name: getattr(row, column.name) for column in row.__table__.columns}
                    if row is not None else None)

    def list_events(self, episode_id: str, *, after_sequence: int = 0, limit: int = 100):
        if not self.settings.enabled:
            return []
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("invalid sequence")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        with self._transaction() as session:
            rows = session.query(AgentEvent).filter(
                AgentEvent.episode_id == episode_id, AgentEvent.sequence > after_sequence,
            ).order_by(AgentEvent.sequence).limit(limit)
            return [{column.name: getattr(row, column.name) for column in row.__table__.columns}
                    for row in rows]

    def apply_observation(self, observation_id: str, *, expected_version: int,
                          events: Sequence[EventInput] = (), now: datetime):
        """Atomically advance state and store caller-classified facts/outbox.

        Version 0 means a new episode. An already-current observation returns its
        committed events (first application wins), even with an old expected version.
        Older cursors are explicitly rejected, not silently replayed or reclassified.
        """
        if not self.settings.enabled:
            return None
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("invalid expected version")
        timestamp = utc_naive(now)
        inputs = [(event, event.values()) for event in events]
        if len({event.key for event, _ in inputs}) != len(inputs):
            raise ValueError("duplicate event key")
        try:
            with self._transaction() as session:
                observation = session.get(AgentObservation, observation_id)
                if observation is None:
                    raise ValueError("unknown observation")
                if timestamp < observation.observed_at:
                    raise ValueError("processing time precedes observation")
                previous = session.get(AgentRuntimeState, observation.episode_id)
                if previous and previous.observation_id == observation_id:
                    ids = tuple(row.id for row in session.query(AgentEvent).filter_by(
                        observation_id=observation_id).order_by(AgentEvent.sequence))
                    return ApplyResult(previous.version, ids, replayed=True)
                version = previous.version if previous else 0
                if version != expected_version:
                    raise ConcurrencyConflict("runtime state version changed")
                if previous:
                    old_input = session.get(AgentObservation, previous.observation_id)
                    if (observation.account_id, observation.holding_id, observation.market,
                        observation.ticker) != (old_input.account_id, old_input.holding_id,
                                                old_input.market, old_input.ticker):
                        raise ValueError("episode cannot change portfolio identity")
                    if (observation.scan_sequence, observation.revision) <= (
                            previous.scan_sequence, previous.observation_revision):
                        raise StaleObservation("observation cursor is older than current state")
                    if (observation.data_as_of is not None and previous.data_as_of is not None
                            and observation.data_as_of < previous.data_as_of):
                        raise StaleObservation("data timestamp would move backwards")
                sequence = previous.event_sequence if previous else 0
                last_valid = previous.last_valid_status if previous else None
                if observation.data_quality == DataQuality.AVAILABLE.value:
                    last_valid = observation.status
                state_values = dict(
                    version=version + 1, event_sequence=sequence + len(inputs),
                    observation_id=observation_id, scan_sequence=observation.scan_sequence,
                    observation_revision=observation.revision,
                    data_as_of=observation.data_as_of or (previous.data_as_of if previous else None),
                    current_status=observation.status, last_valid_status=last_valid,
                    data_quality=observation.data_quality, updated_at=timestamp,
                )
                if previous is None:
                    session.add(AgentRuntimeState(episode_id=observation.episode_id, **state_values))
                    session.flush()  # Unique episode PK arbitrates simultaneous first claims.
                else:
                    changed = session.query(AgentRuntimeState).filter_by(
                        episode_id=observation.episode_id, version=expected_version,
                    ).update(state_values, synchronize_session=False)
                    if changed != 1:
                        raise ConcurrencyConflict("runtime state version changed")
                event_ids = []
                for offset, (event, event_values) in enumerate(inputs, 1):
                    event_id = str(uuid4())
                    session.add(AgentEvent(id=event_id, observation_id=observation_id,
                                           episode_id=observation.episode_id,
                                           sequence=sequence + offset, created_at=timestamp,
                                           **event_values))
                    session.flush()
                    event_ids.append(event_id)
                    if self.settings.delivery_enabled:
                        for target in event.targets:
                            session.add(AgentDelivery(
                                id=str(uuid4()), event_id=event_id, channel=target.channel,
                                destination_key=target.destination_key, status="PENDING",
                                attempts=0, next_attempt_at=timestamp, created_at=timestamp,
                            ))
                session.flush()
                return ApplyResult(version + 1, tuple(event_ids))
        except IntegrityError as exc:
            raise ConcurrencyConflict("runtime write conflicted; transaction rolled back") from exc

    @staticmethod
    def _eligible(now):
        return or_(and_(AgentDelivery.status == "PENDING", AgentDelivery.next_attempt_at <= now),
                   and_(AgentDelivery.status == "SENDING", AgentDelivery.lease_expires_at <= now))

    def claim_due(self, *, now: datetime, limit: int = 20):
        """Claim only; caller must not perform I/O until this method commits."""
        if not (self.settings.enabled and self.settings.delivery_enabled):
            return []
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        timestamp = utc_naive(now)
        expiry = timestamp + timedelta(seconds=self.settings.lease_seconds)
        with self._transaction() as session:
            session.query(AgentDelivery).filter(
                self._eligible(timestamp), AgentDelivery.attempts >= self.settings.max_attempts,
            ).update(dict(status="FAILED", lease_token=None, lease_expires_at=None,
                          error_code="ATTEMPTS_EXHAUSTED"), synchronize_session=False)
            candidates = session.query(AgentDelivery.id).filter(
                self._eligible(timestamp), AgentDelivery.attempts < self.settings.max_attempts,
            ).order_by(AgentDelivery.next_attempt_at, AgentDelivery.id).limit(limit).all()
            claims = []
            for (delivery_id,) in candidates:
                token = str(uuid4())
                changed = session.query(AgentDelivery).filter(
                    AgentDelivery.id == delivery_id, self._eligible(timestamp),
                    AgentDelivery.attempts < self.settings.max_attempts,
                ).update(dict(status="SENDING", attempts=AgentDelivery.attempts + 1,
                              lease_token=token, lease_expires_at=expiry), synchronize_session=False)
                if not changed:
                    continue
                row = session.get(AgentDelivery, delivery_id)
                claims.append(DeliveryClaim(row.id, row.event_id, row.channel, row.destination_key,
                                            row.attempts, token, expiry.replace(tzinfo=timezone.utc)))
            return claims

    def _owned(self, session, delivery_id, token, timestamp):
        return session.query(AgentDelivery).filter(
            AgentDelivery.id == delivery_id, AgentDelivery.status == "SENDING",
            AgentDelivery.lease_token == token, AgentDelivery.lease_expires_at > timestamp,
        )

    def mark_sent(self, delivery_id: str, token: str, *, now: datetime) -> bool:
        if not (self.settings.enabled and self.settings.delivery_enabled):
            return False
        timestamp = utc_naive(now)
        with self._transaction() as session:
            return self._owned(session, delivery_id, token, timestamp).update(
                dict(status="SENT", sent_at=timestamp, error_code=None,
                     lease_token=None, lease_expires_at=None), synchronize_session=False) == 1

    def mark_failed(self, delivery_id: str, token: str, *, now: datetime,
                    error_code: ErrorCode = ErrorCode.SEND_FAILED) -> bool:
        if not (self.settings.enabled and self.settings.delivery_enabled):
            return False
        timestamp = utc_naive(now)
        code = ErrorCode(error_code).value
        with self._transaction() as session:
            owned = self._owned(session, delivery_id, token, timestamp)
            row = owned.first()
            if row is None:
                return False
            exhausted = row.attempts >= self.settings.max_attempts
            delay = min(self.settings.retry_max_seconds,
                        self.settings.retry_seconds * 2 ** (row.attempts - 1))
            return owned.update(dict(
                status="FAILED" if exhausted else "PENDING",
                next_attempt_at=timestamp + timedelta(seconds=delay), error_code=code,
                lease_token=None, lease_expires_at=None,
            ), synchronize_session=False) == 1

    def list_deliveries(self, *, status=None, limit=100):
        if not self.settings.enabled:
            return []
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        if status is not None and status not in ("PENDING", "SENDING", "SENT", "FAILED"):
            raise ValueError("invalid delivery status")
        with self._transaction() as session:
            query = session.query(AgentDelivery)
            if status is not None:
                query = query.filter_by(status=status)
            return [{column.name: getattr(row, column.name) for column in row.__table__.columns}
                    for row in query.order_by(AgentDelivery.created_at, AgentDelivery.id).limit(limit)]
