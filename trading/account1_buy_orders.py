"""자유투자(account1) 미국주식 스캐너 후보 지정가 매수.

한 번의 사용자 확인으로 한 건의 지정가 매수만 보낸다. 상세 설계와 남은 제한은
docs/account1_scanner_buy.md를 따른다.

- 미리보기(PREPARED)는 영속 SQLite 저널에 먼저 기록하고 짧은 유효기간을 둔다.
- 전송 직전 같은 종목·지정가의 ust31490 주문가능수량을 다시 확인한다.
- 전송 전 SENDING을 커밋한다. 응답 유실은 UNKNOWN으로 남기고 재전송하지 않는다.
- 같은 종목에 SENDING/UNKNOWN 주문이 있으면 새 주문을 받지 않는다(유일 인덱스).
- 접수(SUBMITTED)는 체결이 아니다. 체결 여부는 주문내역 대조로 따로 확인한다.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from trading.kiwoom_allocation_live_broker import KiwoomAllocationLiveBroker
from trading.kiwoom_execution_evidence import ExecutionEvidence
from trading.kiwoom_orders import KiwoomOrderClient, OrderRejected
from trading.kiwoom_readonly import REAL_BASE_URL, KiwoomError, KiwoomReadOnlyClient

NEW_YORK = ZoneInfo("America/New_York")
US_EXCHANGES = ("ND", "NY", "NA")
IN_FLIGHT_STATES = ("SENDING", "UNKNOWN")
PREVIEW_TTL_SECONDS = 120
# 매수 지정가가 현재가보다 이만큼 높으면 오입력으로 보고 차단한다.
MAX_ABOVE_CURRENT_PCT = Decimal("10")
LOW_LIMIT_WARN_PCT = Decimal("10")
STALE_QUOTE_WARN_SECONDS = 120
# 실행 중인 요청과 겹치지 않도록 SENDING은 이 시간이 지난 뒤에만 수동 정리할 수 있다.
SENDING_RESOLVE_AFTER_SECONDS = 120


class Account1BuyBlocked(Exception):
    """주문을 보내지 않았거나 보낼 수 없는 상태. 메시지는 사용자에게 보여준다."""


def _cents(value) -> Decimal:
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise Account1BuyBlocked("지정가 형식이 올바르지 않습니다.") from exc
    if not price.is_finite() or price <= 0:
        raise Account1BuyBlocked("지정가는 0보다 커야 합니다.")
    if price != price.quantize(Decimal("0.01")):
        raise Account1BuyBlocked("미국주식 지정가는 소수점 둘째 자리까지만 입력할 수 있습니다.")
    return price.quantize(Decimal("0.01"))


class Account1LiveBroker:
    """account1 실계좌 전용 조회·매수 어댑터. 주문 TR은 매수만 노출한다."""

    def __init__(self, config, *, client=None, token=None, evidence=None,
                 orders=None, reserve_bps=100, clock=time.time):
        if config.mode != "real" or config.base_url != REAL_BASE_URL:
            raise KiwoomError("account1 주문은 실계좌 설정에서만 허용합니다.")
        self.client = client or KiwoomReadOnlyClient(config)
        self.token = str(token or self.client.issue_token()["token"])
        self.evidence = evidence or ExecutionEvidence(self.client, self.token, account="account1")
        self.orders = orders or KiwoomOrderClient(config, self.client.session)
        self.reserve_bps = int(reserve_bps)
        self.clock = clock

    def quote(self, ticker):
        """세 거래소 중 종목·거래소가 정확히 일치하는 호가 응답 하나만 채택한다."""
        matches = []
        for exchange in US_EXCHANGES:
            try:
                quote = KiwoomAllocationLiveBroker._read_retry(
                    lambda: self.client.get_overseas_orderbook(
                        self.token, exchange=exchange, ticker=ticker,
                    )
                )["quote"]
            except KiwoomError:
                continue
            if (str(quote.get("stk_cd") or "").strip().upper() != ticker
                    or str(quote.get("stex_tp") or "").strip().upper() != exchange):
                continue
            try:
                price = abs(Decimal(str(quote.get("cur_prc")).replace(",", ""))).quantize(
                    Decimal(".01"), rounding=ROUND_HALF_UP,
                )
            except (InvalidOperation, ValueError):
                continue
            if price <= 0:
                continue
            try:
                as_of = datetime.strptime(
                    f"{quote['dt']} {quote['bid_tm']}", "%Y%m%d %H:%M"
                ).replace(tzinfo=NEW_YORK).timestamp()
            except (KeyError, TypeError, ValueError):
                as_of = None
            matches.append({"exchange": exchange, "price": price, "as_of": as_of})
        if len(matches) != 1:
            raise Account1BuyBlocked(
                f"{ticker}: 키움에서 거래소를 하나로 확정하지 못했습니다(일치 {len(matches)}건)."
            )
        return matches[0]

    def buy_capacity(self, ticker, exchange, price):
        return self.evidence.buy_capacity(ticker, exchange, str(price), reserve_bps=self.reserve_bps)

    def buy(self, ticker, exchange, quantity, price):
        return self.orders.buy_us_limit(
            self.token, exchange=exchange, ticker=ticker,
            quantity=int(quantity), price=float(price),
        )

    def lookup(self, intent):
        return self.evidence.lookup(intent)


class Account1BuyOrders:
    def __init__(self, path, broker_factory, *, clock=time.time,
                 preview_ttl=PREVIEW_TTL_SECONDS):
        self.path = str(path)
        self.broker_factory = broker_factory
        self.clock = clock
        self.preview_ttl = preview_ttl
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS account1_buy_orders (
                id TEXT PRIMARY KEY,
                scan_result_id INTEGER,
                ticker TEXT NOT NULL,
                name TEXT,
                exchange TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity >= 1),
                price TEXT NOT NULL,
                state TEXT NOT NULL,
                order_number TEXT,
                broker_state TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                sent_at REAL,
                updated_at REAL NOT NULL,
                details TEXT NOT NULL)''')
            # 같은 종목의 확인되지 않은 주문은 하나만 존재할 수 있다.
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS account1_buy_in_flight"
                       " ON account1_buy_orders(ticker)"
                       " WHERE state IN ('SENDING', 'UNKNOWN')")

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

    def status(self, order_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM account1_buy_orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise Account1BuyBlocked("주문 기록을 찾을 수 없습니다.")
        result = dict(row)
        result["details"] = json.loads(result["details"])
        return result

    def recent(self, ticker=None, limit=20):
        query, args = "SELECT * FROM account1_buy_orders WHERE state != 'PREPARED'", []
        if ticker:
            query += " AND ticker=?"
            args.append(str(ticker).strip().upper())
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        with self.db() as db:
            rows = [dict(r) for r in db.execute(query, args)]
        for row in rows:
            row["details"] = json.loads(row["details"])
        return rows

    def _in_flight(self, ticker):
        with self.db() as db:
            return db.execute(
                "SELECT id, state FROM account1_buy_orders WHERE ticker=? AND state IN (?, ?)",
                (ticker, *IN_FLIGHT_STATES),
            ).fetchone()

    def _set_state(self, order_id, state, *, expected, **fields):
        details = fields.pop("details", None)
        sets = ["state=?", "updated_at=?"]
        args = [state, self.clock()]
        for key, value in fields.items():
            sets.append(f"{key}=?")
            args.append(value)
        with self.db() as db:
            if details is not None:
                current = db.execute("SELECT details FROM account1_buy_orders WHERE id=?",
                                     (order_id,)).fetchone()
                merged = {**json.loads(current["details"]), **details}
                sets.append("details=?")
                args.append(json.dumps(merged, ensure_ascii=False))
            cursor = db.execute(
                f"UPDATE account1_buy_orders SET {', '.join(sets)} WHERE id=? AND state=?",
                (*args, order_id, expected),
            )
            return cursor.rowcount == 1

    def prepare(self, candidate, quantity, limit_price):
        """스캔 후보 한 건의 매수 미리보기를 만든다. 주문은 보내지 않는다."""
        if str(candidate.get("market") or "").upper() != "US":
            raise Account1BuyBlocked("자유투자(account1)는 미국주식만 주문합니다. 국내 계좌는 아직 연결하지 않았습니다.")
        if candidate.get("strict_filter_passed") is False:
            raise Account1BuyBlocked("Strict 필터에서 거부된 신호는 매수할 수 없습니다.")
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker or len(ticker) > 12 or not ticker.replace(".", "").isalnum():
            raise Account1BuyBlocked("유효한 미국주식 종목코드가 아닙니다.")
        if type(quantity) is not int or quantity < 1:
            raise Account1BuyBlocked("수량은 1주 이상의 정수여야 합니다.")
        price = _cents(limit_price)
        busy = self._in_flight(ticker)
        if busy:
            raise Account1BuyBlocked(
                f"{ticker}에 접수 여부가 확인되지 않은 주문({busy['state']})이 있습니다. 키움에서 확인 후 정리하세요."
            )

        broker = self.broker_factory()
        quote = broker.quote(ticker)
        exchange, current = quote["exchange"], quote["price"]
        if price > current * (1 + MAX_ABOVE_CURRENT_PCT / 100):
            raise Account1BuyBlocked(
                f"지정가가 현재가 ${current}보다 {MAX_ABOVE_CURRENT_PCT}% 넘게 높습니다. 입력을 확인하세요."
            )
        warnings = []
        if price < current * (1 - LOW_LIMIT_WARN_PCT / 100):
            warnings.append(f"지정가가 현재가보다 {LOW_LIMIT_WARN_PCT}% 넘게 낮아 체결되지 않을 수 있습니다.")
        now = self.clock()
        if quote.get("as_of") is None:
            warnings.append("키움 시세 시각을 확인하지 못했습니다.")
        elif now - quote["as_of"] > STALE_QUOTE_WARN_SECONDS:
            warnings.append("시세가 최신이 아닙니다(장 시간 외일 수 있음). 지정가를 다시 확인하세요.")

        stop = candidate.get("stop_loss")
        risk = None
        if stop is not None:
            stop = Decimal(str(stop))
            if price <= stop:
                raise Account1BuyBlocked(f"지정가가 손절가 ${stop:.2f} 이하입니다.")
            risk = {"stop_loss": str(stop.quantize(Decimal("0.01"))),
                    "risk_per_share": str((price - stop).quantize(Decimal("0.01"))),
                    "total_risk": str(((price - stop) * quantity).quantize(Decimal("0.01")))}
        else:
            warnings.append("이 스캔 결과에는 손절가가 없습니다.")

        try:
            capacity = broker.buy_capacity(ticker, exchange, price)
        except KiwoomError as exc:
            raise Account1BuyBlocked("키움 주문가능수량을 확인하지 못했습니다.") from exc
        max_qty = int(capacity["max_quantity_with_reserve"])
        if quantity > max_qty:
            raise Account1BuyBlocked(f"키움 주문가능수량(여유금 적용) {max_qty}주보다 많습니다.")

        order_id = uuid.uuid4().hex
        details = {"current_price": str(current), "quote_as_of": quote.get("as_of"),
                   "risk": risk, "warnings": warnings,
                   "capacity_at_preview": {"max_quantity_with_reserve": max_qty,
                                           "broker_orderable_cash": capacity.get("broker_orderable_cash")}}
        with self.db() as db:
            db.execute(
                "INSERT INTO account1_buy_orders (id, scan_result_id, ticker, name, exchange,"
                " quantity, price, state, created_at, expires_at, updated_at, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?, ?, ?, ?)",
                (order_id, candidate.get("id"), ticker, candidate.get("name"), exchange,
                 quantity, str(price), now, now + self.preview_ttl, now,
                 json.dumps(details, ensure_ascii=False)),
            )
        return self.status(order_id)

    def execute(self, order_id, confirmation_ticker):
        """미리보기 한 건을 한 번만 전송한다. 실패를 재전송으로 메우지 않는다."""
        row = self.status(order_id)
        if str(confirmation_ticker or "").strip().upper() != row["ticker"]:
            raise Account1BuyBlocked("확인용 종목코드가 일치하지 않습니다.")
        now = self.clock()
        try:
            with self.db() as db:
                claimed = db.execute(
                    "UPDATE account1_buy_orders SET state='SENDING', updated_at=?"
                    " WHERE id=? AND state='PREPARED' AND expires_at >= ?",
                    (now, order_id, now),
                ).rowcount == 1
        except sqlite3.IntegrityError as exc:
            raise Account1BuyBlocked(
                f"{row['ticker']}에 확인되지 않은 주문이 있어 전송하지 않았습니다."
            ) from exc
        if not claimed:
            raise Account1BuyBlocked("미리보기가 만료되었거나 이미 사용되었습니다. 다시 미리보기하세요.")

        price = Decimal(row["price"])
        try:
            broker = self.broker_factory()
            capacity = broker.buy_capacity(row["ticker"], row["exchange"], price)
            max_qty = int(capacity["max_quantity_with_reserve"])
        except Exception as exc:  # 전송 전 검증 실패: 주문을 보내지 않았다.
            self._set_state(order_id, "BLOCKED", expected="SENDING",
                            details={"error": "전송 직전 주문가능수량 확인 실패"})
            raise Account1BuyBlocked("전송 직전 키움 주문가능수량을 확인하지 못해 주문하지 않았습니다.") from exc
        if row["quantity"] > max_qty:
            self._set_state(order_id, "BLOCKED", expected="SENDING",
                            details={"error": f"주문가능수량 {max_qty}주 초과"})
            raise Account1BuyBlocked(f"전송 직전 주문가능수량이 {max_qty}주로 줄어 주문하지 않았습니다.")

        sent_at = self.clock()
        with self.db() as db:
            db.execute("UPDATE account1_buy_orders SET sent_at=?, updated_at=? WHERE id=? AND state='SENDING'",
                       (sent_at, sent_at, order_id))
        try:
            result = broker.buy(row["ticker"], row["exchange"], row["quantity"], price)
        except OrderRejected as exc:
            self._set_state(order_id, "REJECTED", expected="SENDING", details={"error": str(exc)})
            return self.status(order_id)
        except Exception:
            # 요청이 증권사에 닿았을 수 있다. 주문내역 확인 전에는 재전송하지 않는다.
            self._set_state(order_id, "UNKNOWN", expected="SENDING",
                            details={"error": "접수 여부 불명: 키움 주문내역 확인 전 재주문 금지"})
            return self.status(order_id)
        self._set_state(order_id, "SUBMITTED", expected="SENDING",
                        order_number=str(result["order_number"]))
        return self.status(order_id)

    def refresh(self, order_id):
        """접수된 주문을 키움 주문내역과 대조해 체결 상태를 갱신한다. 조회만 한다."""
        row = self.status(order_id)
        if row["state"] != "SUBMITTED" or not row["order_number"]:
            raise Account1BuyBlocked("주문번호가 있는 접수 주문만 조회할 수 있습니다.")
        intent = {"id": row["id"], "order_number": row["order_number"], "ticker": row["ticker"],
                  "side": "BUY", "quantity": row["quantity"], "price": row["price"],
                  "created_at": row["created_at"], "sent_at": row["sent_at"]}
        try:
            match = self.broker_factory().lookup(intent)
        except KiwoomError as exc:
            raise Account1BuyBlocked("키움 주문내역을 조회하지 못했습니다.") from exc
        if match is None:
            raise Account1BuyBlocked("키움 주문내역에서 이 주문을 고유하게 찾지 못했습니다.")
        self._set_state(order_id, "SUBMITTED", expected="SUBMITTED",
                        broker_state=match["state"],
                        details={"filled_quantity": match["filled_quantity"],
                                 "remaining_quantity": match["remaining_quantity"],
                                 "checked_at": self.clock()})
        return self.status(order_id)

    def resolve(self, order_id, confirmation_ticker, note=""):
        """사용자가 키움에서 직접 확인한 SENDING/UNKNOWN 주문의 종목 잠금을 푼다."""
        row = self.status(order_id)
        if str(confirmation_ticker or "").strip().upper() != row["ticker"]:
            raise Account1BuyBlocked("확인용 종목코드가 일치하지 않습니다.")
        if row["state"] not in IN_FLIGHT_STATES:
            raise Account1BuyBlocked("확인되지 않은 주문만 수동 정리할 수 있습니다.")
        if (row["state"] == "SENDING"
                and self.clock() - row["updated_at"] < SENDING_RESOLVE_AFTER_SECONDS):
            raise Account1BuyBlocked("전송 중인 주문입니다. 잠시 후 다시 확인하세요.")
        if not self._set_state(order_id, "RESOLVED_MANUAL", expected=row["state"],
                               details={"resolved_from": row["state"],
                                        "resolved_note": str(note)[:200]}):
            raise Account1BuyBlocked("주문 상태가 바뀌었습니다. 다시 확인하세요.")
        return self.status(order_id)
