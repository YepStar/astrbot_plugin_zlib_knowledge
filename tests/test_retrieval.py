import importlib.util
import json
from pathlib import Path

from astrbot_plugin_zlib_knowledge import retrieval as b

def fixture(tmp_path):
    chapter={'id':'c01','number':1,'title':'first','start_page':1,'end_page':2,
             'doc_id':'one','text':'[PDF p=1]aaa\n[PDF p=2]bbb','chunks':['a','b','c','d']}
    other=dict(chapter,id='c02',number=2,title='second',doc_id='two',chunks=['secret'])
    book={'schema':1,'book_id':'book','title':'test','edition':'test edition','kb_id':'allowed','kb_name':'allowed','chapters':[chapter,other]}
    (tmp_path/'book.json').write_text(json.dumps(book));return b.Books(tmp_path),book


def test_expansion_gets_neighbors_but_never_crosses_chapter(tmp_path):
    books,_=fixture(tmp_path)
    data={'results':[{'doc_id':'one','chunk_index':3,'content':'d'}]}
    r=books.expand(data)['results'][0]
    assert r['chapter_context']['leaf_indices']==[2,3]
    assert 'secret' not in r['content']
    assert not r['chapter_context']['complete_chapter']


def test_overlapping_hits_are_not_returned_twice(tmp_path):
    books,_=fixture(tmp_path)
    rows=[{'doc_id':'one','chunk_index':1,'content':'b'},{'doc_id':'one','chunk_index':2,'content':'c'}]
    r=books.expand({'results':rows})
    assert len(r['results'])==1
    assert r['results'][0]['chapter_context']['leaf_indices']==[0,1,2]


def test_unmanaged_documents_keep_content(tmp_path):
    books,_=fixture(tmp_path)
    row={'doc_id':'unmanaged','content':'original'}
    assert books.expand({'results':[row]})['results']==[row]


def test_chapter_parts_cover_complete_raw_text(tmp_path):
    books,book=fixture(tmp_path)
    result=[];part=1
    while True:
        r=books.read(book,'1',part,part_chars=8)
        assert r['complete'] is False
        result.append(r['content'].split('\n\n',1)[1])
        if not r['next_part']:break
        part=r['next_part']
    assert ''.join(result)==book['chapters'][0]['text']
    assert books.read(book,'unknown')['status']=='needs_chapter'
    assert books.read(book,'1',999,part_chars=8)['status']=='invalid_part'


def test_empty_session_permission_does_not_inherit_global(tmp_path):
    _,book=fixture(tmp_path)
    assert b.authorized_books([book],{'kb_ids':[]},['allowed'])==[]
    assert b.authorized_books([book],{'kb_ids':['other']},['allowed'])==[]
    assert b.authorized_books([book],{},['allowed'])==[book]
