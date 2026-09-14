"""Install the isolated portfolio-read MCP after its TLS certificate exists.

Run as root with the scanner venv Python after the Git deployment. This creates
a dedicated API credential but never prints it or calls the portfolio API.
"""

import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import time


repo = Path("/home/ubuntu/apps/myStockApp/stock-scanner")
service_name = "mystockapp-portfolio-mcp"
try:
    account = pwd.getpwnam(service_name)
except KeyError:
    subprocess.run([
        "useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin",
        service_name,
    ], check=True)
    account = pwd.getpwnam(service_name)

root = Path("/opt/mystockapp-portfolio-mcp")
root.mkdir(mode=0o755, exist_ok=True)
for source, target in (
    (repo / "integrations/portfolio_mcp/server.py", root / "server.py"),
    (repo / "integrations/scanner_mcp/remote.py", root / "remote.py"),
    (repo / "integrations/portfolio_mcp/requirements.txt", root / "requirements.txt"),
):
    shutil.copy2(source, target)
    target.chmod(0o644)

venv = root / ".venv"
if not (venv / "bin/python").exists():
    subprocess.run(["/usr/bin/python3", "-m", "venv", "--copies", str(venv)], check=True)
subprocess.run([str(venv / "bin/pip"), "install", "-q", "-r", str(root / "requirements.txt")], check=True)

dotenv = repo / ".env"
content = dotenv.read_text()
match = re.search(r"(?m)^PORTFOLIO_READ_TOKEN=(.*)$", content)
if match:
    read_token = match.group(1).strip().strip('"').strip("'")
    if len(read_token) < 32:
        raise RuntimeError("Existing PORTFOLIO_READ_TOKEN is invalid")
else:
    read_token = secrets.token_urlsafe(32)
    dotenv.write_text(content.rstrip("\n") + "\nPORTFOLIO_READ_TOKEN=" + read_token + "\n")
    dotenv.chmod(0o600)

scanner_config = json.loads(Path("/etc/mystockapp-mcp/config.json").read_text())
owner_hash = scanner_config.get("owner_hash", "")
if len(owner_hash) != 64:
    raise RuntimeError("Existing owner approval hash is unavailable")

state = Path("/var/lib/mystockapp-portfolio-mcp")
state.mkdir(mode=0o700, exist_ok=True)
os.chown(state, account.pw_uid, account.pw_gid)
conf = Path("/etc/mystockapp-portfolio-mcp")
conf.mkdir(mode=0o750, exist_ok=True)
os.chown(conf, 0, account.pw_gid)
config = {
    "owner_hash": owner_hash,
    "origin": "https://portfolio.161-33-212-161.sslip.io",
    "oauth_db": str(state / "oauth.sqlite"),
    "read_token": read_token,
    "scope": "portfolio:read",
    "consent_title": "계좌 현황 조회 연결",
    "consent_description": "계좌 요약과 보유종목 조회만 허용합니다. 주문·환전 권한은 없습니다.",
    "port": 8003,
}
config_path = conf / "config.json"
config_path.write_text(json.dumps(config))
os.chown(config_path, 0, account.pw_gid)
config_path.chmod(0o640)

shutil.copy2(
    repo / "deploy/mystockapp-portfolio-mcp.service",
    "/etc/systemd/system/mystockapp-portfolio-mcp.service",
)
shutil.copy2(
    repo / "deploy/portfolio-mcp.nginx.conf",
    "/etc/nginx/conf.d/portfolio-mcp.conf",
)
subprocess.run(["systemctl", "daemon-reload"], check=True)
subprocess.run(["systemctl", "enable", "--now", "mystockapp-portfolio-mcp.service"], check=True)
subprocess.run(["systemctl", "restart", "mystockapp-portfolio-mcp.service"], check=True)
subprocess.run(["nginx", "-t"], check=True)
subprocess.run(["systemctl", "reload", "nginx"], check=True)
subprocess.run(["systemctl", "restart", "mystockapp-scanner.service"], check=True)

for attempt in range(15):
    try:
        request = __import__("urllib.request").request.Request(
            "http://127.0.0.1:8003/.well-known/oauth-authorization-server",
            headers={"Host": "portfolio.161-33-212-161.sslip.io"},
        )
        with __import__("urllib.request").request.urlopen(request, timeout=2) as response:
            if response.status == 200:
                break
    except Exception:
        if attempt == 14:
            raise RuntimeError("Portfolio MCP readiness failed")
        time.sleep(1)

subprocess.run(["systemctl", "is-active", "mystockapp-portfolio-mcp.service"], check=True)
subprocess.run(["systemctl", "is-active", "mystockapp-scanner.service"], check=True)
print("Portfolio MCP installed with an isolated read credential.")
