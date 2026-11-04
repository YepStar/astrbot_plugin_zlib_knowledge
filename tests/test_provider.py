import asyncio
import json
from pathlib import Path
import pytest
from astrbot_plugin_zlib_knowledge.providers import zlibrary as z

@pytest.fixture
def provider(tmp_path):return z.ZLibrary({'base_url':'https://example.com','max_download_mb':1},tmp_path)

def test_cookie_login_validates_before_persist_and_survives_restart(provider):
    async def request(method,path,data=None,cookies=None):
        assert path=='/eapi/user/profile' and cookies['remix_userkey']=='token'
        return {'success':1,'user':{'id':123,'downloads_today':2,'downloads_limit':10}},{}
    provider.request=request
    asyncio.run(provider.login({'mode':'cookie','remix_userid':'123','remix_userkey':'token'}))
    other=z.ZLibrary(provider.settings,provider.root)
    assert other.configured() and other.cookies()['remix_userkey']=='token'
    provider.settings['base_url']='https://other.example'
    assert not provider.cookies()

def test_password_never_persisted(provider):
    async def request(method,path,data=None,cookies=None):
        assert data['password']=='very-secret-password'
        return {'success':1,'user':{'id':123,'remix_userkey':'token'}},{}
    provider.request=request
    asyncio.run(provider.login({'mode':'password','email':'me@example.com','password':'very-secret-password'}))
    assert 'very-secret-password' not in provider.secret.read_text()

def test_failed_cookie_login_does_not_overwrite_saved_login(provider):
    provider.credentials={'origin':'https://example.com','cookies':{'remix_userid':'1','remix_userkey':'old'}}
    async def request(*args,**kwargs):raise ValueError('expired')
    provider.request=request
    with pytest.raises(ValueError):asyncio.run(provider.login({'mode':'cookie','remix_userid':'1','remix_userkey':'bad'}))
    assert provider.cookies()['remix_userkey']=='old'

def test_search_sends_format_filter_and_filters_malicious_ids(provider):
    provider.credentials={'origin':'https://example.com','cookies':{'remix_userid':'1','remix_userkey':'key'}}
    async def request(method,path,data=None,cookies=None):
        assert data['extensions[]']=='epub' and data['page']==2
        return {'success':1,'books':[{'id':1,'hash':'abc','title':'Book','extension':'epub'},{'id':'../file','hash':'abc'}]},{}
    provider.request=request
    books=asyncio.run(provider.search('Book','epub',2))
    assert len(books)==1 and books[0]['id']=='1'

def test_private_network_and_non_https_rejected():
    for url in ['https://127.0.0.1','https://[::1]','http://example.com']:
        with pytest.raises(ValueError):asyncio.run(z.public_url(url))

def test_atomic_download_does_not_forward_cookies_or_overwrite(provider,tmp_path,monkeypatch):
    async def check(url):return url
    monkeypatch.setattr(z,'public_url',check)
    async def request(*args,**kwargs):return {'success':1,'file':{'extension':'pdf','downloadLink':'https://cdn.example/book.pdf'}},{}
    provider.request=request
    class Content:
        async def iter_chunked(self,size):yield b'%PDF-1.4 actual document'
    class Response:
        status=200;headers={'Content-Type':'application/pdf','Content-Length':'24'};content=Content()
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Client:
        def get(self,url,**kwargs):
            assert 'cookies' not in kwargs
            return Response()
    async def client():return Client()
    provider.client=client
    async def progress(*args):pass
    book={'id':'123','hash':'abc','title':'title','extension':'pdf'}
    result=asyncio.run(provider.download(book,tmp_path,progress))
    assert (tmp_path/result['name']).read_bytes().startswith(b'%PDF')
    with pytest.raises(ValueError):asyncio.run(provider.download(book,tmp_path,progress))
    assert not list(tmp_path.glob('*.part'))

def test_fake_ip_proxy_domain_is_allowed_but_literal_is_not():
    assert z.allowed_address('z-library.sk','198.18.2.13')
    assert z.allowed_address('download.example.com','198.18.255.254')
    assert not z.allowed_address('198.18.2.13','198.18.2.13')

def test_real_private_and_loopback_remain_blocked():
    for ip in ['127.0.0.1','10.1.1.1','172.16.0.1','192.168.1.1','169.254.169.254','::1','fc00::1','198.19.2.13']:
        assert not z.allowed_address('z-library.sk',ip)
    assert z.allowed_address('example.com','8.8.8.8')

def test_preflight_accepts_fake_dns_and_rejects_mixed_private(monkeypatch):
    async def scenario():
        loop=asyncio.get_running_loop()
        async def resolve(*args,**kwargs):return [(2,1,6,'',('198.18.2.13',443))]
        monkeypatch.setattr(loop,'getaddrinfo',resolve)
        assert await z.public_url('https://z-library.sk')=='https://z-library.sk'
        async def mixed(*args,**kwargs):return [(2,1,6,'',('198.18.2.13',443)),(2,1,6,'',('127.0.0.1',443))]
        monkeypatch.setattr(loop,'getaddrinfo',mixed)
        with pytest.raises(ValueError):await z.public_url('https://z-library.sk')
    asyncio.run(scenario())

