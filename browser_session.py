"""User-operated browser login, persistent origin-isolated sessions and same-browser API.
No passwords/cookies are returned to the UI. No independent listening port is opened.
"""
import asyncio
import base64
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from .providers.zlibrary import public_url
from .store import atomic_json

FETCH_API = r'''async ({url, method, data}) => {
  for (let hop=0; hop<5; hop++) {
    const options={method,credentials:'same-origin',redirect:'manual',headers:{'Accept':'application/json, text/plain, */*'}};
    if(data!==null && !['GET','HEAD'].includes(method))options.body=new URLSearchParams(data);
    const r=await fetch(url,options);
    // A manual opaque redirect receives its Set-Cookie, but is never followed to
    // an untrusted host. Retry only the original endpoint for same-address handshakes.
    if(r.type==='opaqueredirect')continue;
    const reader=r.body?.getReader(); let size=0; const parts=[];
    if(reader){for(;;){const item=await reader.read();if(item.done)break;size+=item.value.length;if(size>4194304){await reader.cancel();return {status:r.status,too_large:true};}parts.push(item.value);}}
    const bytes=new Uint8Array(size);let offset=0;for(const p of parts){bytes.set(p,offset);offset+=p.length;}
    return {status:r.status,text:new TextDecoder().decode(bytes)};
  }
  return {status:0,redirect_limit:true};
}'''


