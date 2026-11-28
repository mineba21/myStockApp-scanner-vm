"""자동 손절 테스트 대역: 시계, 브로커, 엔진 조립. 실계좌·네트워크 호출 없음."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.auto_stop.engine import Engine
from trading.auto_stop.quotes import Quote, QuoteUnavailable
from trading.auto_stop.settings import CONTRACTS, Settings
from trading.auto_stop.schema import upgrade_journal
from trading.auto_stop.store import AutoStopStore

NY = ZoneInfo("America/New_York")
LIVE_ENV = {"KIWOOM_WEB_ENABLED": "true", "KIWOOM_TRADING_ENABLED": "true",
            "ACCOUNT1_ORDER_ENABLED": "true", "AUTO_STOP_US_MODE": "LIVE",
            "AUTO_STOP_US_REPLACE_ENABLED": "true"}


def ts(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=NY).timestamp()


MON_10 = ts(2026, 11, 23, 10, 0)     # 월요일 정규장


class Clock:
    def __init__(self, t=MON_10):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec

    def set(self, t):
        self.t = t


class FakeBroker:
    def __init__(self, clock, *, price="50.00", qty=10, sellable=None, avg="60"):
        self.clock = clock
        self.price = Decimal(price)
        self.quotes = []          # 우선 소비할 가격 목록
        self.quote_age = 30
        self.holding = {"T": {"qty": qty, "sellable": qty if sellable is None else sellable,
                              "avg_cost": Decimal(avg)}}
        self.sells, self.cancels, self.quote_calls = [], [], 0
        self.rows = {}
        self.sell_exc = self.cancel_exc = self.quote_exc = self.holdings_exc = None
        self.external = []
        # 다종목 시나리오용 (기본값이면 기존 동작과 같다)
        self.prices = {}          # ticker → 가격
        self.quote_exc_by = {}    # ticker → 예외
        self.exc_seq = []         # 호출 순서대로 소비할 예외(None 이면 정상)
        self.fixed_as_of = {}     # ticker → 고정 시세 시각 (같은 시세 반복)
        self.age_by = {}          # ticker → 시세 나이
        self.latency = []         # 호출마다 순환 적용할 응답 지연(초). FakeClock 을 앞당긴다
        self.quote_log = []       # (ticker, 시작 시각)
        self.http_429 = 0

    def pop_429(self):
        n, self.http_429 = self.http_429, 0
        return n

    # 조회
    def quote(self, ticker, exchange):
        self.quote_calls += 1
        self.quote_log.append((ticker, self.clock()))
        if self.latency:
            self.clock.advance(self.latency[(self.quote_calls - 1) % len(self.latency)])
        if self.exc_seq:
            exc = self.exc_seq.pop(0)
            if exc is not None:
                raise exc
        if ticker in self.quote_exc_by and self.quote_exc_by[ticker] is not None:
            raise self.quote_exc_by[ticker]
        if self.quote_exc:
            raise self.quote_exc
        price = Decimal(self.quotes.pop(0)) if self.quotes else Decimal(self.prices.get(ticker, self.price))
        as_of = self.fixed_as_of.get(ticker, self.clock() - self.age_by.get(ticker, self.quote_age))
        return Quote(ticker, exchange, price, as_of, "FAKE")

    def probe_exchange(self, ticker):
        return self.quote(ticker, "ND")

    def holdings(self):
        if self.holdings_exc:
            raise self.holdings_exc
        return copy.deepcopy(self.holding)

    def day_orders(self, ticker):
        return list(self.external)

    def status(self, order):
        n = order.get("order_number")
        return dict(self.rows[n]) if n and n in self.rows else None

    # 전송
    def sell(self, ticker, exchange, qty, price):
        if self.sell_exc:
            raise self.sell_exc
        n = str(1000 + len(self.sells))
        self.sells.append({"ticker": ticker, "qty": qty, "price": price, "number": n})
        self.rows[n] = {"order_number": n, "ticker": ticker, "side": "SELL", "quantity": qty,
                        "state": "OPEN", "filled_quantity": 0, "remaining_quantity": qty}
        return {"order_number": n}

    def cancel(self, ticker, exchange, number):
        if self.cancel_exc:
            raise self.cancel_exc
        self.cancels.append(number)
        return {"order_number": "9" + number}

    # 테스트 조작
    def fill(self, n, filled, state=None):
        row = self.rows[n]
        q = row["quantity"]
        delta = filled - row["filled_quantity"]
        row["filled_quantity"] = filled
        row["remaining_quantity"] = 0 if state == "CANCELLED" else q - filled
        row["state"] = state or ("FILLED" if filled == q else "PARTIAL" if filled else "OPEN")
        held = self.holding[row["ticker"]]          # 체결분만큼 실제 잔고도 줄어든다
        held["qty"] -= delta
        held["sellable"] -= delta
        return row


class NoOrderReader(FakeBroker):
    """SHADOW 용 조회 대역: 실제 주문 경로를 부르면 실패한다."""

    def sell(self, *a, **k):
        raise AssertionError("SHADOW 에서 실주문 경로 호출")

    def cancel(self, *a, **k):
        raise AssertionError("SHADOW 에서 실취소 경로 호출")


def migrated_journal(path):
    """테스트 저널에 실제 저널 전용 Migration 을 적용한다 (런타임은 DDL 을 실행하지 않는다)."""
    upgrade_journal(str(path), allow_create=True)
    return str(path)


def settings(tmp_path, **overrides):
    # 엔진 로직 테스트는 '모든 계약이 검증된' 설정을 기준으로 한다. 계약 게이트는 별도 테스트에서 끈다.
    base = Settings(mode="LIVE", replace_enabled=True, shadow_replace_enabled=True, price_interval_sec=0, status_interval_sec=0,
                    balance_sync_sec=0, journal_path=str(tmp_path / "j.sqlite"),
                    verified_contracts=frozenset(CONTRACTS), shadow_fill_model="IMMEDIATE")
    return replace(base, **overrides)


class Harness:
    def __init__(self, tmp_path, monkeypatch, *, env=LIVE_ENV, broker=None, factory=None, **over):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("SCANNER_ORDER_JOURNAL_PATH", str(tmp_path / "j.sqlite"))
        self.clock = Clock()
        self.st = settings(tmp_path, **over)
        migrated_journal(self.st.journal_path)
        self.store = AutoStopStore(self.st.journal_path)
        self.broker = broker or FakeBroker(self.clock)
        self.factory = factory or (lambda mode: self.broker)
        self.sent_alerts = []
        self.engine = self.new_engine("w1")

    def new_engine(self, owner):
        return Engine(self.store, self.factory, settings=self.st, settings_loader=lambda: self.st,
                      clock=self.clock, owner=owner,
                      notifier=lambda sev, text: self.sent_alerts.append((sev, text)) or True)

    def set(self, **over):
        self.st = replace(self.st, **over)

    def add(self, *, mode="LIVE", qty=10, stop="45.00", ticker="T", **kw):
        return self.store.insert_position(
            self.clock(), mode=mode, ticker=ticker, exchange="ND", source="MANUAL_LINK", scan_result_id=1,
            signal_snapshot={}, stop_price=Decimal(stop), qty=qty, broker_qty=kw.pop("broker_qty", 10),
            avg_cost=kw.pop("avg_cost", "60"), max_active=20, **kw)

    def tick(self, n=1):
        out = None
        for _ in range(n):
            out = self.engine.tick()
        return out

    def pos(self, pid):
        return self.store.position(pid)

    def event(self, pid):
        return [e for e in self.store.events() if e["position_id"] == pid][-1]

    def orders(self):
        return self.store.orders()

    def alert_keys(self):
        return [a["dedup_key"] for a in self.store.recent_alerts(500)]
