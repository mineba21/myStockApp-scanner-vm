"""Isolated portfolio OAuth/MCP integration check; no real account access."""

import base64
import hashlib
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient


HERE = Path(__file__).resolve().parent
server_spec = importlib.util.spec_from_file_location("server", HERE / "server.py")
portfolio_server = importlib.util.module_from_spec(server_spec)
sys.modules["server"] = portfolio_server
server_spec.loader.exec_module(portfolio_server)
remote_spec = importlib.util.spec_from_file_location(
    "portfolio_oauth_remote", HERE.parent / "scanner_mcp" / "remote.py"
)
remote = importlib.util.module_from_spec(remote_spec)
remote_spec.loader.exec_module(remote)


class Reader:
    def overview(self):
        return {"accounts": [{"account_profile": "account1"}], "source": "fake"}

    def holdings(self, account="ALL"):
        return {"account": account, "items": [{"ticker": "SPY"}], "source": "fake"}


class PortfolioOAuthTest(unittest.TestCase):
    def test_portfolio_scope_and_exactly_two_tools(self):
        origin = "https://portfolio.example"
        with tempfile.TemporaryDirectory() as directory:
            provider = remote.OwnerProvider(
                directory + "/oauth.sqlite", origin, remote.digest("owner-test-password"),
                scope="portfolio:read", consent_title="계좌 현황 조회 연결",
                consent_description="계좌 요약과 보유종목 조회만 허용합니다. 주문 권한은 없습니다.",
            )
            with TestClient(remote.build_app(Reader(), provider), base_url=origin,
                            follow_redirects=False) as client:
                registration = client.post("/register", json={
                    "client_name": "Test web client",
                    "redirect_uris": ["https://client.example/callback"],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": "portfolio:read",
                })
                self.assertEqual(registration.status_code, 201, registration.text)
                client_id = registration.json()["client_id"]
                verifier = "v" * 64
                challenge = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()
                ).decode().rstrip("=")
                params = {
                    "response_type": "code", "client_id": client_id,
                    "redirect_uri": "https://client.example/callback",
                    "code_challenge": challenge, "code_challenge_method": "S256",
                    "scope": "portfolio:read", "state": "state123",
                    "resource": origin + "/mcp",
                }
                authorization = client.get("/authorize", params=params)
                page = client.get(authorization.headers["location"])
                self.assertIn("계좌 현황 조회 연결", page.text)
                fields = {key: re.search(
                    'name="' + key + '" value="([^"]+)"', page.text
                ).group(1) for key in ("flow", "csrf")}
                approved = client.post("/consent", data={
                    **fields, "password": "owner-test-password", "decision": "approve"
                }, headers={"Origin": origin})
                code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
                token = client.post("/token", data={
                    "grant_type": "authorization_code", "client_id": client_id,
                    "code": code, "code_verifier": verifier,
                    "redirect_uri": "https://client.example/callback",
                    "resource": origin + "/mcp",
                })
                self.assertEqual(token.status_code, 200, token.text)
                headers = {"Authorization": "Bearer " + token.json()["access_token"],
                           "Accept": "application/json, text/event-stream"}
                listed = client.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/list"
                })
                self.assertEqual(
                    {tool["name"] for tool in listed.json()["result"]["tools"]},
                    {"portfolio_overview", "portfolio_holdings"},
                )
                called = client.post("/mcp", headers=headers, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "portfolio_holdings",
                               "arguments": {"account": "account2"}},
                })
                self.assertIn("SPY", called.text)
                metadata = client.get("/.well-known/oauth-authorization-server").json()
                self.assertEqual(metadata["scopes_supported"], ["portfolio:read"])


if __name__ == "__main__":
    unittest.main()
