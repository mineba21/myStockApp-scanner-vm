"""키움 토큰 캐시 — 만료 시각 반영과 8005 재시도.

운영 장애: 캐시가 토큰을 "발급 시각 + 23시간" 으로 붙들었는데, 키움은 재발급
요청에 **기존 토큰을 그대로** 돌려주므로 발급이 만료 시계를 초기화하지 않는다.
수명이 얼마 안 남은 토큰이 23시간짜리로 캐시되어, 죽은 뒤에도 계속 재사용되며
8005 를 반복했다(복구가 사실상 서비스 재시작뿐이었다).
"""
from datetime import datetime, timedelta, timezone

import pytest

from trading.kiwoom_readonly import KiwoomConfig, KiwoomError
from web import kiwoom_holdings as kh


KST = timezone(timedelta(hours=9))


def _expires_dt(after_seconds: float) -> str:
    """지금부터 N초 뒤를 키움 표기(KST, YYYYMMDDHHMMSS)로."""
    t = datetime.now(timezone.utc) + timedelta(seconds=after_seconds)
    return t.astimezone(KST).strftime("%Y%m%d%H%M%S")


@pytest.fixture(autouse=True)
def _clear_token_cache():
    kh._TOKEN_CACHE.clear()
    yield
    kh._TOKEN_CACHE.clear()


class _Client:
    """issue_token 호출 횟수를 세는 가짜 클라이언트."""
    def __init__(self, expires_dt, token="TOK"):
        self.expires_dt = expires_dt
        self.token = token
        self.issued = 0

    def issue_token(self):
        self.issued += 1
        return {"token": self.token, "expires_dt": self.expires_dt}


CFG = KiwoomConfig("key", "secret", "real")


class TestTokenTtl:
    def test_expires_dt_is_read_as_kst(self):
        """UTC 로 읽으면 9시간을 더 얹어 만료된 토큰을 살아있다고 판단한다."""
        ttl = kh._token_ttl_seconds({"expires_dt": _expires_dt(24 * 3600)})
        # 24시간 - 10분 여유
        assert 23.7 * 3600 < ttl < 24 * 3600

    def test_ttl_shrinks_with_remaining_life(self):
        """재발급이 시계를 초기화하지 않으므로 잔여 수명이 그대로 TTL 이 된다."""
        ttl = kh._token_ttl_seconds({"expires_dt": _expires_dt(2 * 3600)})
        assert 1.7 * 3600 < ttl < 2 * 3600

    def test_already_expired_gives_zero(self):
        assert kh._token_ttl_seconds({"expires_dt": _expires_dt(-60)}) == 0.0

    def test_within_safety_margin_gives_zero(self):
        """만료 5분 전 토큰은 재사용하지 않는다."""
        assert kh._token_ttl_seconds({"expires_dt": _expires_dt(300)}) == 0.0

    @pytest.mark.parametrize("raw", [None, "", "not-a-date", "2026090521380", "20261301000000"])
    def test_unparseable_falls_back_to_short_ttl(self, raw):
        """형식이 바뀌어도 조용히 23시간 캐시로 되돌아가지 않는다."""
        assert kh._token_ttl_seconds({"expires_dt": raw}) == kh._TOKEN_FALLBACK_TTL_SECONDS


class TestTokenCache:
    def test_reuses_token_within_validity(self):
        c = _Client(_expires_dt(20 * 3600))
        assert kh._get_token("account1", CFG, c) == "TOK"
        assert kh._get_token("account1", CFG, c) == "TOK"
        assert c.issued == 1

    def test_short_lived_token_is_not_cached(self):
        """수명이 여유분보다 짧으면 캐시하지 않고 매번 다시 받는다.

        예전 코드는 이런 토큰도 23시간 붙들어 8005 를 반복했다.
        """
        c = _Client(_expires_dt(120))
        kh._get_token("account1", CFG, c)
        kh._get_token("account1", CFG, c)
        assert c.issued == 2
        assert kh._token_key("account1", CFG) not in kh._TOKEN_CACHE

    def test_force_bypasses_cache(self):
        c = _Client(_expires_dt(20 * 3600))
        kh._get_token("account1", CFG, c)
        kh._get_token("account1", CFG, c, force=True)
        assert c.issued == 2

    def test_cache_is_per_profile(self):
        c1, c2 = _Client(_expires_dt(20 * 3600)), _Client(_expires_dt(20 * 3600))
        kh._get_token("account1", CFG, c1)
        kh._get_token("account2", CFG, c2)
        assert c1.issued == 1 and c2.issued == 1


