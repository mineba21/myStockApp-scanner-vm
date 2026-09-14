"""Isolated stdio MCP check with a fake local portfolio API."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


TOKEN = "portfolio-protocol-test-" + "x" * 32


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.send_response(401); self.end_headers(); return
        if self.path == "/api/portfolio-read/overview":
            data = {"accounts": [{"account_profile": "account1"}], "source": "fake"}
        elif self.path == "/api/portfolio-read/holdings?account=account2":
            data = {"items": [{"account_profile": "account2", "ticker": "SPY"}], "source": "fake"}
        else:
            self.send_response(404); self.end_headers(); return
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


def rpc(process, request):
    process.stdin.write(json.dumps(request) + "\n"); process.stdin.flush()
    while True:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(process.stderr.read())
        result = json.loads(line)
        if result.get("id") == request.get("id"):
            return result


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as directory:
        token = Path(directory) / "token"
        token.write_text(TOKEN); token.chmod(0o600)
        env = {**os.environ, "PORTFOLIO_API_URL": f"http://127.0.0.1:{server.server_port}",
               "PORTFOLIO_READ_TOKEN_FILE": str(token)}
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("server.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        try:
            init = rpc(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"}}})
            assert "result" in init
            tools = rpc(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert {item["name"] for item in tools["result"]["tools"]} == {
                "portfolio_overview", "portfolio_holdings"
            }
            overview = rpc(process, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "portfolio_overview", "arguments": {}}})
            assert "account1" in json.dumps(overview)
            holdings = rpc(process, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "portfolio_holdings",
                                                "arguments": {"account": "account2"}}})
            assert "SPY" in json.dumps(holdings)
            bad = rpc(process, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                "params": {"name": "portfolio_holdings", "arguments": {"account": "bad"}}})
            assert bad["result"]["isError"] is True
        finally:
            process.terminate(); process.wait(timeout=5); server.shutdown()
    print("portfolio MCP protocol smoke: OK")


if __name__ == "__main__":
    main()
