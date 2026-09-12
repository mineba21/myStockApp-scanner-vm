"""Isolated HTTP OAuth/MCP integration tests. No scanner API or database access."""
import base64
import hashlib
import re
import tempfile
import unittest
from urllib.parse import urlsplit,parse_qs
from starlette.testclient import TestClient
from remote import OwnerProvider,build_app,digest

class Reader:
    def status(self): return {'source':'fake_scanner'}
    def signals(self,*args): return {'items':[]}
    def signal(self,*args): return {'id':1}

class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.origin='https://scanner.example'
        self.provider=OwnerProvider(self.tmp.name+'/auth.db',self.origin,digest('owner-test-password'))
        self.client=TestClient(build_app(Reader(),self.provider),base_url=self.origin,follow_redirects=False)
        self.client.__enter__()
        self.verifier='v'*64
        self.challenge=base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        response=self.client.post('/register',json={'client_name':'Test web client','redirect_uris':['https://client.example/callback'],
            'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','scope':'scanner:read'})
        self.assertEqual(response.status_code,201,response.text)
        self.cid=response.json()['client_id']
    def tearDown(self):
        self.client.__exit__(None,None,None); self.tmp.cleanup()
    def authorize(self,resource=None):
        return self.client.get('/authorize',params={'response_type':'code','client_id':self.cid,
            'redirect_uri':'https://client.example/callback','code_challenge':self.challenge,
            'code_challenge_method':'S256','scope':'scanner:read','state':'state123','resource':resource or self.origin+'/mcp'})
    def approve(self):
        a=self.authorize(); self.assertEqual(a.status_code,302,a.text)
        page=self.client.get(a.headers['location']); self.assertEqual(page.status_code,200,page.text)
        self.assertEqual(page.headers['referrer-policy'],'same-origin')
        self.assertIn("form-action 'self' https://client.example",page.headers['content-security-policy'])
        fields={k:re.search('name="'+k+'" value="([^"]+)"',page.text).group(1) for k in ('flow','csrf')}
        r=self.client.post('/consent',data={**fields,'password':'owner-test-password','decision':'approve'},headers={'Origin':self.origin})
        self.assertEqual(r.status_code,303,r.text)
        query=parse_qs(urlsplit(r.headers['location']).query)
        self.assertEqual(query['state'],['state123'])
        return query['code'][0]
    def exchange(self,code,**kw):
        data=dict(grant_type='authorization_code',client_id=self.cid,code=code,code_verifier=self.verifier,
                  redirect_uri='https://client.example/callback',resource=self.origin+'/mcp')
        data.update(kw); return self.client.post('/token',data=data)
    def refresh(self,token):
        return self.client.post('/token',data={'grant_type':'refresh_token','client_id':self.cid,
                                             'refresh_token':token,'resource':self.origin+'/mcp'})
    def test_discovery_and_no_auth(self):
        self.assertEqual(self.client.post('/mcp',json={}).status_code,401)
        self.assertIn('resource_metadata',self.client.post('/mcp',json={}).headers['www-authenticate'])
        protected=self.client.get('/.well-known/oauth-protected-resource/mcp').json()
        authorization=self.client.get('/.well-known/oauth-authorization-server').json()
        self.assertEqual(protected['resource'],self.origin+'/mcp')
        self.assertIn('S256',authorization['code_challenge_methods_supported'])
        self.assertIn('none',authorization['token_endpoint_auth_methods_supported'])
        self.assertEqual(authorization['registration_endpoint'],self.origin+'/register')
        for path in ('/.well-known/oauth-protected-resource',
                     '/.well-known/oauth-protected-resource/',
                     '/.well-known/oauth-protected-resource/mcp/'):
            response=self.client.get(path)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json(),protected)
        response=self.client.get('/.well-known/oauth-authorization-server/')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json(),authorization)
        self.assertEqual(self.client.get('/authorize',headers={'Host':'evil.example'}).status_code,400)
    def test_pkce_replay_and_three_read_tools(self):
        code=self.approve()
        self.assertEqual(self.exchange(code,code_verifier='x'*64).status_code,400)
        self.assertEqual(self.exchange(code,resource='https://wrong.example/mcp').status_code,400)
        r=self.exchange(code); self.assertEqual(r.status_code,200,r.text)
        token=r.json()['access_token']
        self.assertEqual(self.exchange(code).status_code,400)
        headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}
        r=self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize','params':
            {'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        self.assertEqual(r.status_code,200,r.text)
        r=self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertEqual({t['name'] for t in r.json()['result']['tools']},{'scanner_status','scanner_signals','scanner_signal'})
        r=self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'scanner_status','arguments':{}}})
        self.assertIn('fake_scanner',r.text)
    def test_refresh_rotation_reuse_revokes_family(self):
        old=self.exchange(self.approve()).json()
        new=self.refresh(old['refresh_token']); self.assertEqual(new.status_code,200,new.text)
        self.assertEqual(self.refresh(old['refresh_token']).status_code,400)
        self.assertEqual(self.client.post('/mcp',headers={'Authorization':'Bearer '+new.json()['access_token']},json={}).status_code,401)
    def test_revoke_and_restart(self):
        token=self.exchange(self.approve()).json()
        provider=OwnerProvider(self.tmp.name+'/auth.db',self.origin,digest('owner-test-password'))
        import asyncio
        self.assertIsNotNone(asyncio.run(provider.load_access_token(token['access_token'])))
        r=self.client.post('/revoke',data={'client_id':self.cid,'token':token['refresh_token']})
        self.assertEqual(r.status_code,200,r.text)
        self.assertIsNone(asyncio.run(provider.load_access_token(token['access_token'])))
    def test_consent_and_redirect_guards(self):
        r=self.client.post('/register',json={'redirect_uris':['http://evil.example/callback']})
        self.assertEqual(r.status_code,400)
        a=self.authorize(); page=self.client.get(a.headers['location'])
        flow=re.search('name="flow" value="([^"]+)"',page.text).group(1)
        r=self.client.post('/consent',data={'flow':flow,'csrf':'bad','password':'owner-test-password'},headers={'Origin':self.origin})
        self.assertEqual(r.status_code,403)
        r=self.client.post('/consent',data={'flow':flow},headers={'Origin':'https://evil.example'})
        self.assertEqual(r.status_code,403)
        r=self.authorize(resource='https://wrong.example/mcp')
        self.assertNotIn('/consent',r.headers.get('location',''))
        self.assertIn('invalid_request',r.headers.get('location',r.text))

    def test_wrong_password_expiry_and_no_grant(self):
        a=self.authorize(); page=self.client.get(a.headers['location'])
        self.assertEqual(page.headers['referrer-policy'],'same-origin')
        self.assertIn("form-action 'self' https://client.example",page.headers['content-security-policy'])
        fields={k:re.search('name="'+k+'" value="([^"]+)"',page.text).group(1) for k in ('flow','csrf')}
        for _ in range(5):
            r=self.client.post('/consent',data={**fields,'password':'wrong','decision':'approve'},headers={'Origin':self.origin})
            self.assertEqual(r.status_code,403)
        self.assertEqual(self.client.get(a.headers['location']).status_code,400)
        token=self.exchange(self.approve()).json()['access_token']
        with self.provider.db() as db:
            db.execute("UPDATE objects SET expires=0 WHERE kind='access'")
        self.assertEqual(self.client.post('/mcp',json={},headers={'Authorization':'Bearer '+token}).status_code,401)

if __name__=='__main__': unittest.main()