class TestInvalidTokenDetection:
    def test_recognises_production_error_text(self):
        exc = KiwoomError("계좌 잔고 조회 실패 (3): 인증에 실패했습니다[8005:Token이 유효하지 않습니다]")
        assert kh._is_invalid_token_error(exc)

    def test_other_errors_are_not_token_errors(self):
        for msg in ("미국주식 원장잔고 조회 실패 (20): [2000](508540:해외증권주문 가능 계좌가 아닙니다.)",
                    "계좌 잔고 조회 실패 (1): 조회내역이 없습니다"):
            assert not kh._is_invalid_token_error(KiwoomError(msg))

    def test_overseas_helper_does_not_swallow_token_errors(self):
        """토큰 오류를 빈 결과로 덮으면 재시도가 발동하지 못한다."""
        def boom():
            raise KiwoomError("실패 (20): 인증에 실패했습니다[8005:Token이 유효하지 않습니다]")
        with pytest.raises(KiwoomError):
            kh._safe_overseas_call(boom)

    def test_overseas_helper_still_swallows_permission_errors(self):
        def boom():
            raise KiwoomError("미국주식 원장잔고 조회 실패 (20): [2000](508540:해외증권주문 가능 계좌가 아닙니다.)")
        assert kh._safe_overseas_call(boom) == kh._empty_report()


# ── 통합: 죽은 캐시 토큰에서 자동 복구 ─────────────────────────────

class _StatefulClient:
    """서버에서 이미 죽은 토큰을 캐시가 들고 있는 상황을 재현한다."""

    DEAD, LIVE = "DEAD-TOKEN", "LIVE-TOKEN"

    def __init__(self):
        self.issued = 0

    def issue_token(self):
        self.issued += 1
        # 첫 발급은 죽은 토큰, 재발급부터 살아있는 토큰
        token = self.DEAD if self.issued == 1 else self.LIVE
        return {"token": token, "expires_dt": _expires_dt(20 * 3600)}

    def _guard(self, token):
        if token == self.DEAD:
            raise KiwoomError(
                "계좌 잔고 조회 실패 (3): 인증에 실패했습니다[8005:Token이 유효하지 않습니다]")

    def get_account_balance(self, token, **kw):
        self._guard(token)
        return {"summary": {}, "holdings": []}

    def get_overseas_account_balance(self, token, **kw):
        self._guard(token)
        return {"summary": {}, "holdings": []}

    def get_domestic_deposit(self, token, **kw):
        self._guard(token); return {"summary": {}, "items": []}

    def get_overseas_deposit(self, token, **kw):
        self._guard(token); return {"summary": {}, "items": []}

    def get_overseas_currency_valuation(self, token, **kw):
        self._guard(token); return {"summary": {}, "items": []}

    def get_overseas_valuation(self, token, **kw):
        self._guard(token); return {"summary": {}, "items": []}


class TestRecoveryFromDeadCachedToken:
    def test_retries_once_with_fresh_token(self, monkeypatch):
        """8005 를 만나면 강제 재발급 후 1회 재시도해 스스로 복구한다.

        예전에는 캐시가 만료될 때까지(최대 23시간) 계속 실패했고, 실질적인
        복구 수단이 서비스 재시작뿐이었다.
        """
        client = _StatefulClient()
        monkeypatch.setattr(kh, "load_profile_configs", lambda: {"account1": CFG})
        kh._BALANCE_CACHE.update(expires_at=0.0, rows=[], accounts=[])

        result = kh._load_kiwoom_portfolio(force=True, client_factory=lambda cfg: client)

        assert client.issued == 2, "강제 재발급이 일어나지 않았다"
        assert isinstance(result["accounts"], list) and len(result["accounts"]) == 1
        # 살아있는 토큰이 캐시에 남아야 다음 호출이 재시도 없이 지나간다
        assert kh._TOKEN_CACHE[kh._token_key("account1", CFG)][1] == _StatefulClient.LIVE

    def test_non_token_errors_still_propagate(self, monkeypatch):
        """토큰과 무관한 오류까지 재시도로 삼키지 않는다."""
        class Broken(_StatefulClient):
            def get_account_balance(self, token, **kw):
                raise KiwoomError("계좌 잔고 조회 실패 (9): 서버 점검 중")

        monkeypatch.setattr(kh, "load_profile_configs", lambda: {"account1": CFG})
        kh._BALANCE_CACHE.update(expires_at=0.0, rows=[], accounts=[])
        with pytest.raises(KiwoomError, match="서버 점검"):
            kh._load_kiwoom_portfolio(force=True, client_factory=lambda cfg: Broken())
