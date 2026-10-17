"""킬 스위치, 일일 원장, 감사 로그, 장 시간 계산."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def require_aware(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("시간대가 없는 datetime은 받지 않습니다 (UTC 등 tz-aware로 전달)")
    return now


def trading_day(now: datetime, tz: str) -> date:
    return require_aware(now).astimezone(ZoneInfo(tz)).date()


def is_regular_session(now: datetime, tz: str, holidays: tuple[date, ...] = ()) -> bool:
    """미국 정규장(평일 09:30~16:00 현지시각). 반일장은 처리하지 않음."""
    local = require_aware(now).astimezone(ZoneInfo(tz))
    if local.weekday() >= 5 or local.date() in holidays:
        return False
    return REGULAR_OPEN <= local.time() < REGULAR_CLOSE


class KillSwitch:
    """파일이 존재하면 모든 신규 주문을 막는다. 텔레그램 명령·cron·수동으로 만들 수 있다."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def is_active(self) -> bool:
        return self.path.exists()

    def reason(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "(사유 없음)"
        except FileNotFoundError:
            return ""

    def activate(self, reason: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(reason, encoding="utf-8")

    def deactivate(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class RecentOrder:
    symbol: str
    side: str
    ts: datetime
    decision_id: str


@dataclass(frozen=True)
class LedgerView:
    orders_today: int
    buy_notional_today: float
    new_positions_today: frozenset[str]
    recent: tuple[RecentOrder, ...]

    def executed_ids(self) -> set[str]:
        return {r.decision_id for r in self.recent}


class Ledger:
    """거래일별 주문 수·매수 금액·신규 종목과 최근 주문 이력을 파일에 보관.

    단일 프로세스 사용을 전제로 한다. 여러 프로세스가 동시에 쓰면 파일 잠금이 필요하다.
    """

    RECENT_KEEP = timedelta(days=7)

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {"day": None, "orders": 0, "buy_notional": 0.0, "new_positions": [], "recent": []}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)  # 중간에 죽어도 원장이 깨지지 않게

    def _recent(self) -> tuple[RecentOrder, ...]:
        return tuple(
            RecentOrder(r["symbol"], r["side"], datetime.fromisoformat(r["ts"]), r["decision_id"])
            for r in self._data.get("recent", [])
        )

    def view(self, day: date) -> LedgerView:
        same_day = self._data.get("day") == day.isoformat()
        return LedgerView(
            orders_today=self._data["orders"] if same_day else 0,
            buy_notional_today=float(self._data["buy_notional"]) if same_day else 0.0,
            new_positions_today=frozenset(self._data["new_positions"]) if same_day else frozenset(),
            recent=self._recent(),
        )

    def record(self, *, symbol: str, side: str, qty: int, price: float, new_position: bool,
               decision_id: str, now: datetime, day: date) -> None:
        if self._data.get("day") != day.isoformat():
            self._data.update(day=day.isoformat(), orders=0, buy_notional=0.0, new_positions=[])
        if side == "BUY":
            self._data["orders"] += 1
            self._data["buy_notional"] = round(self._data["buy_notional"] + qty * price, 4)
            if new_position and symbol not in self._data["new_positions"]:
                self._data["new_positions"].append(symbol)
        cutoff = now - self.RECENT_KEEP
        recent = [r for r in self._data.get("recent", []) if datetime.fromisoformat(r["ts"]) >= cutoff]
        recent.append({"symbol": symbol, "side": side, "ts": now.isoformat(), "decision_id": decision_id})
        self._data["recent"] = recent
        self._save()


class AuditLog:
    """모든 판정과 실행을 JSON Lines로 남긴다. API 키 같은 비밀값은 넣지 않는다.

    경로가 빈 문자열이면 아무것도 쓰지 않는다 (표시 전용 판정용).
    """

    def __init__(self, path: str | Path):
        self.path = Path(path) if str(path) else None

    def write(self, event: str, payload: dict, now: datetime) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = {"ts": now.isoformat(), "event": event, **payload}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
