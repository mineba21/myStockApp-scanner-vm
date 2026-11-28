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
from trading.auto_stop.store import UNRESOLVED, ClaimRefused, LeaseLost, ScannerJournalUnreadable, dec
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
        """전체 신규 진입 보류 사유. 계좌 잔고·주문 상태·주문내역 확인 불가, 시세 시스템 장애(incident).

        시세 incident 는 auto_stop_runtime.quote_global_hold 에 원인·영향 범위와 함께 보존된다. 종목 추가로 광범위
        실패 비율이 낮아지거나 장애 종목이 일시중지·관리 종료돼도 사유는 사라지지 않는다 (복구 증거로만 해제)."""
        errs, thr = self._errors(), self.settings.max_consecutive_errors
        reasons = [c for c in ("balance", "status", "orders") if int(errs.get(c, 0) or 0) >= thr]
        inc = self.store.runtime_get("quote_global_hold")
        if inc:
            for trigger in (inc.get("triggers") or ["quote_system"]):
                if trigger not in reasons:
                    reasons.append(trigger)
        elif int(errs.get("quote", 0) or 0) >= thr:
            reasons.append("quote_system")            # 기록 없는 임계 카운터: fail closed (이행 단계가 기록을 만든다)
        return reasons

    def _check_broad(self) -> bool:
        """광범위 조회 실패(감시 종목 중 장애 2개 이상이면서 절반 이상)면 incident 를 만들거나 넓힌다.
        영향 대상은 그 시점의 장애 종목이며, 이후 종목이 늘어 비율이 낮아져도 대상은 빠지지 않는다."""
        mode = self.settings.mode
        if mode not in ("SHADOW", "LIVE"):
            return False
        health = self._health_load(mode)
        watched = [p for p in self.store.positions(mode=mode, states=("ACTIVE", "EXITING")) if not p["auto_paused"]]
        faulted = [p for p in watched if (health.get(str(p["id"])) or {}).get("state", QH.OK) != QH.OK]
        if not QH.is_broad(len(faulted), len(watched)):
            return False
        now = self.now()
        opened = {}

        def fn(vals, db):
            inc, audits, changed = vals["quote_global_hold"], [], False
            if not inc:
                inc = QH.incident_new(now, "quote_broad", "broad_fault", watched_at_start=len(watched))
                audits.append(("engine", "quote_hold_on", "global",
                               {"reason": "quote_broad", "faulted": [p["ticker"] for p in faulted],
                                "watched": len(watched)}))
                opened["on"] = changed = True
            elif "quote_broad" not in inc.setdefault("triggers", []):
                inc["triggers"].append("quote_broad")
                audits.append(("engine", "quote_hold_extend", "global",
                               {"trigger": "quote_broad", "faulted": [p["ticker"] for p in faulted]}))
                changed = True
            for p in faulted:
                a = (inc.get("affected") or {}).get(str(p["id"]))
                if a is None or QH.CAUSE_BROAD not in a["causes"]:
                    QH.incident_add(inc, p, QH.CAUSE_BROAD, "broad_fault", now, reset=False)
                    changed = True
            return ({"quote_global_hold": inc}, audits) if changed else None
        self.store.runtime_txn(now, ["quote_global_hold"], fn)
        if opened:
            self._alert(f"health:quote_broad:{int(now)}", "URGENT",
                        f"[자동손절] 광범위 시세 장애 ({len(faulted)}/{len(watched)}종목) — 전체 신규 손절 진입을 "
                        "보류합니다. 장애 종목이 실제로 회복될 때까지 해제하지 않습니다.")
        return True

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

        한 SQLite 트랜잭션(BEGIN IMMEDIATE)에서 카운터 보존·전체 보류 기록·관련 종목 FAULT·완료 버전·감사 기록을
        함께 쓴다. 도중에 실패하면 모두 롤백되어 부분 변환이 남지 않고, 다음 tick 에 같은 변환을 다시 시도한다.
        완료 버전(quote_health_version=1)이 있으면 아무것도 하지 않는다 (반복 재시작에도 감사 기록 1건).

        - 공통 카운터가 임계값 이상이면 전체 시세 보류 incident(legacy_common_counter)를 만들고 카운터는 그대로 둔다.
        - 어느 종목의 오류였는지 알 수 없으므로 감시 중인 모든 종목을 FAULT(legacy_unattributed)이자 incident 영향
          대상으로 두고, 종목별 회복 확인(서로 다른 시세 시각 2회)과 시스템 복구 증거를 요구한다.
        """
        now, thr = self.now(), self.settings.max_consecutive_errors
        keys = ["quote_health_version", "api_errors", "quote_global_hold", "quote_health:SHADOW", "quote_health:LIVE"]

        def fn(vals, db):
            errs = dict(vals["api_errors"] or {})
            legacy = int(errs.get("quote", 0) or 0)
            updates, audits = {}, []
            inc = vals["quote_global_hold"]
            done = vals["quote_health_version"] == 1
            if done and (inc or legacy < thr):
                return None
            if not done:
                marked = []
                for mode in ("SHADOW", "LIVE"):
                    health = dict(vals[f"quote_health:{mode}"] or {})
                    rows = [dict(r) for r in db.execute(
                        "SELECT * FROM auto_stop_positions WHERE mode=? AND state IN ('ACTIVE','EXITING') ORDER BY id",
                        (mode,))]
                    if legacy >= thr:
                        for p in rows:
                            rec = health.get(str(p["id"])) or QH.new_record(now, p)
                            rec.update(state=QH.FAULT, fault_started_at=now, fault_reason="legacy_unattributed",
                                       legacy_global_count=legacy, confirm_as_of=[])
                            health[str(p["id"])] = rec
                            marked.append((mode, p))
                        if rows:
                            updates[f"quote_health:{mode}"] = health
                if legacy >= thr and not inc:
                    inc = QH.incident_new(now, "quote_system", "legacy_common_counter", legacy_count=legacy)
                    for _, p in marked:
                        QH.incident_add(inc, p, QH.CAUSE_SYSTEM, "legacy_unattributed", now)
                    updates["quote_global_hold"] = inc
                    audits.append(("engine", "quote_hold_on", "global",
                                   {"reason": "legacy_common_counter", "count": legacy}))
                updates["quote_health_version"] = 1
                audits.append(("engine", "quote_health_migrate", "global",
                               {"legacy_api_errors": errs, "marked_fault": [f"{m}:{p['ticker']}" for m, p in marked]}))
            else:
                # 완료 후 임계 카운터만 남은 경우(카운터 기록 직후 중단 등): fail closed 로 incident 를 만든다.
                inc = QH.incident_new(now, "quote_system", "counter_without_record", legacy_count=legacy)
                updates["quote_global_hold"] = inc
                audits.append(("engine", "quote_hold_on", "global",
                               {"reason": "counter_without_record", "count": legacy}))
            return updates, audits
        self.store.runtime_txn(now, keys, fn)

    def _quote_global_error(self, record, pos):
        """SYSTEM/UNKNOWN 범위 시세 오류: 전체 공통 카운터 +1, 오류 종목을 연속 오류 범위(streak)에 기록.
        임계값이면 incident 를 만들고 streak 의 종목을 영향 대상으로 둔다. incident 중이면 그 종목을 대상에
        넣고(이미 있으면 증거를 처음부터) 미해결로 둔다. 한 트랜잭션에서 처리한다."""
        now, thr = self.now(), self.settings.max_consecutive_errors
        out = {}

        def fn(vals, db):
            errs = dict(vals["api_errors"] or {})
            errs["quote"] = n = int(errs.get("quote", 0) or 0) + 1
            streak = dict(vals["quote_error_streak"] or {})
            streak[str(pos["id"])] = {"id": pos["id"], "ticker": pos["ticker"], "exchange": pos["exchange"],
                                      "mode": pos["mode"], "category": record.get("category")}
            inc, audits = vals["quote_global_hold"], []
            if inc:
                if "quote_system" not in inc.setdefault("triggers", []):
                    inc["triggers"].append("quote_system")
                    audits.append(("engine", "quote_hold_extend", "global",
                                   {"trigger": "quote_system", "ticker": pos["ticker"],
                                    "category": record.get("category")}))
                QH.incident_add(inc, pos, QH.CAUSE_SYSTEM, record.get("category"), now, reset=True)
                # 같은 트랜잭션에서: 영향 대상 증거 초기화(위), 공통 정상 라운드 초기화, 원인·발생 시각 갱신
                inc["rounds_ok"] = 0
                inc["last_system_error_at"] = now
                inc["last_error"] = {"category": record.get("category"), "scope": record.get("scope"),
                                     "ticker": pos["ticker"], "at": now}
            elif n >= thr:
                inc = QH.incident_new(now, "quote_system", record.get("category"), count=n)
                for e in streak.values():
                    QH.incident_add(inc, e, QH.CAUSE_SYSTEM, e.get("category"), now)
                audits.append(("engine", "quote_hold_on", "global",
                               {"reason": record.get("category"), "count": n,
                                "affected": sorted(e["ticker"] for e in streak.values())}))
            out["n"] = n
            updates = {"api_errors": errs, "consecutive_api_errors": max(errs.values()), "quote_error_streak": streak}
            if inc:
                updates["quote_global_hold"] = inc
            return updates, audits
        self.store.runtime_txn(now, ["api_errors", "quote_error_streak", "quote_global_hold"], fn)
        if out.get("n", 0) >= thr:
            self._alert(f"health:api_errors:quote:{int(now // 3600)}", "URGENT",
                        f"[자동손절] 키움 시세 공통 오류 연속 {out['n']}회 ({record.get('category')}) — 전체 신규 손절 "
                        "진입을 보류합니다. 장애 종목·공통 경로가 실제로 회복될 때까지 해제하지 않습니다.")

    def _quote_global_ok(self):
        """증권사가 응답함(유효 시세 또는 종목 단위 검증 실패): incident 가 없으면 공통 카운터·streak 을 0으로."""
        def fn(vals, db):
            if vals["quote_global_hold"]:
                return None
            errs = dict(vals["api_errors"] or {})
            if not errs.get("quote") and not vals["quote_error_streak"]:
                return None
            errs["quote"] = 0
            return {"api_errors": errs, "consecutive_api_errors": max(errs.values(), default=0),
                    "quote_error_streak": {}}, []
        self.store.runtime_txn(self.now(), ["api_errors", "quote_error_streak", "quote_global_hold"], fn)

    def _incident_update(self, fn_inc):
        """incident 를 읽어 fn_inc(inc) 로 바꾸고 저장한다 (incident 가 없으면 아무것도 하지 않음)."""
        def fn(vals, db):
            inc = vals["quote_global_hold"]
            if not inc or fn_inc(inc) is False:
                return None
            return {"quote_global_hold": inc}, []
        self.store.runtime_txn(self.now(), ["quote_global_hold"], fn)

    def _incident_observe(self, pid, kind, *, as_of=None, health_state=None, managed=True):
        """영향 대상 조회 결과 기록. 응답 수신·유효 시세 회복 증거를 따로 센다 (incident_response / incident_evidence)."""
        tick_at, now = self._tick_started or self.now(), self.now()
        out = {}

        def apply(inc):
            if str(pid) not in (inc.get("affected") or {}):
                return False
            out["r"] = QH.incident_observe(inc, pid, tick_at, kind, now, as_of=as_of, health_state=health_state,
                                           managed=managed)
        self._incident_update(apply)
        if out.get("r"):
            self._stat("incident_response")
            if out["r"] == "evidence":
                self._stat("incident_evidence")

    def _incident_status(self, pid, status):
        """조회하지 않은 영향 대상의 상태(백오프·시간 예산 초과·일시중지 등). 증거로 쓰지 않는다."""
        now = self.now()

        def apply(inc):
            a = (inc.get("affected") or {}).get(str(pid))
            if a is None or a.get("last_status") == status:
                return False
            if not a.get("resolved"):
                a.update(last_status=status, last_at=now)
            else:
                return False
        self._incident_update(apply)

    def _evaluate_quote_hold(self, hold_at_start, rnd):
        """incident 해제 판단. 서로 다른 tick 2회 연속 새 유효 시세가 있고 SYSTEM 오류가 없으며(공통 경로),
        모든 영향 대상이 원인별 실제 복구 증거를 충족해야 한다. 조회하지 않은 대상은 증거가 없다."""
        if not hold_at_start:
            return
        now = self.now()
        out = {}

        def fn(vals, db):
            inc = vals["quote_global_hold"]
            if not inc:
                return None
            if rnd["attempts"]:
                if rnd["system"]:
                    inc["rounds_ok"] = 0                 # 공통 장애 신호: 처음부터 다시
                elif rnd["new_ok"]:
                    inc["rounds_ok"] = int(inc.get("rounds_ok") or 0) + 1
                    inc["seen"] = (list(inc.get("seen") or []) + rnd["new_ok"])[-50:]
                elif rnd["errors"]:
                    inc["rounds_ok"] = 0                 # 새 유효 시세 없이 실패만 있음
                # 같은 시세 반복만 있던 라운드는 증거도 실패도 아니다: 그대로 둔다
            inc["last_round"] = {"at": now, "attempts": rnd["attempts"], "system": rnd["system"],
                                 "errors": rnd["errors"], "new_ok": len(rnd["new_ok"]),
                                 "skipped": dict(rnd.get("skipped") or {})}
            if not QH.incident_releasable(inc):
                return {"quote_global_hold": inc}, []
            errs = dict(vals["api_errors"] or {})
            out["before"] = errs.get("quote", 0)
            errs["quote"] = 0
            out["inc"] = inc
            return ({"api_errors": errs, "consecutive_api_errors": max(errs.values(), default=0),
                     "quote_error_streak": {}, "quote_global_hold": None},
                    [("engine", "quote_hold_off", "global",
                      {"since": inc.get("since"), "reason": inc.get("reason"), "triggers": inc.get("triggers"),
                       "counter_before": out["before"], "rounds_ok": inc.get("rounds_ok"),
                       "affected": {k: {"ticker": a["ticker"], "causes": a["causes"],
                                        "evidence_ticks": a["evidence_ticks"][-2:]}
                                    for k, a in (inc.get("affected") or {}).items()},
                       "confirmed": inc.get("seen", [])[-4:]})])
        self.store.runtime_txn(now, ["api_errors", "quote_error_streak", "quote_global_hold"], fn)
        if out.get("inc"):
            self._stat("incident_release")
            self._alert(f"health:quote_hold_off:{int(now)}", "INFO",
                        "[자동손절] 시세 장애 보류 해제 — 영향 종목별 복구 증거와 공통 시세 경로의 연속 정상 응답을 "
                        "확인했습니다.")

    def _watch_end(self, pos, rec=None):
        """감시 종료 직전(전환 전 행) 상태 근거 기록. 보고서의 정상 상태 공백 판정 근거."""
        from trading.auto_stop.service import record_watch_end
        record_watch_end(self.store, pos["mode"], pos, self.now(), rec)

    def _review_event(self, ev, reason, *, position=True):
        if position:
            self._watch_end(self.store.position(ev["position_id"]))
        self.store.review(self.now(), event_id=ev["id"], position_id=ev["position_id"] if position else None,
                          reason=reason)
        pos = self.store.position(ev["position_id"])
        self._alert(f"event:{ev['id']}:review", "URGENT",
                    f"[자동손절 확인 필요] {pos['ticker']} ({ev['mode']}) — {reason}. 신규 매도를 멈췄습니다. "
                    "기존 주문 체결 추적은 계속합니다.")

    def _review_position(self, pos, reason):
        self._watch_end(self.store.position(pos["id"]))
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
            if self.settings.mode in ("SHADOW", "LIVE"):
                # 이 거래일에 신규 슬롯 형식(SLOT_V2) 기록을 시작한 시각 (보고서의 LEGACY/MIXED 구분 근거)
                self.store.stat_set_if_absent(ny_date(now).isoformat(), self.settings.mode, "slot_v2_since", "", now)
            if self.settings.mode == "SHADOW":
                self._track_shadow_config()
            self._recover_stale()
            self._migrate_quote_health()
            self._reconcile_all()
            self._reconcile_wait_records()
            self._rollover(mc)
            if mc.is_open:
                self._tick_stats(mc)
                self._sync_other_modes()
            if self.schema_block:
                self.summary["schema_block"] = self.schema_block
            elif self.settings.mode in ("SHADOW", "LIVE") and mc.is_open:
                self._session_notice(mc)
                self._check_broad()
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
                self._check_broad()
                hold = self._hold_reasons()
                self._track_global_hold(hold)
                self.summary["hold"] = hold
                if not hold:
                    self._advance(mc)
                else:
                    self._note_replace_waiting_under_hold(hold)
            self._finalize()
            if self.settings.mode in ("SHADOW", "LIVE") and mc.after_close and not self.schema_block:
                self._close_session_slots(mc)
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
                or rep.get("recovery", {}).get("fail") or rep.get("holds", {}).get("global_seconds")
                or (rep["watch"].get("gaps") or {}).get("over_120") or rep.get("data_quality")
                # 측정 품질이 불완전한 보고서는 정상(INFO)으로 표시하지 않는다
                or (rep.get("watched_positions") and (
                    rep["watch"].get("missed") is None or rep["watch"].get("ok_coverage") != "full"
                    or rep["watch"].get("ok_integrity") != "valid"
                    or (rep["watch"].get("gaps") or {}).get("normal_coverage") != "full")))
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
        """주문 1건 대조. 반환: False(조회 실패), None(증권사 주문내역에서 아직 보이지 않음), 조회 행(dict)."""
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
            return False
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
            return None
        result = self.store.apply_fill(o["id"], now, int(row["filled_quantity"]),
                                       {"source": "status", "row": row})
        if result == "ANOMALY":
            self._review_event(ev, f"체결 수량 이상 (기록 {o['filled_qty']} → 조회 {row['filled_quantity']})")
            return row
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
                return row
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
        return row

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
                self._watch_end(pos)
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
              "order": ("order_quote_fail", "order_quote_invalid", "quote_order"),
              "cancel_check": ("cancel_quote_fail", "cancel_quote_invalid", "quote_cancel_check"),
              "incident": ("incident_fail", "incident_invalid", "quote_incident")}

    def _fetch_quote(self, pos, purpose, broker_mode=None):
        """증권사 시세 1회. (quote, 오류 기록|None, 메타). 예외는 모두 분류해 돌려준다 (다음 종목 조회를 막지 않음).
        broker_mode 를 주면 그 모드의 조회 경로를 쓴다 (관리가 끝난 영향 종목의 읽기 전용 확인용)."""
        started = self.now()
        q, err, broker = None, None, None
        self._renew()
        try:
            broker = self._broker(broker_mode or pos["mode"])
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
        self._pstat(f"attempt_{purpose}", pos)      # 목적별 조회 시도 (일반 감시·회복·주문 전·취소 전)
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

    def _record_quote_error(self, pos, purpose, err) -> dict:
        """통계·구조화 기록·로그 (상태 전이 없음)."""
        now, ticker = self.now(), pos["ticker"]
        fail, invalid, api_key = self._STATS[purpose]
        validation = bool(err.get("validation"))
        self._stat(invalid if validation else fail, ticker)
        self._pstat(f"{purpose}_{'invalid' if validation else 'fail'}", pos)
        self._stat("quote_error", f"{purpose}:{err.get('category')}")
        record = {**err, "mode": pos["mode"], "day": self._day(), "at": now}
        if not validation:
            self._stat("api_error", api_key)
            self.store.runtime_set("last_api_error", {"at": now, "where": f"시세 {ticker} ({purpose})",
                                                      "type": err.get("error_type"),
                                                      "category": err.get("category")}, now)
        recent = list(self.store.runtime_get("quote_errors_recent", []) or [])
        recent.append(record)
        self.store.runtime_set("quote_errors_recent", recent[-QH.RECENT_ERRORS_KEEP:], now)
        log.warning("[자동손절] 시세 오류 (%s) %s", purpose, json.dumps(record, default=str, ensure_ascii=False))
        return record

    def _quote_failed(self, pos, rec, purpose, err):
        """종목별 오류 반영 + 구조화 기록 + 범위별 전체 카운터·incident. 반환: quote_health 전이."""
        now, ticker = self.now(), pos["ticker"]
        record = self._record_quote_error(pos, purpose, err)
        if (rec.get("first_error") or {}).get("day") != record["day"]:
            rec["first_error"] = record           # 그날 최초 오류 (원인 미확인 사고 재발 시 근거)
        transition = QH.on_error(rec, now, record, self.settings.max_consecutive_errors)
        rec["last_observation"] = "failed"
        if transition == "fault":
            self.store.audit(now, "engine", "quote_fault", f"{pos['mode']}:{ticker}",
                             {"position_id": pos["id"], "category": err.get("category"), "scope": err.get("scope"),
                              "consecutive": rec["consecutive_errors"]})
            sev = "URGENT" if pos["mode"] == "LIVE" else "WARN"
            self._alert(f"health:quote_fault:{pos['mode']}:{pos['id']}:{int(now // 3600)}", sev,
                        f"[자동손절] {ticker} 시세 연속 오류 {rec['consecutive_errors']}회 ({err.get('category')}) — "
                        "이 종목의 신규 손절 진입을 보류하고 회복 조회를 계속합니다.")
        if err.get("scope") in (QH.SYSTEM, QH.UNKNOWN):
            self._quote_global_error(record, pos)
        else:
            self._quote_global_ok()               # 증권사는 응답했다 (종목 단위 문제)
            self._incident_observe(pos["id"], "symbol", health_state=rec.get("state"))
        return transition

    def _quote_ok(self, pos, rec, purpose, q, *, managed=True):
        """유효 시세 반영. 반환: quote_health 전이 (confirm/recovered/stale_repeat/None)."""
        now = self.now()
        result = QH.on_success(rec, now, q.as_of)
        rec["last_attempt"] = {"purpose": purpose, "at": now, "quote_as_of": q.as_of,
                               "quote_age_sec": None if q.as_of is None else round(now - q.as_of, 1)}
        rec["last_observation"] = {"recovered": "recovered", "confirm": "recovering",
                                   "stale_repeat": "recovering"}.get(result, "ok")
        self._quote_global_ok()
        self._incident_observe(pos["id"], "ok", as_of=q.as_of, health_state=rec.get("state"), managed=managed)
        if result == "recovered":
            self.store.audit(now, "engine", "quote_recovered", f"{pos['mode']}:{pos['ticker']}",
                             {"position_id": pos["id"], "fault_started_at": rec.get("fault_started_at")})
            self._alert(f"health:quote_recovered:{pos['mode']}:{pos['id']}:{int(now)}", "INFO",
                        f"[자동손절] {pos['ticker']} 시세 회복 확인 (서로 다른 시각 유효 시세 "
                        f"{QH.RECOVERY_CONFIRMATIONS}회) — 전체 보류가 없으면 다음 감시 슬롯부터 손절 판단을 재개합니다.")
        return result

    def _pstat(self, metric, pos, n=1):
        """position_id 기준 통계 (같은 날 같은 티커 재등록과 섞이지 않게). metric 이름은 'pos_' 접두사."""
        self.store.bump(self._day(), self.settings.mode, "pos_" + metric, str(pos["id"]), n)

    def _roll_slots(self, pos, rec, now, interval, ctx):
        today = ny_date(now).isoformat()
        if rec.get("anchor_day") != today and interval > 0:
            # 그날 슬롯 격자의 기준 시각 (보고서가 기대 슬롯을 같은 격자로 계산한다)
            self.store.stat_set(today, pos["mode"], "slot_anchor", str(pos["id"]), rec.get("anchor") or now)
            rec["anchor_day"] = today
        self._persist_slots(pos["id"], pos["mode"], pos["ticker"], QH.roll_slots(rec, now, interval, ctx))

    _LEGACY_SLOT_STATS = {"watch_fail": "slot_failed", "invalid": "slot_failed", "held": "slot_held",
                          "paused": "slot_paused"}

    def _persist_slots(self, pid, mode, ticker, rows):
        """완료 슬롯의 최종 분류를 한 트랜잭션으로 기록한다 (같은 키 재기록은 덮어씀: 중복 증가 없음)."""
        if not rows:
            return
        self.store.stat_set_many([(ny_date(start).isoformat(), mode, "slot", f"{pid}:{start:.3f}",
                                   QH.SLOT_CODES[cls]) for start, cls in rows])
        for start, cls in rows:
            legacy = self._LEGACY_SLOT_STATS.get(cls)
            if legacy and ticker:
                self.store.bump(ny_date(start).isoformat(), mode, legacy, ticker)

    def _close_session_slots(self, mc):
        """장 마감 후 첫 tick: 진행 중이던 마지막 슬롯을 확정한다 (하루 1회)."""
        mode, day = self.settings.mode, mc.session.day.isoformat()
        key = f"slots_closed:{day}:{mode}"
        if self.store.runtime_get(key):
            return
        health = self._health_load(mode)
        for k, rec in health.items():
            start = rec.get("slot_start")
            if start is not None and ny_date(start).isoformat() == day:
                self._persist_slots(int(k), mode, rec.get("ticker"), QH.close_slot(rec))
        self._health_save(mode, health)
        # 마감 당시 상태 이력의 완전성 근거: 장 마감까지 감시한 포지션의 일시중지·종목 장애 상태가 감사 기록과 맞는지
        # 저장한다. 장중에 감시가 끝난 포지션은 그 시점에 이미 기록했다(_watch_end). 과거 거래일 보고서는 이 기록으로
        # 판정한다 (조회 시각·다음 날 상태를 쓰지 않는다). state_audit_done 은 일자 처리 완료 표식일 뿐이다.
        from trading.auto_stop.service import current_state_issues
        now = self.now()
        for p in self.store.positions(mode=mode, states=("ACTIVE", "EXITING")):
            issues = current_state_issues(self.store, mode, p, health.get(str(p["id"])), now)
            self.store.stat_min(day, mode, "state_audit", str(p["id"]), 0 if issues else 1)
        self.store.stat_set(day, mode, "state_audit_done", "", now)
        self.store.runtime_set(key, True, self.now())

    def _watch(self, mc, hold=()):
        """종목별 예정 시각(next_due_at)에 따라 조회한다. 한 종목의 예외는 다음 종목을 막지 않는다.

        - 일반 감시(watch): 종목 상태 OK + 전체 보류 없음 + ACTIVE. 유효 시세 ≤ 손절가면 청산 이벤트.
        - 회복 조회(recovery): 종목 장애·회복 확인 중이거나 전체 보류 중. 같은 검증을 하되 트리거·주문은 없다.
          회복을 확정한 시세는 정상 감시 성공으로 세지 않고 손절 판단에도 쓰지 않는다 (다음 슬롯부터 일반 감시).
        - incident 영향 대상이지만 일반 감시 대상이 아닌 포지션(일시중지·관리 종료·다른 모드)은 읽기 전용으로
          계속 조회해 복구 증거를 모은다 (_incident_probe). 조회하지 않은 대상은 증거가 생기지 않는다.
        """
        mode, interval = self.settings.mode, self.settings.price_interval_sec
        hold = list(hold or [])
        positions = self.store.positions(mode=mode, states=("ACTIVE", "EXITING"))
        loaded = self._health_load(mode)
        live_ids = {str(p["id"]) for p in positions}
        for k, rec in loaded.items():
            if k not in live_ids:
                # 관리 종료·확인 필요로 감시에서 빠짐: 진행 중이던 슬롯을 지금 확정한다.
                self._persist_slots(int(k), mode, rec.get("ticker"), QH.close_slot(rec))
        health = {k: v for k, v in loaded.items() if k in live_ids}
        for p in positions:
            health.setdefault(str(p["id"]), QH.new_record(self.now(), p))
        quote_hold = self.store.runtime_get("quote_global_hold")
        affected = set((quote_hold or {}).get("affected", {}).keys())
        rnd = {"attempts": 0, "system": 0, "errors": 0, "new_ok": [], "skipped": {}, "probed": set()}
        budget_end = (self._tick_started or self.now()) + 1.5 * self.settings.tick_sec
        ordered = sorted(positions, key=lambda p: health[str(p["id"])].get("next_due_at") or 0)

        def skipped(pid, status):
            rnd["skipped"][status] = rnd["skipped"].get(status, 0) + 1
            if str(pid) in affected:
                self._incident_status(pid, status)

        for pos in ordered:
            pos = self.store.position(pos["id"])
            if pos["state"] not in ("ACTIVE", "EXITING"):
                continue
            rec, ticker, now = health[str(pos["id"])], pos["ticker"], self.now()
            if pos["state"] == "ACTIVE":
                ctx = "paused" if pos["auto_paused"] else ("held" if (rec["state"] != QH.OK or hold) else "ok")
                self._roll_slots(pos, rec, now, interval, ctx)
            if pos["auto_paused"]:
                if pos["state"] == "ACTIVE" and QH.is_due(rec, now):
                    QH.schedule_next_slot(rec, now, interval)
                rec["last_observation"] = "paused"
                self._health_save(mode, health)
                if str(pos["id"]) in affected:
                    # 일시중지는 트리거·주문만 멈춘다. 장애 영향 대상이면 읽기 전용 회복 조회는 계속한다.
                    self._incident_probe(pos, mc, rnd, budget_end, health=health, rec=rec)
                continue
            if pos["state"] == "EXITING" and rec["state"] == QH.OK and not hold:
                continue                      # 청산 진행 중 정상 종목은 _send 가 주문 직전에 시세를 확인한다
            if not QH.is_due(rec, now):
                status = QH.not_due_status(rec, now)
                if status == "skipped_backoff" and rec.get("last_observation") != status:
                    rec["last_observation"] = status
                    self._health_save(mode, health)
                if status == "skipped_backoff":
                    skipped(pos["id"], status)
                continue
            if now > budget_end:
                self._stat("slot_deferred", ticker)     # 다음 tick 에 가장 먼저 (예정 시각 순)
                self._pstat("deferred", pos)
                rec["last_observation"] = "skipped_budget"
                skipped(pos["id"], "skipped_budget")
                self._health_save(mode, health)
                continue
            purpose = "watch" if (rec["state"] == QH.OK and not hold and pos["state"] == "ACTIVE") else "recovery"
            flags = rec.setdefault("slot_flags", {})
            attempts = flags.setdefault("attempts", {})
            attempts[purpose] = int(attempts.get(purpose) or 0) + 1   # 같은 슬롯의 재시도는 시도 횟수로만
            if purpose == "watch":
                self._stat("watch_attempt", ticker)
            else:
                self._stat("recovery_attempt", ticker)
                if pos["state"] == "ACTIVE":
                    flags["held"] = True          # 이 감시 슬롯은 보류로 일반 감시를 하지 못함
            rnd["probed"].add(str(pos["id"]))
            q, problem, err = self._confirmed_quote(pos, mc, purpose)
            rnd["attempts"] += 1
            if problem:
                rnd["errors"] += 1
                QH.schedule_next_slot(rec, now, interval)
                self._health_save(mode, health)
                self._review_position(pos, problem)
                continue
            if err:
                transition = self._quote_failed(pos, rec, purpose, err)
                if purpose == "watch":
                    rec.setdefault("slot_flags", {})["fail"] = "invalid" if err.get("validation") else "watch_fail"
                rnd["errors"] += 1
                if err.get("scope") in (QH.SYSTEM, QH.UNKNOWN):
                    rnd["system"] += 1            # UNKNOWN 도 공통 정상 라운드로 인정하지 않는다
                if rec["state"] == QH.OK:
                    QH.retry_next_tick(rec, now)
                else:
                    QH.schedule_next_slot(rec, now, interval, QH.backoff_factor(rec))
                self._health_save(mode, health)
                if transition == "fault":
                    self._check_broad()
                if not hold and self.store.runtime_get("quote_global_hold"):
                    hold = ["quote_system"]           # 이 tick 도중 전체 보류 진입: 남은 종목도 트리거 없이 관측만
                continue
            result = self._quote_ok(pos, rec, purpose, q)
            key = [pos["id"], q.as_of]
            if quote_hold and key not in (quote_hold.get("seen") or []) and key not in rnd["new_ok"]:
                rnd["new_ok"].append(key)
            fields = {"last_quote_price": str(q.price), "last_quote_at": q.as_of}
            if purpose == "watch":
                done_at = self.now()
                fields["last_price_check_at"] = done_at
                flags = rec.setdefault("slot_flags", {})
                if not flags.get("ok"):
                    self._stat("quote_ok", ticker)        # 슬롯당 1회: 유효 시세 감시 성공
                flags["ok"] = True
                # 일반 감시 실제 성공 완료 시각 (최대 감시 공백 측정 근거, 재시작 후에도 유지)
                self.store.stat_set(ny_date(done_at).isoformat(), mode, "watch_ok_at",
                                    f"{pos['id']}:{done_at:.3f}", done_at)
            else:
                self._stat("recovery_stale" if result == "stale_repeat" else "recovery_ok", ticker)
                self._pstat("recovery_stale" if result == "stale_repeat" else "recovery_ok", pos)
            QH.schedule_next_slot(rec, now, interval)
            self._health_save(mode, health)
            self.store.update_position(pos["id"], self.now(), **fields)
            if purpose != "watch":
                if pos["state"] == "ACTIVE" and q.price <= dec(pos["stop_price"]):
                    # 보류 중 관측: 주문 근거로 쓰지 않는다. 보류 해제 후 최신 시세로 다시 판단한다.
                    self._stat("held_trigger", ticker)
                    self._pstat("held_trigger", pos)
                    self._alert(f"held_trigger:{pos['id']}:{int(self.now() // 3600)}", "WARN",
                                f"[자동손절] {ticker} 보류 중 손절가 이하 시세 관측 ({q.price} ≤ {pos['stop_price']}) — "
                                "보류가 풀리면 최신 시세로 다시 판단합니다 (소급 주문 없음).")
                continue
            if q.price > dec(pos["stop_price"]):
                continue
            state = "OPEN" if mc.before_cutoff else "CARRY_OVER"
            self._watch_end(pos, rec)                     # 손절 트리거: 감시 구간이 여기서 끝난다
            eid = self.store.create_event(self.now(), pos, state=state, trigger_price=q.price,
                                          trigger_as_of=q.as_of, session_date=mc.session.day.isoformat())
            if eid:
                # 손절 트리거: 감시 구간이 여기서 끝난다. 트리거 시세를 얻은 현재 슬롯을 확정한다.
                self._persist_slots(pos["id"], mode, ticker, QH.close_slot(rec))
                self._health_save(mode, health)
                self.summary["triggers"] += 1
                note = "" if state == "OPEN" else " (마감 cutoff 이후 — 다음 정규장에 주문)"
                self._alert(f"event:{eid}:trigger", "URGENT",
                            f"[자동손절 발생] {pos['ticker']} 현재가 {q.price} ≤ 손절가 {pos['stop_price']} "
                            f"· 관리 {pos['remaining_managed_qty']}주 ({pos['mode']}){note}")
        # 일반 감시 대상이 아닌 영향 대상(관리 종료·확인 필요·다른 모드): 읽기 전용 확인 경로
        watched_ids = {str(p["id"]) for p in positions}
        for pid in sorted(affected - watched_ids, key=int):
            row = self.store.position(int(pid))
            if row is not None:
                self._incident_probe(row, mc, rnd, budget_end)
        self._evaluate_quote_hold(quote_hold, rnd)

    def _incident_probe(self, pos, mc, rnd, budget_end, *, health=None, rec=None):
        """incident 영향 대상의 읽기 전용 회복 조회 (트리거·주문 없음). 일반 감시와 같은 시세 검증(validate_detail:
        종목·거래소·가격·당일 정규장·개장 이후·120초)을 쓴다. 관리가 끝난 포지션은 거래 판단에 쓰이지 않으므로
        급변 재조회·잔고 확인은 하지 않고, 같은 요청 경로의 정상 응답만 증거로 기록한다."""
        inc = self.store.runtime_get("quote_global_hold") or {}
        a = (inc.get("affected") or {}).get(str(pos["id"]))
        # 증거를 다 모은 대상도 incident 가 끝날 때까지 계속 조회한다: 공통 경로의 정상 라운드 근거가 되고,
        # 다시 실패하면 그 대상은 처음부터 재검증된다.
        if a is None or str(pos["id"]) in rnd["probed"]:
            return
        now, interval = self.now(), self.settings.price_interval_sec
        if not QH.is_due(a, now):
            return
        if now > budget_end:
            rnd["skipped"]["skipped_budget"] = rnd["skipped"].get("skipped_budget", 0) + 1
            self._incident_status(pos["id"], "skipped_budget")
            return
        managed = pos["state"] in ("ACTIVE", "EXITING") and pos["mode"] == self.settings.mode
        rnd["probed"].add(str(pos["id"]))
        self._stat("incident_probe", pos["ticker"])
        self._pstat("attempt_incident", pos)
        q, err, meta = self._fetch_quote(pos, "incident", broker_mode=self.settings.mode)
        rnd["attempts"] += 1
        if err is None:
            found = validate_detail(q, self.now(), mc, self.settings, expect=(pos["ticker"], pos["exchange"]))
            if found:
                err = {**meta, **QH.validation_error(*found), "quote_as_of": q.as_of}
        factor = 1
        if err:
            rnd["errors"] += 1
            if err.get("scope") in (QH.SYSTEM, QH.UNKNOWN):
                rnd["system"] += 1
            record = self._record_quote_error(pos, "incident", err)
            if rec is not None:
                if QH.on_error(rec, self.now(), record, self.settings.max_consecutive_errors) == "fault":
                    # 상태 전이는 항상 감사 기록으로 남긴다 (보고서의 상태 구간 복원 근거)
                    self.store.audit(self.now(), "engine", "quote_fault", f"{pos['mode']}:{pos['ticker']}",
                                     {"position_id": pos["id"], "category": err.get("category"),
                                      "scope": err.get("scope"), "consecutive": rec["consecutive_errors"],
                                      "path": "incident_probe"})
                rec["last_observation"] = "failed"
            if err.get("scope") in (QH.SYSTEM, QH.UNKNOWN):
                self._quote_global_error(record, pos)
            else:
                self._incident_observe(pos["id"], "symbol", health_state=(rec or {}).get("state"), managed=managed)
            inc2 = self.store.runtime_get("quote_global_hold") or {}
            factor = QH.backoff_factor((inc2.get("affected") or {}).get(str(pos["id"])) or {})
        else:
            key = [pos["id"], q.as_of]
            if key not in (inc.get("seen") or []) and key not in rnd["new_ok"]:
                rnd["new_ok"].append(key)
            state = None
            if rec is not None:
                if QH.on_success(rec, self.now(), q.as_of) == "recovered":
                    self.store.audit(self.now(), "engine", "quote_recovered", f"{pos['mode']}:{pos['ticker']}",
                                     {"position_id": pos["id"], "fault_started_at": rec.get("fault_started_at"),
                                      "path": "incident_probe"})
                rec["last_observation"] = "recovering" if rec["state"] != QH.OK else "ok"
                state = rec["state"]
            self._incident_observe(pos["id"], "ok", as_of=q.as_of, health_state=state, managed=managed)
            self._stat("incident_probe_ok", pos["ticker"])
        if health is not None:
            self._health_save(pos["mode"], health)

        def schedule(inc3):
            a3 = (inc3.get("affected") or {}).get(str(pos["id"]))
            if a3 is None:
                return False
            QH.schedule_next_slot(a3, now, interval, factor)
        self._incident_update(schedule)

    def _ticker_ok(self, pos) -> bool:
        """저장된 종목 상태가 OK 인지 (필요조건일 뿐이다: 주문·취소 직전에는 새 유효 시세를 따로 확인한다)."""
        rec = self._health_load(pos["mode"]).get(str(pos["id"]))
        return rec is None or rec.get("state", QH.OK) == QH.OK

    # ── 6. 청산 진행 ─────────────────────────────────────────────
    # 공통 실행 게이트: 신규 매도·재주문(claim_sell)과 재주문 목적 취소(claim_cancel)의 선점 트랜잭션 안에서,
    # 쓰기 잠금을 얻은 뒤의 실제 현재 시각으로 다시 확인하는 불변 조건 (docs/auto_stop_sell.md "공통 실행 게이트").
    _SNAP_POS = ("state", "auto_paused", "remaining_managed_qty", "total_filled_qty", "initial_managed_qty")
    _SNAP_EV = ("state", "next_order_kind", "session_replace_count", "event_replace_count")
    _SNAP_ORDER = ("state", "filled_qty", "remaining_order_qty", "requested_qty", "order_number", "cancel_rejected")

    def _snapshot(self, pos, ev, o=None) -> dict:
        """사전 검증 시점의 비교 가능한 스냅샷 (불변 dict). 선점 시 최신 DB 행과 하나라도 다르면 검증을 무효로 본다."""
        snap = {"position": {k: pos[k] for k in self._SNAP_POS}, "event": {k: ev[k] for k in self._SNAP_EV}}
        if o is not None:
            snap["order"] = {k: o[k] for k in self._SNAP_ORDER}
        return snap

    ORDER_KINDS = ("ORIGINAL", "REPLACE", "CARRY_OVER", "CANCEL_FOR_REPLACE")
    REPLACE_KINDS = ("REPLACE", "CANCEL_FOR_REPLACE")

    def _exec_gate(self, *, kind, mode, position_id, event_id, quote_as_of, snapshot, order_id=None, qty=None,
                   limit_price=None):
        """선점 트랜잭션 안에서 실행되는 주문 종류별 최종 게이트 → 호출 가능 객체(db, now) → 거부 사유 또는 None.

        - 주문 종류: ORIGINAL / REPLACE / CARRY_OVER (신규·재·이월 매도), CANCEL_FOR_REPLACE (재주문 목적 취소).
          알 수 없는 종류, 최신 설정을 읽지 못함은 fail closed.
        - 최신 설정은 이 호출에서 한 번만 읽고(gate.settings), store.claim_* 가 그 설정으로 한도·lease 를 판단한다.
        - 네트워크 호출 없음. 저널 DB 읽기와 env·스키마 재확인만 한다. 조건표는 docs/auto_stop_sell.md
          "주문 종류별 안전 조건 매트릭스"."""
        engine, store = self, self.store

        class Gate:
            settings = None

            def __call__(self, db, now):
                self.settings = None
                if kind not in Engine.ORDER_KINDS:
                    return f"kind: 알 수 없는 주문 종류 {kind}"
                try:
                    settings = engine.settings_loader()
                except Exception as exc:                      # 최신 설정 확인 불가: 전송하지 않는다
                    return f"settings_unavailable: {type(exc).__name__}"
                if settings is None:
                    return "settings_unavailable: 설정 없음"
                if engine.schema_block:
                    return f"schema: {engine.schema_block}"
                mc = market_clock(now, settings)
                if not mc.is_open:
                    return "session_closed: 정규장이 아닙니다"
                if not mc.before_cutoff:
                    return "cutoff: 마감 cutoff 이후에는 신규 매도·재주문 목적 취소를 하지 않습니다"
                if settings.mode != mode:
                    return f"mode: 운영 모드 {settings.mode} ≠ 주문 모드 {mode}"
                if mode == "LIVE":
                    # 모드별 주문 활성화 스위치 + API Contract Gate (+ 취소면 LIVE 재주문 스위치·부분 취소 계약)
                    block = (S.live_cancel_block_reason(settings) if kind == "CANCEL_FOR_REPLACE"
                             else S.live_order_block_reason(settings))
                    if block:
                        return f"live_gate: {block}"
                thr = settings.max_consecutive_errors
                errs = store.rt(db, "api_errors", {}) or {}
                busy = [c for c in ("balance", "status", "orders", "quote") if int(errs.get(c, 0) or 0) >= thr]
                if busy:
                    return f"global_hold: 연속 오류 보류 ({', '.join(busy)})"
                if store.rt(db, "quote_global_hold"):
                    return "global_hold: 시세 시스템 장애 보류"
                rec = (store.rt(db, f"quote_health:{mode}", {}) or {}).get(str(position_id))
                if rec and rec.get("state", QH.OK) != QH.OK:
                    return f"symbol_hold: 종목 시세 {rec.get('state')}"
                pos = db.execute("SELECT * FROM auto_stop_positions WHERE id=?", (position_id,)).fetchone()
                ev = db.execute("SELECT * FROM auto_stop_exit_events WHERE id=?", (event_id,)).fetchone()
                if pos is None or pos["state"] != "EXITING" or pos["auto_paused"]:
                    return "position: 청산 진행 상태가 아니거나 일시중지됨"
                if ev is None or ev["state"] != "OPEN":
                    return "event: 청산 이벤트가 OPEN 이 아님"
                for k, v in snapshot["position"].items():
                    if pos[k] != v:
                        return f"stale_precheck: position.{k} {v} → {pos[k]}"
                for k, v in snapshot["event"].items():
                    if ev[k] != v:
                        return f"stale_precheck: event.{k} {v} → {ev[k]}"
                if kind != "CANCEL_FOR_REPLACE" and ev["next_order_kind"] != kind:
                    return f"kind_mismatch: 호출 {kind} ≠ 이벤트 {ev['next_order_kind']}"
                if order_id is not None:
                    o = db.execute("SELECT * FROM auto_stop_orders WHERE id=?", (order_id,)).fetchone()
                    for k, v in snapshot["order"].items():
                        if o is None or o[k] != v:
                            return f"stale_precheck: order.{k} {v} → {None if o is None else o[k]}"
                elif kind == "CANCEL_FOR_REPLACE":
                    return "kind: 취소 대상 주문 없음"
                if kind in Engine.REPLACE_KINDS:
                    if not settings.replace_enabled_for(mode):
                        flag = "AUTO_STOP_US_REPLACE_ENABLED" if mode == "LIVE" else "AUTO_STOP_US_SHADOW_REPLACE_ENABLED"
                        return f"replace_switch: {flag} 꺼짐"
                    if (ev["session_replace_count"] >= settings.max_replaces_per_session
                            or ev["event_replace_count"] >= settings.max_replaces_per_event):
                        return (f"replace_limit: 세션 {ev['session_replace_count']}/{settings.max_replaces_per_session}"
                                f", 이벤트 {ev['event_replace_count']}/{settings.max_replaces_per_event}")
                if kind == "CARRY_OVER":
                    # 기존 이월 정책 (_rollover): 이월 횟수 한도, 오늘 세션으로 이월됨, LIVE 는 POST_CLOSE_EXPIRY 검증
                    if ev["carry_over_count"] > settings.max_carry_over_sessions:
                        return f"carry_limit: 이월 {ev['carry_over_count']}/{settings.max_carry_over_sessions}"
                    if ev["session_date"] != mc.session.day.isoformat():
                        return "carry_session: 오늘 정규장으로 이월되지 않은 이벤트"
                    if mode == "LIVE" and not settings.verified("POST_CLOSE_EXPIRY"):
                        return "carry_contract: POST_CLOSE_EXPIRY 미검증"
                if qty is None or limit_price is None or int(qty) < 1 or Decimal(limit_price) <= 0:
                    return "order_size: 수량·가격 없음"
                if int(qty) > settings.max_order_qty or Decimal(limit_price) * int(qty) > settings.max_order_notional_usd:
                    return f"order_cap: 주문 상한 초과 ({qty}주 × {limit_price})"
                if mode == "LIVE" and int(qty) > settings.live_qty_cap():
                    return f"order_cap: LIVE 수량 제한 {settings.live_qty_cap()}주 ({qty}주)"
                if quote_as_of is None:
                    return "quote: 시세 시각 없음"
                if ny_date(quote_as_of) != mc.session.day or quote_as_of < mc.session.open_ts:
                    return "quote: 당일 정규장 시세가 아님"
                age = now - quote_as_of
                if age > settings.quote_max_age_sec or age < -60:
                    return f"quote_stale: 시세 {int(age)}초 경과 (한도 {settings.quote_max_age_sec}초)"
                if mode == "LIVE":
                    block = Engine.scanner_block_db(store, db, pos["ticker"])
                    if block:
                        return block
                self.settings = settings
                return None

        return Gate()

    @staticmethod
    def scanner_block_db(store, db, ticker):
        """Scanner 매수 저널 확인 → 차단 사유 | None. 사전 검사와 최종 선점 게이트가 같은 의미·같은 문구를 쓴다.

        ScannerJournalUnreadable(객체 판정·메타데이터·미확정 조회 실패)은 '미확정 없음'이 아니라 예상 가능한 주문
        안전 차단 사유다. 다른 예외(저널 자체 손상, LeaseLost 등)는 여기서 잡지 않고 기존 정책대로 올린다."""
        try:
            if store.scanner_buy_in_flight_db(db, ticker):
                return "scanner_buy_in_flight: 같은 계좌·종목 Scanner 미확정 매수"
        except ScannerJournalUnreadable as exc:
            return f"scanner_journal_unreadable: {exc}"
        return None

    def _scanner_block(self, ticker):
        """LIVE 사전 검사용 (별도 읽기 트랜잭션). 최종 판단은 선점 트랜잭션 안의 게이트가 다시 한다."""
        with self.store.tx() as db:
            return self.scanner_block_db(self.store, db, ticker)

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
            status, detail = self._send(mc, ev, pos)
            self._after_send(ev, pos, status, detail)

    # _send 결과: 주문 행이 생긴 것과 증권사 접수가 확인된 것은 다르다.
    ACCEPTED = ("SUBMITTED", "PARTIAL_FILLED", "FILLED", "CANCEL_SENDING", "CANCEL_PENDING", "CANCEL_UNKNOWN")

    def _after_send(self, ev, pos, status, detail):
        """매도 시도 결과 기록. status: blocked(전송 전 차단, detail=사유) / order(주문 행 생성, detail=주문 id).

        - 접수 확인(SUBMITTED 이후): 재주문 대기·차단 기록 해제.
        - SENDING·UNKNOWN(전송 결과 불명): 해제하지 않는다. 긴급 알림은 기존 UNKNOWN 알림이 맡는다.
        - REJECTED·NOT_SENT: 해제하지 않는다. 이벤트는 기존 정책대로 확인 필요(NEEDS_REVIEW) 이며 그 알림이 맡는다.
        - 차단: 재주문이면 재주문 대기(URGENT, 대기 시작 시각 유지), 최초·이월이면 주문 차단 기록."""
        kind = ev["next_order_kind"]
        if status == "blocked":
            if kind == "REPLACE":
                self._note_replace_waiting(ev, pos, detail)
            else:
                self._note_order_block(ev, pos, kind, detail)
            return
        if status != "order":
            return
        from trading.auto_stop.service import reconcile_wait_records
        o = self.store.order(detail)
        keys = [f"replace_waiting:{ev['id']}", f"order_block:{ev['id']}"]
        if kind == "REPLACE" and not self.store.runtime_get(keys[0]) and o["state"] not in self.ACCEPTED:
            # 접수가 확인되지 않은 재주문: 결과 확인 전까지 대기 기록을 남긴다 (대기 시작 = 주문 생성 시각).
            self.store.runtime_set(keys[0], {
                "since": o["created_at"], "at": self.now(), "reason": f"재주문 결과 {o['state']}",
                "ticker": pos["ticker"], "remaining": pos["remaining_managed_qty"]}, self.now())
        # 기록 판정은 실제 주문 상태로 한 곳에서 한다 (접수 확인이면 해제, 불명·거부면 결과 기록).
        reconcile_wait_records(self.store, self.now(), keys=keys)

    def _reconcile_wait_records(self):
        """주문 대조 직후, 모든 대기 기록을 실제 주문·이벤트 상태와 맞춘다 (OPEN 이벤트 여부와 무관, 주문 없음)."""
        from trading.auto_stop.service import reconcile_wait_records
        reconcile_wait_records(self.store, self.now())

    def _note_order_block(self, ev, pos, kind, reason):
        """최초·이월 매도 전송 전 차단: 이벤트·트리거 유지, 실제 사유 기록. Scanner 저널 읽기 실패는 URGENT."""
        now = self.now()
        key = f"order_block:{ev['id']}"
        prev = self.store.runtime_get(key) or {}
        if prev.get("status", "waiting") != "waiting" or prev.get("order_id"):
            prev = {}                                  # 이전 시도의 결과 기록 → 새 차단 구간
        self.store.runtime_set(key, {"since": prev.get("since") or now, "at": now, "kind": kind, "reason": reason,
                                     "ticker": pos["ticker"], "remaining": pos["remaining_managed_qty"]}, now)
        cat = reason.split(":")[0]
        self._stat("order_blocked", cat)
        if cat == "scanner_journal_unreadable":
            self._alert(f"event:{ev['id']}:order_block:{cat}:{int(now // 3600)}", "URGENT",
                        f"[자동손절 매도 보류] {pos['ticker']} Scanner 매수 저널을 읽을 수 없어 매도를 보내지 않습니다 — "
                        f"{reason}. 청산 이벤트는 유지되며 저널이 정상화되면 모든 안전 조건을 다시 확인해 매도합니다.")

    def _send(self, mc, ev, pos):
        mode, now = ev["mode"], self.now()
        if not self._ticker_ok(pos):
            # 이 종목 시세가 장애·회복 확인 중이면 신규 주문을 하지 않는다 (회복 조회는 _watch 가 계속).
            self._stat("order_held_symbol", pos["ticker"])
            return "blocked", "symbol_hold: 종목 시세 장애·회복 확인 중"
        if mode == "LIVE":
            reason = S.live_order_block_reason(self.settings_loader())
            if reason:
                self._alert(f"event:{ev['id']}:blocked:{mc.session.day}", "WARN",
                            f"[자동손절] {pos['ticker']} 매도 대기 — 실거래 스위치 차단: {reason}")
                return "blocked", f"live_gate: {reason}"
            block = self._scanner_block(pos["ticker"])
            if block:
                if block.startswith("scanner_buy_in_flight"):
                    self._alert(f"event:{ev['id']}:buy_in_flight:{mc.session.day}", "WARN",
                                f"[자동손절] {pos['ticker']} 스캐너 매수 주문이 미확정이라 매도를 보류합니다.")
                return "blocked", block
        ev = self.store.event(ev["id"])
        pos = self.store.position(pos["id"])
        if ev["state"] != "OPEN" or pos["state"] != "EXITING":
            return "skipped", "position_state"
        snapshot = self._snapshot(pos, ev)          # 사전 검증 기준 (선점 시 최신 DB 와 비교)
        holdings = self._balance_sync(force=True)
        if holdings is None:
            return "blocked", "balance_unavailable: 잔고 확인 실패"
        pos = self.store.position(pos["id"])
        if pos["state"] != "EXITING":
            return "skipped", "position_state"
        h = holdings.get(pos["ticker"])
        if h is None:
            self._review_event(ev, "잔고에 종목이 없습니다")
            return "review", "잔고에 종목 없음"
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
                return "blocked", "orders_unavailable: 주문내역 확인 실패"
            if external:
                self._review_event(ev, f"같은 종목의 외부 미체결 주문 {len(external)}건이 있습니다")
                return "review", "external_orders"
        qty = min(pos["remaining_managed_qty"], h["sellable"])
        if qty <= 0:
            self._review_event(ev, f"매도가능수량 {h['sellable']}주 — 매도할 수 없습니다")
            return "review", "sellable"
        health = self._health_load(mode)
        rec = health.setdefault(str(pos["id"]), QH.new_record(self.now(), pos))
        q, problem, err = self._confirmed_quote(pos, mc, "order")
        if problem:
            self._review_event(ev, problem)
            return "review", problem
        if err:
            # 무효 시세는 임계값 전이라도 주문에 쓰지 않는다.
            self._quote_failed(pos, rec, "order", err)
            self._health_save(mode, health)
            return "blocked", f"quote_invalid: {err.get('category')}"
        self._quote_ok(pos, rec, "order", q)
        self._health_save(mode, health)
        self._stat("order_quote_ok", pos["ticker"])
        self._pstat("order_ok", pos)
        self.store.update_position(pos["id"], self.now(), last_quote_price=str(q.price), last_quote_at=q.as_of)
        ref, src = reference_price(q, allow_bid=(mode != "LIVE" or self.settings.verified("USA20101_BID")))
        if ref < MIN_AUTO_PRICE:
            self._review_event(ev, f"기준가 {ref} < $1 — 호가 단위 미검증으로 자동 매도하지 않습니다")
            return "review", "price"
        price = limit_price(ref, self.settings.slip_bps)
        if price <= 0:
            self._review_event(ev, "지정가 계산 결과가 유효하지 않습니다")
            return "review", "price"
        if qty > self.settings.max_order_qty or price * qty > self.settings.max_order_notional_usd:
            self._review_event(ev, f"주문 상한 초과 ({qty}주 × {price})")
            return "review", "order_cap"
        if mode == "LIVE" and qty > self.settings.live_qty_cap():
            self._review_event(ev, f"UST20001_SELL 1주 실매도 검증 전에는 LIVE 주문을 1주로 제한합니다 ({qty}주)")
            return "review", "order_cap"
        kind = ev["next_order_kind"]
        # 가상 체결 모델을 주문마다 고정 기록한다: 모델이 바뀌어도 이 주문은 만든 모델로 판정·집계한다.
        evidence = {"shadow": [self._shadow_config(mode)]} if mode == "SHADOW" else {}
        evidence["claim"] = [{"quote_as_of": q.as_of, "ref": str(ref), "limit": str(price), "qty": qty,
                              "sellable": int(h["sellable"]), "held": int(h["qty"])}]
        try:
            # 실거래 스위치·API 계약·재주문 스위치·한도는 게이트가 같은 트랜잭션에서 한 번 읽은 최신 설정으로 본다.
            order = self.store.claim_sell(
                self.now(), owner=self.owner, event_id=ev["id"], kind=kind, qty=qty, ref_price=ref, ref_source=src,
                quote_as_of=q.as_of, limit_price=price, settings=self.settings, guard=lambda: None,
                clock=self.clock, sent_evidence=evidence,
                gate=self._exec_gate(kind=kind, mode=mode, position_id=pos["id"], event_id=ev["id"],
                                     quote_as_of=q.as_of, snapshot=snapshot, qty=qty, limit_price=price))
        except ClaimRefused as exc:
            log.info("[자동손절] 매도 선점 거부: %s %s", pos["ticker"], exc)
            self._stat("claim_refused", str(exc).split(":")[0])
            return "blocked", str(exc)
        sent_at = order["sent_at"]
        # 선점 커밋 직후 전송한다: 이 사이에 다른 DB·API 작업을 두지 않는다.
        try:
            res = self._broker(mode).sell(pos["ticker"], pos["exchange"], qty, price)
        except OrderRejected as exc:
            self.store.transition(order["id"], self.now(), frm="SENDING", to="REJECTED", broker_state="REJECTED")
            self.store.add_order_evidence(order["id"], self.now(), "reject", str(exc)[:200])
            self._review_event(ev, "매도 주문이 명시적으로 거부되었습니다")
            return "order", order["id"]
        except OrderUnknown:
            self._mark_unknown(order, "매도 응답 불명")
            return "order", order["id"]
        except KiwoomError as exc:          # 전송 전 입력 검증 실패: 증권사로 나가지 않음
            self.store.transition(order["id"], self.now(), frm="SENDING", to="NOT_SENT")
            self.store.add_order_evidence(order["id"], self.now(), "not_sent", str(exc)[:200])
            self._review_event(ev, "주문 입력 검증 실패로 전송하지 않았습니다")
            return "order", order["id"]
        except Exception:
            self._mark_unknown(order, "매도 중 예외")
            return "order", order["id"]
        number = str(res.get("order_number") or "")
        if not self.store.transition(order["id"], self.now(), frm="SENDING", to="SUBMITTED",
                                     order_number=number, order_date=ny_date(sent_at).isoformat()):
            # lease 만료로 다른 작업자가 UNKNOWN 처리함 → 상태는 바꾸지 않고 증거만 남긴다
            self.store.add_order_evidence(order["id"], self.now(), "late_response", {"order_number": number})
            return "order", order["id"]
        self.summary["sent"] += 1
        label = {"ORIGINAL": "최초 주문", "REPLACE": "재주문", "CARRY_OVER": "이월 주문"}[kind]
        self._alert(f"order:{order['id']}:submitted", "INFO",
                    f"[자동손절] {pos['ticker']} {label} 접수 {qty}주 @ {price} (기준 {src} {ref}, {mode}, 주문 {number})")
        return "order", order["id"]

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
        reason, ctx = self._replace_precheck(mc, ev, pos, o)
        if reason:
            # 어느 하나라도 확인하지 못하면 재주문 목적 취소를 하지 않는다. 기존 주문 유지·대조만 계속한다.
            self._stat("replace_precheck_fail", reason.split(":")[0])
            self._alert(f"order:{o['id']}:cancel_held:{int(now // 3600)}", "WARN",
                        f"[자동손절] {o['ticker']} 미체결 주문 {o['order_number']} 재주문용 취소 보류 — {reason}. "
                        "기존 주문은 유지하고 체결·상태 대조를 계속합니다.")
            return
        snap = ctx["snapshot"]
        try:
            order = self.store.claim_cancel(
                self.now(), owner=self.owner, order_id=o["id"], expect_state=snap["order"]["state"],
                guard=lambda: None, lease_sec=self.settings.lease_sec, clock=self.clock,
                evidence={"quote_as_of": ctx["quote_as_of"], "replace_qty": ctx["replace_qty"]},
                gate=self._exec_gate(kind="CANCEL_FOR_REPLACE", mode=ev["mode"], position_id=pos["id"],
                                     event_id=ev["id"], quote_as_of=ctx["quote_as_of"], snapshot=snap,
                                     order_id=o["id"], qty=ctx["replace_qty"], limit_price=ctx["limit"]))
        except ClaimRefused as exc:
            # 사전 검증 이후 조건이 바뀜: 기존 주문 상태 유지. 다음 tick 에 주문 대조부터 다시 한다.
            self._stat("claim_refused", str(exc).split(":")[0])
            log.info("[자동손절] 취소 선점 거부: %s %s", o["ticker"], exc)
            self._alert(f"order:{o['id']}:cancel_held:{int(now // 3600)}", "WARN",
                        f"[자동손절] {o['ticker']} 미체결 주문 {o['order_number']} 재주문용 취소 보류 — {exc}. "
                        "기존 주문은 유지하고 체결·상태 대조를 계속합니다.")
            return
        o = {**o, "state": snap["order"]["state"]}
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

    def _replace_precheck(self, mc, ev, pos, o):
        """재주문 목적 취소 전 검증 → (거부 사유 | None, 문맥). 취소하면 걸려 있던 매도 주문을 잃으므로, 취소 후
        곧바로 안전하게 재주문할 수 있다는 현재 증거가 모두 있어야 한다.

        - 먼저 이 주문을 증권사 주문내역으로 다시 대조해 추가 체결을 반영한다 (체결 증거).
        - 그 시점의 주문·포지션·이벤트를 스냅샷으로 남긴다. 선점 트랜잭션은 이 스냅샷과 최신 DB 를 비교한다.
        - 보유수량이 관리 잔여보다 적고 체결 증거로 설명되지 않으면 취소하지 않는다 (긴급 알림, 대조 계속).
        - 시세는 일반 감시·주문 직전과 같은 _confirmed_quote 로 새로 검증한다. 저장된 종목 상태 OK 만으로 허용하지 않는다.
        - 취소 후 매도가능 추정식은 이 사전 검토에만 쓴다. 실제 재주문 수량은 취소 확정 뒤 _send 가 브로커의 최신
          매도가능수량을 다시 조회해 정한다."""
        mode = ev["mode"]
        if self.killed():
            return "kill_switch: 전체 긴급 중지", None
        hold = self._hold_reasons()
        if hold:
            return f"global_hold: 전체 보류({', '.join(hold)})", None
        if not self._ticker_ok(pos):
            return "symbol_hold: 종목 시세 장애·회복 확인 중", None
        row = self._reconcile_one(o)                  # 추가 체결 증거 반영 (읽기 전용 조회)
        if row is False:
            return "status_unavailable: 주문 상태 확인 실패", None
        if row is None:
            return "order_not_visible: 증권사 주문내역에서 주문을 확인하지 못함", None
        o, pos, ev = self.store.order(o["id"]), self.store.position(pos["id"]), self.store.event(ev["id"])
        if o["state"] not in ("SUBMITTED", "PARTIAL_FILLED") or not o["order_number"] or o["cancel_rejected"]:
            return f"order_state: 주문 상태 {o['state']}", None
        if ev["state"] != "OPEN" or pos["state"] != "EXITING" or pos["auto_paused"]:
            return f"position_state: {pos['state']}/{ev['state']}", None
        open_remaining = int(o["requested_qty"]) - int(o["filled_qty"])
        if open_remaining <= 0:
            return "order_state: 미체결 잔량 없음", None
        snapshot = self._snapshot(pos, ev, o)
        if mode == "LIVE":
            block = self._scanner_block(pos["ticker"])
            if block:
                return block, None
            try:
                self._renew()
                ours = {x["order_number"] for x in self.store.orders(mode="LIVE") if x["order_number"]}
                external = [r for r in self._broker(mode).day_orders(pos["ticker"])
                            if r["state"] in ("OPEN", "PARTIAL") and r["order_number"] not in ours]
                self._renew()
                self._api_ok("orders")
            except LeaseLost:
                raise
            except Exception as exc:
                self._api_error("orders", f"주문내역 {pos['ticker']}", exc)
                return "orders_unavailable: 주문내역 확인 실패", None
            if external:
                return f"external_orders: 같은 종목 외부 미체결 {len(external)}건", None
        holdings = self._balance_sync(force=True)
        if holdings is None:
            return "balance_unavailable: 잔고 확인 실패", None
        pos = self.store.position(pos["id"])
        if pos["state"] != "EXITING" or pos["auto_paused"]:
            return f"position_state: {pos['state']}", None
        h = holdings.get(pos["ticker"])
        if h is None:
            self._holding_mismatch(pos, o, {"qty": 0, "sellable": 0})
            return "holding_mismatch: 잔고에 종목 없음", None
        if int(h["qty"]) < int(pos["remaining_managed_qty"]):
            # 체결 증거(방금 대조한 누적 체결)를 반영한 뒤에도 보유가 관리 잔여보다 적다: 원인 불명 → 취소하지 않는다.
            self._holding_mismatch(pos, o, h)
            return (f"holding_mismatch: 보유 {h['qty']}주 < 관리 잔여 {pos['remaining_managed_qty']}주 "
                    "(체결 증거로 설명되지 않음)"), None
        # 미체결 매도가 예약한 수량 고려: 브로커 매도가능이 미체결분을 이미 뺐는지는 미검증(UST21070_SELLABLE).
        after_cancel = min(int(h["qty"]), int(h["sellable"]) + open_remaining)
        qty = min(int(pos["remaining_managed_qty"]), after_cancel)
        if qty <= 0:
            return f"sellable: 취소 후 매도가능 추정 {after_cancel}주", None
        health = self._health_load(mode)
        rec = health.setdefault(str(pos["id"]), QH.new_record(self.now(), pos))
        q, problem, err = self._confirmed_quote(pos, mc, "cancel_check")
        if problem:
            return f"quote_problem: {problem}", None
        if err:
            self._quote_failed(pos, rec, "cancel_check", err)
            self._health_save(mode, health)
            return f"quote_invalid: {err.get('category')}", None
        self._quote_ok(pos, rec, "cancel_check", q)
        self._health_save(mode, health)
        self.store.update_position(pos["id"], self.now(), last_quote_price=str(q.price), last_quote_at=q.as_of)
        ref, src = reference_price(q, allow_bid=(mode != "LIVE" or self.settings.verified("USA20101_BID")))
        if ref < MIN_AUTO_PRICE:
            return f"price: 기준가 {ref} < $1", None
        price = limit_price(ref, self.settings.slip_bps)
        if price <= 0:
            return "price: 지정가 계산 결과 무효", None
        if qty > self.settings.max_order_qty or price * qty > self.settings.max_order_notional_usd:
            return f"order_cap: 주문 상한 초과 ({qty}주 × {price})", None
        if mode == "LIVE" and qty > self.settings.live_qty_cap():
            return f"order_cap: LIVE 1주 상한 ({qty}주)", None
        ctx = {"snapshot": snapshot, "quote_as_of": q.as_of, "replace_qty": qty, "ref": str(ref), "ref_source": src,
               "limit": str(price), "sellable": int(h["sellable"]), "held": int(h["qty"]),
               "open_remaining": open_remaining}
        self.store.add_order_evidence(o["id"], self.now(), "replace_precheck", {"at": self.now(), **ctx,
                                                                                 "snapshot": None})
        return None, ctx

    def _holding_mismatch(self, pos, o, h):
        """취소 전 보유수량 불일치: 기존 주문을 유지하고 사람 확인을 요청한다 (기존 주문 대조는 계속)."""
        now = self.now()
        detail = {"at": now, "ticker": pos["ticker"], "held": int(h.get("qty") or 0),
                  "sellable": int(h.get("sellable") or 0), "remaining_managed": pos["remaining_managed_qty"],
                  "initial_managed": pos["initial_managed_qty"], "total_filled": pos["total_filled_qty"],
                  "order_id": o["id"], "order_filled": o["filled_qty"], "order_open": o["requested_qty"] - o["filled_qty"]}
        self.store.runtime_set(f"holding_mismatch:{pos['id']}", detail, now)
        self._stat("holding_mismatch", pos["ticker"])
        self._alert(f"position:{pos['id']}:holding_mismatch:{int(now // 3600)}", "URGENT",
                    f"[자동손절 확인 필요] {pos['ticker']} 보유 {detail['held']}주 < 관리 잔여 "
                    f"{detail['remaining_managed']}주 (증권사 체결 증거로 설명되지 않음) — 재주문용 취소를 하지 않고 기존 "
                    f"주문 {o['order_number']}을 유지합니다. 키움 앱에서 잔고·주문을 확인하세요.")

    def _note_replace_waiting(self, ev, pos, reason):
        """취소 확정 뒤 재주문을 못 하고 있음: 청산 이벤트·트리거는 그대로 두고 대기 상태를 기록, 긴급 알림."""
        now = self.now()
        key = f"replace_waiting:{ev['id']}"
        prev = self.store.runtime_get(key) or {}
        if prev.get("status", "waiting") != "waiting" or prev.get("order_id"):
            prev = {}                                  # 이전 시도의 결과 기록 → 새 대기 구간
        detail = {"since": prev.get("since") or now, "at": now, "reason": reason, "ticker": pos["ticker"],
                  "remaining": pos["remaining_managed_qty"], "last_quote_issue": self.store.runtime_get("last_quote_issue"),
                  "last_api_error": self.store.runtime_get("last_api_error")}
        self.store.runtime_set(key, detail, now)
        self._stat("replace_waiting", pos["ticker"])
        self._alert(f"event:{ev['id']}:replace_waiting:{int(now // 3600)}", "URGENT",
                    f"[자동손절 재주문 대기] {pos['ticker']} 취소 확정 후 잔여 {pos['remaining_managed_qty']}주 재주문 "
                    f"보류 — {reason}. 청산 이벤트는 유지되며 조건이 확인되면 최신 시세로 재주문합니다.")

    def _note_replace_waiting_under_hold(self, hold):
        """전체 보류로 청산 진행을 하지 않는 동안, 취소 확정 뒤 재주문을 기다리는 이벤트를 기록·알린다."""
        for ev in self.store.events(states=("OPEN",), mode=self.settings.mode):
            if ev["next_order_kind"] != "REPLACE":
                continue
            pos = self.store.position(ev["position_id"])
            if pos["state"] != "EXITING" or pos["remaining_managed_qty"] <= 0:
                continue
            orders = self.store.orders(event_id=ev["id"])
            if orders and orders[-1]["state"] == "CANCELLED":
                self._note_replace_waiting(ev, pos, f"전체 보류({', '.join(hold)})")

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
