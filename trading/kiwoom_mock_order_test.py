"""Durable, mock-only one-share US order/cancel verification workflow.

This diagnostic never runs from the web application.  Every network side effect
must be invoked as a separate, explicitly confirmed step; ambiguous responses
remain blocked until broker history is reconciled.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import MOCK_BASE_URL


class MockOrderTestBlocked(RuntimeError):
    pass


class KiwoomMockTestBroker:
    """Narrow adapter used only by this diagnostic state machine."""

    def __init__(self, config, token, order_client, evidence, *, day):
        self.mode, self.base_url = config.mode, config.base_url
        self.token, self.order_client, self.evidence, self.day = token, order_client, evidence, day

    def snapshot(self):
        return self.evidence.cash_check(self.day, include_previous_day=True)

    def buy(self, test):
        return self.order_client.buy_us_limit(
            self.token, exchange=test["exchange"], ticker=test["ticker"],
            quantity=1, price=float(test["price"]),
        )

    def cancel(self, test):
        return self.order_client.cancel_us_order(
            self.token, exchange=test["exchange"], ticker=test["ticker"],
            original_order_number=test["buy_order_number"],
        )

    def lookup(self, test):
        return self.evidence.lookup({
            "id": test["id"], "order_number": test["buy_order_number"],
            "ticker": test["ticker"], "side": "BUY", "quantity": 1,
            "price": test["price"], "created_at": test["created_at"],
        })


def _price(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MockOrderTestBlocked("센트 단위 양수 지정가가 필요합니다.") from exc
    if not result.is_finite() or result <= 0 or result != result.quantize(Decimal(".01")):
        raise MockOrderTestBlocked("센트 단위 양수 지정가가 필요합니다.")
    return result


def _cash(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MockOrderTestBlocked("유효한 USD 주문가능 현금이 필요합니다.") from exc
    if not result.is_finite() or result < 0:
        raise MockOrderTestBlocked("유효한 USD 주문가능 현금이 필요합니다.")
    return result


class MockOrderTest:
    """State machine for one mock buy, open-order check, cancel and cash check."""

    def __init__(self, path, broker):
        if getattr(broker, "mode", None) != "mock" or getattr(broker, "base_url", None) != MOCK_BASE_URL:
            raise MockOrderTestBlocked("키움 해외 모의투자 서버에서만 실행할 수 있습니다.")
        self.path, self.broker = str(path), broker
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mock_order_tests (
                id TEXT PRIMARY KEY, test_key TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, ticker TEXT NOT NULL, exchange TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity = 1), price TEXT NOT NULL,
                buy_order_number TEXT, cancel_order_number TEXT,
                cash_before TEXT NOT NULL, cash_after_cancel TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                details TEXT NOT NULL)''')

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

    def status(self, test_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM mock_order_tests WHERE id=?", (test_id,)).fetchone()
        if row is None:
            raise MockOrderTestBlocked("테스트 기록을 찾을 수 없습니다.")
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def prepare(self, test_key, *, ticker, exchange, price,
                allow_unvalidated_capacity=False):
        ticker, exchange = str(ticker).strip().upper(), str(exchange).strip().upper()
        value = _price(price)
        if not ticker or len(ticker) > 12 or exchange not in {"NA", "ND", "NY"}:
            raise MockOrderTestBlocked("유효한 미국 종목과 거래소가 필요합니다.")
        with self.db() as db:
            existing = db.execute("SELECT id FROM mock_order_tests WHERE test_key=?", (test_key,)).fetchone()
        if existing:
            return self.status(existing["id"])
        snapshot = self.broker.snapshot()
        if snapshot.get("account") != "account2" or snapshot.get("currency") != "USD":
            raise MockOrderTestBlocked("account2의 USD 계좌 증거가 필요합니다.")
        capacity_unverified = snapshot.get("validated_for_execution") is not True
        if capacity_unverified and not allow_unvalidated_capacity:
            blockers = snapshot.get("blockers")
            reason = "; ".join(str(item) for item in blockers) if isinstance(blockers, list) else "검증 근거 부족"
            raise MockOrderTestBlocked(
                f"해외 주문가능 현금 검증이 완료되지 않아 모의 주문을 차단합니다: {reason}"
            )
        if snapshot.get("open_order_count") != 0:
            raise MockOrderTestBlocked("기존 미체결 주문이 있어 테스트를 시작하지 않습니다.")
        cash = _cash(snapshot.get("broker_orderable_cash"))
        if cash < value * Decimal("1.01"):
            raise MockOrderTestBlocked("1주 주문금액과 1% 여유금보다 주문가능 현금이 적습니다.")
        now, test_id = time.time(), uuid.uuid4().hex
        confirmation = f"BUY:{ticker}:1@{value:.2f}"
        if capacity_unverified:
            confirmation += ":UNVALIDATED_CAPACITY"
        details = {"cash_source": snapshot.get("cash_source"), "prepared_at": now,
                   "confirmation": confirmation,
                   "capacity_unverified": capacity_unverified,
                   "capacity_blockers": snapshot.get("blockers", [])}
        try:
            with self.db() as db:
                db.execute('''INSERT INTO mock_order_tests
                    (id,test_key,state,ticker,exchange,quantity,price,
                     buy_order_number,cancel_order_number,cash_before,cash_after_cancel,
                     created_at,updated_at,details)
                    VALUES (?,?,?,?,?,1,?,?,?,?,?,?,?,?)''',
                           (test_id, test_key, "PREPARED", ticker, exchange, f"{value:.2f}",
                            None, None, str(cash), None, now, now,
                            json.dumps(details, ensure_ascii=False)))
        except sqlite3.IntegrityError:
            with self.db() as db:
                row = db.execute("SELECT id FROM mock_order_tests WHERE test_key=?", (test_key,)).fetchone()
            return self.status(row["id"])
        return self.status(test_id)

    def submit_buy(self, test_id, confirmation):
        current = self.status(test_id)
        if current["state"] != "PREPARED":
            return current
        if confirmation != current["details"]["confirmation"]:
            raise MockOrderTestBlocked("준비 결과에 표시된 확인 문구가 일치해야 합니다.")
        if not self._claim(test_id, "PREPARED", "BUY_SENDING"):
            return self.status(test_id)
        try:
            result = self.broker.buy(current)
            number = self._order_number(result, "매수")
        except OrderRejected as exc:
            self._set(test_id, "BUY_REJECTED", error=str(exc))
        except Exception as exc:
            self._set(test_id, "BUY_UNKNOWN", error=str(exc))
        else:
            self._set(test_id, "BUY_SUBMITTED", buy_order_number=number)
        return self.status(test_id)

    def verify_open(self, test_id):
        current = self.status(test_id)
        if current["state"] == "OPEN_VERIFIED":
            return current
        if current["state"] != "BUY_SUBMITTED":
            raise MockOrderTestBlocked("매수 접수 주문번호가 확인된 뒤에만 미체결을 검증합니다.")
        evidence = self.broker.lookup(current)
        state = evidence.get("state") if evidence else None
        if state == "FILLED":
            self._set(test_id, "FILLED_UNEXPECTED", evidence=evidence)
        elif state == "OPEN" and evidence.get("remaining_quantity") == 1:
            self._set(test_id, "OPEN_VERIFIED", evidence=evidence,
                      cash_after_buy=self.broker.snapshot().get("broker_orderable_cash"))
        else:
            raise MockOrderTestBlocked(f"미체결 1주를 확인하지 못했습니다: {state or '조회 불일치'}")
        return self.status(test_id)

    def submit_cancel(self, test_id, confirmation):
        current = self.status(test_id)
        if current["state"] != "OPEN_VERIFIED":
            return current if current["state"] in {"CANCEL_SENDING", "CANCEL_UNKNOWN", "CANCEL_SUBMITTED", "COMPLETE"} else self._blocked("미체결 확인 후에만 취소합니다.")
        expected = f"CANCEL:{current['buy_order_number']}"
        if confirmation != expected:
            raise MockOrderTestBlocked(f"취소 확인 문구가 일치해야 합니다: {expected}")
        if not self._claim(test_id, "OPEN_VERIFIED", "CANCEL_SENDING"):
            return self.status(test_id)
        try:
            result = self.broker.cancel(current)
            number = self._order_number(result, "취소")
        except OrderRejected as exc:
            self._set(test_id, "CANCEL_REJECTED", error=str(exc))
        except Exception as exc:
            self._set(test_id, "CANCEL_UNKNOWN", error=str(exc))
        else:
            self._set(test_id, "CANCEL_SUBMITTED", cancel_order_number=number)
        return self.status(test_id)

    def verify_cancel(self, test_id):
        current = self.status(test_id)
        if current["state"] == "COMPLETE":
            return current
        if current["state"] not in {"CANCEL_SUBMITTED", "CANCEL_UNKNOWN", "CASH_RESTORE_PENDING"}:
            raise MockOrderTestBlocked("취소 전송 기록이 있는 테스트만 대조할 수 있습니다.")
        evidence = self.broker.lookup(current)
        if not evidence or evidence.get("state") != "CANCELLED" or evidence.get("remaining_quantity") != 0:
            raise MockOrderTestBlocked("증권사 내역에서 원주문 취소 완료를 확인하지 못했습니다.")
        snapshot = self.broker.snapshot()
        after = Decimal(str(snapshot.get("broker_orderable_cash")))
        before = Decimal(current["cash_before"])
        state = "COMPLETE" if after >= before else "CASH_RESTORE_PENDING"
        self._set(test_id, state, cash_after_cancel=str(after), evidence=evidence,
                  cash_restored=after >= before)
        return self.status(test_id)

    def _claim(self, test_id, old, new):
        with self.db() as db:
            return db.execute("UPDATE mock_order_tests SET state=?, updated_at=? WHERE id=? AND state=?",
                              (new, time.time(), test_id, old)).rowcount == 1

    def _set(self, test_id, state, *, buy_order_number=None,
             cancel_order_number=None, cash_after_cancel=None, **details):
        current = self.status(test_id)
        merged = {**current["details"], **details}
        with self.db() as db:
            db.execute('''UPDATE mock_order_tests SET state=?,
                buy_order_number=COALESCE(?,buy_order_number),
                cancel_order_number=COALESCE(?,cancel_order_number),
                cash_after_cancel=COALESCE(?,cash_after_cancel), updated_at=?, details=? WHERE id=?''',
                (state, buy_order_number, cancel_order_number, cash_after_cancel,
                 time.time(), json.dumps(merged, ensure_ascii=False), test_id))

    @staticmethod
    def _order_number(result, action):
        raw = result.get("order_number") if isinstance(result, dict) else None
        number = str(raw).strip() if type(raw) in (str, int) else ""
        if (not number.isascii() or not number.isdigit()
                or int(number) == 0 or len(number) > 9):
            raise OrderUnknown(f"{action} 접수 주문번호를 확인할 수 없습니다.")
        return number

    @staticmethod
    def _blocked(message):
        raise MockOrderTestBlocked(message)
