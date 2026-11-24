"""스케줄러 진입점과 실 브로커 조립.

이 tick 은 모드가 OFF여도 실행한다: 과거 LIVE/SHADOW 주문의 대조와 종료 판정은 모드와 무관하다.
신규 감시·주문은 engine 이 mode 와 실거래 스위치를 보고 결정한다.
"""
from __future__ import annotations

import logging
import threading
import time

from trading.auto_stop import settings as S
from trading.auto_stop.brokers import LiveStopSellBroker, ShadowBroker
from trading.auto_stop.engine import Engine, new_owner
from trading.auto_stop.schema import schema_status
from trading.auto_stop.store import AutoStopStore
from trading.kiwoom_readonly import KiwoomError

log = logging.getLogger(__name__)
_OWNER = new_owner()          # 프로세스당 하나 (lease 소유자)
_LOCK = threading.Lock()


class ReadAccessBlocked(KiwoomError):
    """조회 조건이 꺼져 있어 증권사에 접근하지 않음."""


def store_or_none(*, allow_compatible=False):
    """스키마가 준비된 저널의 store. 스키마가 없거나 다르면 None (DDL·파일 생성 없음)."""
    path = S.Settings.from_env().journal_path
    if not path:
        return None
    st = schema_status(path)
    if st["schema_ready"] or (allow_compatible and st["reconcile_compatible"]):
        return AutoStopStore(path)
    return None


_SCHEMA_NOTICES: dict = {}


def _schema_notice(key, severity, text, notify=None):
    """스키마 문제 알림 (outbox 테이블을 쓸 수 없으므로 직접 전송, 프로세스·하루 단위 중복 방지)."""
    day = time.strftime("%Y-%m-%d")
    if _SCHEMA_NOTICES.get(key) == day:
        return
    _SCHEMA_NOTICES[key] = day
    log.warning("[자동손절] %s", text)
    try:
        (notify or notifier)(severity, text)
    except Exception:
        log.warning("[자동손절] 스키마 알림 전송 실패", exc_info=True)


MANUAL_CHECK = ("키움 앱 account1 미국 주문내역에서 미체결 매도를 직접 확인하고 필요하면 앱에서 취소하세요. "
                "복구: docs/auto_stop_sell.md '저널 스키마와 Migration'.")


def schema_gate(settings, notify=None):
    """(진행 여부, schema_block, 상태). 스키마가 없으면 OFF 는 조용히 건너뛰고 SHADOW·LIVE 는 차단 알림."""
    st = schema_status(settings.journal_path)
    if st["schema_ready"]:
        return True, None, st
    live_open = st["live_unresolved_orders"] or 0
    if st["reconcile_compatible"]:
        block = f"자동 손절 스키마 불일치로 신규 주문 차단 ({st['schema_error']})"
        if settings.mode != "OFF" or live_open:
            _schema_notice(f"compat:{st['schema_version']}", "URGENT" if live_open or settings.mode == "LIVE"
                           else "WARN", f"[자동손절] {block}. 기존 주문 대조는 계속합니다."
                           + (f" LIVE 미확정 주문 {live_open}건." if live_open else ""), notify)
        return True, block, st
    if st["auto_stop_objects"] or live_open:
        # 자동 손절 기록이 있는데 구조를 신뢰할 수 없다: 대조 불가 → 증권사 직접 확인
        _schema_notice(f"broken:{st['schema_version']}:{st['auto_stop_objects']}", "URGENT",
                       f"[자동손절 URGENT] 저널 스키마 문제로 주문 대조를 할 수 없습니다 ({st['schema_error']}). "
                       + MANUAL_CHECK, notify)
    elif settings.mode != "OFF":
        _schema_notice(f"missing:{settings.mode}", "URGENT" if settings.mode == "LIVE" else "WARN",
                       f"[자동손절] {settings.mode} 모드지만 저널 스키마가 없어 감시·주문을 하지 않습니다 "
                       f"({st['schema_error']}). 배포 작업 'python -m scripts.auto_stop_journal_migrate'가 필요합니다.",
                       notify)
    return False, None, st


def live_reader(*, allow_orders=False):
    reason = S.read_access_block_reason()
    if reason:
        raise ReadAccessBlocked(reason)
    from trading.kiwoom_readonly import KiwoomConfig, KiwoomReadOnlyClient
    from web.kiwoom_holdings import _get_token
    config = KiwoomConfig.from_profile(S.ACCOUNT_PROFILE)
    client = KiwoomReadOnlyClient(config)
    token = _get_token(S.ACCOUNT_PROFILE, config, client)
    return LiveStopSellBroker(config, client=client, token=token, allow_orders=allow_orders)


def broker_factory(mode):
    if mode == "LIVE":
        # 주문 객체는 모든 실거래 스위치가 켜졌을 때만 만든다. 전송 직전 재검증은 store.claim_* 에서 다시 한다.
        return live_reader(allow_orders=S.live_order_block_reason() is None)
    if mode == "SHADOW":
        # SHADOW 는 조회 전용 어댑터만 쓴다. STRESS 를 포함한 모든 가상 모델은 실주문 객체가 없다.
        return ShadowBroker(live_reader(allow_orders=False), S.Settings.from_env().shadow_fill_model)
    raise ValueError(mode)


def notifier(severity, text):
    from notifications.slack import send_slack_message
    from notifications.telegram import send_telegram_message
    prefix = {"URGENT": "🚨 ", "WARN": "⚠️ ", "INFO": ""}.get(severity, "")
    sent = False
    for fn in (lambda: send_slack_message(prefix + text),
               lambda: send_telegram_message(prefix + text, parse_mode="")):
        try:
            sent = bool(fn()) or sent
        except Exception:
            log.warning("[자동손절] 알림 전송 실패", exc_info=True)
    return sent


def auto_stop_us_tick():
    """APScheduler 작업. 같은 프로세스 안의 중복 실행은 잠금으로, 프로세스 간은 lease 로 막는다."""
    if not _LOCK.acquire(blocking=False):
        return {"status": "BUSY"}
    try:
        settings = S.Settings.from_env()
        if not settings.journal_path:
            return {"status": "NO_JOURNAL"}
        go, block, st = schema_gate(settings)
        if not go:
            return {"status": "SCHEMA_NOT_READY", "mode": settings.mode, "schema_error": st["schema_error"]}
        return Engine(AutoStopStore(settings.journal_path), broker_factory, owner=_OWNER, notifier=notifier,
                      schema_block=block).tick()
    except Exception:
        log.error("[자동손절] tick 실패", exc_info=True)
        return {"status": "ERROR"}
    finally:
        _LOCK.release()
