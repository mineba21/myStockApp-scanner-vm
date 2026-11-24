"""자동 손절 상태 머신. 30초 tick 하나가 lease 를 잡고 내부 주기를 나눠 실행한다.

순서: 미확정 복구 → 주문 대조(모든 mode, OFF 포함) → 세션 이월 → [SHADOW/LIVE·정규장]
잔고 동기화 → 손절 감시 → 청산 진행(신규/재주문) → 포지션 종료 판정 → 알림 전송.

안전 원칙
- 취소 확정과 최종 체결 수량 반영 전에는 새 매도를 내지 않는다 (DB 유니크 + 선점 조건).
- 전송·취소 결과 불명(UNKNOWN, CANCEL_UNKNOWN)은 자동 재시도하지 않는다. 증권사 주문내역 증거로만 해제한다.
- 신규 주문의 명시적 거부, 취소 거부 후 미체결, 원인 불명 수량 변화는 NEEDS_REVIEW 로 멈춘다.
- LIVE 주문·취소 직전에 모든 실거래 스위치를 다시 읽는다.
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from datetime import date
from decimal import Decimal

from trading.auto_stop import settings as S
from trading.auto_stop.brokers import BalanceUnavailable
from trading.auto_stop.quotes import (MIN_AUTO_PRICE, QuoteUnavailable, consistent, is_jump,
                                      limit_price, reference_price, validate)
from trading.auto_stop.store import UNRESOLVED, ClaimRefused, LeaseLost, dec
from trading.auto_stop.us_calendar import market_clock, ny_date, session_for
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import KiwoomError

log = logging.getLogger(__name__)


def new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Engine:
    def __init__(self, store, broker_factory, *, settings=None, clock=time.time, owner=None,
                 notifier=None, settings_loader=S.Settings.from_env):
        self.store = store
        self.broker_factory = broker_factory      # mode -> broker ("LIVE" 은 조회+주문, 주문 가능 여부는 별도 검사)
        self.settings_loader = settings_loader
        self.settings = settings or settings_loader()
        self.clock = clock
        self.owner = owner or new_owner()
        self.notifier = notifier
        self._brokers = {}
        self.summary = {}

    # ── 공통 ─────────────────────────────────────────────────────
    def now(self):
        return self.clock()

    def _broker(self, mode):
        if mode not in self._brokers:
            self._brokers[mode] = self.broker_factory(mode)
        return self._brokers[mode]

    def _renew(self):
        self.store.renew_lease(self.owner, self.now(), self.settings.lease_sec)

    def _alert(self, key, severity, text):
        self.store.enqueue_alert(self.now(), key, severity, text)

    # 연속 오류는 종류별(status/balance/quote/orders)로 센다: 한 종류의 성공이 다른 종류의 반복 실패를 가리지 않게.
    def _errors(self):
        return dict(self.store.runtime_get("api_errors", {}) or {})

    def _api_ok(self, cat):
        errs = self._errors()
        if errs.get(cat):
            errs[cat] = 0
            self.store.runtime_set("api_errors", errs, self.now())
            self.store.runtime_set("consecutive_api_errors", max(errs.values(), default=0), self.now())

    def _api_error(self, cat, where, exc):
        errs = self._errors()
        errs[cat] = n = int(errs.get(cat, 0)) + 1
        self.store.runtime_set("api_errors", errs, self.now())
        self.store.runtime_set("consecutive_api_errors", max(errs.values()), self.now())
        self.store.runtime_set("last_api_error", {"at": self.now(), "where": where,
                                                  "type": type(exc).__name__}, self.now())
        self.store.bump(ny_date(self.now()).isoformat(), self.settings.mode, "api_error", cat)
        log.warning("[자동손절] %s 실패 (%s, 연속 %d회)", where, type(exc).__name__, n)
        if n >= self.settings.max_consecutive_errors:
            hour = int(self.now() // 3600)
            self._alert(f"health:api_errors:{cat}:{hour}", "URGENT",
                        f"[자동손절] 키움 API({cat}) 연속 오류 {n}회 — 신규 손절 진입을 보류합니다 (기존 주문 대조는 계속).")

    def _error_hold(self) -> bool:
        return max(self._errors().values(), default=0) >= self.settings.max_consecutive_errors

    def _review_event(self, ev, reason, *, position=True):
        self.store.review(self.now(), event_id=ev["id"], position_id=ev["position_id"] if position else None,
                          reason=reason)
        pos = self.store.position(ev["position_id"])
        self._alert(f"event:{ev['id']}:review", "URGENT",
                    f"[자동손절 확인 필요] {pos['ticker']} ({ev['mode']}) — {reason}. 신규 매도를 멈췄습니다. "
                    "기존 주문 체결 추적은 계속합니다.")

    def _review_position(self, pos, reason):
        self.store.review(self.now(), position_id=pos["id"], reason=reason)
        ev = self.store.open_event_for(pos["id"])
        if ev and ev["state"] in ("OPEN", "CARRY_OVER"):
            self.store.review(self.now(), event_id=ev["id"], reason=reason)
        self._alert(f"position:{pos['id']}:review:{reason[:40]}", "URGENT",
                    f"[자동손절 확인 필요] {pos['ticker']} ({pos['mode']}) — {reason}")

    # ── tick ────────────────────────────────────────────────────
    def tick(self) -> dict:
        self.settings = self.settings_loader()
        now = self.now()
        self.summary = {"status": "OK", "mode": self.settings.mode, "sent": 0, "cancels": 0, "triggers": 0}
        if not self.store.acquire_lease(self.owner, now, self.settings.lease_sec):
            self.summary["status"] = "LEASE_BUSY"
            return self.summary
        try:
            mc = market_clock(now, self.settings)
            self.summary["market_open"] = mc.is_open
            if self.settings.mode == "SHADOW":
                self._track_shadow_config()
            self._recover_stale()
            self._reconcile_all()
            self._rollover(mc)
            if mc.is_open:
                self._tick_stats(mc)
                self._sync_other_modes()
            if self.settings.mode in ("SHADOW", "LIVE") and mc.is_open:
                self._session_notice(mc)
                if not self._error_hold():
                    holdings = self._balance_sync(force=False)
                    self._watch(mc, holdings)
                    self._advance(mc)
                else:
                    self._probe()
            self._finalize()
            if self.settings.mode in ("SHADOW", "LIVE") and mc.after_close:
                self._session_summary(mc)
            self.store.runtime_set("last_tick_ok_at", self.now(), self.now())
        except LeaseLost:
            # lease 를 잃은 작업자는 heartbeat·알림 전송을 포함한 모든 쓰기를 멈춘다.
            self.summary["status"] = "LEASE_LOST"
            log.warning("[자동손절] lease 상실 — 이 작업자는 중단합니다.")
            return self.summary
        except BaseException:
            self._end_tick()
            raise
        self._end_tick()
        return self.summary

    # ── SHADOW 설정 기록 ──────────────────────────────────────────
    def _shadow_config(self, mode="SHADOW"):
        model = getattr(self._broker(mode), "model", None) or self.settings.shadow_fill_model
        return {"fill_model": model, "shadow_replace_enabled": self.settings.shadow_replace_enabled}

    def _track_shadow_config(self):
        """SHADOW 체결 모델·재주문 스위치 변경 시각을 감사 기록에 남긴다 (일일 보고의 모델 분리 근거)."""
        current = {"fill_model": self.settings.shadow_fill_model,
                   "shadow_replace_enabled": self.settings.shadow_replace_enabled}
        prev = self.store.runtime_get("shadow_config")
        if prev == current:
            return
        now = self.now()
        self.store.runtime_set("shadow_config", current, now)
        self.store.audit(now, "config", "shadow_config", "SHADOW", {"from": prev, "to": current})
        if prev is not None:
            self._alert(f"shadow_config:{int(now)}", "WARN",
                        f"[자동손절] SHADOW 설정 변경: {prev} → {current}. 일일 보고는 모델별로 분리해 집계합니다.")

    # ── 운영 통계 (SHADOW 일일 보고서 근거) ────────────────────────
    def _day(self):
        return ny_date(self.now()).isoformat()

    def _stat(self, metric, key="", n=1):
        self.store.bump(self._day(), self.settings.mode, metric, key, n)

    def _tick_stats(self, mc):
        last = self.store.runtime_get(f"last_open_tick_at:{self.settings.mode}")
        now = self.now()
        if last and ny_date(last) == mc.session.day:
            self.store.stat_max(self._day(), self.settings.mode, "max_tick_gap_sec", now - last)
        self.store.runtime_set(f"last_open_tick_at:{self.settings.mode}", now, now)
        self._stat("ticks")

    def _collect_429(self, broker):
        pop = getattr(broker, "pop_429", None)
        n = pop() if pop else 0
        if n:
            self._stat("http_429", "", n)

    def _sync_other_modes(self):
        """현재 운영 모드가 아닌 포지션(OFF 포함)도 잔고는 주기적으로 맞춘다. 주문·트리거는 하지 않는다."""
        for mode in ("SHADOW", "LIVE"):
            if mode == self.settings.mode or not self.store.positions(mode=mode, states=("ACTIVE", "EXITING")):
                continue
            self._balance_sync(force=False, mode=mode)

    def killed(self):
        return self.store.kill_switch()

    def _end_tick(self):
        self.store.runtime_set("heartbeat_at", self.now(), self.now())
        self._flush_alerts()
        self.store.release_lease(self.owner, self.now())

    def _session_summary(self, mc):
        from trading.auto_stop.service import daily_report, report_text
        day = mc.session.day.isoformat()
        rep = daily_report(self.store, day, self.settings.mode, self.settings, now=self.now())
        warn = rep["unresolved_events"] or rep["unresolved_orders"] or rep["watch"]["missed"] or rep["api"]["errors"]
        self._alert(f"session:{day}:{self.settings.mode}:summary", "WARN" if warn else "INFO", report_text(rep))

    def _probe(self):
        """오류 보류 중 회복 확인: 조회만 하고 성공한 종류의 카운터를 0으로 되돌린다. 주문·트리거 없음."""
        last = self.store.runtime_get("last_probe_at", 0) or 0
        if self.now() - last < self.settings.price_interval_sec:
            return
        self.store.runtime_set("last_probe_at", self.now(), self.now())
        mode = self.settings.mode
        positions = self.store.positions(mode=mode, states=("ACTIVE", "EXITING"))
        errs = self._errors()
        try:
            self._renew()
            broker = self._broker(mode)
            if errs.get("balance"):
                broker.holdings()
                self._api_ok("balance")
            if positions and errs.get("quote"):
                broker.quote(positions[0]["ticker"], positions[0]["exchange"])
                self._api_ok("quote")
            if positions and errs.get("orders"):
                broker.day_orders(positions[0]["ticker"])
                self._api_ok("orders")
        except LeaseLost:
            raise
        except Exception as exc:
            log.info("[자동손절] 회복 확인 실패: %s", type(exc).__name__)

    # ── 1. 재시작·lease 만료 복구 ────────────────────────────────
    def _recover_stale(self):
        """tick 시작 시점에 이 작업자가 전송 중인 주문은 없다 → 남은 SENDING 은 이전 작업자의 것."""
        for o in self.store.orders(states=("SENDING",)):
            if self.store.transition(o["id"], self.now(), frm="SENDING", to="UNKNOWN"):
                self._alert(f"order:{o['id']}:unknown", "URGENT",
                            f"[자동손절 UNKNOWN] {o['ticker']} 매도 {o['requested_qty']}주 — 전송 중 중단되어 접수 여부 불명. "
                            "자동 재전송하지 않습니다. 증권사 주문내역 대조가 필요합니다.")
        for o in self.store.orders(states=("CANCEL_SENDING",)):
            if self.store.transition(o["id"], self.now(), frm="CANCEL_SENDING", to="CANCEL_UNKNOWN"):
                self._alert(f"order:{o['id']}:cancel_unknown", "URGENT",
                            f"[자동손절 CANCEL_UNKNOWN] {o['ticker']} 취소 요청 중 중단 — 자동 재시도하지 않습니다.")

    # ── 2. 주문 대조 (mode 는 주문 행 기준) ──────────────────────
    def _reconcile_all(self, force=False):
        for o in self.store.orders(states=UNRESOLVED):
            if o["state"] in ("SENDING", "CANCEL_SENDING"):
                continue
            if not force and o["last_checked_at"] and self.now() - o["last_checked_at"] < self.settings.status_interval_sec:
                continue
            self._reconcile_one(o)

    def _reconcile_one(self, o):
        now = self.now()
        try:
            broker = self._broker(o["mode"])
            self._renew()
            row = broker.status(o)
            self._renew()
            self._collect_429(broker)
            self._api_ok("status")
        except LeaseLost:
            raise
        except Exception as exc:
            self._api_error("status", f"주문대조 {o['ticker']}", exc)
            self._stat("status_fail", o["ticker"])
            return
        self.store.set_order_fields(o["id"], now, last_checked_at=now)
        ev = self.store.event(o["exit_event_id"])
        if row is None:
            if not o["order_number"]:
                self._alert(f"order:{o['id']}:no_number", "URGENT",
                            f"[자동손절] {o['ticker']} 주문번호 없는 UNKNOWN — 시각·수량만 맞는 주문은 자동 채택하지 않습니다. "
                            "키움 주문내역을 확인하고 증거 기반 해제를 진행하세요.")
            elif o["sent_at"] and now - o["sent_at"] > 600:
                self._alert(f"order:{o['id']}:not_visible", "WARN",
                            f"[자동손절] {o['ticker']} 주문 {o['order_number']}이 10분째 주문내역에서 대조되지 않습니다.")
            return
        result = self.store.apply_fill(o["id"], now, int(row["filled_quantity"]),
                                       {"source": "status", "row": row})
        if result == "ANOMALY":
            self._review_event(ev, f"체결 수량 이상 (기록 {o['filled_qty']} → 조회 {row['filled_quantity']})")
            return
        if result == "APPLIED":
            done = row["filled_quantity"] >= o["requested_qty"]
            self._alert(f"order:{o['id']}:fill:{row['filled_quantity']}", "INFO",
                        f"[자동손절] {o['ticker']} {'전량' if done else '부분'} 체결 {row['filled_quantity']}/"
                        f"{o['requested_qty']}주 @ 지정가 {o['limit_price']} ({o['mode']})")
        state = row["state"]
        if state == "FILLED":
            self.store.transition(o["id"], now, frm=UNRESOLVED, to="FILLED", broker_state=state)
        elif state == "CANCELLED":
            if int(row.get("remaining_quantity", 0)) != 0:
                self._review_event(ev, "취소 응답의 잔여 수량이 0이 아닙니다")
                return
            was_ours = o["state"] in ("CANCEL_PENDING", "CANCEL_UNKNOWN")
            expired = self._after_order_session(o)
            self.store.transition(o["id"], now, frm=UNRESOLVED, to="CANCELLED", broker_state=state)
            if was_ours:
                self._alert(f"order:{o['id']}:cancelled", "INFO",
                            f"[자동손절] {o['ticker']} 취소 확정 (최종 체결 {row['filled_quantity']}주)")
            elif expired:
                # 장 마감 후 소멸로 본다. 증권사 표기 방식은 미검증이므로 알림으로 남긴다.
                self._alert(f"order:{o['id']}:expired", "WARN",
                            f"[자동손절] {o['ticker']} 주문 {o['order_number']}이 장 마감 후 취소 상태로 확인됨 "
                            f"(최종 체결 {row['filled_quantity']}주). 잔여는 이월 규칙을 따릅니다.")
            else:
                self._review_event(ev, "자동 손절 주문이 외부에서 취소되었습니다")
        elif state == "REJECTED":
            self.store.transition(o["id"], now, frm=UNRESOLVED, to="REJECTED", broker_state=state)
            self._review_event(ev, "증권사가 주문을 무효/거부 처리했습니다")
        elif state in ("OPEN", "PARTIAL"):
            if o["state"] in ("SUBMITTED", "PARTIAL_FILLED", "UNKNOWN"):
                to = "PARTIAL_FILLED" if int(row["filled_quantity"]) > 0 else "SUBMITTED"
                self.store.transition(o["id"], now, frm=o["state"], to=to, broker_state=state)
                if o["state"] == "UNKNOWN":
                    self._alert(f"order:{o['id']}:unknown_resolved", "INFO",
                                f"[자동손절] {o['ticker']} UNKNOWN 주문이 증권사 주문내역으로 확인되어 추적을 재개합니다.")
            else:
                self.store.set_order_fields(o["id"], now, broker_state=state)
        else:
            self.store.set_order_fields(o["id"], now, broker_state=state)
            self._review_event(ev, f"증권사 주문 상태 해석 불가({state})")

    def _after_order_session(self, o) -> bool:
        day = date.fromisoformat(o["order_date"]) if o["order_date"] else (
            ny_date(o["sent_at"]) if o["sent_at"] else None)
        if day is None:
            return False
        s = session_for(day, extra_closed=self.settings.extra_closed_dates,
                        extra_early=self.settings.extra_early_close_dates)
        return s is not None and self.now() >= s.close_ts

    # ── 3. 세션 이월 ─────────────────────────────────────────────
    def _rollover(self, mc):
        now = self.now()
        today = mc.session.day if mc.session else ny_date(now)
        for ev in self.store.events(states=("OPEN", "CARRY_OVER")):
            pos = self.store.position(ev["position_id"])
            if pos["remaining_managed_qty"] == 0:
                continue
            unresolved = self.store.orders(event_id=ev["id"], states=UNRESOLVED)
            ev_day = date.fromisoformat(ev["session_date"])
            ev_session = session_for(ev_day, extra_closed=self.settings.extra_closed_dates,
                                     extra_early=self.settings.extra_early_close_dates)
            ev_closed = ev_session is None or now >= ev_session.close_ts
            # A) 장 종료 후: 모든 주문이 종료·체결 확정됐을 때만 이월 대기
            if ev["state"] == "OPEN" and ev_closed and not unresolved:
                if self.store.update_event(ev["id"], now, expect_states=("OPEN",), state="CARRY_OVER"):
                    self._alert(f"event:{ev['id']}:carry:{ev['session_date']}", "WARN",
                                f"[자동손절] {pos['ticker']} 장 마감 미청산 {pos['remaining_managed_qty']}주 — "
                                "다음 정규장에 청산을 이어갑니다.")
            # B) 새 정규장: 이월 횟수 검증 후 재개 (현재 운영 모드의 이벤트만. 다른 모드 이벤트는 대조만 계속)
            if mc.is_open and ev_day < today and ev["mode"] == self.settings.mode:
                ev = self.store.event(ev["id"])
                carry = ev["carry_over_count"] + 1
                if carry > self.settings.max_carry_over_sessions:
                    self._review_event(ev, f"이월 한도({self.settings.max_carry_over_sessions}회) 초과로 청산 미완료")
                    continue
                fields = {"carry_over_count": carry, "session_date": today.isoformat(), "session_replace_count": 0}
                has_orders = bool(self.store.orders(event_id=ev["id"]))
                if ev["mode"] == "LIVE" and has_orders and not self.settings.verified("POST_CLOSE_EXPIRY"):
                    # 장 마감 후 미체결 지정가 처리 방식이 미검증이면 다음 날 자동 재주문하지 않는다 (대조는 계속).
                    self._review_event(ev, "장 마감 후 주문 처리(POST_CLOSE_EXPIRY) 미검증 — LIVE 이월 주문을 하지 않습니다")
                    continue
                if unresolved:
                    # 전날 주문이 살아 있으면 추적을 이어가고, 새 주문은 그 주문이 종료된 뒤에만 가능
                    self.store.update_event(ev["id"], now, expect_states=("OPEN", "CARRY_OVER"), state="OPEN",
                                            **fields)
                    self._alert(f"event:{ev['id']}:overnight:{today}", "WARN",
                                f"[자동손절] {pos['ticker']} 전날 주문이 아직 종료 확인되지 않았습니다. 새 주문 없이 추적합니다.")
                    continue
                fields["next_order_kind"] = "CARRY_OVER" if has_orders else "ORIGINAL"
                self.store.update_event(ev["id"], now, expect_states=("OPEN", "CARRY_OVER"), state="OPEN", **fields)

    # ── 4. 잔고 동기화 ───────────────────────────────────────────
    def _balance_sync(self, force: bool, mode=None):
        mode = mode or self.settings.mode
        positions = self.store.positions(mode=mode, states=("ACTIVE", "EXITING"))
        if not positions:
            return {}
        last = self.store.runtime_get(f"last_balance_at:{mode}", 0) or 0
        if not force and self.now() - last < self.settings.balance_sync_sec:
            return None
        try:
            self._renew()
            broker = self._broker(mode)
            holdings = broker.holdings()
            self._renew()
            self._collect_429(broker)
            self._api_ok("balance")
        except LeaseLost:
            raise
        except Exception as exc:
            self._api_error("balance", "잔고 동기화", exc)
            return None
        now = self.now()
        self.store.runtime_set(f"last_balance_at:{mode}", now, now)
        for pos in positions:
            h = holdings.get(pos["ticker"], {"qty": 0, "sellable": 0, "avg_cost": None})
            unresolved = any(self.store.orders(event_id=e["id"], states=UNRESOLVED)
                             for e in self.store.events() if e["position_id"] == pos["id"])
            self.store.update_position(pos["id"], now, broker_qty_last=h["qty"], last_balance_at=now)
            if unresolved:
                continue
            if h["qty"] == 0 and pos["state"] == "ACTIVE":
                self.store.update_position(pos["id"], now, expect_states=("ACTIVE",), state="EXTERNAL_CLOSED",
                                           review_reason="외부에서 전량 매도됨")
                self._alert(f"position:{pos['id']}:external_closed", "WARN",
                            f"[자동손절] {pos['ticker']} 보유 0주 — 외부 전량 매도로 보고 자동 손절을 종료합니다. "
                            "재매수해도 자동 복구하지 않습니다.")
                continue
            if 0 < h["qty"] < pos["remaining_managed_qty"] or (h["qty"] == 0 and pos["remaining_managed_qty"] > 0):
                self._review_position(pos, f"보유 {h['qty']}주 < 관리 잔여 {pos['remaining_managed_qty']}주 (원인 불명)")
                continue
            avg0, avg = pos["avg_cost_at_register"], h["avg_cost"]
            if avg0 and avg and Decimal(avg0) > 0 and h["qty"] != pos["broker_qty_at_register"]:
                ratio = Decimal(avg) / Decimal(avg0)
                if ratio < Decimal("0.6") or ratio > Decimal("1.6"):
                    self._review_position(pos, "보유수량과 평균단가가 함께 크게 변해 분할·병합이 의심됩니다")
        return holdings

    # ── 5. 손절 감시 ─────────────────────────────────────────────
    def _quote(self, pos):
        self._renew()
        broker = self._broker(pos["mode"])
        q = broker.quote(pos["ticker"], pos["exchange"])
        self._renew()
        self._collect_429(broker)
        self._api_ok("quote")
        return q

    def _confirmed_quote(self, pos, mc):
        """유효 시세. 직전 유효값 대비 급변이면 즉시 재조회해 교차 확인한다. 실패 시 (None, 사유)."""
        try:
            q = self._quote(pos)
        except LeaseLost:
            raise
        except (QuoteUnavailable, KiwoomError) as exc:
            self._api_error("quote", f"시세 {pos['ticker']}", exc)
            self._stat("quote_fail", pos["ticker"])
            return None, None
        reason = validate(q, self.now(), mc, self.settings)
        if reason:
            self.store.runtime_set("last_quote_issue", {"ticker": pos["ticker"], "reason": reason,
                                                        "at": self.now()}, self.now())
            self._stat("quote_invalid", pos["ticker"])
            if q.as_of is not None:
                self.store.stat_max(self._day(), self.settings.mode, "max_quote_age_sec", self.now() - q.as_of,
                                    pos["ticker"])
            return None, None
        if q.as_of is not None:
            self.store.stat_max(self._day(), self.settings.mode, "max_quote_age_sec", self.now() - q.as_of,
                                pos["ticker"])
        prev = Decimal(pos["last_quote_price"]) if pos["last_quote_price"] else None
        if is_jump(prev, q.price, self.settings):
            try:
                q2 = self._quote(pos)
            except LeaseLost:
                raise
            except Exception:
                return None, "급변 시세 재조회 실패"
            if validate(q2, self.now(), mc, self.settings) or not consistent(q.price, q2.price, self.settings):
                return None, f"급변 시세({prev}→{q.price})를 재조회로 확인하지 못했습니다"
            if q2.bid is not None and not consistent(q2.price, q2.bid, self.settings) and q2.bid > q2.price:
                return None, "현재가와 호가가 일치하지 않습니다"
            holdings = self._balance_sync(force=True)
            fresh = self.store.position(pos["id"])
            if holdings is None or fresh["state"] not in ("ACTIVE", "EXITING"):
                return None, "급변 시 잔고 확인 실패 또는 분할·병합 의심"
            q = q2
        self.store.runtime_set("last_quote_ok_at", self.now(), self.now())
        return q, None

    def _watch(self, mc, holdings):
        now = self.now()
        for pos in self.store.positions(mode=self.settings.mode, states=("ACTIVE",)):
            if pos["auto_paused"]:
                continue
            if pos["last_price_check_at"] and now - pos["last_price_check_at"] < self.settings.price_interval_sec:
                continue
            q, problem = self._confirmed_quote(pos, mc)
            if problem:
                self._review_position(pos, problem)
                continue
            if q is None:
                continue
            self.store.update_position(pos["id"], self.now(), last_quote_price=str(q.price),
                                       last_quote_at=q.as_of, last_price_check_at=self.now())
            self._stat("quote_ok", pos["ticker"])
            if q.price > dec(pos["stop_price"]):
                continue
            state = "OPEN" if mc.before_cutoff else "CARRY_OVER"
            eid = self.store.create_event(self.now(), pos, state=state, trigger_price=q.price,
                                          trigger_as_of=q.as_of, session_date=mc.session.day.isoformat())
            if eid:
                self.summary["triggers"] += 1
                note = "" if state == "OPEN" else " (마감 cutoff 이후 — 다음 정규장에 주문)"
                self._alert(f"event:{eid}:trigger", "URGENT",
                            f"[자동손절 발생] {pos['ticker']} 현재가 {q.price} ≤ 손절가 {pos['stop_price']} "
                            f"· 관리 {pos['remaining_managed_qty']}주 ({pos['mode']}){note}")

    # ── 6. 청산 진행 ─────────────────────────────────────────────
    def _guard(self, mode):
        if mode == "LIVE":
            return lambda: S.live_order_block_reason(self.settings_loader())
        return lambda: None

    def _cancel_guard(self, mode):
        if mode == "LIVE":
            return lambda: S.live_cancel_block_reason(self.settings_loader())
        # SHADOW 가상 취소: SHADOW 전용 스위치만 본다. ShadowBroker 는 주문 클라이언트가 없어 실취소가 불가능하다.
        return lambda: (None if self.settings_loader().shadow_replace_enabled
                        else "AUTO_STOP_US_SHADOW_REPLACE_ENABLED 꺼짐")

    def _replace_allowed(self, ev):
        return (ev["session_replace_count"] < self.settings.max_replaces_per_session
                and ev["event_replace_count"] < self.settings.max_replaces_per_event)

    def _advance(self, mc):
        kill = self.killed()
        if kill:
            if self.store.events(states=("OPEN",), mode=self.settings.mode):
                self._alert(f"kill:{self._day()}:{kill.get('at')}", "WARN",
                            "[자동손절] 전체 긴급 중지 중 — 신규 매도·취소·재주문을 하지 않습니다. 기존 주문 대조는 계속합니다.")
            return
        for ev in self.store.events(states=("OPEN",), mode=self.settings.mode):
            pos = self.store.position(ev["position_id"])
            if pos["auto_paused"] or pos["state"] != "EXITING":
                continue
            unresolved = self.store.orders(event_id=ev["id"], states=UNRESOLVED)
            if unresolved:
                self._maybe_replace(mc, ev, pos, unresolved[0])
                continue
            if pos["remaining_managed_qty"] == 0 or not mc.before_cutoff:
                continue
            orders = self.store.orders(event_id=ev["id"])
            if orders and orders[-1]["state"] not in ("FILLED", "CANCELLED"):
                self._review_event(ev, f"직전 주문 상태 {orders[-1]['state']}")
                continue
            if ev["next_order_kind"] == "REPLACE" and not self.settings.replace_enabled_for(ev["mode"]):
                flag = "AUTO_STOP_US_REPLACE_ENABLED" if ev["mode"] == "LIVE" else "AUTO_STOP_US_SHADOW_REPLACE_ENABLED"
                self._review_event(ev, f"잔여 수량 재주문이 필요하지만 {flag}가 꺼져 있습니다")
                continue
            if ev["next_order_kind"] == "REPLACE" and not self._replace_allowed(ev):
                self._review_event(ev, "재주문 한도 도달 (세션 "
                                       f"{ev['session_replace_count']}/{self.settings.max_replaces_per_session}, 이벤트 "
                                       f"{ev['event_replace_count']}/{self.settings.max_replaces_per_event})")
                continue
            self._send(mc, ev, pos)

    def _send(self, mc, ev, pos):
        mode, now = ev["mode"], self.now()
        if mode == "LIVE":
            reason = S.live_order_block_reason(self.settings_loader())
            if reason:
                self._alert(f"event:{ev['id']}:blocked:{mc.session.day}", "WARN",
                            f"[자동손절] {pos['ticker']} 매도 대기 — 실거래 스위치 차단: {reason}")
                return
            if self.store.scanner_buy_in_flight(pos["ticker"]):
                self._alert(f"event:{ev['id']}:buy_in_flight:{mc.session.day}", "WARN",
                            f"[자동손절] {pos['ticker']} 스캐너 매수 주문이 미확정이라 매도를 보류합니다.")
                return
        holdings = self._balance_sync(force=True)
        if holdings is None:
            return
        pos = self.store.position(pos["id"])
        if pos["state"] != "EXITING":
            return
        h = holdings.get(pos["ticker"])
        if h is None:
            self._review_event(ev, "잔고에 종목이 없습니다")
            return
        if mode == "LIVE":
            try:
                self._renew()
                ours = {o["order_number"] for o in self.store.orders(mode="LIVE") if o["order_number"]}
                external = [r for r in self._broker(mode).day_orders(pos["ticker"])
                            if r["state"] in ("OPEN", "PARTIAL") and r["order_number"] not in ours]
                self._renew()
                self._api_ok("orders")
            except LeaseLost:
                raise
            except Exception as exc:
                self._api_error("orders", f"주문내역 {pos['ticker']}", exc)
                return
            if external:
                self._review_event(ev, f"같은 종목의 외부 미체결 주문 {len(external)}건이 있습니다")
                return
        qty = min(pos["remaining_managed_qty"], h["sellable"])
        if qty <= 0:
            self._review_event(ev, f"매도가능수량 {h['sellable']}주 — 매도할 수 없습니다")
            return
        q, problem = self._confirmed_quote(pos, mc)
        if problem:
            self._review_event(ev, problem)
            return
        if q is None:
            return
        self.store.update_position(pos["id"], self.now(), last_quote_price=str(q.price), last_quote_at=q.as_of)
        ref, src = reference_price(q, allow_bid=(mode != "LIVE" or self.settings.verified("USA20101_BID")))
        if ref < MIN_AUTO_PRICE:
            self._review_event(ev, f"기준가 {ref} < $1 — 호가 단위 미검증으로 자동 매도하지 않습니다")
            return
        price = limit_price(ref, self.settings.slip_bps)
        if price <= 0:
            self._review_event(ev, "지정가 계산 결과가 유효하지 않습니다")
            return
        if qty > self.settings.max_order_qty or price * qty > self.settings.max_order_notional_usd:
            self._review_event(ev, f"주문 상한 초과 ({qty}주 × {price})")
            return
        if mode == "LIVE" and qty > self.settings.live_qty_cap():
            self._review_event(ev, f"UST20001_SELL 1주 실매도 검증 전에는 LIVE 주문을 1주로 제한합니다 ({qty}주)")
            return
        kind = ev["next_order_kind"]
        try:
            order = self.store.claim_sell(self.now(), owner=self.owner, event_id=ev["id"], kind=kind, qty=qty,
                                          ref_price=ref, ref_source=src, quote_as_of=q.as_of, limit_price=price,
                                          settings=self.settings, guard=self._guard(mode))
        except ClaimRefused as exc:
            log.info("[자동손절] 매도 선점 거부: %s %s", pos["ticker"], exc)
            return
        sent_at = self.now()
        self.store.set_order_fields(order["id"], sent_at, sent_at=sent_at)
        if mode == "SHADOW":
            # 가상 체결 모델을 주문마다 고정 기록한다: 모델이 바뀌어도 이 주문은 만든 모델로 판정·집계한다.
            self.store.add_order_evidence(order["id"], sent_at, "shadow", self._shadow_config(mode))
        try:
            res = self._broker(mode).sell(pos["ticker"], pos["exchange"], qty, price)
        except OrderRejected as exc:
            self.store.transition(order["id"], self.now(), frm="SENDING", to="REJECTED", broker_state="REJECTED")
            self.store.add_order_evidence(order["id"], self.now(), "reject", str(exc)[:200])
            self._review_event(ev, "매도 주문이 명시적으로 거부되었습니다")
            return
        except OrderUnknown:
            self._mark_unknown(order, "매도 응답 불명")
            return
        except KiwoomError as exc:          # 전송 전 입력 검증 실패: 증권사로 나가지 않음
            self.store.transition(order["id"], self.now(), frm="SENDING", to="NOT_SENT")
            self.store.add_order_evidence(order["id"], self.now(), "not_sent", str(exc)[:200])
            self._review_event(ev, "주문 입력 검증 실패로 전송하지 않았습니다")
            return
        except Exception:
            self._mark_unknown(order, "매도 중 예외")
            return
        number = str(res.get("order_number") or "")
        if not self.store.transition(order["id"], self.now(), frm="SENDING", to="SUBMITTED",
                                     order_number=number, order_date=ny_date(sent_at).isoformat()):
            # lease 만료로 다른 작업자가 UNKNOWN 처리함 → 상태는 바꾸지 않고 증거만 남긴다
            self.store.add_order_evidence(order["id"], self.now(), "late_response", {"order_number": number})
            return
        self.summary["sent"] += 1
        label = {"ORIGINAL": "최초 주문", "REPLACE": "재주문", "CARRY_OVER": "이월 주문"}[kind]
        self._alert(f"order:{order['id']}:submitted", "INFO",
                    f"[자동손절] {pos['ticker']} {label} 접수 {qty}주 @ {price} (기준 {src} {ref}, {mode}, 주문 {number})")

    def _mark_unknown(self, order, why):
        self.store.transition(order["id"], self.now(), frm="SENDING", to="UNKNOWN")
        self._alert(f"order:{order['id']}:unknown", "URGENT",
                    f"[자동손절 UNKNOWN] {order['ticker']} {why} — 자동 재전송하지 않습니다. 증권사 주문내역 대조가 필요합니다.")

    def _maybe_replace(self, mc, ev, pos, o):
        if o["state"] not in ("SUBMITTED", "PARTIAL_FILLED") or not o["sent_at"]:
            return
        now = self.now()
        if now - o["sent_at"] < self.settings.replace_after_sec:
            return
        self._alert(f"order:{o['id']}:stale", "WARN",
                    f"[자동손절] {o['ticker']} 주문 {o['order_number']} {int((now - o['sent_at']) // 60)}분 미체결 "
                    f"(체결 {o['filled_qty']}/{o['requested_qty']}주)")
        if o["cancel_rejected"] or not mc.before_cutoff:
            return
        blocked = self._cancel_guard(ev["mode"])()
        if blocked:
            return
        if not self._replace_allowed(ev):
            self._review_event(ev, "재주문 한도 도달 — 기존 주문은 계속 추적합니다")
            return
        try:
            order = self.store.claim_cancel(now, owner=self.owner, order_id=o["id"], expect_state=o["state"],
                                            guard=self._cancel_guard(ev["mode"]),
                                            lease_sec=self.settings.lease_sec)
        except ClaimRefused:
            return
        try:
            res = self._broker(ev["mode"]).cancel(o["ticker"], o["exchange"], o["order_number"])
        except OrderRejected as exc:
            self.store.transition(o["id"], self.now(), frm="CANCEL_SENDING", to=o["state"], cancel_rejected=1)
            self.store.add_order_evidence(o["id"], self.now(), "cancel_reject", str(exc)[:200])
            self._reconcile_one(self.store.order(o["id"]))
            after = self.store.order(o["id"])
            if after["state"] in UNRESOLVED:
                self._review_event(ev, "취소가 거부되었고 원주문이 아직 미체결입니다")
            return
        except OrderUnknown:
            self._cancel_unknown(order)
            return
        except KiwoomError as exc:
            self.store.transition(o["id"], self.now(), frm="CANCEL_SENDING", to=o["state"], cancel_rejected=1)
            self.store.add_order_evidence(o["id"], self.now(), "cancel_not_sent", str(exc)[:200])
            self._review_event(ev, "취소 입력 검증 실패")
            return
        except Exception:
            self._cancel_unknown(order)
            return
        if self.store.transition(o["id"], self.now(), frm="CANCEL_SENDING", to="CANCEL_PENDING",
                                 cancel_order_number=str(res.get("order_number") or "")):
            self.summary["cancels"] += 1
            self._alert(f"order:{o['id']}:cancel_requested", "INFO",
                        f"[자동손절] {o['ticker']} 미체결 잔여 취소 요청 — 취소 확정 후 최신 시세로 잔여만 재주문합니다.")

    def _cancel_unknown(self, order):
        self.store.transition(order["id"], self.now(), frm="CANCEL_SENDING", to="CANCEL_UNKNOWN")
        self._alert(f"order:{order['id']}:cancel_unknown", "URGENT",
                    f"[자동손절 CANCEL_UNKNOWN] {order['ticker']} 취소 응답 불명 — 자동 재시도·재주문하지 않습니다.")

    # ── 7. 종료 판정 ─────────────────────────────────────────────
    def _finalize(self):
        now = self.now()
        for pos in self.store.positions(states=("EXITING", "NEEDS_REVIEW")):
            if pos["remaining_managed_qty"] != 0:
                continue
            events = [e for e in self.store.events() if e["position_id"] == pos["id"]]
            if any(self.store.orders(event_id=e["id"], states=UNRESOLVED) for e in events):
                continue
            if self.store.update_position(pos["id"], now, expect_states=("EXITING", "NEEDS_REVIEW"), state="SOLD"):
                for e in events:
                    if e["state"] in ("OPEN", "CARRY_OVER", "NEEDS_REVIEW"):
                        self.store.update_event(e["id"], now, state="DONE")
                self._alert(f"position:{pos['id']}:sold", "INFO",
                            f"[자동손절 완료] {pos['ticker']} 관리 수량 {pos['initial_managed_qty']}주 전량 청산 ({pos['mode']})")

    # ── 8. 알림·세션 안내 ────────────────────────────────────────
    def _session_notice(self, mc):
        day = mc.session.day.isoformat()
        n = len(self.store.positions(mode=self.settings.mode, states=("ACTIVE", "EXITING")))
        self._alert(f"session:{day}:{self.settings.mode}:open", "INFO",
                    f"[자동손절] {day} 정규장 감시 시작 ({self.settings.mode}, 대상 {n}종목)")

    def _flush_alerts(self):
        if self.notifier is None:
            return
        for a in self.store.pending_alerts():
            try:
                ok = bool(self.notifier(a["severity"], a["text"]))
            except Exception:
                ok = False
            self.store.mark_alert(a["id"], self.now(), ok)
