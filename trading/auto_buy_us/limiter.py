"""SHADOW 호출 예산 (설계 §9.3).

account1 App Key 의 조회 한도(초당 5회)를 S01·주문·화면 조회와 공유하므로 SHADOW 는
전체 초당 1회·TR별 초당 1회·tick 당 20회, KST 09:00~10:00 호출 0, 429 즉시 정지 후 60초 쿨다운.
RateLimited·BudgetExhausted 는 KiwoomError 가 아니다: S01 의 429 재시도(_read_retry)와
QuoteUnavailable 변환을 타지 않고 호출자까지 그대로 올라간다 (재시도 루프 없음).
"""
from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

KST = ZoneInfo("Asia/Seoul")
PEAK_START_HOUR, PEAK_END_HOUR = 9, 10
OAUTH_API_ID = "oauth"
OUTCOMES = ("ok", "err", "http_429", "timeout", "budget_skip")


class RateLimited(Exception):
    """429 응답: 그 tick 의 조회를 즉시 멈춘다."""


class BudgetExhausted(Exception):
    """쿨다운·tick 상한·피크 시간이라 이번 호출을 보내지 않았다."""


class CallBudget:
    """tick 단위 SHADOW 호출 예산. 한 프로세스(엔진 tick)에서만 쓴다."""

    def __init__(self, settings, clock=time.time, sleep=time.sleep):
        self.settings = settings
        self.clock, self.sleep = clock, sleep
        self.calls_this_tick = 0
        self.cooldown_until = -math.inf
        self._last_any = -math.inf
        self._last_by_tr: dict[str, float] = {}
        self._stats: Counter = Counter()

    def start_tick(self) -> None:
        self.calls_this_tick = 0

    def _skip(self, api_id: str, reason: str):
        self._stats[(api_id, "budget_skip")] += 1
        raise BudgetExhausted(f"{api_id}: {reason}")

    def acquire(self, api_id: str) -> None:
        now = self.clock()
        if PEAK_START_HOUR <= datetime.fromtimestamp(now, KST).hour < PEAK_END_HOUR:
            self._skip(api_id, "KST 09:00~10:00 피크 시간에는 SHADOW 조회를 하지 않습니다.")
        if now < self.cooldown_until:
            self._skip(api_id, f"429 쿨다운 중입니다 ({int(self.cooldown_until - now) + 1}초 남음).")
        if self.calls_this_tick >= self.settings.max_calls_per_tick:
            self._skip(api_id, f"tick 당 호출 상한 {self.settings.max_calls_per_tick}회에 도달했습니다.")
        due = max(self._last_any + 1.0 / self.settings.calls_per_sec,
                  self._last_by_tr.get(api_id, -math.inf) + 1.0 / self.settings.per_tr_per_sec)
        wait = due - self.clock()
        if wait > 0:
            self.sleep(wait)
        at = self.clock()
        self._last_any = at
        self._last_by_tr[api_id] = at
        self.calls_this_tick += 1

    def on_response(self, api_id: str, status_code) -> None:
        if status_code == 429:
            self._stats[(api_id, "http_429")] += 1
            self.cooldown_until = self.clock() + self.settings.cooldown_429_sec
            raise RateLimited(f"{api_id}: HTTP 429 — SHADOW 조회를 멈추고 "
                              f"{self.settings.cooldown_429_sec}초 쉽니다.")
        self._stats[(api_id, "ok" if status_code == 200 else "err")] += 1

    def on_timeout(self, api_id: str) -> None:
        self._stats[(api_id, "timeout")] += 1

    def on_error(self, api_id: str) -> None:
        """응답 없이 끝난 연결 오류 (timeout 제외)."""
        self._stats[(api_id, "err")] += 1

    def drain_stats(self) -> dict[tuple[str, str], int]:
        out, self._stats = dict(self._stats), Counter()
        return out


class ThrottledSession(requests.Session):
    """SHADOW 전용 세션. 모든 POST 가 예산을 거치고, 429 는 응답을 돌려주기 전에 RateLimited 로 끊는다."""

    def __init__(self, budget: CallBudget):
        super().__init__()
        self.budget = budget

    def post(self, url, *args, headers=None, **kw):
        api_id = (headers or {}).get("api-id") or OAUTH_API_ID
        self.budget.acquire(api_id)
        try:
            response = super().post(url, *args, headers=headers, **kw)
        except requests.Timeout:
            self.budget.on_timeout(api_id)
            raise
        except requests.RequestException:
            self.budget.on_error(api_id)
            raise
        self.budget.on_response(api_id, response.status_code)
        return response
