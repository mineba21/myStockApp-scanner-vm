"""Contracts only. No strategy, network, or order execution code."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import re
from typing import Any, Dict, Optional, Tuple


class HoldingStatus(str, Enum):
    HOLD = "HOLD"
    CAUTION = "CAUTION"
    REVIEW = "REVIEW"
    SELL_REQUIRED = "SELL_REQUIRED"


class DataQuality(str, Enum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class EventType(str, Enum):
    INITIAL_ALERT = "INITIAL_ALERT"
    HOLDING_STATE_CHANGED = "HOLDING_STATE_CHANGED"
    HOLDING_ALERT_DUE = "HOLDING_ALERT_DUE"
    DATA_QUALITY_CHANGED = "DATA_QUALITY_CHANGED"
    BASELINE_RESET = "BASELINE_RESET"


class Severity(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ErrorCode(str, Enum):
    SEND_FAILED = "SEND_FAILED"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"


def utc_naive(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def identifier(value: str, name: str, limit: int = 128) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or value != value.strip():
        raise ValueError(f"invalid {name}")
    return value


def canonical_json(payload: Dict[str, Any]) -> str:
    def check(value):
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("JSON keys must be strings")
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
        elif value is not None and type(value) not in (str, int, float, bool):
            raise ValueError("unsupported JSON value")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    try:
        check(payload)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, RecursionError) as exc:
        raise ValueError("invalid JSON payload") from exc


@dataclass(frozen=True)
class Observation:
    id: str
    episode_id: str
    account_id: int
    holding_id: int
    market: str
    ticker: str
    scan_sequence: int
    observed_at: datetime
    data_as_of: Optional[datetime]
    strategy_version: str
    input_version: str
    status: Optional[HoldingStatus]
    data_quality: DataQuality
    revision: int = 0
    payload: Dict[str, Any] = field(default_factory=dict)

    def values(self) -> dict:
        for key in ("id", "episode_id", "strategy_version", "input_version"):
            identifier(getattr(self, key), key)
        identifier(self.ticker, "ticker", 20)
        if self.market not in ("KR", "US"):
            raise ValueError("unsupported market")
        for key in ("account_id", "holding_id", "scan_sequence", "revision"):
            value = getattr(self, key)
            if type(value) is not int or value < (0 if key == "revision" else 1):
                raise ValueError(f"invalid {key}")
        quality = DataQuality(self.data_quality)
        status = HoldingStatus(self.status).value if self.status is not None else None
        if (quality == DataQuality.UNAVAILABLE) != (status is None):
            raise ValueError("only unavailable observations must have no status")
        observed_at = utc_naive(self.observed_at)
        data_as_of = utc_naive(self.data_as_of) if self.data_as_of is not None else None
        if status is not None and data_as_of is None:
            raise ValueError("a judgment needs a data timestamp")
        if data_as_of is not None and data_as_of > observed_at:
            raise ValueError("data timestamp is after observation")
        return dict(id=self.id, episode_id=self.episode_id, account_id=self.account_id,
                    holding_id=self.holding_id, market=self.market, ticker=self.ticker,
                    scan_sequence=self.scan_sequence, revision=self.revision,
                    observed_at=observed_at, data_as_of=data_as_of,
                    strategy_version=self.strategy_version, input_version=self.input_version,
                    status=status, data_quality=quality.value,
                    payload_json=canonical_json(self.payload))


@dataclass(frozen=True)
class DeliveryTarget:
    channel: str
    destination_key: str

    def validate(self):
        if self.channel not in ("telegram", "slack"):
            raise ValueError("unsupported channel")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", self.destination_key):
            raise ValueError("destination must be an opaque config key, not a URL/token")


@dataclass(frozen=True)
class EventInput:
    key: str
    event_type: EventType
    severity: Severity
    payload: Dict[str, Any] = field(default_factory=dict)
    targets: Tuple[DeliveryTarget, ...] = ()

    def values(self):
        identifier(self.key, "event key")
        for target in self.targets:
            target.validate()
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("duplicate delivery target")
        return dict(event_key=self.key, event_type=EventType(self.event_type).value,
                    severity=Severity(self.severity).value, payload_json=canonical_json(self.payload))


@dataclass(frozen=True)
class RuntimeSettings:
    enabled: bool = False
    delivery_enabled: bool = False
    max_attempts: int = 5
    lease_seconds: int = 60
    retry_seconds: int = 30
    retry_max_seconds: int = 3600

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.delivery_enabled) is not bool:
            raise ValueError("enabled flags must be boolean")
        for name, upper in (("max_attempts", 100), ("lease_seconds", 86400),
                            ("retry_seconds", 86400), ("retry_max_seconds", 604800)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"invalid {name}")
        if self.retry_seconds > self.retry_max_seconds:
            raise ValueError("retry cap below initial delay")

    @classmethod
    def from_config(cls):
        import config
        return cls(config.AGENT_RUNTIME_ENABLED, config.AGENT_DELIVERY_ENABLED,
                   config.AGENT_DELIVERY_MAX_ATTEMPTS, config.AGENT_DELIVERY_LEASE_SECONDS,
                   config.AGENT_DELIVERY_RETRY_SECONDS, config.AGENT_DELIVERY_RETRY_MAX_SECONDS)


@dataclass(frozen=True)
class ApplyResult:
    version: int
    event_ids: Tuple[str, ...]
    replayed: bool = False


@dataclass(frozen=True)
class DeliveryClaim:
    id: str
    event_id: str
    channel: str
    destination_key: str
    attempts: int
    token: str
    expires_at: datetime  # aware UTC at the API boundary
