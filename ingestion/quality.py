"""Conservative deterministic filtering. Text is tested before source headers are added."""
import re
import unicodedata
from collections import Counter
from .ocr import ocr_failure_reason

AUXILIARY = re.compile(r'^(?:(?:NI|SI)[-–]?\d+\s+)?(?:name\s*index|subject\s*index|author\s*index|index|index of (?:names|subjects)|table of contents|detailed contents|contents|preface|foreword|前言|序言|序|copyright|front matter|title page|cover|references|bibliography|chapter sources|acknowledg(?:e)?ments|about the authors|参考文献|参考书目|章节来源|作者简介|致谢|版权(?:页)?|封面|扉页|目录|人名索引|姓名索引|主题索引|索引|著者索引)\s*$', re.I)
PAGE = re.compile(r'\[PDF p=\d+\]')
INDEX_LINE = re.compile(r'^(?:[A-ZÀ-ž\u4e00-\u9fff][^!?。！？]{0,110}?,?\s+)(?:\d{1,4}(?:\s*[-–,，、]\s*\d{1,4})*)\s*$')
BIB_LINE = re.compile(r'(?:\b(?:19|20)\d{2}[a-z]?\b.*(?:\bdoi\b|https?://|\d+\s*\(\d+\)|\d+\s*[-–]\s*\d+)|^[^\n]{2,70}\((?:19|20)\d{2}[a-z]?\)\.)', re.I)


def is_auxiliary_title(title):
    title=unicodedata.normalize('NFKC',str(title)).strip(' .:：')
    title=re.sub(r'^(?:(?:chapter|section)\s+)?(?:\d+|[IVXLC]+)[.、:：]\s*','',title,flags=re.I)
    return any(AUXILIARY.fullmatch(part.strip(' .:：')) for part in re.split(r'\s+[|/—–]\s+',title))


def content_body(text):
    # Source labels are not knowledge. Never let them make an empty/index leaf pass.
    if text.startswith('Book:'):
        match=re.search(r'^Leaf:\s*\d+;[^\n]*\n',text,re.M)
        if match:return text[match.end():]
        lines=[line for line in text.splitlines() if not re.match(r'^(?:Book:|Edition:|Chapter:|PDF chapter pages:|来源定位|页码是PDF|以下为资料)',line)]
        return '\n'.join(lines)
    return text


def useful(text):
    if text.startswith('Book:'):
        header=re.search(r'^Chapter:\s*[^|\n]*\|\s*([^\n]+)',text,re.M)
        if header and is_auxiliary_title(header.group(1)):
            return False,'来源标签显示该块属于索引、书目或出版辅助章节'
    if ocr_failure_reason(content_body(text)):
        return False,'识图失败回复，不是书籍正文'
    body = PAGE.sub('', content_body(text))
    lines = [x.strip() for x in body.splitlines() if x.strip()]
    if not lines or not re.search(r'[A-Za-z\u3400-\u9fff]', body):
        return False, '空白、符号、来源标签或纯页码'
    relevant = [x for x in lines if not re.fullmatch(r'[A-Za-z]|\d+(?:\s*[,，-]\s*\d+)*', x)]
    if not relevant:
        return False, '字母分隔或页码列表'
    indexed=[x for x in relevant if INDEX_LINE.match(x) or AUXILIARY.match(x)]
    named=sum(bool(re.match(r"^[A-ZÀ-ž][A-Za-zÀ-ž'’ -]+,\s*(?:[A-Z]\.\s*){1,5},?\s*\d",x)) for x in indexed)
    leaders=sum(bool(re.search(r'\.{2,}\s*\d+\s*$',x)) for x in indexed)
    multi_refs=sum(bool(re.search(r'\d+(?:\s*[,，、]\s*\d+){2,}\s*$',x)) for x in indexed)
    heading=any(re.fullmatch(r'(?:(?:NI|SI)[-–]?\d+\s+)?(?:Name Index|Subject Index|Author Index|Index|Contents|人名索引|主题索引|目录)',x,re.I) for x in lines)
    if len(relevant)>=4 and len(indexed)/len(relevant)>=.65 and (named>=3 or leaders>=3 or multi_refs>=3 or heading):
        return False,'索引条目或目录页码列表'
    bibliographies=sum(bool(BIB_LINE.search(x)) for x in relevant)
    if len(relevant)>=4 and bibliographies/len(relevant)>=.7:
        return False,'纯参考文献列表'
    if all(is_auxiliary_title(x) for x in relevant):
        return False,'无正文的辅助标题'
    return True,''


