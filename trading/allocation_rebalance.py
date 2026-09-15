"""Durable target-minus-holdings rebalance workflow with broker evidence.

No HTTP body may supply holdings, cash, quotes, or execution evidence. See
docs/etf_rebalance_review.md for the account2 adapter contract.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN

from trading.kiwoom_orders import OrderRejected, OrderUnknown


class RebalanceBlocked(RuntimeError):
    pass


LIVE_BLOCK_REASON = (
    "ETF 주문 차단: 주문내역 조회·대조는 구현되었지만, 주문가능 현금의 "
    "예약금·수수료 반영 의미, 전체 유효 미체결 범위와 시세 시각은 아직 "
    "검증되지 않았습니다. 매도 및 재매수를 실행하지 않습니다."
)


class UnavailableBroker:
    def snapshot(self):
        raise RebalanceBlocked(LIVE_BLOCK_REASON)

    def quotes(self, tickers):
        raise RebalanceBlocked(LIVE_BLOCK_REASON)

    def lookup(self, intent):
        raise RebalanceBlocked(LIVE_BLOCK_REASON)

    def submit(self, intent):
        raise RebalanceBlocked(LIVE_BLOCK_REASON)


def number(value, name, *, positive=False):
    try:
        n = Decimal(str(value))
    except Exception as exc:
        raise RebalanceBlocked(f"{name}: 잘못된 숫자") from exc
    if not n.is_finite() or n < 0 or (positive and n <= 0):
        raise RebalanceBlocked(f"{name}: 유효한 양수/0이 필요합니다.")
    return n


def weights(values):
    if not isinstance(values, dict) or not values:
        raise RebalanceBlocked("배분값이 없습니다.")
    result = {}
    for ticker, weight in values.items():
        key = str(ticker).strip().upper()
        if not key or key in result:
            raise RebalanceBlocked("중복/빈 종목코드")
        result[key] = number(weight, "배분값")
    if not 0 < sum(result.values()) <= 1:
        raise RebalanceBlocked("배분값 합계는 0 초과 1 이하여야 합니다.")
    return result


def buy_plan(cash, allocations, quotes, reserve_bps=100, max_age=60):
    """Net broker cash already includes pending reservations: never subtract twice.

    100 bps is a configurable conservative operational reserve, NOT a broker
    fee quote. Limit orders bound principal; the reserve covers fees/rounding.
    Unallocated strategy cash is preserved; weights are never normalized.
    """
    cash = number(cash, "주문가능 현금")
    reserve = number(reserve_bps, "여유금 bps")
    if reserve >= 10000:
        raise RebalanceBlocked("여유금 bps는 10000 미만이어야 합니다.")
    budget = cash * (1 - reserve / 10000)
    orders = []
    valid_until = time.time() + max_age
    for ticker, weight in sorted(weights(allocations).items()):
        if not weight:
            continue
        q = quotes.get(ticker) or {}
        try:
            age = time.time() - float(q["as_of"])
        except (KeyError, ValueError, TypeError) as exc:
            raise RebalanceBlocked(f"{ticker}: 시세 시각 누락") from exc
        if not math.isfinite(age) or not 0 <= age <= max_age:
            raise RebalanceBlocked(f"{ticker}: 오래되거나 잘못된 시세")
        valid_until = min(valid_until, float(q["as_of"]) + max_age)
        price = number(q.get("limit_price"), f"{ticker} 지정가", positive=True)
        if price != price.quantize(Decimal(".01")):
            raise RebalanceBlocked(f"{ticker}: 지정가 센트 단위 오류")
        qty = int((budget * weight / price).to_integral_value(rounding=ROUND_DOWN))
        if qty:
            orders.append({"ticker": ticker, "side": "BUY", "quantity": qty,
                           "limit_price": str(price)})
    cost = sum((number(o["limit_price"], "가격") * o["quantity"] for o in orders), Decimal(0))
    if cost > budget:
        raise RebalanceBlocked("총 주문금액이 예산을 초과합니다.")
    return orders, {"orderable_cash": str(cash), "budget": str(budget),
                    "estimated_cost": str(cost), "reserve_bps": str(reserve), "valid_until": valid_until}


def differential_plan(cash, allocations, holdings, quotes, reserve_bps=100):
    """Value only scoped positions; sell excess and buy deficits in integer shares.

    Targets use total equity, not just cash. Cash limits buys independently;
    insufficient settled cash scales deficits down, never creates sell proceeds.
    """
    cash = number(cash, "주문가능 현금")
    positions = {t: number(q, "보유수량") for t, q in holdings.items()}
    if any(q != int(q) for q in positions.values()):
        raise RebalanceBlocked("소수주 차액 리밸런싱 지원 없음")
    prices = {}
    valid_until = time.time() + 60
    for ticker in set(allocations) | {t for t, q in positions.items() if q}:
        # Validate quotes for both retained and sold positions too.
        _, info = buy_plan(0, {ticker: 1}, quotes, 0)
        prices[ticker] = number(quotes[ticker]["limit_price"], "가격", positive=True)
        valid_until = min(valid_until, info["valid_until"])
    equity = cash + sum(q * prices[t] for t, q in positions.items() if q)
    target_orders, _ = buy_plan(equity, allocations, quotes, 0)
    targets = {o["ticker"]: o["quantity"] for o in target_orders}
    sells, buys, retained = [], [], {}
    for ticker in sorted(set(positions) | set(allocations)):
        held = int(positions.get(ticker, 0))
        target = targets.get(ticker, 0)
        retained[ticker] = min(held, target)
        delta = target - held
        if delta:
            order = {"ticker": ticker, "side": "BUY" if delta > 0 else "SELL",
                     "quantity": abs(delta), "limit_price": str(prices[ticker])}
            (buys if delta > 0 else sells).append(order)
    _, cash_info = buy_plan(cash, allocations, quotes, reserve_bps)
    budget = number(cash_info["budget"], "매수 예산")
    required = sum((Decimal(o["limit_price"]) * o["quantity"] for o in buys), Decimal(0))
    factor = min(Decimal(1), budget / required) if required else Decimal(1)
    for order in buys:
        order["quantity"] = int(Decimal(order["quantity"]) * factor)
    buys = [o for o in buys if o["quantity"]]
    cost = sum((Decimal(o["limit_price"]) * o["quantity"] for o in buys), Decimal(0))
    return sells, buys, {**cash_info, "equity": str(equity), "targets": targets,
                        "retained_quantities": retained, "required_cost": str(required),
                        "estimated_cost": str(cost), "valid_until": valid_until}


class Rebalance:
    def __init__(self, path, broker, *, reserve_bps=100):
        self.path, self.broker, self.reserve_bps = str(path), broker, reserve_bps
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS allocation_cycles (
                    id TEXT PRIMARY KEY, cycle_key TEXT NOT NULL UNIQUE,
                    active_account TEXT UNIQUE, state TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS allocation_intents (
                    id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, side TEXT NOT NULL,
                    ticker TEXT NOT NULL, quantity INTEGER NOT NULL, price TEXT NOT NULL,
                    state TEXT NOT NULL, order_number TEXT, created_at REAL NOT NULL,
                    UNIQUE(cycle_id, side, ticker));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self, cycle_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM allocation_cycles WHERE id=?", (cycle_id,)).fetchone()
            if row is None:
                raise RebalanceBlocked("재구성 기록이 없습니다.")
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            result["orders"] = [dict(r) for r in db.execute(
                "SELECT * FROM allocation_intents WHERE cycle_id=? ORDER BY created_at, id", (cycle_id,))]
            return result

    def snapshot(self):
        s = self.broker.snapshot()
        if (s.get("account_profile") != "account2" or s.get("currency") != "USD"
                or s.get("complete") is not True
                or s.get("cash_includes_reservations") is not True):
            raise RebalanceBlocked("완전한 account2 잔고·미체결·순주문가능현금 증거가 없습니다.")
        age = time.time() - float(number(s.get("as_of"), "계좌 조회 시각"))
        if not math.isfinite(age) or not 0 <= age <= 60:
            raise RebalanceBlocked("계좌 조회가 오래되었습니다.")
        if not isinstance(s.get("open_orders"), list) or not isinstance(s.get("holdings"), dict):
            raise RebalanceBlocked("잔고/미체결 응답 누락")
        number(s.get("orderable_cash"), "주문가능 현금")
        for qty in s["holdings"].values():
            number(qty, "잔여 수량")
        return s

    def create(self, cycle_key, allocations, scope):
        # One permanent key per strategy period. Re-preview cannot create a new
        # cycle while account2 is active, even across workers/process restarts.
        with self.db() as db:
            row = db.execute("SELECT id FROM allocation_cycles WHERE cycle_key=?", (cycle_key,)).fetchone()
            if row:
                return self.status(row["id"])
        allocations = {t: str(w) for t, w in weights(allocations).items()}
        scope = sorted({str(t).strip().upper() for t in scope})
        if not scope or not set(allocations) <= set(scope):
            raise RebalanceBlocked("자산배분 전용 보유 범위를 명시해야 합니다.")
        snapshot = self.snapshot()
        if snapshot["open_orders"]:
            raise RebalanceBlocked("기존 미체결 주문 대조가 필요합니다.")
        payload = self._sell_payload(snapshot, allocations, scope, self.reserve_bps)
        cycle_id = uuid.uuid4().hex
        try:
            with self.db() as db:
                db.execute("INSERT INTO allocation_cycles VALUES (?, ?, 'account2', 'SELL_PREVIEW', ?)",
                           (cycle_id, cycle_key, json.dumps(payload)))
        except sqlite3.IntegrityError as exc:
            raise RebalanceBlocked("account2의 기존 재구성 기록을 먼저 확인하세요.") from exc
        return self.status(cycle_id)

    def _sell_payload(self, snapshot, allocations, scope, reserve_bps):
        positions = {t: snapshot["holdings"].get(t, 0) for t in scope}
        quotes = self.broker.quotes(sorted(set(allocations) | {t for t, q in positions.items() if number(q, "보유수량")}))
        sells, _, info = differential_plan(snapshot["orderable_cash"], allocations, positions, quotes, reserve_bps)
        return {"mode": "DELTA_V1", "allocations": allocations, "scope": scope,
                "preview": sells, "expires_at": info["valid_until"],
                "reserve_bps": reserve_bps, "planning": info,
                "original_quantities": {t: str(q) for t, q in positions.items()},
                "retained_quantities": info["retained_quantities"]}

    @staticmethod
    def _check_mode(current):
        if current["payload"].get("mode") != "DELTA_V1":
            raise RebalanceBlocked("이전 전량매도 계획입니다. 기존 주문 대조 및 계획 재검토가 필요합니다.")

    def confirm(self, cycle_id, side):
        if side not in {"SELL", "BUY"}:
            raise RebalanceBlocked("잘못된 주문 방향")
        current = self.status(cycle_id)
        self._check_mode(current)
        if current["state"] != side + "_PREVIEW":
            return current  # duplicate confirm: no network submission
        payload = current["payload"]
        if payload["expires_at"] < time.time():
            raise RebalanceBlocked("미리보기가 만료되었습니다. 기존 사이클에서 갱신해야 합니다.")
        s = self.snapshot()
        if s["open_orders"]:
            raise RebalanceBlocked("미체결 주문이 있어 실행을 중단했습니다.")
        for ticker in payload["scope"]:
            expected = number(payload["original_quantities"][ticker] if side == "SELL"
                              else payload["retained_quantities"].get(ticker, 0), "기대 보유수량")
            if number(s["holdings"].get(ticker, 0), "잔여 수량") != expected:
                raise RebalanceBlocked(f"{ticker}: 보유수량 변경/매도 잔여, 미리보기 재검토 필요")
        if side == "BUY":
            cost = sum(number(o["limit_price"], "가격") * o["quantity"] for o in payload["preview"])
            budget = number(s["orderable_cash"], "현금") * (1 - number(payload["reserve_bps"], "여유금") / 10000)
            if cost > budget:
                raise RebalanceBlocked("주문가능 현금 감소: 매수 미리보기를 갱신하세요.")
        # Commit every intent BEFORE the first network side effect. Atomic state
        # transition gives exactly one dispatcher; crash leaves durable evidence.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE allocation_cycles SET state=? WHERE id=? AND state=? AND payload=?",
                                 (side + "_PENDING", cycle_id, side + "_PREVIEW", json.dumps(payload))).rowcount
            if not changed:
                return self.status(cycle_id)
            for o in payload["preview"]:
                db.execute("INSERT INTO allocation_intents VALUES (?, ?, ?, ?, ?, ?, 'PREPARED', NULL, ?)",
                           (uuid.uuid4().hex, cycle_id, side, o["ticker"], o["quantity"], o["limit_price"], time.time()))
        for order in self.status(cycle_id)["orders"]:
            if order["side"] != side or order["state"] != "PREPARED":
                continue
            if time.time() > payload["expires_at"]:
                break  # stale quote: leave unsent intents for review
            if side == "BUY":
                fresh = self.snapshot()
                available = number(fresh["orderable_cash"], "현금")
                # Available is already net of broker reservations. Only this
                # unsent order's principal + reserve must fit; do not subtract
                # previous SUBMITTED orders again.
                cost = number(order["price"], "지정가") * order["quantity"]
                if cost > available * (1 - number(payload["reserve_bps"], "여유금") / 10000):
                    break  # PREPARED: not sent, manual review required
            with self.db() as db:
                db.execute("UPDATE allocation_intents SET state='SENDING' WHERE id=?", (order["id"],))
            try:
                result = self.broker.submit(order)
                order_no = result.get("order_number")
                if result.get("status") != "submitted" or not isinstance(order_no, str) or not order_no.strip() or not order_no.isdigit() or int(order_no) == 0:
                    raise OrderUnknown("접수 응답의 주문번호를 확인할 수 없습니다.")
            except OrderRejected:
                self._order_state(order["id"], "REJECTED")
                break
            except Exception:
                self._order_state(order["id"], "UNKNOWN")
                break
            self._order_state(order["id"], "SUBMITTED", order_no)
        return self.status(cycle_id)

    def execute_authorized_cycle(self, cycle_id):
        """Persist one user authorization, then start the sell phase once."""
        current = self.status(cycle_id)
        self._check_mode(current)
        if current["state"] != "SELL_PREVIEW":
            if current["payload"].get("execution_authorized") is True:
                return self.advance_authorized_cycle(cycle_id)
            raise RebalanceBlocked("최종 매도·매수 계획을 다시 확인해야 합니다.")
        previous_payload = json.dumps(current["payload"])
        payload = dict(current["payload"])
        payload["execution_authorized"] = True
        payload["authorized_at"] = time.time()
        payload["authorization_scope"] = "FULL_CYCLE_V1"
        with self.db() as db:
            changed = db.execute(
                "UPDATE allocation_cycles SET payload=? WHERE id=? AND state='SELL_PREVIEW' AND payload=?",
                (json.dumps(payload), cycle_id, previous_payload),
            ).rowcount
        if not changed:
            return self.status(cycle_id)
        self.confirm(cycle_id, "SELL")
        return self.advance_authorized_cycle(cycle_id)

    def advance_authorized_cycle(self, cycle_id):
        """Advance only a persisted user-authorized cycle; stop on blockers."""
        for _ in range(4):
            current = self.status(cycle_id)
            self._check_mode(current)
            if current["payload"].get("execution_authorized") is not True:
                raise RebalanceBlocked("사용자가 승인한 리밸런싱 사이클이 아닙니다.")
            if current["state"] in {"SELL_PENDING", "BUY_PENDING"}:
                result = self.reconcile(cycle_id)
                if result.get("blockers") or result["state"] == current["state"]:
                    return result
                continue
            if current["state"] == "SELL_CONFIRMED":
                result = self.preview_buys(cycle_id)
                if result["payload"].get("remaining_sell_adjustments"):
                    return {**result, "blockers": [
                        "가격·현금 변동으로 추가 매도가 필요해 매수 전에 멈췄습니다."
                    ]}
                continue
            if current["state"] == "BUY_PREVIEW":
                result = self.confirm(cycle_id, "BUY")
                stopped = []
                for order in result["orders"]:
                    if order["side"] != "BUY":
                        continue
                    if order["state"] == "REJECTED":
                        stopped.append(f"{order['ticker']}: 증권사 주문 거절 — 이후 주문 중단")
                    elif order["state"] in {"UNKNOWN", "PREPARED", "SENDING"}:
                        stopped.append(f"{order['ticker']}: {order['state']} — 자동 진행 중단")
                return {**result, "blockers": stopped} if stopped else result
            return current
        return self.status(cycle_id)

    def _order_state(self, intent_id, state, order_number=None):
        with self.db() as db:
            db.execute("UPDATE allocation_intents SET state=?, order_number=COALESCE(?, order_number) WHERE id=? AND state != 'FILLED'",
                       (state, order_number, intent_id))

    def reconcile(self, cycle_id):
        current = self.status(cycle_id)
        self._check_mode(current)
        if current["state"] not in {"SELL_PENDING", "BUY_PENDING"}:
            return current
        side = current["state"].split("_")[0]
        problems = []
        for order in current["orders"]:
            if order["side"] != side:
                continue
            if order["state"] == "PREPARED":
                problems.append(f"{order['ticker']}: 미전송 의도 검토 필요")
                continue
            if order["state"] == "REJECTED":
                problems.append(f"{order['ticker']}: 증권사 주문 거절 — 이후 주문 중단")
                continue
            evidence = self.broker.lookup(order)
            # Adapter must match order identity (including date/account/side/
            # ticker/qty/price for unknown replies) uniquely, never merely ticker.
            if not evidence or evidence.get("matched_intent_id") != order["id"]:
                problems.append(f"{order['ticker']}: 접수 여부 불명 — 주문내역 대조 필요")
                continue
            state = evidence.get("state")
            filled = number(evidence.get("filled_quantity"), "체결수량")
            if state != "FILLED" or filled != order["quantity"]:
                problems.append(f"{order['ticker']}: {state}, 잔여 {max(Decimal(0), order['quantity'] - filled)}주")
                continue
            self._order_state(order["id"], "FILLED")
        s = self.snapshot()  # only after reconciliation; never infer cash from sells
        if s["open_orders"]:
            problems.append("미체결 주문이 남아 있습니다.")
        if side == "SELL":
            for ticker in current["payload"]["scope"]:
                remaining = number(s["holdings"].get(ticker, 0), "잔여 수량")
                expected = number(current["payload"]["retained_quantities"].get(ticker, 0), "유지수량")
                if remaining != expected:
                    problems.append(f"{ticker}: 보유 {remaining}주 / 유지 예정 {expected}주 — 조정 매도 확인 필요")
        if problems:
            return {**self.status(cycle_id), "blockers": problems}
        new_state = "SELL_CONFIRMED" if side == "SELL" else "COMPLETE"
        with self.db() as db:
            db.execute("UPDATE allocation_cycles SET state=?, active_account=? WHERE id=? AND state=?",
                       (new_state, None if side == "BUY" else "account2", cycle_id, side + "_PENDING"))
        return self.status(cycle_id)

    def preview_buys(self, cycle_id):
        current = self.status(cycle_id)
        self._check_mode(current)
        if current["state"] not in {"SELL_CONFIRMED", "BUY_PREVIEW"}:
            raise RebalanceBlocked("계획한 조정 매도의 체결 확인이 먼저 필요합니다.")
        payload = current["payload"]
        previous_payload = json.dumps(payload)
        s = self.snapshot()
        if s["open_orders"] or any(number(s["holdings"].get(t, 0), "잔여수량") != number(payload["retained_quantities"].get(t, 0), "유지수량") for t in payload["scope"]):
            raise RebalanceBlocked("예정과 다른 보유수량/미체결 주문이 있어 매수를 중단합니다.")
        positions = {t: s["holdings"].get(t, 0) for t in payload["scope"]}
        quotes = self.broker.quotes(sorted(set(payload["allocations"]) | {t for t, q in positions.items() if number(q, "보유수량")}))
        extra_sells, orders, budget = differential_plan(s["orderable_cash"], payload["allocations"], positions, quotes, payload["reserve_bps"])
        # Price/cash changes may imply another sale. Never place a new sale
        # without a new confirmation; disclose the drift in the buy preview.
        payload["remaining_sell_adjustments"] = extra_sells
        payload.update(preview=orders, budget=budget, expires_at=budget["valid_until"])
        with self.db() as db:
            db.execute("UPDATE allocation_cycles SET state='BUY_PREVIEW', payload=? WHERE id=? AND state IN ('SELL_CONFIRMED', 'BUY_PREVIEW') AND payload=?",
                       (json.dumps(payload), cycle_id, previous_payload))
        return self.status(cycle_id)

    def refresh_sell_preview(self, cycle_id):
        current = self.status(cycle_id)
        self._check_mode(current)
        if current["state"] != "SELL_PREVIEW" or current["orders"]:
            raise RebalanceBlocked("이미 전송한 주문은 새 미리보기로 재전송할 수 없습니다.")
        payload = current["payload"]
        previous_payload = json.dumps(payload)
        snapshot = self.snapshot()
        if snapshot["open_orders"]:
            raise RebalanceBlocked("미체결 주문 확인이 필요합니다.")
        payload = self._sell_payload(snapshot, payload["allocations"], payload["scope"], payload["reserve_bps"])
        with self.db() as db:
            db.execute("UPDATE allocation_cycles SET payload=? WHERE id=? AND state='SELL_PREVIEW' AND payload=?",
                       (json.dumps(payload), cycle_id, previous_payload))
        return self.status(cycle_id)

    def active(self):
        with self.db() as db:
            row = db.execute("SELECT id FROM allocation_cycles WHERE active_account='account2'").fetchone()
        return self.status(row["id"]) if row else None
