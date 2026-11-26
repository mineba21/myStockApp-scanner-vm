"""자동 손절 상태 머신. 30초 tick 하나가 lease 를 잡고 내부 주기를 나눠 실행한다.

순서: 미확정 복구 → 주문 대조(모든 mode, OFF 포함) → 세션 이월 → [SHADOW/LIVE·정규장]
잔고 동기화 → 손절 감시 → 청산 진행(신규/재주문) → 포지션 종료 판정 → 알림 전송.

안전 원칙
- 취소 확정과 최종 체결 수량 반영 전에는 새 매도를 내지 않는다 (DB 유니크 + 선점 조건).
- 전송·취소 결과 불명(UNKNOWN, CANCEL_UNKNOWN)은 자동 재시도하지 않는다. 증권사 주문내역 증거로만 해제한다.
- 신규 주문의 명시적 거부, 취소 거부 후 미체결, 원인 불명 수량 변화는 NEEDS_REVIEW 로 멈춘다.
- LIVE 주문·취소 직전에 모든 실거래 스위치를 다시 읽는다.

시세 장애 격리 (2026-11-26, docs/auto_stop_sell.md "시세 장애 격리와 복구")
- 시세 오류는 종목별로 센다(quote_health). 한 종목의 장애가 다른 정상 종목의 감시·손절 판단을 막지 않는다.
- 범위를 확정할 수 없는 오류(SYSTEM/UNKNOWN)는 전체 카운터에도 센다. 임계값이면 전체 신규 진입을 보류한다.
- 보류 중에도 모든 종목을 예정 시각마다 같은 검증으로 회복 조회하고, 기존 주문 대조는 계속한다.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from datetime import date
from decimal import Decimal

from trading.auto_stop import quote_health as QH
from trading.auto_stop import settings as S
from trading.auto_stop.brokers import BalanceUnavailable
from trading.auto_stop.quotes import (MIN_AUTO_PRICE, consistent, is_jump,
                                      limit_price, reference_price, validate_detail)
from trading.auto_stop.store import UNRESOLVED, ClaimRefused, LeaseLost, dec
from trading.auto_stop.us_calendar import market_clock, ny_date, session_for
from trading.kiwoom_orders import OrderRejected, OrderUnknown
from trading.kiwoom_readonly import KiwoomError

log = logging.getLogger(__name__)


def new_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class Engine:
    def __init__(self, store, broker_factory, *, settings=None, clock=time.time, owner=None,
                 notifier=None, settings_loader=S.Settings.from_env, schema_block=None):
        self.store = store
        # 스키마 버전 불일치(대조 가능한 구조): 기존 주문 대조·잔고 동기화만 하고 신규 감시·주문·취소는 하지 않는다.
        self.schema_block = schema_block
        self.broker_factory = broker_factory      # mode -> broker ("LIVE" 은 조회+주문, 주문 가능 여부는 별도 검사)
        self.settings_loader = settings_loader
        self.settings = settings or settings_loader()
        self.clock = clock
        self.owner = owner or new_owner()
        self.notifier = notifier
        self._brokers = {}
        self._broker_errors = {}
        self.summary = {}
        self._tick_started = None

    # ── 공통 ─────────────────────────────────────────────────────
    def now(self):
        return self.clock()

    def _broker(self, mode):
        # 브로커 생성(토큰 발급 등) 실패는 이 tick 안에서 다시 시도하지 않는다: 종목 수만큼 토큰 요청을 반복하지 않게.
        if mode in self._broker_errors:
            raise self._broker_errors[mode]
        if mode not in self._brokers:
            try:
                self._brokers[mode] = self.broker_factory(mode)
            except Exception as exc:
                try:
                    exc.broker_init = True      # 분류용 표시 (quote_health: SYSTEM 'broker_init')
                except Exception:
                    pass
                self._broker_errors[mode] = exc
                raise
        return self._brokers[mode]

    def _renew(self):
        self.store.renew_lease(self.owner, self.now(), self.settings.lease_sec)

    def _alert(self, key, severity, text):
        self.store.enqueue_alert(self.now(), key, severity, text)

    # 연속 오류는 종류별(status/balance/quote/orders)로 센다: 한 종류의 성공이 다른 종류의 반복 실패를 가리지 않게.
    # 'quote' 는 종목 공통(SYSTEM/UNKNOWN 범위) 시세 오류만 센다. 종목별 시세 오류는 quote_health 레코드에 센다.
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
        return bool(self._hold_reasons())

    def _hold_reasons(self) -> list:
        """전체 신규 진입 보류 사유. 계좌 잔고·주문 상태·주문내역 확인 불가, 시세 공통 오류, 광범위 조회 실패."""
        errs, thr = self._errors(), self.settings.max_consecutive_errors
        reasons = [c for c in ("balance", "status", "orders") if int(errs.get(c, 0) or 0) >= thr]
        if self.store.runtime_get("quote_global_hold") or int(errs.get("quote", 0) or 0) >= thr:
            reasons.append("quote_system")
        if self.settings.mode in ("SHADOW", "LIVE"):
            health = self._health_load(self.settings.mode)
            watched = [p for p in self.store.positions(mode=self.settings.mode, states=("ACTIVE", "EXITING"))
                       if not p["auto_paused"]]
            faulted = sum((health.get(str(p["id"])) or {}).get("state", QH.OK) != QH.OK for p in watched)
            if QH.is_broad(faulted, len(watched)):
                reasons.append("quote_broad")
        return reasons

    def _track_global_hold(self, reasons):
        """전체 보류 사유 변화를 감사 기록에 남긴다 (일일 보고의 보류 시간 근거)."""
        prev = self.store.runtime_get("global_hold_state") or {"reasons": [], "since": None}
        if sorted(prev.get("reasons") or []) == sorted(reasons):
            return
        now = self.now()
        since = (prev.get("since") or now) if prev.get("reasons") else now
        state = {"reasons": sorted(reasons), "since": since if reasons else None}
        self.store.runtime_set("global_hold_state", state, now)
        self.store.audit(now, "engine", "global_hold", self.settings.mode,
                         {"from": sorted(prev.get("reasons") or []), "to": sorted(reasons)})
        if reasons and not prev.get("reasons"):
            self._alert(f"health:global_hold:{int(now)}", "URGENT",
                        f"[자동손절] 전체 신규 손절 진입 보류 ({', '.join(sorted(reasons))}) — 모든 종목 회복 조회와 "
                        "기존 주문 대조는 계속합니다.")
        elif not reasons:
            self._alert(f"health:global_hold_off:{int(now)}", "INFO",
                        f"[자동손절] 전체 보류 해제 (이전 사유: {', '.join(prev.get('reasons') or [])}).")

    # ── 종목별 시세 상태 (auto_stop_runtime JSON, 스키마 변경 없음) ──────────
    def _health_load(self, mode) -> dict:
        return dict(self.store.runtime_get(f"quote_health:{mode}", {}) or {})

    def _health_save(self, mode, health):
        self.store.runtime_set(f"quote_health:{mode}", health, self.now())

    def _migrate_quote_health(self):
        """이전 버전의 종목 공통 시세 카운터(api_errors.quote)를 새 구조로 옮긴다. 근거 없이 0으로 만들지 않는다.

        - 공통 카운터가 임계값 이상이면 전체 시세 보류 레코드를 만든다 (카운터 값은 그대로 둔다).
        - 처음 이행할 때 공통 카운터가 임계값 이상이었다면, 어느 종목의 오류였는지 알 수 없으므로 감시 중인 모든
          종목을 FAULT(legacy_unattributed)로 두고 종목별 회복 확인(서로 다른 시세 시각 2회)을 요구한다.
        """
        now, thr = self.now(), self.settings.max_consecutive_errors
        errs = self._errors()
        legacy = int(errs.get("quote", 0) or 0)
        if legacy >= thr and not self.store.runtime_get("quote_global_hold"):
            self.store.runtime_set("quote_global_hold", {"since": now, "reason": "legacy_common_counter",
                                                         "legacy_count": legacy, "rounds_ok": 0, "seen": []}, now)
            self.store.audit(now, "engine", "quote_hold_on", "global",
                             {"reason": "legacy_common_counter", "count": legacy})
        if self.store.runtime_get("quote_health_version") == 1:
            return
        marked = []
        if legacy >= thr:
            for mode in ("SHADOW", "LIVE"):
                health = self._health_load(mode)
                for p in self.store.positions(mode=mode, states=("ACTIVE", "EXITING")):
                    rec = health.get(str(p["id"])) or QH.new_record(now, p)
                    rec.update(state=QH.FAULT, fault_started_at=now, fault_reason="legacy_unattributed",
                               legacy_global_count=legacy, confirm_as_of=[])
                    health[str(p["id"])] = rec
                    marked.append(f"{mode}:{p['ticker']}")
                if health:
                    self._health_save(mode, health)
        self.store.runtime_set("quote_health_version", 1, now)
        self.store.audit(now, "engine", "quote_health_migrate", "global",
                         {"legacy_api_errors": errs, "marked_fault": marked})

    def _quote_global_error(self, record):
        """SYSTEM/UNKNOWN 범위 시세 오류: 전체 공통 카운터 +1. 임계값이면 전체 시세 보류."""
        now, thr = self.now(), self.settings.max_consecutive_errors
        errs = self._errors()
        errs["quote"] = n = int(errs.get("quote", 0) or 0) + 1
        self.store.runtime_set("api_errors", errs, now)
        self.store.runtime_set("consecutive_api_errors", max(errs.values()), now)
        if n < thr:
            return
        if not self.store.runtime_get("quote_global_hold"):
            self.store.runtime_set("quote_global_hold", {"since": now, "reason": record.get("category"),
                                                         "rounds_ok": 0, "seen": []}, now)
            self.store.audit(now, "engine", "quote_hold_on", "global",
                             {"reason": record.get("category"), "count": n, "ticker": record.get("ticker")})
        self._alert(f"health:api_errors:quote:{int(now // 3600)}", "URGENT",
                    f"[자동손절] 키움 시세 공통 오류 연속 {n}회 ({record.get('category')}) — 전체 신규 손절 진입을 "
                    "보류합니다. 모든 종목 회복 조회와 기존 주문 대조는 계속합니다.")

    def _quote_global_ok(self):
        """증권사가 응답함(유효 시세 또는 종목 단위 검증 실패): 보류 중이 아니면 공통 카운터를 0으로."""
        if self.store.runtime_get("quote_global_hold"):
            return
        errs = self._errors()
        if errs.get("quote"):
            errs["quote"] = 0
            self.store.runtime_set("api_errors", errs, self.now())
            self.store.runtime_set("consecutive_api_errors", max(errs.values(), default=0), self.now())

    def _evaluate_quote_hold(self, hold_at_start, rnd):
        """전체 시세 보류 해제: 서로 다른 tick 2회 연속, 새 유효 시세(종목·시세 시각 쌍)가 있고 SYSTEM 오류가 없을 때.
        특정 종목 하나의 정상 응답 1건만으로는 해제하지 않는다."""
        hold = self.store.runtime_get("quote_global_hold")
        if not hold_at_start or not hold or not rnd["attempts"]:
            return
        now = self.now()
        if rnd["system"]:
            hold["rounds_ok"] = 0                 # 공통 장애 신호: 처음부터 다시
        elif rnd["new_ok"]:
            hold["rounds_ok"] = int(hold.get("rounds_ok") or 0) + 1
            hold["seen"] = (list(hold.get("seen") or []) + rnd["new_ok"])[-50:]
        elif rnd["errors"]:
            hold["rounds_ok"] = 0                 # 새 유효 시세 없이 실패만 있음
        # 같은 시세 반복만 있던 라운드는 증거도 실패도 아니다: 그대로 둔다
        if hold["rounds_ok"] < QH.GLOBAL_RECOVERY_ROUNDS:
            self.store.runtime_set("quote_global_hold", hold, now)
            return
        errs = self._errors()
        before = errs.get("quote", 0)
        errs["quote"] = 0
        self.store.runtime_set("api_errors", errs, now)
        self.store.runtime_set("consecutive_api_errors", max(errs.values(), default=0), now)
        self.store.runtime_set("quote_global_hold", None, now)
        self.store.audit(now, "engine", "quote_hold_off", "global",
                         {"since": hold.get("since"), "reason": hold.get("reason"), "counter_before": before,
                          "confirmed": hold.get("seen", [])[-4:]})
        self._alert(f"health:quote_hold_off:{int(now)}", "INFO",
                    "[자동손절] 시세 공통 오류 보류 해제 — 서로 다른 시각의 유효 시세를 연속 확인했습니다.")

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
        self._tick_started = now
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
            self._migrate_quote_health()
            self._reconcile_all()
            self._rollover(mc)
            if mc.is_open:
                self._tick_stats(mc)
                self._sync_other_modes()
            if self.schema_block:
                self.summary["schema_block"] = self.schema_block
            elif self.settings.mode in ("SHADOW", "LIVE") and mc.is_open:
                self._session_notice(mc)
                hold = self._hold_reasons()
                self._track_global_hold(hold)
                if "balance" in hold:
                    self._recover_balance()
                else:
                    self._balance_sync(force=False)
                if "orders" in hold:
                    self._recover_orders()
                # 보류 중에도 모든 종목을 예정 시각마다 같은 검증으로 조회한다 (트리거·주문 없이 회복 확인).
                self._watch(mc, hold)
                hold = self._hold_reasons()
                self._track_global_hold(hold)
                self.summary["hold"] = hold
                if not hold:
                    self._advance(mc)
            self._finalize()
            if self.settings.mode in ("SHADOW", "LIVE") and mc.after_close and not self.schema_block:
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

    def _collect_http(self, broker):
        """실제 HTTP 요청 수와 429 를 집계한다. 예외로 끝난 호출의 429 도 tick 종료 시 모두 거둔다."""
        pop = getattr(broker, "pop_429", None)
        n = pop() if pop else 0
        if n:
            self._stat("http_429", "", n)
        pop_http = getattr(broker, "pop_http", None)
        for api, count in ((pop_http() if pop_http else None) or {}).items():
            if count:
                self._stat("http_requests", str(api), count)


    def _sync_other_modes(self):
        """현재 운영 모드가 아닌 포지션(OFF 포함)도 잔고는 주기적으로 맞춘다. 주문·트리거는 하지 않는다."""
        for mode in ("SHADOW", "LIVE"):
            if mode == self.settings.mode or not self.store.positions(mode=mode, states=("ACTIVE", "EXITING")):
                continue
            self._balance_sync(force=False, mode=mode)

    def killed(self):
        return self.store.kill_switch()

    def _end_tick(self):
        for broker in list(self._brokers.values()):
            try:
                self._collect_http(broker)
            except Exception:
                log.warning("[자동손절] HTTP 집계 실패", exc_info=True)
        self.store.runtime_set("heartbeat_at", self.now(), self.now())
        self._flush_alerts()
        self.store.release_lease(self.owner, self.now())

    def _session_summary(self, mc):
        from trading.auto_stop.service import daily_report, report_text
        day = mc.session.day.isoformat()
        rep = daily_report(self.store, day, self.settings.mode, self.settings, now=self.now())
        warn = (rep["unresolved_events"] or rep["unresolved_orders"] or rep["watch"]["missed"] or rep["api"]["errors"]
                or rep.get("recovery", {}).get("fail") or rep.get("holds", {}).get("global_seconds"))
        self._alert(f"session:{day}:{self.settings.mode}:summary", "WARN" if warn else "INFO", report_text(rep))

    def _recover_due(self, cat) -> bool:
        key = f"last_probe_at:{cat}"
        last = self.store.runtime_get(key, 0) or 0
        if self.now() - last < self.settings.price_interval_sec:
            return False
        self.store.runtime_set(key, self.now(), self.now())
        return True

    def _recover_balance(self):
        """잔고 오류 보류 중 회복 확인: 조회만 한다. 주문·트리거 없음. 실패도 일일 보고에 센다."""
        if not self._recover_due("balance"):
            return
        self._stat("recovery_attempt", "balance")
        broker = None
        try:
            self._renew()
            broker = self._broker(self.settings.mode)
            broker.holdings()
            self._renew()
            self._collect_http(broker)
            self._api_ok("balance")
            self._stat("recovery_ok", "balance")
        except LeaseLost:
            raise
        except Exception as exc:
            self._renew()
            self._stat("recovery_fail", "balance")
            self._stat("api_error", "balance_recovery")
            log.warning("[자동손절] 잔고 회복 확인 실패 %s", json.dumps(QH.classify(exc), default=str, ensure_ascii=False))

    def _recover_orders(self):
        """주문내역 오류 보류 중 회복 확인: 감시 종목을 순환하며 조회한다 (첫 종목 고정 금지)."""
        if not self._recover_due("orders"):
            return
        positions = self.store.positions(mode=self.settings.mode, states=("ACTIVE", "EXITING"))
        if not positions:
            return
        idx = int(self.store.runtime_get("orders_probe_idx", 0) or 0)
        pos = positions[idx % len(positions)]
        self.store.runtime_set("orders_probe_idx", idx + 1, self.now())
        self._stat("recovery_attempt", "orders")
        try:
            self._renew()
            broker = self._broker(self.settings.mode)
            broker.day_orders(pos["ticker"])
            self._renew()
            self._collect_http(broker)
            self._api_ok("orders")
            self._stat("recovery_ok", "orders")
        except LeaseLost:
            raise
        except Exception as exc:
            self._renew()
            self._stat("recovery_fail", "orders")
            self._stat("api_error", "orders_recovery")
            log.warning("[자동손절] 주문내역 회복 확인 실패 %s %s", pos["ticker"],
                        json.dumps(QH.classify(exc), default=str, ensure_ascii=False))

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
            self._collect_http(broker)
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
            self._collect_http(broker)
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
    # 일반 감시(watch), 회복 조회(recovery), 주문 직전(order) 모두 _confirmed_quote 하나로 검증한다.
    _STATS = {"watch": ("quote_fail", "quote_invalid", "quote"),
              "recovery": ("recovery_fail", "recovery_invalid", "quote_recovery"),
              "order": ("order_quote_fail", "order_quote_invalid", "quote_order")}

    def _fetch_quote(self, pos, purpose):
        """증권사 시세 1회. (quote, 오류 기록|None, 메타). 예외는 모두 분류해 돌려준다 (다음 종목 조회를 막지 않음)."""
        started = self.now()
        q, err, broker = None, None, None
        self._renew()
        try:
            broker = self._broker(pos["mode"])
            q = broker.quote(pos["ticker"], pos["exchange"])
        except LeaseLost:
            raise
        except Exception as exc:
            err = QH.classify(exc)
            if getattr(exc, "broker_init", False):
                err.update(category="broker_init", scope=QH.SYSTEM)
        self._renew()                         # lease 를 잃었으면 여기서 멈춘다 (이후 쓰기 없음)
        if broker is not None:
            self._collect_http(broker)
        finished = self.now()
        meta = {"ticker": pos["ticker"], "request_exchange": pos["exchange"], "purpose": purpose,
                "started_at": started, "finished_at": finished, "duration_sec": round(finished - started, 3)}
        diag = getattr(broker, "last_quote_diag", None) if broker is not None else None
        if isinstance(diag, dict) and diag.get("ticker") == pos["ticker"]:
            meta.update(attempts=diag.get("attempts"), retries=diag.get("retries"),
                        http_429_in_call=diag.get("http_429"))
        if err is not None:
            err = {**meta, **err}
        return q, err, meta

    def _confirmed_quote(self, pos, mc, purpose="watch"):
        """유효 시세. (quote, 확인필요 사유, 오류 기록). 직전 유효값 대비 급변이면 즉시 재조회해 교차 확인한다.
        단순히 broker.quote() 가 반환됐다는 이유로 정상으로 보지 않는다: 종목·거래소, 가격, 당일 정규장, 120초,
        개장 이후, 급변을 모두 확인한다."""
        q, err, meta = self._fetch_quote(pos, purpose)
        if err:
            return None, None, err
        now = self.now()
        if q.as_of is not None:
            self.store.stat_max(self._day(), self.settings.mode, "max_quote_age_sec", now - q.as_of, pos["ticker"])
        found = validate_detail(q, now, mc, self.settings, expect=(pos["ticker"], pos["exchange"]))
        if found:
            self.store.runtime_set("last_quote_issue", {"ticker": pos["ticker"], "reason": found[1],
                                                        "code": found[0], "at": now, "purpose": purpose}, now)
            err = {**meta, **QH.validation_error(*found), "response_exchange": q.exchange,
                   "quote_as_of": q.as_of, "quote_age_sec": None if q.as_of is None else round(now - q.as_of, 1)}
            return None, None, err
        prev = Decimal(pos["last_quote_price"]) if pos["last_quote_price"] else None
        if is_jump(prev, q.price, self.settings):
            q2, err2, _ = self._fetch_quote(pos, purpose)
            if err2:
                return None, "급변 시세 재조회 실패", None
            if validate_detail(q2, self.now(), mc, self.settings, expect=(pos["ticker"], pos["exchange"])) \
                    or not consistent(q.price, q2.price, self.settings):
                return None, f"급변 시세({prev}→{q.price})를 재조회로 확인하지 못했습니다", None
            if q2.bid is not None and not consistent(q2.price, q2.bid, self.settings) and q2.bid > q2.price:
                return None, "현재가와 호가가 일치하지 않습니다", None
            holdings = self._balance_sync(force=True)
            fresh = self.store.position(pos["id"])
            if holdings is None or fresh["state"] not in ("ACTIVE", "EXITING"):
                return None, "급변 시 잔고 확인 실패 또는 분할·병합 의심", None
            q = q2
        self.store.runtime_set("last_quote_ok_at", self.now(), self.now())
        return q, None, None

    def _quote_failed(self, pos, rec, purpose, err):
        """종목별 오류 반영 + 구조화 기록 + 범위별 전체 카운터. 반환: quote_health 전이."""
        now, ticker = self.now(), pos["ticker"]
        fail, invalid, api_key = self._STATS[purpose]
        validation = bool(err.get("validation"))
        self._stat(invalid if validation else fail, ticker)
        self._stat("quote_error", f"{purpose}:{err.get('category')}")
        record = {**err, "mode": pos["mode"], "day": self._day(), "at": now}
        if not validation:
            self._stat("api_error", api_key)
            self.store.runtime_set("last_api_error", {"at": now, "where": f"시세 {ticker} ({purpose})",
                                                      "type": err.get("error_type"),
                                                      "category": err.get("category")}, now)
        if (rec.get("first_error") or {}).get("day") != record["day"]:
            rec["first_error"] = record           # 그날 최초 오류 (원인 미확인 사고 재발 시 근거)
        recent = list(self.store.runtime_get("quote_errors_recent", []) or [])
        recent.append(record)
        self.store.runtime_set("quote_errors_recent", recent[-QH.RECENT_ERRORS_KEEP:], now)
        transition = QH.on_error(rec, now, record, self.settings.max_consecutive_errors)
        log.warning("[자동손절] 시세 오류 (%s, 종목 연속 %d회) %s", purpose, rec["consecutive_errors"],
                    json.dumps(record, default=str, ensure_ascii=False))
        if transition == "fault":
            self.store.audit(now, "engine", "quote_fault", f"{pos['mode']}:{ticker}",
                             {"position_id": pos["id"], "category": err.get("category"), "scope": err.get("scope"),
                              "consecutive": rec["consecutive_errors"]})
            sev = "URGENT" if pos["mode"] == "LIVE" else "WARN"
            self._alert(f"health:quote_fault:{pos['mode']}:{pos['id']}:{int(now // 3600)}", sev,
                        f"[자동손절] {ticker} 시세 연속 오류 {rec['consecutive_errors']}회 ({err.get('category')}) — "
                        "이 종목의 신규 손절 진입을 보류하고 회복 조회를 계속합니다. 다른 종목 감시는 계속합니다.")
        if err.get("scope") in (QH.SYSTEM, QH.UNKNOWN):
            self._quote_global_error(record)
        else:
            self._quote_global_ok()               # 증권사는 응답했다 (종목 단위 문제)
        return transition

    def _quote_ok(self, pos, rec, purpose, q):
        """유효 시세 반영. 반환: quote_health 전이 (confirm/recovered/stale_repeat/None)."""
        now = self.now()
        result = QH.on_success(rec, now, q.as_of)
        rec["last_attempt"] = {"purpose": purpose, "at": now, "quote_as_of": q.as_of,
                               "quote_age_sec": None if q.as_of is None else round(now - q.as_of, 1)}
        self._quote_global_ok()
        if result == "recovered":
            self.store.audit(now, "engine", "quote_recovered", f"{pos['mode']}:{pos['ticker']}",
                             {"position_id": pos["id"], "fault_started_at": rec.get("fault_started_at")})
            self._alert(f"health:quote_recovered:{pos['mode']}:{pos['id']}:{int(now)}", "INFO",
                        f"[자동손절] {pos['ticker']} 시세 회복 확인 (서로 다른 시각 유효 시세 "
                        f"{QH.RECOVERY_CONFIRMATIONS}회) — 다음 감시 슬롯부터 손절 판단을 재개합니다.")
        return result

    def _watch(self, mc, hold=()):
        """종목별 예정 시각(next_due_at)에 따라 조회한다. 한 종목의 예외는 다음 종목을 막지 않는다.

        - 일반 감시(watch): 종목 상태 OK + 전체 보류 없음 + ACTIVE. 유효 시세 ≤ 손절가면 청산 이벤트.
        - 회복 조회(recovery): 종목 장애·회복 확인 중이거나 전체 보류 중. 같은 검증을 하되 트리거·주문은 없다.
          회복을 확정한 시세는 정상 감시 성공으로 세지 않고 손절 판단에도 쓰지 않는다 (다음 슬롯부터 일반 감시).
        """
        mode, interval = self.settings.mode, self.settings.price_interval_sec
        hold = list(hold or [])
        positions = self.store.positions(mode=mode, states=("ACTIVE", "EXITING"))
        health = self._health_load(mode)
        health = {k: v for k, v in health.items() if k in {str(p["id"]) for p in positions}}
        for p in positions:
            health.setdefault(str(p["id"]), QH.new_record(self.now(), p))
        quote_hold = self.store.runtime_get("quote_global_hold")
        rnd = {"attempts": 0, "system": 0, "errors": 0, "new_ok": []}
        budget_end = (self._tick_started or self.now()) + 1.5 * self.settings.tick_sec
        ordered = sorted(positions, key=lambda p: health[str(p["id"])].get("next_due_at") or 0)
        for pos in ordered:
            pos = self.store.position(pos["id"])
            if pos["state"] not in ("ACTIVE", "EXITING"):
                continue
            rec, ticker, now = health[str(pos["id"])], pos["ticker"], self.now()
            if pos["auto_paused"]:
                if pos["state"] == "ACTIVE" and QH.is_due(rec, now):
                    QH.enter_slot(rec, now, interval)
                    self._stat("slot_paused", ticker)
                    QH.schedule_next_slot(rec, now, interval)
                    self._health_save(mode, health)
                continue
            if pos["state"] == "EXITING" and rec["state"] == QH.OK and not hold:
                continue                      # 청산 진행 중 정상 종목은 _send 가 주문 직전에 시세를 확인한다
            if not QH.is_due(rec, now):
                continue
            if now > budget_end:
                self._stat("slot_deferred", ticker)     # 다음 tick 에 가장 먼저 (예정 시각 순)
                continue
            purpose = "watch" if (rec["state"] == QH.OK and not hold and pos["state"] == "ACTIVE") else "recovery"
            if QH.enter_slot(rec, now, interval) == "failed":
                self._stat("slot_failed", ticker)
            rec["slot_attempts"] = int(rec.get("slot_attempts") or 0) + 1
            if purpose == "watch":
                self._stat("watch_attempt", ticker)
                rec["slot_purpose"] = rec.get("slot_purpose") or "watch"
            else:
                self._stat("recovery_attempt", ticker)
                if pos["state"] == "ACTIVE" and not rec.get("slot_held"):
                    self._stat("slot_held", ticker)       # 이 감시 슬롯은 보류로 일반 감시를 하지 못함
                    rec["slot_held"] = True
                rec["slot_purpose"] = "recovery"
            q, problem, err = self._confirmed_quote(pos, mc, purpose)
            rnd["attempts"] += 1
            if problem:
                rnd["errors"] += 1
                QH.schedule_next_slot(rec, now, interval)
                self._health_save(mode, health)
                self._review_position(pos, problem)
                continue
            if err:
                self._quote_failed(pos, rec, purpose, err)
                rnd["errors"] += 1
                if err.get("scope") == QH.SYSTEM:
                    rnd["system"] += 1
                if not hold and self.store.runtime_get("quote_global_hold"):
                    hold = ["quote_system"]           # 이 tick 도중 전체 임계값 도달: 남은 종목도 트리거 없이 관측만
                if rec["state"] == QH.OK:
                    QH.retry_next_tick(rec, now)
                else:
                    QH.schedule_next_slot(rec, now, interval, QH.backoff_factor(rec))
                self._health_save(mode, health)
                continue
            result = self._quote_ok(pos, rec, purpose, q)
            key = [pos["id"], q.as_of]
            if quote_hold and key not in (quote_hold.get("seen") or []) and key not in rnd["new_ok"]:
                rnd["new_ok"].append(key)
            fields = {"last_quote_price": str(q.price), "last_quote_at": q.as_of}
            if purpose == "watch":
                fields["last_price_check_at"] = self.now()
                if not rec.get("slot_ok"):
                    self._stat("quote_ok", ticker)        # 슬롯당 1회: 유효 시세 감시 성공
                    rec["slot_ok"] = True
            else:
                self._stat("recovery_stale" if result == "stale_repeat" else "recovery_ok", ticker)
            QH.schedule_next_slot(rec, now, interval)
            self._health_save(mode, health)
            self.store.update_position(pos["id"], self.now(), **fields)
            if purpose != "watch":
                if pos["state"] == "ACTIVE" and q.price <= dec(pos["stop_price"]):
                    # 보류 중 관측: 주문 근거로 쓰지 않는다. 보류 해제 후 최신 시세로 다시 판단한다.
                    self._stat("held_trigger", ticker)
                    self._alert(f"held_trigger:{pos['id']}:{int(self.now() // 3600)}", "WARN",
                                f"[자동손절] {ticker} 보류 중 손절가 이하 시세 관측 ({q.price} ≤ {pos['stop_price']}) — "
                                "보류가 풀리면 최신 시세로 다시 판단합니다 (소급 주문 없음).")
                continue
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
        self._evaluate_quote_hold(quote_hold, rnd)

    def _ticker_ok(self, pos) -> bool:
        rec = self._health_load(pos["mode"]).get(str(pos["id"]))
        return rec is None or rec.get("state", QH.OK) == QH.OK

    # ── 6. 청산 진행 ─────────────────────────────────────────────
    def _guard(self, mode):
        if self.schema_block:
            return lambda: self.schema_block
        if mode == "LIVE":
            return lambda: S.live_order_block_reason(self.settings_loader())
        return lambda: None

    def _cancel_guard(self, mode):
        if self.schema_block:
            return lambda: self.schema_block
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
        if not self._ticker_ok(pos):
            # 이 종목 시세가 장애·회복 확인 중이면 신규 주문을 하지 않는다 (회복 조회는 _watch 가 계속).
            self._stat("order_held_symbol", pos["ticker"])
            return
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
        health = self._health_load(mode)
        rec = health.setdefault(str(pos["id"]), QH.new_record(self.now(), pos))
        q, problem, err = self._confirmed_quote(pos, mc, "order")
        if problem:
            self._review_event(ev, problem)
            return
        if err:
            # 무효 시세는 임계값 전이라도 주문에 쓰지 않는다.
            self._quote_failed(pos, rec, "order", err)
            self._health_save(mode, health)
            return
        self._quote_ok(pos, rec, "order", q)
        self._health_save(mode, health)
        self._stat("order_quote_ok", pos["ticker"])
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
        if not self._ticker_ok(pos):
            # 시세 장애 종목은 재주문 근거 시세가 없으므로 취소도 하지 않는다 (걸려 있는 주문을 잃지 않게).
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