def audit_book(book):
    invalid=[];count=0;names=set()
    ocr=book.get('quality',{}).get('ocr')
    if ocr is not None:
        expected=ocr.get('expected_pages',[]);validated=ocr.get('validated_pages',[])
        if ocr.get('protocol')==2:
            skipped=ocr.get('skipped_pages',[])
            resolved=validated+skipped
            valid=(ocr.get('status')=='reviewed' and ocr.get('review_confirmed') is True
                   and bool(ocr.get('approved_at')) and bool(expected) and sorted(resolved)==expected
                   and len(set(resolved))==len(resolved))
        else:
            valid=ocr.get('status')=='completed' and bool(expected) and expected==validated and len(set(expected))==len(expected)
        if not valid:
            invalid.append({'reason':'OCR 页码遗漏、重复或人工复核尚未全部确认'})
    for chapter in book.get('chapters',[]):
        if is_auxiliary_title(chapter.get('title','')):
            invalid.append({'chapter':chapter.get('title'),'reason':'辅助章节不应入库'})
        if ocr_failure_reason(chapter.get('text','')):
            invalid.append({'chapter':chapter.get('title'),'reason':'章节正文包含识图失败回复'})
        name=chapter.get('doc_name')
        if name and name in names:invalid.append({'chapter':chapter.get('title'),'reason':'章节文档名重复'})
        names.add(name)
        for leaf,chunk in enumerate(chapter.get('chunks',[])):
            count+=1
            good,reason=useful(chunk)
            if not good:invalid.append({'chapter':chapter.get('title'),'leaf':leaf,'reason':reason,'sample':content_body(chunk)[:180]})
            if chapter.get('start_page') is not None and chapter.get('end_page') is not None:
                pages=[int(p) for p in re.findall(r'\[PDF p=(\d+)\]',content_body(chunk))]
                if any(not chapter['start_page']<=p<=chapter['end_page'] for p in pages):
                    invalid.append({'chapter':chapter.get('title'),'leaf':leaf,'reason':'正文页码超出章节边界'})
    return {'version':3,'checked_chunks':count,'invalid_count':len(invalid),'invalid':invalid}


def clean_pages(pages):
    normalized = [unicodedata.normalize('NFKC', t).replace('\r', '').replace('\x00', '') for t in pages]
    edges = Counter()
    for text in normalized:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        edges.update(set(re.sub(r'\d+', '#', l) for l in lines[:1] + lines[-1:] if len(l) < 100 and not re.search(r'[.!?。！？:：;；]', l)))
    repeated = {x for x, n in edges.items() if n >= max(3, len(pages) * .3)}
    result = []
    for text in normalized:
        lines = text.splitlines()
        nonempty = [i for i,l in enumerate(lines) if l.strip()]
        edge_ids = set(nonempty[:2] + nonempty[-2:])
        cleaned = []
        for i,l in enumerate(lines):
            if i in edge_ids and (re.sub(r'\d+', '#', l.strip()) in repeated or re.fullmatch(r'\s*\d{1,4}\s*', l)):
                continue
            cleaned.append(l.rstrip())
        result.append(re.sub(r'(?<=[A-Za-z])-\n(?=[a-z])', '', '\n'.join(cleaned)).strip())
    return result


def semantic_chunks(text, size=1800, overlap=250):
    """Soft target: preserve paragraph/idea units, split huge paragraphs at sentences.
    Page markers never create a chunk boundary. Hard ceiling is 8000 characters.
    """
    if not 200 <= size <= 12000 or not 0 <= overlap < size:
        raise ValueError('分块大小或重叠无效')
    units = []
    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = re.sub(r'(?<!\n)\n(?!\n|\[PDF)', ' ', paragraph).strip()
        if not paragraph:
            continue
        if len(paragraph) <= max(size * 2, 4000):
            units.append(paragraph)
        else:
            sentences = re.split(r'(?<=[.!?。！？])\s+(?=[A-Z\u3400-\u9fff])|(?<=[。！？])(?=[\u3400-\u9fff])', paragraph)
            for sentence in sentences:
                units.extend(sentence[a:a+8000] for a in range(0, len(sentence), 8000))
    chunks, current, tail = [], '', ''
    for unit in units:
        if current and len(current) + len(unit) + 2 > max(size * 2, 4000):
            chunks.append(current)
            tail = current[-overlap:] if overlap else ''
            current = tail + '\n\n' + unit
        else:
            current = (current + '\n\n' + unit).strip() if current else (tail + '\n\n' + unit).strip()
        if len(current) >= size:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ''
            current = ''
    if current:
        if chunks and len(current) < size // 4 and len(chunks[-1]) + len(current) < 8000:
            chunks[-1] += '\n\n' + current
        else:
            chunks.append(current)
    return chunks
