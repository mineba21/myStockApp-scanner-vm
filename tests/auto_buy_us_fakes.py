"""auto_buy_us 테스트 보조: 운영 DB(임시 SQLite) 시드 헬퍼."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def utc_naive(aware: datetime) -> datetime:
    """aware datetime → 운영 DB 방식의 naive UTC."""
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def seed_scan(session, started_utc, finished_utc, rows, triggered_by="scheduler", status="DONE", market="US"):
    """ScanLog 1건과 ScanResult 행을 넣고 ScanLog 를 돌려준다. rows 는 dict 목록(ticker 필수).

    scan_time 기본값은 started + 1분, market 기본값은 'US'. 시각은 naive UTC.
    """
    from database.models import ScanLog, ScanResult

    log = ScanLog(started_at=started_utc, finished_at=finished_utc, market=market,
                  status=status, triggered_by=triggered_by)
    session.add(log)
    session.flush()
    for r in rows:
        kw = {"scan_time": started_utc + timedelta(minutes=1), "market": "US", "price": 100.0,
              "pivot_price": 98.0, "stop_loss": 92.0, "signal_type": "BREAKOUT",
              "signal_date": "2026-11-24"}
        kw.update(r)
        session.add(ScanResult(**kw))
    session.commit()
    return log


# ---------------------------------------------------------------------------
# 키움 조회 대역 (Task 8): 가짜 시계 + api-id 별 대본 응답 어댑터. 네트워크 호출 없음.
# ---------------------------------------------------------------------------
import json
from zoneinfo import ZoneInfo

import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

NY = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")


def ny_ts(y, m, d, hh, mm, ss=0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=NY).timestamp()


def kst_ts(y, m, d, hh, mm, ss=0) -> float:
    return datetime(y, m, d, hh, mm, ss, tzinfo=KST).timestamp()


class FakeClock:
    """호출 가능한 시계. sleep 은 실제로 자지 않고 시각만 앞당긴다."""

    def __init__(self, t: float):
        self.t = float(t)
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, sec: float) -> None:
        self.slept.append(sec)
        self.t += sec

    def advance(self, sec: float) -> None:
        self.t += sec

    def set(self, t: float) -> None:
        self.t = float(t)


def orderbook(ticker: str, exchange: str, price: str, at: float) -> dict:
    """usa20101 응답 본문. dt/bid_tm 은 at(뉴욕 시각) 기준 분 단위."""
    local = datetime.fromtimestamp(at, NY)
    return {"return_code": 0, "stk_cd": ticker, "stex_tp": exchange, "cur_prc": price,
            "dt": local.strftime("%Y%m%d"), "bid_tm": local.strftime("%H:%M")}


NOT_FOUND = {"return_code": 1903, "return_msg": "[1903:조회 결과가 없습니다]"}
INVALID_TOKEN = {"return_code": 8005, "return_msg": "Token이 유효하지 않습니다"}


class FakeKiwoomAdapter(BaseAdapter):
    """세션에 mount 하는 전송 대역. api-id 헤더(없으면 'oauth')로 대본을 고른다.

    script[api_id] 는 (status, body) · callable(body_json) -> (status, body) · 그 목록(차례로 소비) 중 하나.
    값이 "TIMEOUT" 이면 requests.Timeout 을 낸다. calls 에 (시각, api_id, 요청 본문)을 남긴다.
    """

    def __init__(self, clock, script: dict | None = None):
        super().__init__()
        self.clock = clock
        self.script = dict(script or {})
        self.calls: list[tuple[float, str, dict]] = []

    def mount(self, session):
        session.mount("https://", self)
        session.mount("http://", self)
        return self

    def _answer(self, api_id, body):
        entry = self.script.get(api_id)
        if isinstance(entry, list):
            entry = entry.pop(0) if len(entry) > 1 else entry[0]
        if entry is None:
            return 500, {"return_code": -1, "return_msg": f"대본 없음: {api_id}"}
        if callable(entry):
            entry = entry(body)
        return entry

    def send(self, request, **kwargs):
        api_id = request.headers.get("api-id") or "oauth"
        body = json.loads(request.body) if request.body else {}
        self.calls.append((self.clock(), api_id, body))
        answer = self._answer(api_id, body)
        if answer == "TIMEOUT":
            raise requests.exceptions.ReadTimeout("fake timeout")
        status, payload = answer
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode("utf-8")
        response.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        return response

    def close(self):
        pass

    def count(self, api_id: str | None = None) -> int:
        return sum(1 for _, a, _ in self.calls if api_id is None or a == api_id)