class BrowserSession:
    def __init__(self, settings, root, provider):
        self.settings, self.root, self.provider = settings, Path(root), provider
        self.runtime = self.root/'runtime'
        self.profiles = self.root/'browser_profiles'
        self.pw = self.context = self.page = self.display = None
        self.origin = ''
        self.active = False
        self.ready = False
        self.viewer_id = ''
        self.message = '点击“浏览器登录”，在官网页面完成登录，插件自动保存会话。'
        self.start_lock = asyncio.Lock()
        self.op_lock = asyncio.Lock()
        self.probe_task = None
        self.last_probe = 0

    def executable(self):
        candidates = list((self.runtime/'browsers').glob('chromium-*/chrome-*/chrome'))
        if not candidates:
            return None
        return sorted(candidates,key=lambda p:p.parents[1].name,reverse=True)[0]

    def state(self):
        return {'installed':bool(self.executable()),'active':self.active,'ready':self.ready,'message':self.message}

    async def setup(self, progress):
        self.runtime.mkdir(parents=True,exist_ok=True)
        cmd=[sys.executable,'-m','playwright','install']
        if sys.platform=='linux' and os.geteuid()==0:
            cmd.append('--with-deps')
        cmd.append('chromium')
        env=dict(os.environ,PLAYWRIGHT_BROWSERS_PATH=str(self.runtime/'browsers'))
        await progress('安装官方 Chromium 浏览器组件，首次可能需要几分钟')
        with (self.runtime/'installation.log').open('wb') as log:
            process=await asyncio.create_subprocess_exec(*cmd,env=env,stdout=log,stderr=log)
            try:
                code=await process.wait()
            except asyncio.CancelledError:
                process.terminate();await process.wait();raise
        if code or not self.executable():
            raise ValueError('浏览器组件安装失败，请检查网络后重试；安装日志保存在插件数据目录')
        return {'installed':True}

    async def ensure(self):
        async with self.start_lock:
            origin=self.settings['base_url']
            if self.context and self.origin==origin:
                return
            if self.context:
                await self.close()
            executable=self.executable()
            if not executable:
                raise ValueError('首次使用请点击“安装浏览器组件”')
            await public_url(origin)
            from playwright.async_api import async_playwright
            self.origin=origin
            self.profiles.mkdir(parents=True,exist_ok=True,mode=0o700)
            self.profiles.chmod(0o700)
            profile=self.profile_path(origin)
            profile.mkdir(parents=True,exist_ok=True,mode=0o700)
            profile.chmod(0o700)
            env=dict(os.environ)
            try:
                headless=True
                if sys.platform=='linux' and shutil.which('Xvfb'):
                    self.display=await asyncio.create_subprocess_exec('Xvfb','-displayfd','1','-screen','0','1100x820x24','-nolisten','tcp',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
                    display_number=(await asyncio.wait_for(self.display.stdout.readline(),5)).decode().strip()
                    if not display_number.isdigit():
                        raise ValueError('无法启动浏览器显示环境')
                    env['DISPLAY']=':'+display_number;headless=False
                self.pw=await async_playwright().start()
                self.downloads=self.root/'download_staging'
                self.downloads.mkdir(parents=True,exist_ok=True,mode=0o700)
                self.downloads.chmod(0o700)
                self.context=await self.pw.chromium.launch_persistent_context(str(profile),executable_path=str(executable),headless=headless,env=env,viewport={'width':1100,'height':760},args=['--no-sandbox','--disable-dev-shm-usage','--disk-cache-size=33554432'],accept_downloads=True,downloads_path=str(self.downloads))
                cache={}
                async def guard(route):
                    url=route.request.url
                    scheme=urlsplit(url).scheme
                    if scheme in ('data','blob','about'):
                        return await route.continue_()
                    parsed=urlsplit(url)
                    host=parsed.hostname
                    if parsed.scheme!='https' or not host or parsed.username or parsed.password or parsed.port not in (None,443):
                        return await route.abort()
                    try:
                        if cache.get(host,0)<time.monotonic():
                            await public_url(url);cache[host]=time.monotonic()+30
                        await route.continue_()
                    except Exception:
                        await route.abort()
                await self.context.route('**/*',guard)
                self.page=self.context.pages[0] if self.context.pages else await self.context.new_page()
                self.follow_popups(self.page)
                existing=await self.context.cookies(origin)
                if not any(c['name']=='remix_userkey' for c in existing):
                    auth=self.provider.cookies()
                    initial=[{'name':k,'value':str(auth[k]),'url':origin,'secure':True,'sameSite':'Lax'} for k in ('remix_userid','remix_userkey') if auth.get(k)]
                    if initial:await self.context.add_cookies(initial)
                self.active=True;self.ready=False
                self.message='请在下方真实官网页面登录；验证完成后将自动保存会话。'
                try:
                    await self.page.goto(origin,wait_until='domcontentloaded',timeout=45000)
                except Exception:
                    self.message='页面仍在加载，请查看窗口；必要时点击刷新官网。'
            except Exception:
                await self.close()
                raise ValueError('浏览器启动失败，请安装或修复浏览器组件后重试') from None

    def follow_popups(self,page):
        def opened(popup):
            parent=self.page
            self.page=popup
            self.follow_popups(popup)
            def closed():
                if self.page is popup and parent and not parent.is_closed():self.page=parent
            popup.on('close',closed)
        page.on('popup',opened)

    def profile_path(self,origin=None):
        return self.profiles/hashlib.sha256((origin or self.origin).encode()).hexdigest()[:24]

    async def start(self):
        await self.ensure()
        self.viewer_id=uuid.uuid4().hex
        self.last_probe=0
        return {'viewer_id':self.viewer_id,**self.state()}

    def check_viewer(self, key):
        if not key or key!=self.viewer_id or not self.page:
            raise ValueError('登录窗口已关闭或过期，请重新打开')

    async def frame(self,key):
        self.check_viewer(key)
        async with self.op_lock:
            try:
                jpeg=await self.page.screenshot(type='jpeg',quality=70,timeout=10000)
                url=urlsplit(self.page.url)
            except Exception:
                raise ValueError('浏览器页面正在切换，请稍后重试') from None
        if not self.ready and time.monotonic()-self.last_probe>10 and (not self.probe_task or self.probe_task.done()):
            self.last_probe=time.monotonic()
            self.probe_task=asyncio.create_task(self.check_ready(quiet=True))
        return {'image':'data:image/jpeg;base64,'+base64.b64encode(jpeg).decode(),'width':1100,'height':760,'location':(url.hostname or '')+url.path,'ready':self.ready,'message':self.message}

    async def input(self,key,body):
        self.check_viewer(key)
        kind=body.get('kind')
        async with self.op_lock:
            if kind=='click':
                x,y=body.get('x'),body.get('y')
                if type(x) not in (int,float) or type(y) not in (int,float) or not 0<=x<=1100 or not 0<=y<=760:
                    raise ValueError('点击坐标无效')
                await self.page.mouse.click(x,y)
            elif kind=='drag':
                values=[body.get(k) for k in ('x','y','to_x','to_y')]
                if any(type(v) not in (int,float) for v in values) or not all(0<=v<=(1100 if i%2==0 else 760) for i,v in enumerate(values)):
                    raise ValueError('拖动坐标无效')
                await self.page.mouse.move(values[0],values[1]);await self.page.mouse.down()
                try:await self.page.mouse.move(values[2],values[3],steps=20)
                finally:await self.page.mouse.up()
            elif kind=='text':
                text=body.get('text','')
                if not isinstance(text,str) or len(text)>4000:
                    raise ValueError('输入长度无效')
                await self.page.keyboard.insert_text(text)
            elif kind=='key':
                value=body.get('key','')
                if value not in {'Enter','Tab','Backspace','Delete','Escape','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown','Control+A','Meta+A','Shift+Tab'}:
                    raise ValueError('按键无效')
                await self.page.keyboard.press(value)
            elif kind=='scroll':
                delta=body.get('delta',0)
                if type(delta) not in (int,float):raise ValueError('滚动参数无效')
                await self.page.mouse.wheel(0,max(-1500,min(1500,delta)))
            elif kind=='reload':
                await self.page.reload(wait_until='domcontentloaded',timeout=45000)
                self.ready=False;self.last_probe=0
            elif kind=='home':
                await self.page.goto(self.origin,wait_until='domcontentloaded',timeout=45000)
            else:raise ValueError('不支持的浏览器操作')
        return {'ok':True}

    async def api_request(self,method,endpoint,data=None):
        if not self.active or not self.page:
            raise ValueError('请先点击“浏览器登录”完成网站验证')
        if self.settings['base_url']!=self.origin or urlsplit(self.page.url).hostname!=urlsplit(self.origin).hostname:
            raise ValueError('请在登录窗口返回配置的官网，再完成登录')
        try:
            async with self.op_lock:
                for attempt in range(4):
                    try:
                        result=await asyncio.wait_for(self.page.evaluate(FETCH_API,{'url':self.origin+endpoint,'method':method,'data':data}),65)
                        break
                    except Exception as e:
                        # The verification page can replace itself just after DOM ready.
                        # Retry only read-only profile/search queries on navigation races;
                        # never replay a download-link request or other uncertain action.
                        navigation_race=any(key in str(e) for key in ('Execution context was destroyed','Cannot find context','because of a navigation'))
                        if attempt==3 or endpoint not in ('/eapi/user/profile','/eapi/book/search') or not navigation_race:
                            raise
                        await self.page.wait_for_load_state('domcontentloaded',timeout=15000)
                        await asyncio.sleep(.8)
        except Exception as e:
            from astrbot.api import logger
            detail=str(e)
            category=next((key for key in ('different loop','loop is closed','has been closed','Execution context was destroyed','Failed to fetch','Timeout') if key in detail),'other')
            logger.warning('z-lib browser API failure: type=%s category=%s',type(e).__name__,category)
            raise ValueError('浏览器页面正在验证或连接超时，请打开登录窗口检查') from None
        if result.get('too_large'):raise ValueError('站点接口响应过大')
        status=result.get('status')
        if status in (0,403,513):
            self.ready=False
            raise ValueError('网站还需要验证，请打开“浏览器登录”窗口直接完成验证，无需复制 Cookie')
        if status==429:raise ValueError('站点限流，请稍后再试')
        if status!=200:raise ValueError(f'站点返回 HTTP {status}，请打开登录窗口检查')
        try:body=json.loads(result.get('text',''))
        except ValueError:raise ValueError('站点返回网页，请在浏览器窗口完成登录或验证') from None
        if not isinstance(body,dict) or not body.get('success'):
            raise ValueError('站点未接受当前登录态，请在浏览器窗口重新登录')
        return body,{}

    async def check_ready(self,quiet=False):
        try:
            if not self.context:
                raise ValueError('请先打开浏览器登录窗口')
            cookies={c['name']:c['value'] for c in await self.context.cookies(self.origin)}
            if not cookies.get('remix_userid') or not cookies.get('remix_userkey'):
                raise ValueError('请在官网页面完成账号登录，插件会自动接续')
            profile,_=await self.api_request('GET','/eapi/user/profile')
            # Account authentication alone is not success: verify that book search
            # is actually accepted from this same browser before reporting readiness.
            await self.api_request('POST','/eapi/book/search',{'message':'psychology','limit':1,'page':1})
            await self.save_session()
            user=profile.get('user') or {}
            self.ready=True;self.message='登录与搜索验证成功，会话已自动保存，可以关闭窗口。'
            return {'connected':True,'search_ready':True,'downloads_today':user.get('downloads_today'),'downloads_limit':user.get('downloads_limit')}
        except ValueError as e:
            self.ready=False;self.message=str(e)
            if not quiet:raise
            return {'connected':False,'search_ready':False,'message':self.message}

    async def save_session(self):
        cookies={c['name']:c['value'] for c in await self.context.cookies(self.origin)}
        async with self.op_lock:
            agent=await self.page.evaluate('navigator.userAgent')
            state=await self.context.storage_state()
        self.provider.credentials={'origin':self.origin,'cookies':cookies,'user_agent':agent,'browser_managed':True}
        atomic_json(self.provider.secret,self.provider.credentials,private=True)
        atomic_json(self.profile_path()/'session-backup.json',state,private=True)

    async def end_view(self,key):
        self.check_viewer(key)
        self.viewer_id=''
        if self.probe_task and not self.probe_task.done():
            self.probe_task.cancel()
            await asyncio.gather(self.probe_task,return_exceptions=True)
        return {'ok':True,'session_preserved':True}

    async def cover_bytes(self,url):
        await public_url(url)
        if not self.context:raise ValueError('浏览器会话尚未连接')
        page=await self.context.new_page()
        images=[]
        def capture(response):
            if response.request.is_navigation_request() and response.frame==page.main_frame and response.headers.get('content-type','').startswith('image/'):
                images.append(response)
        page.on('response',capture)
        try:
            response=await page.goto(url,referer=self.origin+'/',wait_until='domcontentloaded',timeout=25000)
            if not images:
                # A CDN can run its normal browser verification before returning the image.
                await page.wait_for_function('document.contentType.startsWith("image/")',timeout=12000)
            response=images[-1] if images else response
            if not response or response.status!=200 or not response.headers.get('content-type','').startswith('image/'):
                raise ValueError('站点暂未返回可用封面')
            if int(response.headers.get('content-length','0'))>8*1024*1024:
                raise ValueError('封面超过体积上限')
            data=await response.body()
            if len(data)>8*1024*1024:raise ValueError('封面超过体积上限')
            return data
        except ValueError:raise
        except Exception:raise ValueError('封面暂时加载失败，可稍后重试') from None
        finally:await page.close()

    async def download_to(self,url,destination,limit,progress):
        """Use Chromium's normal download channel and its own scoped cookie jar."""
        await public_url(url)
        if not self.context:
            raise ValueError('请先完成浏览器登录')
        from playwright.async_api import Error as BrowserError
        page=await self.context.new_page()
        baseline=set(self.downloads.iterdir())
        completion=None;download=None
        try:
            async with page.expect_download(timeout=90000) as pending:
                try:
                    await page.goto(url,wait_until='commit',timeout=90000)
                except BrowserError as e:
                    if 'Download is starting' not in str(e) and 'ERR_ABORTED' not in str(e):
                        raise
            download=await pending.value
            completion=asyncio.create_task(download.save_as(str(destination)))
            started=time.monotonic()
            while not completion.done():
                size=0
                for p in self.downloads.iterdir():
                    if p in baseline:continue
                    try:
                        if p.is_file():size+=p.stat().st_size
                    except FileNotFoundError:pass
                if size>limit:
                    await download.cancel()
                    raise ValueError('图书超过配置的下载大小上限')
                if time.monotonic()-started>1800:
                    await download.cancel()
                    raise ValueError('下载超时，请检查书架后手动重试')
                await progress(size,0)
                await asyncio.sleep(.4)
            await completion
            if Path(destination).stat().st_size>limit:
                raise ValueError('图书超过配置的下载大小上限')
            return {'bytes':Path(destination).stat().st_size}
        except asyncio.CancelledError:
            if download:await download.cancel()
            raise
        except ValueError:
            raise
        except Exception:
            raise ValueError('浏览器下载失败，请在登录窗口检查登录态、额度或站点验证') from None
        finally:
            if completion and not completion.done():
                completion.cancel();await asyncio.gather(completion,return_exceptions=True)
            if download:
                try:await download.delete()
                except Exception:pass
            await page.close()

    async def logout(self):
        origin=self.origin or self.settings['base_url']
        await self.close()
        profile=self.profile_path(origin)
        if profile.exists():await asyncio.to_thread(shutil.rmtree,profile)
        self.ready=False;self.message='已退出登录，浏览器会话已清除。'

    async def close(self):
        if self.probe_task and not self.probe_task.done() and self.probe_task is not asyncio.current_task():
            self.probe_task.cancel();await asyncio.gather(self.probe_task,return_exceptions=True)
        if self.context:
            try:await self.context.close()
            except Exception:pass
        if self.pw:
            try:await self.pw.stop()
            except Exception:pass
        if self.display and self.display.returncode is None:
            self.display.terminate();await self.display.wait()
        self.context=self.page=self.pw=self.display=None
        self.active=False;self.ready=False;self.viewer_id=''
