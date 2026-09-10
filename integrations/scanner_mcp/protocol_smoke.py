"""Run with the isolated MCP venv; local fake HTTP server only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN = 'protocol-test-' + 'x' * 40
calls = []

class Handler(BaseHTTPRequestHandler):
    redirect = False
    def log_message(self, *args): pass
    def do_GET(self):
        assert self.headers.get('Authorization') == 'Bearer ' + TOKEN
        calls.append(self.path)
        assert self.path.startswith('/api/scanner-read/')
        if self.redirect:
            self.send_response(302)
            self.send_header('Location', '/api/accounts')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'source':'fake_scanner','path':self.path}).encode())

async def check(port, token_path):
    params=StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name('server.py'))],
        env={'SCANNER_API_URL':f'http://127.0.0.1:{port}','SCANNER_READ_TOKEN_FILE':str(token_path)})
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            tools=(await session.list_tools()).tools
            assert {t.name for t in tools}=={'scanner_status','scanner_signals','scanner_signal'}
            assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)
            for name,args in [('scanner_status',{}),('scanner_signals',{'market':'US','limit':2}),
                              ('scanner_signal',{'result_id':12})]:
                result=await session.call_tool(name,args)
                assert not result.isError, result
            count=len(calls)
            for name,args in [('place_order',{}),('scanner_signal',{'result_id':-1}),
                              ('scanner_signals',{'limit':101}),('scanner_signals',{'market':'../accounts'})]:
                assert (await session.call_tool(name,args)).isError
            assert len(calls)==count==3

if __name__=='__main__':
    from server import ScannerReader
    for url in ('http://example.com','https://user:pass@example.com','https://example.com/api','https://example.com?x=y'):
        try: ScannerReader(url,TOKEN)
        except ValueError: pass
        else: raise AssertionError('Unsafe URL accepted')
    with tempfile.TemporaryDirectory(prefix='scanner-mcp-protocol-') as tmp:
        token_path=Path(tmp)/'token'; token_path.write_text(TOKEN); token_path.chmod(0o600)
        http=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True); thread.start()
        try:
            anyio.run(check,http.server_port,token_path)
            Handler.redirect = True
            count = len(calls)
            try: ScannerReader(f'http://127.0.0.1:{http.server_port}', TOKEN).status()
            except RuntimeError as exc:
                assert TOKEN not in str(exc)
            else: raise AssertionError('Redirect accepted')
            assert len(calls) == count + 1
        finally: http.shutdown(); http.server_close(); thread.join()
    print('MCP stdio handshake, 3 read tools, scoped requests, and invalid-call rejection: PASS')
