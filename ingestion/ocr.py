"""Fail closed on OCR refusals, malformed replies and truncated generations."""
import io
import json
import re
import unicodedata

FAILURE = re.compile(
    r'unsupported[ _-]*image|\[OCR[_ -]?FAILED\]|'
    r'(?:无法|不能|未能|没法|无法完成)[^。！？\n]{0,35}(?:读取|访问|查看|识别|转录|辨认)[^。！？\n]{0,35}(?:图片|图像|图中|本页|页面|内容|正文)|'
    r'(?:图片|图像)[^。！？\n]{0,20}(?:未成功提供|未提供|未加载|无法加载|不受支持|不支持|格式不受支持)|'
    r'(?:无法|不能|未能)[^。！？\n]{0,15}转录|'
    r'请[^。！？\n]{0,15}(?:重新|再次)?(?:上传|提供)[^。！？\n]{0,35}(?:图片|图像|图文|文本)|'
    r'(?:I\s+(?:cannot|can\x27t|am unable to)|unable to|failed to)\s+(?:view|access|read|see|process|transcribe|recognize)[^.!?\n]{0,70}(?:image|picture|page)|'
    r'(?:image|vision)\s+(?:input\s+)?(?:is\s+)?(?:not supported|unavailable)|'
    r'(?:please\s+)(?:upload|provide|re-upload)[^.!?\n]{0,60}(?:image|picture)',
    re.I,
)


def ocr_failure_reason(text):
    normalized = unicodedata.normalize('NFKC', str(text))
    return '识图失败或请求重传图片的模型回复' if FAILURE.search(normalized) else ''


def _field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def validate_response(response, png):
    if _field(response, 'role') in ('err', 'error', 'tool'):
        raise ValueError('OCR 返回错误或工具调用，未取得正文')
    raw = _field(response, 'raw_completion')
    reasons = [_field(raw, 'stop_reason'), _field(raw, 'status')]
    reasons += [_field(c, 'finish_reason') for c in (_field(raw, 'choices', []) or [])]
    reasons += [_field(c, 'finish_reason') for c in (_field(raw, 'candidates', []) or [])]
    if any(any(tag in str(r).lower() for tag in ('length', 'max_token', 'safety', 'content_filter', 'incomplete', 'failed')) for r in reasons if r):
        raise ValueError('OCR 输出被截断或拦截，未取得完整正文')
    text = str(_field(response, 'completion_text', '') or '').strip()
    if ocr_failure_reason(text):
        raise ValueError('OCR 模型未能读取图片，请核对所选模型及其图片输入链路')
    fenced = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.I)
    if fenced:
        text = fenced.group(1)
    try:
        result = json.loads(text)
    except (ValueError, TypeError):
        raise ValueError('OCR 返回格式无效，未确认成功转录') from None
    if not isinstance(result, dict) or result.get('status') not in ('ok', 'empty') or not isinstance(result.get('text'), str):
        raise ValueError('OCR 未明确返回成功状态和正文')
    body = result['text'].strip()
    if ocr_failure_reason(body):
        raise ValueError('OCR 正文包含识图失败提示，已阻止继续分析')
    if result['status'] == 'empty':
        if body:
            raise ValueError('OCR 空白状态与正文矛盾')
        # A model saying "empty" is not proof: only accept a locally blank raster.
        from PIL import Image, ImageChops
        with Image.open(io.BytesIO(png)) as image:
            ink = ImageChops.invert(image.convert('L')).point(lambda x: 255 if x > 20 else 0)
            if ink.getbbox() is not None:
                raise ValueError('OCR 称页面空白，但本地图片仍有内容；停止以免漏页')
        return ''
    if not body or not re.search(r'[A-Za-z\u3400-\u9fff]', body):
        raise ValueError('OCR 未返回可识别正文')
    if body in ('[EMPTY_PAGE]', '[Image]', '[图片]'):
        raise ValueError('OCR 只返回图片占位符')
    return body
