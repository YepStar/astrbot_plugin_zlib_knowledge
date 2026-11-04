import asyncio,io,time
import pytest
from PIL import Image
from astrbot_plugin_zlib_knowledge.covers import CoverCache,cover_url,thumbnail


def image_bytes(color='blue'):
    b=io.BytesIO();Image.new('RGB',(800,1200),color).save(b,'PNG');return b.getvalue()


def test_image_resized_to_bounded_thumbnail():
    import base64
    result=thumbnail(image_bytes())
    with Image.open(io.BytesIO(base64.b64decode(result.split(',',1)[1]))) as image:
        assert image.width<=360 and image.height<=540
    assert len(result)<100_000


def test_invalid_and_oversized_images_rejected():
    with pytest.raises(Exception):thumbnail(b'<html>not an image</html>')
    with pytest.raises(ValueError):thumbnail(b'x'*(8*1024*1024+1))


def test_cover_url_does_not_allow_non_https_credentials_or_ports():
    assert cover_url('/covers/book.jpg','https://example.com')=='https://example.com/covers/book.jpg'
    for url in ['http://example.com/x.jpg','file:///etc/passwd','https://user:pass@example.com/a','https://example.com:444/a']:
        assert not cover_url(url,'https://example.com')


def test_on_demand_cache_has_no_preload_is_bounded_and_expires():
    cache=CoverCache(capacity=2,ttl=100);calls=[]
    async def loader(url):calls.append(url);return image_bytes()
    async def scenario():
        assert not cache.items and not calls
        a=await cache.get('one',loader);assert calls==['one']
        assert await cache.get('one',loader)==a and calls==['one']
        await cache.get('two',loader);await cache.get('three',loader)
        assert list(cache.items)==['two','three']
        cache.items['two']=(time.monotonic()-1,cache.items['two'][1])
        await cache.get('two',loader)
        assert calls==['one','two','three','two']
    asyncio.run(scenario())

def test_book_description_preserves_paragraphs_without_breaking_inline_emphasis():
    from astrbot_plugin_zlib_knowledge.presentation import plain_description
    assert plain_description('<p>Some <em>social</em> text.</p><p>Next paragraph.</p>')=='Some social text.\n\nNext paragraph.'
    assert plain_description('<script>ignore()</script>Book description.')=='Book description.'