def redirect_client(monkeypatch, provider, responses, calls):
    from http.cookies import SimpleCookie
    class Content:
        async def read(self, limit):return b'{"success":1,"books":[]}'
    class Response:
        def __init__(self, status, location='', cookie=''):
            self.status=status;self.headers={'Location':location} if location else {};self.cookies=SimpleCookie();self.cookies.load(cookie);self.content=Content()
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
    class Client:
        def request(self,method,url,**kwargs):
            calls.append({'method':method,'url':url,'data':kwargs.get('data'),'cookies':dict(kwargs['cookies'])})
            spec=responses.pop(0)
            return Response(*spec)
    async def client():return Client()
    async def check(url):return url
    provider.client=client
    monkeypatch.setattr(z,'public_url',check)


def test_same_origin_307_replays_post_with_verification_cookie(provider,monkeypatch):
    calls=[]
    redirect_client(monkeypatch,provider,[(307,'https://example.com/eapi/book/search','__diamwall=verification; Path=/; Secure'),(200,'','')],calls)
    result=asyncio.run(provider.request('POST','/eapi/book/search',{'message':'test'},cookies={'remix_userkey':'auth'}))
    assert result[0]['success']==1
    assert len(calls)==2 and all(c['method']=='POST' and c['data']=={'message':'test'} for c in calls)
    assert calls[1]['cookies']['__diamwall']=='verification'
    assert calls[1]['cookies']['remix_userkey']=='auth'
    assert provider.origin_cookies['https://example.com']['__diamwall']=='verification'


def test_cross_origin_redirect_never_sends_second_request(provider,monkeypatch):
    for target in ['https://other.example/eapi/book/search','http://example.com/eapi/book/search','https://example.com:444/eapi/book/search','https://evil@example.com/eapi/book/search']:
        calls=[]
        redirect_client(monkeypatch,provider,[(307,target,'')],calls)
        with pytest.raises(ValueError,match='其他域名'):
            asyncio.run(provider.request('POST','/eapi/book/search',{'message':'test'},cookies={'remix_userkey':'auth'}))
        assert len(calls)==1


def test_redirect_limit_stops_loop(provider,monkeypatch):
    calls=[]
    redirect_client(monkeypatch,provider,[(307,'/eapi/book/search','')]*5,calls)
    with pytest.raises(ValueError,match='次数过多'):
        asyncio.run(provider.request('POST','/eapi/book/search',{'message':'test'}))
    assert len(calls)==5


def test_303_switches_post_to_get_and_preserves_origin_only(provider,monkeypatch):
    calls=[]
    redirect_client(monkeypatch,provider,[(303,'/eapi/user/profile',''),(200,'','')],calls)
    asyncio.run(provider.request('POST','/eapi/user/login',{'password':'secret'}))
    assert calls[1]['method']=='GET' and calls[1]['data'] is None


def test_logout_clears_transient_cookies_too(provider):
    provider.origin_cookies={'https://example.com':{'__diamwall':'verification','remix_userkey':'auth'}}
    asyncio.run(provider.logout())
    assert not provider.origin_cookies

def test_complete_cookie_import_preserves_browser_verification(provider):
    async def request(method,path,data=None,cookies=None,user_agent=None):
        assert cookies['__diamwall']=='verified'
        assert cookies['remix_userid']=='123'
        assert user_agent=='Browser Test/1.0'
        return {'success':1,'user':{'id':123}},{}
    provider.request=request
    asyncio.run(provider.login({'mode':'cookie','cookie_header':'remix_userid=123; remix_userkey=token; __diamwall=verified; dwid=test','user_agent':'Browser Test/1.0'}))
    loaded=z.ZLibrary(provider.settings,provider.root)
    assert loaded.cookies()['__diamwall']=='verified'
    assert loaded.credentials['user_agent']=='Browser Test/1.0'
    assert loaded.secret.stat().st_mode & 0o777==0o600


def test_invalid_complete_cookie_and_user_agent_rejected(provider):
    for text in ['x=y\r\nHost: other.example', 'x'*32769]:
        with pytest.raises(ValueError):z.parse_cookie_header(text)
    with pytest.raises(ValueError):asyncio.run(provider.login({'mode':'cookie','user_agent':'browser\r\nX-Evil: bad'}))


def test_http_513_identifies_browser_verification(provider,monkeypatch):
    calls=[]
    redirect_client(monkeypatch,provider,[(513,'','')],calls)
    with pytest.raises(ValueError,match='浏览器验证'):
        asyncio.run(provider.request('POST','/eapi/book/search',{'message':'test'}))
