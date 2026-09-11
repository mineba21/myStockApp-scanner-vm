"""Owner-only OAuth + Streamable HTTP MCP, isolated from the scanner process.

SDK handles registration, exact redirect matching, PKCE and token endpoint parsing.
This provider persists grants atomically and requires owner consent for every grant.
"""
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, parse_qs

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
    RefreshToken, AuthorizeError, RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from server import ScannerReader, create_server

SCOPE = 'scanner:read'

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerProvider:
    def __init__(self, db_path, origin, owner_hash):
        parsed=urlsplit(origin)
        if parsed.scheme != 'https' or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('An HTTPS origin is required')
        if len(owner_hash)!=64 or any(c not in '0123456789abcdef' for c in owner_hash):
            raise ValueError('Owner credential SHA256 required')
        self.origin, self.resource, self.owner_hash = origin, origin+'/mcp', owner_hash
        self.path=Path(db_path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS objects(kind TEXT, key TEXT, data TEXT, expires REAL, family TEXT, PRIMARY KEY(kind,key))')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db

    def put(self, db, kind, key, data, expires, family=''):
        db.execute('DELETE FROM objects WHERE expires < ?', (time.time(),))
        if db.execute('SELECT count(*) FROM objects').fetchone()[0] >= 5000:
            raise RuntimeError('OAuth storage capacity reached')
        db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?,?,?)',
                   (kind,digest(key),json.dumps(data),expires,family))

    def get(self, db, kind, key):
        row=db.execute('SELECT * FROM objects WHERE kind=? AND key=? AND expires>?',
                       (kind,digest(key),time.time())).fetchone()
        return json.loads(row['data']) if row else None

    def delete(self, db, kind, key):
        return db.execute('DELETE FROM objects WHERE kind=? AND key=?',(kind,digest(key))).rowcount

    async def get_client(self, client_id):
        with self.db() as db: data=self.get(db,'client',client_id)
        return OAuthClientInformationFull(**data) if data else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris or []:
            p=urlsplit(str(uri))
            if p.scheme!='https' or not p.hostname or p.username or p.fragment:
                raise RegistrationError('invalid_redirect_uri','HTTPS redirect required')
        with self.db() as db:
            if db.execute("SELECT count(*) FROM objects WHERE kind='client'").fetchone()[0]>=500:
                raise RegistrationError('invalid_client_metadata','Registration capacity reached')
            self.put(db,'client',client_info.client_id,client_info.model_dump(mode='json'),time.time()+86400*90)

    async def authorize(self, client, params):
        if params.resource != self.resource or set(params.scopes or [])!={SCOPE}:
            raise AuthorizeError('invalid_request','Exact scanner resource and scope required')
        flow=secrets.token_urlsafe(32)
        with self.db() as db:
            self.put(db,'pending',flow,{'client_id':client.client_id,'params':params.model_dump(mode='json')},time.time()+600)
        return self.origin+'/consent?flow='+flow

    async def consent(self, request):
        headers={'Cache-Control':'no-store','Referrer-Policy':'same-origin',
                 'Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                 'X-Content-Type-Options':'nosniff'}
        flow=request.query_params.get('flow','') if request.method=='GET' else None
        form=None
        if request.method=='POST':
            if request.headers.get('origin')!=self.origin:
                return HTMLResponse('Invalid origin',403,headers=headers)
            form=await request.form(); flow=str(form.get('flow',''))
        with self.db() as db:
            pending=self.get(db,'pending',flow)
            if not pending: return HTMLResponse('연결 요청이 만료되었습니다. 앱에서 다시 연결하세요.',400,headers=headers)
            params=AuthorizationParams(**pending['params'])
            # Chromium applies form-action to the OAuth redirect after POST too.
            callback_url=urlsplit(str(params.redirect_uri))
            callback_origin=callback_url.scheme+'://'+callback_url.netloc
            headers['Content-Security-Policy']=headers['Content-Security-Policy'].replace("form-action 'self'", "form-action 'self' "+callback_origin)
            if request.method=='GET':
                csrf=secrets.token_urlsafe(32)
                pending['csrf']=digest(csrf)
                self.put(db,'pending',flow,pending,time.time()+600)
                callback=html.escape(str(params.redirect_uri))
                body=f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>스캐너 연결 승인</title>
                <h1>스캐너 조회 연결</h1><p>후보 종목·판정 지표·경고 조회만 허용합니다. 계좌·주문 권한은 없습니다.</p>
                <p>연결할 앱의 반환 주소: <strong>{callback}</strong></p>
                <p>본인이 시작한 연결인지 확인하세요.</p><form method="post" action="/consent">
                <input type="hidden" name="flow" value="{html.escape(flow)}">
                <input type="hidden" name="csrf" value="{csrf}">
                <label>연결 승인 암호 <input type="password" name="password" required autocomplete="current-password"></label>
                <button name="decision" value="approve">조회 연결 승인</button><button name="decision" value="deny" formnovalidate>취소</button></form></html>'''
                response=HTMLResponse(body,headers=headers)
                response.set_cookie('mcp_consent',csrf,secure=True,httponly=True,samesite='lax',path='/consent',max_age=600)
                return response
            csrf=str(form.get('csrf',''))
            if (not csrf or not secrets.compare_digest(csrf,request.cookies.get('mcp_consent','')) or
                    not secrets.compare_digest(digest(csrf),pending.get('csrf',''))):
                return HTMLResponse('Invalid consent session',403,headers=headers)
            if form.get('decision')=='deny':
                self.delete(db,'pending',flow)
                destination=construct_redirect_uri(str(params.redirect_uri),error='access_denied',state=params.state)
            else:
                if not secrets.compare_digest(digest(str(form.get('password',''))),self.owner_hash):
                    pending['failures']=pending.get('failures',0)+1
                    if pending['failures']>=5: self.delete(db,'pending',flow)
                    else: self.put(db,'pending',flow,pending,time.time()+300)
                    return HTMLResponse('승인 암호가 올바르지 않습니다. 앱에서 다시 연결하세요.',403,headers=headers)
                self.delete(db,'pending',flow)
                code=secrets.token_urlsafe(32)
                data=AuthorizationCode(code=code,client_id=pending['client_id'],expires_at=time.time()+120,
                    scopes=[SCOPE],code_challenge=params.code_challenge,redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,resource=self.resource).model_dump(mode='json')
                data.pop('code')
                self.put(db,'code',code,data,data['expires_at'])
                destination=construct_redirect_uri(str(params.redirect_uri),code=code,state=params.state)
        response=RedirectResponse(destination,status_code=303,headers=headers)
        response.delete_cookie('mcp_consent',path='/consent')
        return response

    async def load_authorization_code(self, client, authorization_code):
        with self.db() as db: data=self.get(db,'code',authorization_code)
        return AuthorizationCode(code=authorization_code,**data) if data and data['client_id']==client.client_id else None

    def issue(self, db, client_id, family=None):
        family=family or secrets.token_hex(16)
        access,refresh=secrets.token_urlsafe(32),secrets.token_urlsafe(32)
        expires=int(time.time())
        a={'client_id':client_id,'scopes':[SCOPE],'expires_at':expires+3600,'resource':self.resource}
        r={'client_id':client_id,'scopes':[SCOPE],'expires_at':expires+86400*30}
        self.put(db,'access',access,a,a['expires_at'],family)
        self.put(db,'refresh',refresh,r,r['expires_at'],family)
        return OAuthToken(access_token=access,token_type='Bearer',expires_in=3600,refresh_token=refresh,scope=SCOPE)

    async def exchange_authorization_code(self, client, authorization_code):
        with self.db() as db:
            data=self.get(db,'code',authorization_code.code)
            if not data or data['client_id']!=client.client_id:
                raise TokenError('invalid_grant','Code already used or expired')
            self.delete(db,'code',authorization_code.code)
            return self.issue(db,client.client_id)

    async def load_refresh_token(self, client, refresh_token):
        with self.db() as db:
            used=self.get(db,'used',refresh_token)
            if used and used['client_id']==client.client_id:
                db.execute("DELETE FROM objects WHERE family=? AND kind IN ('access','refresh')",(used['family'],))
            data=self.get(db,'refresh',refresh_token)
        return RefreshToken(token=refresh_token,**data) if data and data['client_id']==client.client_id else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        if set(scopes)!={SCOPE}: raise TokenError('invalid_scope','Scanner read only')
        with self.db() as db:
            data=self.get(db,'refresh',refresh_token.token)
            if not data or data['client_id']!=client.client_id: raise TokenError('invalid_grant','Refresh token already used')
            family=db.execute("SELECT family FROM objects WHERE kind='refresh' AND key=?",(digest(refresh_token.token),)).fetchone()[0]
            db.execute("DELETE FROM objects WHERE family=? AND kind IN ('access','refresh')",(family,))
            self.put(db,'used',refresh_token.token,{'family':family,'client_id':client.client_id},time.time()+86400*30,family)
            return self.issue(db,client.client_id,family)

    async def load_access_token(self, token):
        with self.db() as db: data=self.get(db,'access',token)
        if not data or data.get('resource')!=self.resource or data.get('scopes')!=[SCOPE]: return None
        return AccessToken(token=token,**data)

    async def revoke_token(self, token):
        with self.db() as db:
            row=db.execute("SELECT family FROM objects WHERE key=? AND kind IN ('access','refresh')",(digest(token.token),)).fetchone()
            if row: db.execute('DELETE FROM objects WHERE family=?',(row[0],))


def build_app(reader, provider):
    host=urlsplit(provider.origin).netloc
    mcp=create_server(reader,auth_server_provider=provider,
        auth=AuthSettings(issuer_url=provider.origin,resource_server_url=provider.resource,required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=[SCOPE],default_scopes=[SCOPE]),
            revocation_options=RevocationOptions(enabled=True)),
        stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[host],allowed_origins=[provider.origin]))
    mcp.custom_route('/consent',methods=['GET','POST'])(provider.consent)
    app=mcp.streamable_http_app()
    # SDK 1.26 requires a client_secret form field even for public-client
    # revocation. Keep its authenticator, but support RFC7009 public requests.
    from mcp.server.auth.middleware.client_auth import ClientAuthenticator, AuthenticationError
    from starlette.routing import Route
    async def revoke(request):
        try:
            client=await ClientAuthenticator(provider).authenticate_request(request)
        except AuthenticationError:
            return JSONResponse({'error':'invalid_client'},status_code=401)
        form=await request.form()
        raw=form.get('token')
        if not isinstance(raw,str) or not raw:
            return JSONResponse({'error':'invalid_request'},status_code=400)
        token=await provider.load_access_token(raw) or await provider.load_refresh_token(client,raw)
        if token and token.client_id==client.client_id: await provider.revoke_token(token)
        return Response(status_code=200,headers={'Cache-Control':'no-store'})
    for index,route in enumerate(app.routes):
        if getattr(route,'path',None)=='/revoke':
            app.routes[index]=Route('/revoke',revoke,methods=['POST'])
    async def resource_binding(request, call_next):
        if request.method=='POST':
            body=await request.body()
            if len(body)>65536:
                return JSONResponse({'error':'invalid_request'},status_code=413)
            if request.url.path=='/token':
                values=parse_qs(body.decode('utf-8',errors='replace'))
                if values.get('resource') != [provider.resource]:
                    return JSONResponse({'error':'invalid_target'},status_code=400)
        return await call_next(request)
    from starlette.middleware.base import BaseHTTPMiddleware
    app.add_middleware(BaseHTTPMiddleware, dispatch=resource_binding)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[urlsplit(provider.origin).hostname])
    return app


def main():
    os.umask(0o077)
    config=json.loads(Path(os.environ['SCANNER_MCP_CONFIG']).read_text())
    provider=OwnerProvider(config['oauth_db'],config['origin'],config['owner_hash'])
    # Only the dedicated read credential is available in this process.
    reader=ScannerReader('http://127.0.0.1:8000',config['scanner_read_token'])
    import uvicorn
    uvicorn.run(build_app(reader,provider),host='127.0.0.1',port=8001,access_log=False)

if __name__=='__main__': main()
