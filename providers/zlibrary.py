"""Async E-API adapter. Endpoint shapes referenced from OMSociety's MIT client.
No account pool, domain discovery, credential forwarding to download hosts, or retries of downloads.
"""
import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import aiohttp
from ..store import atomic_json
from ..covers import cover_url


FAKE_IP_RANGE = ipaddress.ip_network('198.18.0.0/16')


def allowed_address(host, address):
    """Permit the local proxy's synthetic DNS pool for domain requests only.

    Keep hostname TLS verification and reject actual private/loopback targets.
    An IP literal must not use this exception to address a local service.
    """
    ip = ipaddress.ip_address(address)
    if ip.is_global:
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return ip.version == 4 and ip in FAKE_IP_RANGE and '.' in host
    return False


def parse_cookie_header(value):
    """Accept a browser's Cookie request-header value, scoped by the caller's origin."""
    if not value:
        return {}
    if not isinstance(value, str) or len(value) > 32768 or re.search(r'[\x00-\x1f\x7f]', value):
        raise ValueError('完整 Cookie 格式无效；请粘贴请求头 Cookie 的单行内容')
    value = re.sub(r'^Cookie:\s*', '', value.strip(), flags=re.I)
    parsed = SimpleCookie()
    try:
        parsed.load(value)
    except CookieError:
        raise ValueError('完整 Cookie 无法解析') from None
    if not parsed or len(parsed) > 100:
        raise ValueError('完整 Cookie 为空或数量过多')
    return {k:m.value for k,m in parsed.items()}


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate actual connector resolutions as well as preflight URLs."""
    def __init__(self):
        self.inner = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.inner.resolve(host, port, family)
        if any(not allowed_address(host, r['host']) for r in records):
            raise OSError('private network resolution rejected')
        return records

    async def close(self):
        await self.inner.close()


async def public_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('服务地址必须是 HTTPS 公网地址')
    records = await asyncio.get_running_loop().getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not records or any(not allowed_address(p.hostname, x[4][0]) for x in records):
        raise ValueError('禁止请求本地或私有网络地址')
    return url


def base_url(value):
    p = urlsplit(value.strip())
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.path not in ('', '/') or p.query or p.fragment:
        raise ValueError('请填写已确认的 Z-Library HTTPS 站点根地址')
    return value.rstrip('/')


def safe_filename(title, book_id, ext):
    ext = re.sub('[^a-zA-Z0-9]', '', str(ext)).lower()[:8]
    if not ext:
        raise ValueError('服务未提供有效文件格式')
    title = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', title).strip(' .')[:110] or 'book'
    return f'{title}-{book_id}.{ext}'


class ZLibrary:
    def __init__(self, settings, root):
        self.settings, self.root = settings, Path(root)
        self.secret = self.root / 'session.json'
        self.credentials = json.loads(self.secret.read_text()) if self.secret.exists() else {}
        self.session = None
        self.lock = asyncio.Lock()
        self.origin_cookies = {}

    async def client(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()), cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=90), trust_env=False)
        return self.session

    async def close(self):
        if self.session:
            await self.session.close()

    def cookies(self):
        # Persisted tokens are bound to the exact administrator-configured origin.
        if self.credentials.get('origin') != self.settings['base_url']:
            return {}
        return self.credentials.get('cookies', {})

    def configured(self):
        return bool(self.cookies().get('remix_userid') and self.cookies().get('remix_userkey'))

    async def request(self, method, endpoint, data=None, cookies=None, user_agent=None):
        browser = getattr(self, 'browser', None)
        if browser is not None and browser.active:
            return await browser.api_request(method, endpoint, data)
        origin = base_url(self.settings['base_url'])
        url = origin + endpoint
        await public_url(url)
        client = await self.client()
        outgoing = dict(self.cookies()) if cookies is None else dict(cookies)
        if cookies is None:
            outgoing.update(self.origin_cookies.get(origin, {}))
        headers = {'Accept': 'application/json, text/plain, */*'}
        agent = user_agent if user_agent is not None else self.credentials.get('user_agent', '')
        if agent:
            headers['User-Agent'] = agent
        received = {}
        try:
            for hop in range(5):
                async with client.request(method, url, data=data, cookies=outgoing, headers=headers, allow_redirects=False) as r:
                    # Keep response cookies scoped to this exact origin. DummyCookieJar
                    # prevents these cookies from leaking to the download/CDN requests.
                    for name, morsel in r.cookies.items():
                        if morsel['max-age'] == '0':
                            outgoing.pop(name, None)
                            self.origin_cookies.setdefault(origin, {}).pop(name, None)
                            continue
                        outgoing[name] = morsel.value
                        if name in ('remix_userid', 'remix_userkey'):
                            received[name] = morsel.value
                        else:
                            self.origin_cookies.setdefault(origin, {})[name] = morsel.value
                    if r.status in (301, 302, 303, 307, 308):
                        location = r.headers.get('Location')
                        if not location:
                            raise ValueError('站点跳转缺少目标地址，请稍后重试')
                        target = urljoin(url, location)
                        current_origin = urlsplit(origin)
                        target_origin = urlsplit(target)
                        if (target_origin.scheme, target_origin.hostname, target_origin.port or 443) != (current_origin.scheme, current_origin.hostname, current_origin.port or 443) or target_origin.username or target_origin.password:
                            raise ValueError('站点跳转到了其他域名，已停止以保护登录凭据；请核对站点地址')
                        if hop == 4:
                            raise ValueError('站点验证跳转次数过多，请在浏览器检查该站点或稍后重试')
                        await public_url(target)
                        if (r.status == 303 and method != 'HEAD') or (r.status in (301, 302) and method == 'POST'):
                            method, data = 'GET', None
                        url = target
                        continue
                    if r.status in (401, 403):
                        raise ValueError('登录失效或站点需要浏览器验证；请重新登录或导入 Cookie')
                    if r.status == 513:
                        raise ValueError('站点要求浏览器验证（HTTP 513）。请在浏览器完成验证，再在登录页面导入完整 Cookie；账号登录成功不代表已通过网站验证。')
                    if r.status == 429:
                        raise ValueError('服务限流，请稍后手动重试')
                    if r.status != 200:
                        raise ValueError(f'站点返回 HTTP {r.status}')
                    raw = await r.content.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ValueError('接口响应过大')
                    try:
                        body = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        raise ValueError('返回内容不是 API 数据，可能需要浏览器验证') from None
                    if not isinstance(body, dict) or not body.get('success'):
                        raise ValueError('服务拒绝请求：请检查登录状态、下载额度或图书是否可用')
                    return body, received
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # Never return request URLs, cookies, response bodies or credential-bearing exceptions.
            raise ValueError('无法连接图书服务，请检查站点和网络') from None

    async def login(self, body):
        async with self.lock:
            mode = body.get('mode', 'password')
            user_agent = str(body.get('user_agent', '')).strip()
            if len(user_agent) > 1024 or re.search(r'[\x00-\x1f\x7f]', user_agent):
                raise ValueError('浏览器 User-Agent 无效')
            self.origin_cookies.pop(self.settings['base_url'], None)
            if mode == 'cookie':
                cookies = parse_cookie_header(body.get('cookie_header', ''))
                for k in ('remix_userid', 'remix_userkey'):
                    if str(body.get(k, '')).strip():
                        cookies[k] = str(body[k]).strip()
                if not all(cookies.get(k) for k in ('remix_userid', 'remix_userkey')) or any(len(v) > 8192 or re.search(r'[\s;\x00-\x1f]', v) for v in cookies.values()):
                    raise ValueError('请填写有效的两个 Cookie 值')
                extra = {'user_agent': user_agent} if user_agent else {}
                result, received = await self.request('GET', '/eapi/user/profile', cookies=cookies, **extra)
            elif mode == 'password':
                email, password = str(body.get('email', '')).strip(), str(body.get('password', ''))
                if not email or not password or len(email) > 320 or len(password) > 1024:
                    raise ValueError('请填写邮箱和密码')
                result, received = await self.request('POST', '/eapi/user/login', {'email': email, 'password': password}, cookies={})
                cookies = {}
            else:
                raise ValueError('未知登录方式')
            user = result.get('user') or {}
            cookies.update(received)
            if user.get('id'):
                cookies['remix_userid'] = str(user['id'])
            if user.get('remix_userkey'):
                cookies['remix_userkey'] = str(user['remix_userkey'])
            if not all(cookies.get(k) for k in ('remix_userid', 'remix_userkey')):
                raise ValueError('服务未返回可持久化凭据，请通过浏览器导入 Cookie')
            self.credentials = {'origin': self.settings['base_url'], 'cookies': cookies, 'user_agent': user_agent}
            atomic_json(self.secret, self.credentials, private=True)
            return {'connected': True, 'downloads_today': user.get('downloads_today'), 'downloads_limit': user.get('downloads_limit')}

    async def status(self):
        if not self.configured():
            return {'connected': False, 'message': '尚未登录'}
        result, received = await self.request('GET', '/eapi/user/profile')
        if received:
            self.credentials['cookies'].update(received)
            atomic_json(self.secret, self.credentials, private=True)
        user = result.get('user') or {}
        return {'connected': True, 'downloads_today': user.get('downloads_today'), 'downloads_limit': user.get('downloads_limit')}

    async def logout(self):
        async with self.lock:
            self.credentials = {}
            self.origin_cookies.clear()
            self.secret.unlink(missing_ok=True)
        return {'connected': False}

    async def search(self, query, extension='', page=1):
        if not self.configured():
            raise ValueError('请先在插件页面登录')
        query = str(query).strip()
        if not query or len(query) > 300 or not 1 <= int(page) <= 100:
            raise ValueError('搜索词或页码无效')
        data = {'message': query, 'limit': 20, 'page': int(page)}
        if extension:
            data['extensions[]'] = re.sub('[^a-zA-Z0-9]', '', extension)[:8]
        result, _ = await self.request('POST', '/eapi/book/search', data)
        books = []
        for b in (result.get('books') or [])[:20]:
            if not str(b.get('id', '')).isdigit() or not re.fullmatch('[a-zA-Z0-9]+', str(b.get('hash', ''))):
                continue
            row = {k: str(b.get(k, '') or '')[:2000] for k in ('id','hash','title','author','year','language','extension','filesizeString','description','publisher','isbn','isbn13','isbn10','pages')}
            row['cover_url']=cover_url(b.get('cover') or b.get('cover_url') or b.get('coverUrl'),self.settings['base_url'])
            books.append(row)
        return books

    async def download(self, book, destination, progress):
        expected = Path(destination) / safe_filename(book['title'], book['id'], book['extension'])
        if expected.exists() or expected.is_symlink():
            raise ValueError('此版本已在下载目录，拒绝重复下载')
        result, _ = await self.request('GET', f"/eapi/book/{book['id']}/{book['hash']}/file")
        info = result.get('file') or {}
        url = str(info.get('downloadLink', ''))
        filename = safe_filename(book['title'], book['id'], info.get('extension') or book['extension'])
        dest = Path(destination) / filename
        if dest.exists() or dest.is_symlink():
            raise ValueError('目标文件已存在；请先查看本地书架，避免重复下载')
        temp = dest.with_name('.' + filename + '.part')
        limit = int(self.settings['max_download_mb']) * 1024 * 1024
        owns_temp = False
        try:
            with temp.open('xb') as f:
                owns_temp = True
                browser = getattr(self, 'browser', None)
                if browser is not None and browser.active:
                    f.close()
                    await browser.download_to(url,temp,limit,progress)
                else:
                    client=await self.client()
                    for hop in range(6):
                        await public_url(url)
                        async with client.get(url,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=1800,sock_read=90)) as r:
                            if r.status in (301,302,303,307,308):
                                url=urljoin(url,r.headers.get('Location',''))
                                if hop==5:raise ValueError('下载重定向次数过多')
                                continue
                            if r.status!=200 or 'text/html' in r.headers.get('Content-Type',''):
                                raise ValueError('下载未返回图书文件；请检查登录、网站验证或额度')
                            total=int(r.headers.get('Content-Length',0))
                            if total>limit:raise ValueError('图书超过配置的下载大小上限')
                            count=0
                            async for block in r.content.iter_chunked(256*1024):
                                count+=len(block)
                                if count>limit:raise ValueError('图书超过配置的下载大小上限')
                                f.write(block)
                                await progress(count,total)
                            break
            count=temp.stat().st_size
            if not count:raise ValueError('下载为空文件')
            if count>limit:raise ValueError('图书超过配置的下载大小上限')
            with temp.open('rb') as f:magic=f.read(256).lstrip()
            if filename.lower().endswith('.pdf') and not magic.startswith(b'%PDF-'):
                raise ValueError('返回内容不是有效PDF，已丢弃未完成文件')
            if filename.lower().endswith('.epub') and not magic.startswith(b'PK'):
                raise ValueError('返回内容不是有效EPUB，已丢弃未完成文件')
            if magic.lower().startswith((b'<!doctype html',b'<html')):
                raise ValueError('服务返回了网页而非图书，已丢弃')
            def hash_file():
                digest=hashlib.sha256()
                with temp.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
                return digest.hexdigest()
            digest=await asyncio.to_thread(hash_file)
            import os
            os.link(temp,dest)
            temp.unlink()
            return {'name':filename,'sha256':digest,'size':count,'title':book['title'],'author':book.get('author','')}
        except (aiohttp.ClientError,asyncio.TimeoutError):
            raise ValueError('下载连接中断，请检查书架后手动重试') from None
        finally:
            if owns_temp:temp.unlink(missing_ok=True)
