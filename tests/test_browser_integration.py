"""Real Chromium integration against an in-memory fixture; no accounts or quotas used."""
import asyncio,json,os
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from astrbot_plugin_zlib_knowledge.browser_session import BrowserSession
from astrbot_plugin_zlib_knowledge.providers.zlibrary import ZLibrary
from astrbot_plugin_zlib_knowledge import browser_session as module

RUNTIME=os.environ.get('ZLIB_TEST_BROWSER')
pytestmark=pytest.mark.skipif(not RUNTIME,reason='Set ZLIB_TEST_BROWSER for native Chromium integration')


def test_browser_login_capture_search_download_and_restart(tmp_path,monkeypatch):
    from playwright.async_api import BrowserContext
    original_route=BrowserContext.route
    payload=b'%PDF-1.4\n% fixture download\n%%EOF\n'
    calls=[]
    import io
    from PIL import Image
    image=io.BytesIO();Image.new('RGB',(40,60),'blue').save(image,'PNG');cover=image.getvalue()
    async def public_url(url):return url
    monkeypatch.setattr(module,'public_url',public_url)
    html='''<html><body><label>Fixture username<input id="name" aria-label="Fixture username"></label><button id="done" onclick="document.cookie='remix_userid=123; path=/; secure';document.cookie='remix_userkey=fixture-private; path=/; secure';document.cookie='__diamwall=fixture-verified; path=/; secure';">Log in fixture</button></body></html>'''
    async def route(self,pattern,unused_handler,**kwargs):
        async def fixture(r):
            path=urlsplit(r.request.url).path;calls.append(path)
            if path=='/cover.png':return await r.fulfill(status=200,content_type='image/png',body=cover)
            if path=='/book.pdf':
                return await r.fulfill(status=200,headers={'Content-Type':'application/pdf','Content-Disposition':'attachment; filename="fixture.pdf"'},body=payload)
            if path.startswith('/eapi/'):
                assert 'remix_userkey=fixture-private' in r.request.headers.get('cookie','')
                body={'success':1,'books':[{'id':1,'title':'Fixture book'}]} if path.endswith('/search') else {'success':1,'user':{'id':123,'downloads_today':0,'downloads_limit':10}}
                return await r.fulfill(status=200,content_type='application/json',body=json.dumps(body))
            return await r.fulfill(status=200,content_type='text/html',body=html)
        await original_route(self,pattern,fixture)
    monkeypatch.setattr(BrowserContext,'route',route)
    async def scenario():
        settings={'base_url':'https://example.com','max_download_mb':1}
        p=ZLibrary(settings,tmp_path);b=BrowserSession(settings,tmp_path,p);p.browser=b;b.executable=lambda:Path(RUNTIME)
        try:
            started=await b.start();key=started['viewer_id']
            bounds=await b.page.locator('#name').bounding_box()
            await b.input(key,{'kind':'click','x':bounds['x']+10,'y':bounds['y']+10})
            await b.input(key,{'kind':'text','text':'test-user'})
            assert await b.page.locator('#name').input_value()=='test-user'
            bounds=await b.page.locator('#done').bounding_box()
            await b.input(key,{'kind':'click','x':bounds['x']+5,'y':bounds['y']+5})
            checked=await b.check_ready()
            assert checked['connected'] and checked['search_ready']
            assert json.loads(p.secret.read_text())['cookies']['__diamwall']=='fixture-verified'
            frame=await b.frame(key)
            assert frame['image'].startswith('data:image/jpeg;base64,') and 'fixture-private' not in frame['message']
            assert await b.cover_bytes('https://example.com/cover.png')==cover
            assert b.page.url=='https://example.com/'
            destination=tmp_path/'fixture.pdf'
            async def progress(*args):pass
            await b.download_to('https://example.com/book.pdf',destination,1024*1024,progress)
            assert destination.read_bytes()==payload
            await b.end_view(key)
            assert b.active
        finally:await b.close();await p.close()
        p2=ZLibrary(settings,tmp_path);b2=BrowserSession(settings,tmp_path,p2);p2.browser=b2;b2.executable=lambda:Path(RUNTIME)
        try:
            await b2.ensure()
            checked=await b2.check_ready()
            assert checked['connected'] and checked['search_ready']
        finally:await b2.close();await p2.close()
        assert '/book.pdf' in calls and '/eapi/book/search' in calls
    asyncio.run(scenario())
