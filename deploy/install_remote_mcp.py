"""Run as root with scanner venv Python on the existing VM after Git deployment.
Does not read the application database or call brokerage APIs.
"""
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import time
from dotenv import dotenv_values

repo=Path('/home/ubuntu/apps/myStockApp/stock-scanner')
name='mystockapp-mcp'
try: account=pwd.getpwnam(name)
except KeyError:
    subprocess.run(['useradd','--system','--no-create-home','--shell','/usr/sbin/nologin',name],check=True)
    account=pwd.getpwnam(name)
root=Path('/opt/mystockapp-mcp'); root.mkdir(mode=0o755,exist_ok=True)
for filename in ('server.py','remote.py','requirements.txt'):
    shutil.copy2(repo/'integrations/scanner_mcp'/filename,root/filename)
    (root/filename).chmod(0o644)
venv=root/'.venv'
if (venv/'pyvenv.cfg').exists() and '/home/' in (venv/'pyvenv.cfg').read_text():
    venv.rename(root/('.venv.before-isolation-'+str(int(time.time()))))
if not (venv/'bin/python').exists():
    subprocess.run(['/usr/bin/python3','-m','venv','--copies',str(venv)],check=True)
subprocess.run([str(root/'.venv/bin/pip'),'install','-q','-r',str(root/'requirements.txt')],check=True)
state=Path('/var/lib/mystockapp-mcp'); state.mkdir(mode=0o700,exist_ok=True); os.chown(state,account.pw_uid,account.pw_gid)
conf=Path('/etc/mystockapp-mcp'); conf.mkdir(mode=0o750,exist_ok=True); os.chown(conf,0,account.pw_gid)
cfg=conf/'config.json'
if cfg.exists(): config=json.loads(cfg.read_text())
else:
    password=secrets.token_urlsafe(32)
    config={'owner_hash':hashlib.sha256(password.encode()).hexdigest()}
    # One-time handoff file; operator retrieves privately and removes after delivery.
    p=conf/'owner-password.pending'; p.write_text(password); p.chmod(0o600)
read_key=dotenv_values(repo/'.env').get('SCANNER_READ_TOKEN')
if not read_key or len(read_key)<32: raise RuntimeError('Scanner read credential is not configured')
config.update(origin='https://161.33.212.161',oauth_db=str(state/'oauth.sqlite'),scanner_read_token=read_key)
cfg.write_text(json.dumps(config)); os.chown(cfg,0,account.pw_gid); cfg.chmod(0o640)
domain_cfg=conf/'domain.json'
domain_config={
    'owner_hash':config['owner_hash'],
    'origin':'https://161-33-212-161.sslip.io',
    'oauth_db':str(state/'oauth-domain.sqlite'),
    'scanner_read_token':read_key,
    'port':8002,
}
domain_cfg.write_text(json.dumps(domain_config)); os.chown(domain_cfg,0,account.pw_gid); domain_cfg.chmod(0o640)
shutil.copy2(repo/'deploy/mystockapp-mcp.service','/etc/systemd/system/mystockapp-mcp.service')
shutil.copy2(repo/'deploy/mystockapp-mcp-domain.service','/etc/systemd/system/mystockapp-mcp-domain.service')
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','mystockapp-mcp.service'],check=True)
subprocess.run(['systemctl','restart','mystockapp-mcp.service'],check=True)
subprocess.run(['systemctl','enable','--now','mystockapp-mcp-domain.service'],check=True)
subprocess.run(['systemctl','restart','mystockapp-mcp-domain.service'],check=True)
nginx=Path('/etc/nginx/sites-available/default'); original=nginx.read_text()
backup=Path('/etc/nginx/sites-available/default.before-mcp-'+str(int(time.time())))
backup.write_text(original)
snippet=Path('/etc/nginx/snippets/scanner-mcp.conf')
shutil.copy2(repo/'deploy/scanner-mcp.nginx.conf',snippet)
domain_snippet=Path('/etc/nginx/snippets/scanner-mcp-domain.conf')
shutil.copy2(repo/'deploy/scanner-mcp-domain.nginx.conf',domain_snippet)
Path('/etc/nginx/conf.d/scanner-mcp-rate.conf').write_text('limit_req_zone $binary_remote_addr zone=scanner_mcp:10m rate=5r/s;\n')
marker='    server_name 161.33.212.161;'
if original.count(marker)!=1: raise RuntimeError('Unexpected nginx server layout')
include='    include /etc/nginx/snippets/scanner-mcp.conf;'
if include not in original: nginx.write_text(original.replace(marker,marker+'\n'+include))
updated=nginx.read_text()
domain_marker='    server_name 161-33-212-161.sslip.io;'
domain_include='    include /etc/nginx/snippets/scanner-mcp-domain.conf;'
if updated.count(domain_marker)!=1: raise RuntimeError('Unexpected domain nginx server layout')
if domain_include not in updated: nginx.write_text(updated.replace(domain_marker,domain_marker+'\n'+domain_include))
result=subprocess.run(['nginx','-t'])
if result.returncode:
    nginx.write_text(original)
    raise RuntimeError('nginx validation failed; original site restored')
subprocess.run(['systemctl','reload','nginx'],check=True)
import urllib.request
for attempt in range(15):
    try:
        req=urllib.request.Request('http://127.0.0.1:8001/.well-known/oauth-authorization-server',headers={'Host':'161.33.212.161'})
        with urllib.request.urlopen(req,timeout=2) as response:
            if response.status==200: break
    except Exception:
        if attempt==14: raise RuntimeError('MCP readiness failed; inspect service log')
        time.sleep(1)
subprocess.run(['systemctl','is-active','mystockapp-mcp.service'],check=True)
subprocess.run(['systemctl','is-active','mystockapp-mcp-domain.service'],check=True)
print('Remote MCP service installed. Owner approval password is in the private one-time handoff file.')
