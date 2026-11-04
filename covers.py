"""On-demand cover thumbnails. Bounded memory only; originals are not retained."""
import asyncio
import base64
import io
import time
from collections import OrderedDict
from urllib.parse import urljoin,urlsplit


def cover_url(value,origin):
    if isinstance(value,dict):value=value.get('url') or value.get('medium') or value.get('original') or ''
    if not isinstance(value,str) or not value:return ''
    url=urljoin(origin+'/',value.strip())
    p=urlsplit(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):return ''
    return url


def thumbnail(content):
    from PIL import Image,ImageOps
    if not content or len(content)>8*1024*1024:raise ValueError('封面为空或过大')
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.width*image.height>16_000_000:raise ValueError('封面像素尺寸过大')
            image=ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((360,540),Image.Resampling.LANCZOS)
            output=io.BytesIO();image.save(output,'JPEG',quality=84,optimize=True)
    except OSError:
        raise ValueError('站点未返回有效的封面图片') from None
    return 'data:image/jpeg;base64,'+base64.b64encode(output.getvalue()).decode()


class CoverCache:
    def __init__(self,capacity=24,ttl=1800):
        self.capacity,self.ttl=capacity,ttl
        self.items=OrderedDict()
        self.lock=asyncio.Lock()

    async def get(self,url,loader):
        async with self.lock:
            now=time.monotonic()
            for key,(expires,_) in list(self.items.items()):
                if expires<=now:self.items.pop(key,None)
            if url in self.items:
                expires,value=self.items.pop(url);self.items[url]=(expires,value)
                return value
            data=await loader(url)
            result=await asyncio.to_thread(thumbnail,data)
            self.items[url]=(time.monotonic()+self.ttl,result)
            while len(self.items)>self.capacity:self.items.popitem(last=False)
            return result
