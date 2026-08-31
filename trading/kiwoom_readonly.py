"""키움 REST API의 OAuth 인증과 국내·미국주식 잔고 조회 전용 클라이언트.

주문 API를 노출하지 않고, 고정된 두 엔드포인트만 호출한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv


TOKEN_PATH = "/oauth2/token"
BALANCE_PATH = "/api/dostk/acnt"
BALANCE_API_ID = "kt00018"
DOMESTIC_DEPOSIT_API_ID = "kt00001"
OVERSEAS_BALANCE_PATH = "/api/us/acnt"
OVERSEAS_BALANCE_API_ID = "ust21070"
OVERSEAS_DEPOSIT_API_ID = "ust21110"
OVERSEAS_CURRENCY_API_ID = "ust21120"
OVERSEAS_VALUATION_API_ID = "ust21121"
REAL_BASE_URL = "https://api.kiwoom.com"
MOCK_BASE_URL = "https://mockapi.kiwoom.com"
DEFAULT_PROFILES_FILE = Path.home() / ".config" / "mystockapp" / "kiwoom_profiles.json"


class KiwoomError(RuntimeError):
    """키움 REST API 호출 또는 설정 오류."""


@dataclass(frozen=True)
class KiwoomConfig:
    app_key: str
    app_secret: str
    mode: str = "mock"
    timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "KiwoomConfig":
        load_dotenv()
        mode = os.getenv("KIWOOM_MODE", "mock").strip().lower()
        if mode not in {"real", "mock"}:
            raise KiwoomError("KIWOOM_MODE는 real 또는 mock이어야 합니다.")

        app_key = os.getenv("KIWOOM_APP_KEY", "").strip()
        app_secret = os.getenv("KIWOOM_APP_SECRET", "").strip()
        if not app_key or not app_secret:
            raise KiwoomError(
                "KIWOOM_APP_KEY와 KIWOOM_APP_SECRET 환경변수가 필요합니다."
            )

        try:
            timeout = float(os.getenv("KIWOOM_TIMEOUT_SECONDS", "15"))
        except ValueError as exc:
            raise KiwoomError("KIWOOM_TIMEOUT_SECONDS는 숫자여야 합니다.") from exc
        if timeout <= 0:
            raise KiwoomError("KIWOOM_TIMEOUT_SECONDS는 0보다 커야 합니다.")

        return cls(app_key, app_secret, mode, timeout)

    @classmethod
    def from_profile(
        cls,
        profile: str,
        profiles_file: str | os.PathLike[str] | None = None,
    ) -> "KiwoomConfig":
        profiles = load_profile_configs(profiles_file)
        try:
            return profiles[profile]
        except KeyError as exc:
            available = ", ".join(profiles)
            raise KiwoomError(
                f"알 수 없는 프로필: {profile} (사용 가능: {available})"
            ) from exc

    @property
    def base_url(self) -> str:
        return REAL_BASE_URL if self.mode == "real" else MOCK_BASE_URL


def load_profile_configs(
    profiles_file: str | os.PathLike[str] | None = None,
) -> dict[str, KiwoomConfig]:
    """권한이 제한된 JSON 파일에서 다계좌 설정을 읽는다."""
    load_dotenv()
    configured_path = profiles_file or os.getenv("KIWOOM_PROFILES_FILE")
    path = Path(configured_path).expanduser() if configured_path else DEFAULT_PROFILES_FILE
    if not path.is_file():
        raise KiwoomError(f"키움 프로필 파일을 찾을 수 없습니다: {path}")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise KiwoomError(f"키움 프로필 파일 권한을 600으로 제한하세요: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KiwoomError(f"키움 프로필 파일을 읽을 수 없습니다: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("profiles"), dict):
        raise KiwoomError("키움 프로필 파일 형식이 올바르지 않습니다.")

    mode = str(payload.get("mode", "mock")).strip().lower()
    if mode not in {"real", "mock"}:
        raise KiwoomError("프로필 파일의 mode는 real 또는 mock이어야 합니다.")
    try:
        timeout = float(os.getenv("KIWOOM_TIMEOUT_SECONDS", "15"))
    except ValueError as exc:
        raise KiwoomError("KIWOOM_TIMEOUT_SECONDS는 숫자여야 합니다.") from exc

    result: dict[str, KiwoomConfig] = {}
    for name, values in payload["profiles"].items():
        if not isinstance(name, str) or not isinstance(values, dict):
            raise KiwoomError("키움 프로필 항목 형식이 올바르지 않습니다.")
        app_key = str(values.get("app_key", "")).strip()
        app_secret = str(values.get("app_secret", "")).strip()
        if not app_key or not app_secret:
            raise KiwoomError(f"{name} 프로필의 App Key/Secret이 비어 있습니다.")
        result[name] = KiwoomConfig(app_key, app_secret, mode, timeout)
    if not result:
        raise KiwoomError("등록된 키움 계좌 프로필이 없습니다.")
    return result


class KiwoomReadOnlyClient:
    """OAuth 토큰 발급 및 국내·미국주식 잔고 조회만 지원한다."""

    def __init__(
        self,
        config: KiwoomConfig,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    def issue_token(self) -> dict[str, Any]:
        response = self.session.post(
            self.config.base_url + TOKEN_PATH,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            json={
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "secretkey": self.config.app_secret,
            },
            timeout=self.config.timeout_seconds,
        )
        data = self._parse_response(response, "OAuth 토큰 발급")
        if not data.get("token"):
            raise KiwoomError("OAuth 응답에 접근 토큰이 없습니다.")
        return data

    def get_account_balance(
        self,
        token: str,
        *,
        query_type: str = "1",
        exchange: str = "KRX",
        max_pages: int = 10,
    ) -> dict[str, Any]:
        if query_type not in {"1", "2"}:
            raise KiwoomError("query_type은 1(합산) 또는 2(개별)여야 합니다.")
        if exchange not in {"KRX", "NXT"}:
            raise KiwoomError("exchange는 KRX 또는 NXT여야 합니다.")
        if max_pages < 1:
            raise KiwoomError("max_pages는 1 이상이어야 합니다.")

        holdings: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        pages = 0
        cont_yn: str | None = None
        next_key: str | None = None

        while pages < max_pages:
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": BALANCE_API_ID,
            }
            if cont_yn == "Y" and next_key:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            response = self.session.post(
                self.config.base_url + BALANCE_PATH,
                headers=headers,
                json={"qry_tp": query_type, "dmst_stex_tp": exchange},
                timeout=self.config.timeout_seconds,
            )
            data = self._parse_response(response, "계좌 잔고 조회")
            pages += 1

            if pages == 1:
                summary = {
                    key: value
                    for key, value in data.items()
                    if key not in {"acnt_evlt_remn_indv_tot", "return_code", "return_msg"}
                }
            page_holdings = data.get("acnt_evlt_remn_indv_tot", [])
            if isinstance(page_holdings, list):
                holdings.extend(item for item in page_holdings if isinstance(item, dict))

            cont_yn = response.headers.get("cont-yn")
            next_key = response.headers.get("next-key")
            if cont_yn != "Y" or not next_key:
                break
            time.sleep(0.2)

        return {
            "mode": self.config.mode,
            "api_id": BALANCE_API_ID,
            "pages": pages,
            "summary": summary,
            "holdings": holdings,
        }

    def get_overseas_account_balance(
        self,
        token: str,
        *,
        exchange: str = "",
        ticker: str = "",
        max_pages: int = 10,
    ) -> dict[str, Any]:
        """``ust21070`` 미국주식 원장잔고를 조회한다.

        exchange를 비우면 나스닥·뉴욕·아멕스 잔고를 한 번에 조회한다.
        """
        exchange = exchange.strip().upper()
        ticker = ticker.strip().upper()
        if exchange not in {"", "ND", "NY", "NA"}:
            raise KiwoomError("exchange는 빈 값, ND, NY 또는 NA여야 합니다.")
        if max_pages < 1:
            raise KiwoomError("max_pages는 1 이상이어야 합니다.")

        holdings: list[dict[str, Any]] = []
        summary: dict[str, Any] = {}
        pages = 0
        cont_yn: str | None = None
        next_key: str | None = None

        while pages < max_pages:
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": OVERSEAS_BALANCE_API_ID,
            }
            if cont_yn == "Y" and next_key:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key

            response = self.session.post(
                self.config.base_url + OVERSEAS_BALANCE_PATH,
                headers=headers,
                json={"stex_tp": exchange, "stk_cd": ticker},
                timeout=self.config.timeout_seconds,
            )
            data = self._parse_response(response, "미국주식 원장잔고 조회")
            pages += 1

            if pages == 1:
                summary = {
                    key: value
                    for key, value in data.items()
                    if key not in {"result_list", "return_code", "return_msg"}
                }
            page_holdings = data.get("result_list", [])
            if isinstance(page_holdings, list):
                holdings.extend(item for item in page_holdings if isinstance(item, dict))

            cont_yn = response.headers.get("cont-yn")
            next_key = response.headers.get("next-key")
            if cont_yn != "Y" or not next_key:
                break
            time.sleep(0.2)

        return {
            "mode": self.config.mode,
            "api_id": OVERSEAS_BALANCE_API_ID,
            "pages": pages,
            "summary": summary,
            "holdings": holdings,
        }

    def get_domestic_deposit(
        self,
        token: str,
        *,
        query_type: str = "3",
    ) -> dict[str, Any]:
        """``kt00001`` 국내 예수금·주문가능·출금가능 금액을 조회한다."""
        if query_type not in {"2", "3"}:
            raise KiwoomError("query_type은 2(일반) 또는 3(추정)이어야 합니다.")
        return self._post_read_only_report(
            token,
            path=BALANCE_PATH,
            api_id=DOMESTIC_DEPOSIT_API_ID,
            payload={"qry_tp": query_type},
            operation="국내 예수금 조회",
        )

    def get_overseas_deposit(self, token: str) -> dict[str, Any]:
        """``ust21110`` 원화 및 통화별 외화 예수금을 조회한다."""
        return self._post_read_only_report(
            token,
            path=OVERSEAS_BALANCE_PATH,
            api_id=OVERSEAS_DEPOSIT_API_ID,
            payload={},
            operation="해외주식 예수금 조회",
            list_key="result_list",
        )

    def get_overseas_currency_valuation(self, token: str) -> dict[str, Any]:
        """``ust21120`` 통화별 예수금·증권평가액과 적용환율을 조회한다."""
        return self._post_read_only_report(
            token,
            path=OVERSEAS_BALANCE_PATH,
            api_id=OVERSEAS_CURRENCY_API_ID,
            payload={"cmsn_incl_tp": "1", "exrt_tp": "1"},
            operation="해외 통화별 자산 조회",
            list_key="result_list",
        )

    def get_overseas_valuation(self, token: str) -> dict[str, Any]:
        """``ust21121`` 통화별 해외증권 평가손익을 조회한다."""
        return self._post_read_only_report(
            token,
            path=OVERSEAS_BALANCE_PATH,
            api_id=OVERSEAS_VALUATION_API_ID,
            payload={"cmsn_incl_tp": "1", "exrt_tp": "1"},
            operation="해외증권 평가손익 조회",
            list_key="result_list",
        )

    def _post_read_only_report(
        self,
        token: str,
        *,
        path: str,
        api_id: str,
        payload: dict[str, str],
        operation: str,
        list_key: str | None = None,
        max_pages: int = 10,
    ) -> dict[str, Any]:
        """주문 기능과 분리된 조회 TR을 연속조회까지 안전하게 호출한다."""
        summary: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        cont_yn: str | None = None
        next_key: str | None = None
        pages = 0
        while pages < max_pages:
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": api_id,
            }
            if cont_yn == "Y" and next_key:
                headers["cont-yn"] = cont_yn
                headers["next-key"] = next_key
            response = self.session.post(
                self.config.base_url + path,
                headers=headers,
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            data = self._parse_response(response, operation)
            pages += 1
            if pages == 1:
                summary = {
                    key: value
                    for key, value in data.items()
                    if key not in {list_key, "return_code", "return_msg"}
                }
            if list_key and isinstance(data.get(list_key), list):
                items.extend(item for item in data[list_key] if isinstance(item, dict))
            cont_yn = response.headers.get("cont-yn")
            next_key = response.headers.get("next-key")
            if cont_yn != "Y" or not next_key:
                break
            time.sleep(0.2)
        return {
            "mode": self.config.mode,
            "api_id": api_id,
            "pages": pages,
            "summary": summary,
            "items": items,
        }

    @staticmethod
    def _parse_response(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise KiwoomError(f"{operation} HTTP 오류: {response.status_code}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise KiwoomError(f"{operation} 응답이 JSON 형식이 아닙니다.") from exc
        if not isinstance(data, dict):
            raise KiwoomError(f"{operation} 응답 형식이 올바르지 않습니다.")
        return_code = data.get("return_code")
        if return_code not in (None, 0, "0"):
            message = data.get("return_msg") or "메시지 없음"
            raise KiwoomError(f"{operation} 실패 ({return_code}): {message}")
        return data


def _masked_token(token: str) -> str:
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:6]}...{token[-4:]}"


def _query_profile(
    name: str,
    config: KiwoomConfig,
    *,
    token_only: bool,
    exchange: str,
    query_type: str,
    max_pages: int,
) -> dict[str, Any]:
    client = KiwoomReadOnlyClient(config)
    token_data = client.issue_token()
    if token_only:
        return {
            "profile": name,
            "mode": config.mode,
            "token": _masked_token(str(token_data["token"])),
            "token_type": token_data.get("token_type"),
            "expires_dt": token_data.get("expires_dt"),
        }
    result = client.get_account_balance(
        str(token_data["token"]),
        query_type=query_type,
        exchange=exchange,
        max_pages=max_pages,
    )
    return {"profile": name, **result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="키움 REST API 조회 전용 잔고 확인")
    parser.add_argument(
        "--token-only",
        action="store_true",
        help="토큰 발급 여부와 만료시각만 확인하고 잔고는 조회하지 않음",
    )
    parser.add_argument("--exchange", choices=("KRX", "NXT"), default="KRX")
    parser.add_argument("--query-type", choices=("1", "2"), default="1")
    parser.add_argument("--max-pages", type=int, default=10)
    profile_group = parser.add_mutually_exclusive_group()
    profile_group.add_argument("--profile", help="프로필 파일의 계좌 별칭")
    profile_group.add_argument(
        "--all-profiles",
        action="store_true",
        help="프로필 파일에 등록된 모든 계좌를 순차 조회",
    )
    parser.add_argument("--profiles-file", help="다계좌 프로필 JSON 경로")
    args = parser.parse_args(argv)

    try:
        if args.all_profiles:
            configs = load_profile_configs(args.profiles_file)
            output = {
                "accounts": [
                    _query_profile(
                        name,
                        config,
                        token_only=args.token_only,
                        exchange=args.exchange,
                        query_type=args.query_type,
                        max_pages=args.max_pages,
                    )
                    for name, config in configs.items()
                ]
            }
        else:
            config = (
                KiwoomConfig.from_profile(args.profile, args.profiles_file)
                if args.profile
                else KiwoomConfig.from_env()
            )
            output = _query_profile(
                args.profile or "default",
                config,
                token_only=args.token_only,
                exchange=args.exchange,
                query_type=args.query_type,
                max_pages=args.max_pages,
            )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (KiwoomError, requests.RequestException) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
