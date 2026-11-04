import asyncio,json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from astrbot_plugin_zlib_knowledge.browser_session import BrowserSession
from astrbot_plugin_zlib_knowledge.providers.zlibrary import ZLibrary

@pytest.fixture
def browser(tmp_path):
    settings={'base_url':'https://example.com','max_download_mb':1}
    provider=ZLibrary(settings,tmp_path)
    return BrowserSession(settings,tmp_path,provider)


def prepare_session(browser,search_failure=False):
    class Context:
        async def cookies(self,origin):return [{'name':'remix_userid','value':'123'},{'name':'remix_userkey','value':'PRIVATE'},{'name':'__diamwall','value':'VERIFIED'}]
        async def storage_state(self):return {'cookies':[{'name':'remix_userkey','value':'PRIVATE'}]}
    class Page:
        url='https://example.com/'
        async def evaluate(self,code,*args):return 'Browser Test'
    browser.context=Context();browser.page=Page();browser.origin='https://example.com';browser.active=True
    called=[]
    async def request(method,endpoint,data=None):
        called.append(endpoint)
        if endpoint.endswith('/search') and search_failure:raise ValueError('website verification pending')
        return {'success':1,'user':{'downloads_today':1,'downloads_limit':10}},{}
    browser.api_request=request
    return called


def test_automatic_cookie_capture_requires_real_search_success(browser):
    called=prepare_session(browser)
    result=asyncio.run(browser.check_ready())
    assert result['connected'] and result['search_ready'] and browser.ready
    assert called==['/eapi/user/profile','/eapi/book/search']
    saved=json.loads(browser.provider.secret.read_text())
    assert saved['cookies']['__diamwall']=='VERIFIED'
    assert saved['browser_managed']
    assert browser.provider.secret.stat().st_mode & 0o777==0o600
    assert browser.profile_path().joinpath('session-backup.json').stat().st_mode & 0o777==0o600
    assert 'PRIVATE' not in json.dumps(result)+json.dumps(browser.state())


def test_profile_success_does_not_report_login_ready_when_search_fails(browser):
    prepare_session(browser,search_failure=True)
    result=asyncio.run(browser.check_ready(quiet=True))
    assert not result['connected'] and not browser.ready
    assert not browser.provider.secret.exists()


def test_no_browser_session_and_stale_viewer_rejected(browser):
    with pytest.raises(ValueError):asyncio.run(browser.check_ready())
    for token in ['',None,'not-a-live-window']:
        with pytest.raises(ValueError):asyncio.run(browser.input(token,{'kind':'text','text':'private'}))


def test_browser_api_never_runs_from_unrelated_page_origin(browser):
    browser.active=True;browser.origin='https://example.com';browser.page=NS(url='https://other.example/')
    with pytest.raises(ValueError):asyncio.run(browser.api_request('GET','/eapi/user/profile'))


def test_close_view_keeps_browser_and_saved_session(browser):
    browser.context=NS();browser.page=NS();browser.viewer_id='session';browser.active=True
    result=asyncio.run(browser.end_view('session'))
    assert result['session_preserved'] and browser.context and browser.active
    assert not browser.viewer_id

def test_navigation_race_retries_only_read_only_queries(browser):
    class Page:
        url='https://example.com/'
        calls=0
        async def evaluate(self,*args):
            self.calls+=1
            if self.calls==1:raise RuntimeError('Execution context was destroyed, most likely because of a navigation')
            return {'status':200,'text':'{"success":1,"books":[]}'}
        async def wait_for_load_state(self,*args,**kwargs):pass
    browser.active=True;browser.origin='https://example.com';browser.page=Page()
    result=asyncio.run(browser.api_request('POST','/eapi/book/search',{'message':'test'}))
    assert result[0]['success']==1 and browser.page.calls==2
    browser.page.calls=0
    with pytest.raises(ValueError):asyncio.run(browser.api_request('GET','/eapi/book/123/abc/file'))
    assert browser.page.calls==1
