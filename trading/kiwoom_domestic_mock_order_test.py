"""Durable mock-only one-share domestic buy/sell and cancel test workflow."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal

from trading.kiwoom_domestic_execution_evidence import ticker as valid_ticker
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import MOCK_BASE_URL


class DomesticMockTestBlocked(RuntimeError):
    pass


class KiwoomDomesticMockBroker:
    def __init__(self, config, token, readonly, order_client, evidence):
        self.mode, self.base_url = config.mode, config.base_url
        self.token, self.readonly = token, readonly
        self.order_client, self.evidence = order_client, evidence

    def snapshot(self):
        return self.evidence.snapshot()

    def sellable_quantity(self, ticker):
        report = self.readonly.get_account_balance(self.token, query_type="1", exchange="KRX")
        matches = [row for row in report.get("holdings", [])
                   if str(row.get("stk_cd") or "").strip().lstrip("A") == ticker]
        if len(matches) != 1:
            return 0
        raw = matches[0].get("trde_able_qty", matches[0].get("rmnd_qty"))
        return int(Decimal(str(raw).strip().replace(",", "")))

    def submit(self, test):
        method = (self.order_client.buy_kr_limit if test["side"] == "BUY"
                  else self.order_client.sell_kr_limit)
        kwargs = {"exchange": "KRX", "ticker": test["ticker"],
                  "quantity": 1, "price": int(test["price"])}
        if test["side"] == "BUY":
            kwargs["trade_type"] = test["details"].get("trade_type", "0")
        return method(self.token, **kwargs)

    def cancel(self, test):
        return self.order_client.cancel_kr_order(
            self.token, exchange="KRX", ticker=test["ticker"],
            original_order_number=test["order_number"], quantity=1,
        )

    def lookup_open(self, test):
        return self.evidence.lookup_open(test)

    def lookup_cancel(self, test):
        return self.evidence.lookup_cancel(test)

    def lookup_filled(self, test):
        return self.evidence.lookup_filled(test)


class DomesticMockOrderTest:
    def __init__(self, path, broker, *, account_profile):
        if (getattr(broker, "mode", None) != "mock"
                or getattr(broker, "base_url", None) != MOCK_BASE_URL):
            raise DomesticMockTestBlocked("키움 국내 모의투자 서버에서만 실행할 수 있습니다.")
        if not account_profile:
            raise DomesticMockTestBlocked("국내 모의 계좌 프로필이 필요합니다.")
        self.path, self.broker, self.account_profile = str(path), broker, account_profile
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS domestic_mock_order_tests (
                id TEXT PRIMARY KEY, test_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                account_profile TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
                ticker TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity=1),
                price INTEGER NOT NULL, order_number TEXT, cancel_order_number TEXT,
                cash_before TEXT NOT NULL, cash_after_cancel TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, details TEXT NOT NULL)''')

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
            row = db.execute("SELECT * FROM domestic_mock_order_tests WHERE id=?", (test_id,)).fetchone()
        if row is None:
            raise DomesticMockTestBlocked("국내 모의 주문 테스트 기록이 없습니다.")
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def prepare(self, test_key, *, side, ticker, price, trade_type="0"):
        side, ticker = str(side).upper(), valid_ticker(ticker)
        if side not in {"BUY", "SELL"} or type(price) is not int or price < 1:
            raise DomesticMockTestBlocked("BUY/SELL, 6자리 종목코드, 정수 지정가가 필요합니다.")
        if trade_type not in {"0", "62"} or side == "SELL" and trade_type != "0":
            raise DomesticMockTestBlocked("매수는 보통(0)·시간외단일가(62), 매도 진단은 보통(0)만 지원합니다.")
        with self.db() as db:
            row = db.execute("SELECT id FROM domestic_mock_order_tests WHERE test_key=?", (test_key,)).fetchone()
        if row:
            return self.status(row["id"])
        snapshot = self.broker.snapshot()
        if snapshot.get("account") != self.account_profile or snapshot.get("currency") != "KRW":
            raise DomesticMockTestBlocked("선택한 국내 계좌의 KRW 증거가 필요합니다.")
        if snapshot.get("open_order_count") != 0:
            raise DomesticMockTestBlocked("기존 국내 미체결 주문이 있어 시작하지 않습니다.")
        cash = Decimal(str(snapshot.get("broker_orderable_cash")))
        if side == "BUY" and cash < Decimal(price) * Decimal("1.01"):
            raise DomesticMockTestBlocked("1주 매수금액과 1% 여유금보다 주문가능 현금이 적습니다.")
        holding_before = self.broker.sellable_quantity(ticker)
        sellable_before = holding_before if side == "SELL" else None
        if side == "SELL" and sellable_before < 1:
            raise DomesticMockTestBlocked("해당 계좌에서 매도 가능한 1주를 확인하지 못했습니다.")
        now, test_id = time.time(), uuid.uuid4().hex
        details = {"cash_source": snapshot.get("cash_source"),
                   "confirmation": f"{side}:{ticker}:1@{price}",
                   "sellable_before": sellable_before,
                   "holding_before": holding_before,
                   "trade_type": trade_type}
        with self.db() as db:
            db.execute('''INSERT INTO domestic_mock_order_tests
                (id,test_key,state,account_profile,side,ticker,quantity,price,
                 order_number,cancel_order_number,cash_before,cash_after_cancel,
                 created_at,updated_at,details) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)''',
                (test_id, test_key, "PREPARED", self.account_profile, side, ticker, price,
                 None, None, str(cash), None, now, now, json.dumps(details, ensure_ascii=False)))
        return self.status(test_id)

    def submit(self, test_id, confirmation):
        current = self.status(test_id)
        if current["state"] != "PREPARED":
            return current
        if confirmation != current["details"]["confirmation"]:
            raise DomesticMockTestBlocked("준비 결과의 확인 문구가 일치해야 합니다.")
        if not self._claim(test_id, "PREPARED", "ORDER_SENDING"):
            return self.status(test_id)
        try:
            number = self._number(self.broker.submit(current), "주문")
        except OrderRejected as exc:
            self._set(test_id, "ORDER_REJECTED", error=str(exc))
        except Exception as exc:
            self._set(test_id, "ORDER_UNKNOWN", error=str(exc))
        else:
            self._set(test_id, "ORDER_SUBMITTED", order_number=number)
        return self.status(test_id)

    def verify_open(self, test_id):
        current = self.status(test_id)
        if current["state"] == "OPEN_VERIFIED":
            return current
        if current["state"] != "ORDER_SUBMITTED":
            raise DomesticMockTestBlocked("접수 주문번호 확인 후에만 미체결을 검증합니다.")
        evidence = self.broker.lookup_open({**current, "quantity": 1})
        if not evidence or evidence.get("state") != "OPEN" or evidence.get("remaining_quantity") != 1:
            raise DomesticMockTestBlocked("증권사 내역에서 미체결 1주를 확인하지 못했습니다.")
        snapshot = self.broker.snapshot()
        extra = ({"sellable_after_order": self.broker.sellable_quantity(current["ticker"])}
                 if current["side"] == "SELL" else {})
        self._set(test_id, "OPEN_VERIFIED", evidence=evidence,
                  cash_after_order=snapshot.get("broker_orderable_cash"), **extra)
        return self.status(test_id)

    def verify_filled(self, test_id):
        current = self.status(test_id)
        if current["state"] == "FILLED":
            return current
        if current["state"] not in {"ORDER_SUBMITTED", "ORDER_UNKNOWN"}:
            raise DomesticMockTestBlocked("주문 전송 기록이 있는 테스트만 체결을 검증합니다.")
        evidence = self.broker.lookup_filled({**current, "quantity": 1})
        if not evidence:
            raise DomesticMockTestBlocked("증권사 주문내역에서 1주 체결 완료를 확인하지 못했습니다.")
        holding_after = self.broker.sellable_quantity(current["ticker"])
        before = int(current["details"].get("holding_before", 0))
        expected = before + 1 if current["side"] == "BUY" else max(0, before - 1)
        if holding_after != expected:
            raise DomesticMockTestBlocked("체결내역과 계좌 보유수량 변화가 일치하지 않습니다.")
        self._set(test_id, "FILLED", evidence=evidence, holding_after=holding_after)
        return self.status(test_id)

    def submit_cancel(self, test_id, confirmation):
        current = self.status(test_id)
        if current["state"] != "OPEN_VERIFIED":
            if current["state"] in {"CANCEL_SENDING", "CANCEL_UNKNOWN", "CANCEL_SUBMITTED", "COMPLETE"}:
                return current
            raise DomesticMockTestBlocked("미체결 확인 뒤에만 취소합니다.")
        expected = f"CANCEL:{current['order_number']}"
        if confirmation != expected:
            raise DomesticMockTestBlocked(f"취소 확인 문구가 일치해야 합니다: {expected}")
        if not self._claim(test_id, "OPEN_VERIFIED", "CANCEL_SENDING"):
            return self.status(test_id)
        try:
            number = self._number(self.broker.cancel(current), "취소")
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
        if current["state"] not in {"CANCEL_SUBMITTED", "CANCEL_UNKNOWN", "RESERVATION_RESTORE_PENDING"}:
            raise DomesticMockTestBlocked("취소 전송 기록이 있는 테스트만 대조할 수 있습니다.")
        evidence = self.broker.lookup_cancel(current)
        if not evidence or evidence.get("state") != "CANCELLED":
            raise DomesticMockTestBlocked("증권사 내역에서 취소 완료를 확인하지 못했습니다.")
        snapshot = self.broker.snapshot()
        after, before = Decimal(snapshot["broker_orderable_cash"]), Decimal(current["cash_before"])
        if current["side"] == "SELL":
            sellable_after = self.broker.sellable_quantity(current["ticker"])
            restored = sellable_after >= int(current["details"]["sellable_before"])
        else:
            sellable_after = None
            restored = after >= before
        self._set(test_id, "COMPLETE" if restored else "RESERVATION_RESTORE_PENDING",
                  cash_after_cancel=str(after), evidence=evidence, cash_restored=restored,
                  sellable_after_cancel=sellable_after)
        return self.status(test_id)

    def _claim(self, test_id, old, new):
        with self.db() as db:
            return db.execute("UPDATE domestic_mock_order_tests SET state=?,updated_at=? WHERE id=? AND state=?",
                              (new, time.time(), test_id, old)).rowcount == 1

    def _set(self, test_id, state, *, order_number=None, cancel_order_number=None,
             cash_after_cancel=None, **details):
        current = self.status(test_id)
        merged = {**current["details"], **details}
        with self.db() as db:
            db.execute('''UPDATE domestic_mock_order_tests SET state=?,
                order_number=COALESCE(?,order_number),cancel_order_number=COALESCE(?,cancel_order_number),
                cash_after_cancel=COALESCE(?,cash_after_cancel),updated_at=?,details=? WHERE id=?''',
                (state, order_number, cancel_order_number, cash_after_cancel, time.time(),
                 json.dumps(merged, ensure_ascii=False), test_id))

    @staticmethod
    def _number(result, action):
        raw = result.get("order_number") if isinstance(result, dict) else None
        value = str(raw).strip() if type(raw) in (str, int) else ""
        if not value.isascii() or not value.isdigit() or int(value) == 0 or len(value) > 7:
            raise OrderUnknown(f"국내 {action} 접수 주문번호를 확인할 수 없습니다.")
        return value.zfill(7)
