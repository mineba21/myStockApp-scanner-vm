"""스케줄러 진입점과 실 브로커 조립.

이 tick 은 모드가 OFF여도 실행한다: 과거 LIVE/SHADOW 주문의 대조와 종료 판정은 모드와 무관하다.
신규 감시·주문은 engine 이 mode 와 실거래 스위치를 보고 결정한다.
"""
from __future__ import annotations

import logging
import threading

from trading.auto_stop import settings as S
from trading.auto_stop.brokers import LiveStopSellBroker, ShadowBroker
from trading.auto_stop.engine import Engine, new_owner
from trading.auto_stop.store import AutoStopStore
from trading.kiwoom_readonly import KiwoomError

log = logging.getLogger(__name__)
_OWNER = new_owner()          # 프로세스당 하나 (lease 소유자)
_LOCK = threading.Lock()


class ReadAccessBlocked(KiwoomError):
    """조회 조건이 꺼져 있어 증권사에 접근하지 않음."""


def store_or_none():
    path = S.Settings.from_env().journal_path
    return AutoStopStore(path) if path else None


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
        store = store_or_none()
        if store is None:
            return {"status": "NO_JOURNAL"}
        return Engine(store, broker_factory, owner=_OWNER, notifier=notifier).tick()
    except Exception:
        log.error("[자동손절] tick 실패", exc_info=True)
        return {"status": "ERROR"}
    finally:
        _LOCK.release()
