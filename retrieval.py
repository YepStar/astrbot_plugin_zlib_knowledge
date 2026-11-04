"""Integrated legacy-compatible chapter reading and bounded retrieval expansion."""
import json
import re
from pathlib import Path
from .ingestion.extract import chapter_header

def pdf_pages(text):
    return sorted({int(x) for x in re.findall(r"\[PDF p=(\d+)\]", text)})

class Books:
    def __init__(self, directory):
        self.directory = Path(directory)

    def all(self):
        result = []
        for path in self.directory.glob('*.json'):
            try:
                book = json.loads(path.read_text())
                if book.get('schema') == 1 and book.get('kb_id') and book.get('chapters'):
                    result.append(book)
            except (ValueError, OSError):
                continue
        return result

    def choose(self, books, selector=''):
        if not selector:
            return books[0] if len(books) == 1 else None
        matches = [b for b in books if selector.casefold() in (b['title']+' '+b['book_id']).casefold()]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def chapter(book, selector):
        selector = str(selector).strip()
        matches = [c for c in book['chapters'] if selector == c['id'] or selector == str(c['number']) or selector.casefold() in c['title'].casefold()]
        return matches[0] if len(matches) == 1 else None

    def read(self, book, selector='', part=1, part_chars=50000):
        if not selector:
            return {'status': 'contents', 'book': book['title'], 'chapters': [{k:c[k] for k in ('id','number','title','start_page','end_page')} for c in book['chapters']]}
        chapter = self.chapter(book, selector)
        if chapter is None:
            return {'status': 'needs_chapter', 'message':'章节名称不明确，请先读取目录。'}
        # Non-overlapping parts preserve the whole chapter, including very long ones.
        text = chapter['text']
        count = max(1, (len(text)+part_chars-1)//part_chars)
        if isinstance(part,float) and part.is_integer():part=int(part)
        if type(part) is not int or not 1 <= part <= count:
            return {'status':'invalid_part', 'parts':count}
        offset=(part-1)*part_chars
        content = text[offset:part*part_chars]
        previous=pdf_pages(text[:offset])
        return {'status':'chapter', 'book':book['title'], 'edition':book['edition'], 'chapter':chapter['id'],
                'title':chapter['title'], 'pdf_pages':[chapter['start_page'],chapter['end_page']],
                'part_start_pdf_page':previous[-1] if previous else chapter['start_page'],
                'part':part, 'parts':count, 'is_last_part':part == count, 'complete':count == 1, 'next_part':part+1 if part<count else None,
                'instruction':'只有读取所有parts后才能声称读完整章。用chapter_knowledge_read继续，不要凭首段总结整章。',
                'content':chapter_header(book,chapter)+content}

    def expand(self, result, budget=24000, neighbors=1):
        if not result or not result.get('results'):
            return result
        mapping = {c['doc_id']:(b,c) for b in self.all() for c in b['chapters'] if c.get('doc_id')}
        expanded = []; covered = set(); used = 0
        for row in result['results']:
            pair = mapping.get(row.get('doc_id'))
            if not pair or not 0 <= int(row.get('chunk_index',-1)) < len(pair[1]['chunks']):
                expanded.append(row)
                used += len(row.get('content',''))
                continue
            book, chapter = pair
            center = int(row['chunk_index'])
            if (row['doc_id'],center) in covered:
                continue
            # Keep every unrepresented hit; only add neighbors within a bounded budget.
            ids = [center]
            for n in [center-1,center+1] if neighbors else []:
                if 0 <= n < len(chapter['chunks']) and (row['doc_id'],n) not in covered:
                    if used+sum(len(chapter['chunks'][i]) for i in ids)+len(chapter['chunks'][n]) <= budget:
                        ids.append(n)
            ids.sort()
            for n in ids:covered.add((row['doc_id'],n))
            bodies = [re.split(r'Leaf: \d+; PDF pages: \d+-\d+\n', chapter['chunks'][n], maxsplit=1)[-1] for n in ids]
            merged = bodies[0]
            for body in bodies[1:]:
                merged += body[250:] if len(merged)>=250 and merged[-250:]==body[:250] else '\n\n'+body
            content = chapter_header(book,chapter)+'命中与邻近片段（非整章）：\n'+merged
            row = dict(row, content=content, char_count=len(content),
                       chapter_context={'book':book['title'],'chapter':chapter['id'], 'leaf_indices':ids,
                                        'complete_chapter':False, 'tool':'chapter_knowledge_read'})
            expanded.append(row);used += len(content)
        output = dict(result, results=expanded)
        output['context_text'] = '\n\n'.join(f"【知识 {i}】{r.get('kb_name','')} / {r.get('doc_name','')}\n{r['content']}" for i,r in enumerate(expanded,1))
        return output


def authorized_books(books, session, fallback_names):
    if session and 'kb_ids' in session:
        return [b for b in books if b['kb_id'] in session.get('kb_ids', [])]
    return [b for b in books if b['kb_name'] in fallback_names]
