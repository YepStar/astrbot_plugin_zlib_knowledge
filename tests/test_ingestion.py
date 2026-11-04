import asyncio
from pathlib import Path
import pytest
from astrbot_plugin_zlib_knowledge.ingestion.quality import useful,clean_pages,semantic_chunks
from astrbot_plugin_zlib_knowledge.ingestion.extract import build_book,validate_map,read_pdf,read_epub
from astrbot_plugin_zlib_knowledge.matching import recommend

INDEX='NI-2 Name Index\nAu, T.K., 127\nAugustinos, M., 9\nAumann, K., 57\nAurelius, M., 15\nAustin, J. B., 271\nAverill, J., 310\nAxsom, D., 172\nB\nBaker, L. R., 377\n324, 325'

def test_name_index_from_screenshot_is_rejected():
    assert useful(INDEX)[0] is False

def test_short_definition_and_explanatory_examples_are_kept():
    for text in ['自我效能感：个体对自己能够完成任务的能力判断。','E = mc² describes mass–energy equivalence.','1. 先区分事实与假设。\n2. 检查是否存在其他解释。','相关不意味着因果。']:
        assert useful(text)[0],text

def test_pure_numbers_and_reference_list_rejected():
    assert not useful('1\n2\n3\n4')[0]
    refs='\n'.join(f'Smith, A. (2020). Study {i}. https://doi.org/xxx' for i in range(8))
    assert not useful(refs)[0]

def test_headers_removed_but_body_retained():
    pages=[f'Social Psychology\n{i}\nA meaningful description of social influence on page {i}.\nFooter' for i in range(5)]
    cleaned=clean_pages(pages)
    assert all('Social Psychology' not in t and 'Footer' not in t and 'meaningful description' in t for t in cleaned)

def test_cross_page_idea_not_split_at_physical_page(tmp_path):
    p=tmp_path/'b.pdf';p.write_bytes(b'test')
    raw={'format':'pdf','pages':['社会认同是个体从群体成员身份中获得的','自我概念，它影响判断和行为。','其他章节不应混入此章。',INDEX],
         'chapters':[{'title':'社会认同','start_page':1},{'title':'第二章','start_page':3},{'title':'Name Index','start_page':4}]}
    b=build_book(p,'教材','1',raw)
    assert len(b['chapters'])==2
    first=b['chapters'][0]['chunks'][0]
    assert '获得的' in first and '自我概念' in first and '[PDF p=2]' in first
    assert '其他章节' not in first
    assert not any('Name Index' in c['title'] for c in b['chapters'])
    assert b['quality']['removed']

def test_semantic_overlap_and_no_unbounded_chunk():
    text='\n\n'.join(f'Unit {i}: '+'This is an explanatory sentence. '*40 for i in range(15))
    chunks=semantic_chunks(text,1800,250)
    assert len(chunks)>2 and all(len(x)<8001 for x in chunks)
    for a,b in zip(chunks,chunks[1:]):
        assert b.startswith(a[-250:])
    for i in range(15):assert f'Unit {i}:' in '\n'.join(chunks)

def test_chapter_map_requires_ordered_valid_physical_pages():
    assert validate_map([{'title':'first','start_page':3}],10)[0]['start_page']==1
    for mapping in [[{'title':'bad','start_page':0}],[{'title':'a','start_page':5},{'title':'b','start_page':2}],[{'title':'x','start_page':11}]]:
        with pytest.raises(ValueError):validate_map(mapping,10)

def test_matching_ambiguity_does_not_select():
    bases=[{'kb_id':'1','kb_name':'冷月·社会心理学'},{'kb_id':'2','kb_name':'冷月·社会心理研究'}]
    assert recommend('Social Psychology','', '',bases)['recommended_kb_id']==''
    assert recommend('Social Psychology','', '',bases[:1])['recommended_kb_id']=='1'
    assert recommend('Cooking recipes','', '',bases)['recommended_kb_id']==''

def test_epub_spine_fragments_merge_under_toc_chapter(tmp_path):
    import zipfile
    p=tmp_path/'b.epub'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
        z.writestr('book.opf','<package><manifest><item id="nav" href="nav.html" properties="nav"/><item id="a" href="a.html"/><item id="b" href="b.html"/><item id="c" href="c.html"/></manifest><spine><itemref idref="a"/><itemref idref="b"/><itemref idref="c"/></spine></package>')
        z.writestr('nav.html','<nav><a href="a.html">第一章</a><a href="c.html">第二章</a></nav>')
        z.writestr('a.html','<html><body><h1>第一章</h1><p>第一部分知识</p></body></html>')
        z.writestr('b.html','<html><body><p>续写知识</p></body></html>')
        z.writestr('c.html','<html><body><h1>第二章</h1><p>新知识</p></body></html>')
    raw=read_epub(p)
    assert [c['start_page'] for c in raw['chapters']]==[1,3]
    b=build_book(p,'book','1',raw)
    assert '续写知识' in b['chapters'][0]['text']

def test_generated_source_header_does_not_hide_meaningless_body():
    prefix='Book: Myers Social Psychology\nEdition: Eighth Canadian Edition\nChapter: s22 | Name Index\nPDF chapter pages: 587-606\n页码是PDF物理页码；以下为资料。\n\nLeaf: 2; PDF pages: 587-588\n'
    assert useful(prefix+INDEX)[0] is False
    assert useful(prefix+'Bandura, A., 38')[0] is False
    assert useful('Book: a\nEdition: b\nChapter: c | Concepts\nLeaf: 0; PDF pages: 1-1\n')[0] is False


def test_statistical_sample_size_table_is_not_mistaken_for_index():
    assert useful('Study sample sizes\nControl group 120\nTreatment group 100\nFollow-up group 90\nReplication group 140')[0]


def test_preface_is_excluded_and_every_decorated_leaf_is_audited(tmp_path):
    from astrbot_plugin_zlib_knowledge.ingestion.quality import audit_book
    p=tmp_path/'source.pdf';p.write_bytes(b'source')
    raw={'format':'pdf','pages':['Welcome to the eighth edition. Thanks to the authors.','社会认同是个体从群体身份中获得的自我概念。',INDEX], 'chapters':[{'title':'Preface','start_page':1},{'title':'Social identity','start_page':2},{'title':'Name Index','start_page':3}]}
    book=build_book(p,'Book','1',raw)
    assert [c['title'] for c in book['chapters']]==['Social identity']
    assert book['quality']['version']==3 and book['quality']['audit']['invalid_count']==0
    book['chapters'][0]['chunks'].append('Book: Book\nEdition: 1\nChapter: s22 | Name Index\nLeaf: 0; PDF pages: 3-3\n'+INDEX)
    assert audit_book(book)['invalid_count']>0

def test_numbered_chapters_do_not_count_removed_sections_or_part_introductions(tmp_path):
    p=tmp_path/'book.pdf';p.write_bytes(b'book')
    raw={'format':'pdf','pages':['Publisher welcome.','第一章的具体理论。','Part two introduces new topics.','第二章的实验结果。','归属感：个体感到自己被群体接纳。'],'chapters':[{'title':'Preface','start_page':1},{'title':'First theory','start_page':2},{'title':'Introduction','start_page':3},{'title':'Second theory','start_page':4},{'title':'Glossary','start_page':5}]}
    book=build_book(p,'Book','1',raw)
    assert [(c['title'],c['number']) for c in book['chapters']]==[('First theory',1),('Introduction',None),('Second theory',2),('Glossary',None)]

def test_index_title_variants_are_rejected_but_real_indexing_topic_is_kept():
    from astrbot_plugin_zlib_knowledge.ingestion.quality import is_auxiliary_title
    for title in ['Index','NameIndex','SUBJECT INDEX','NI-2 Name Index','Index / 索引']:
        assert is_auxiliary_title(title),title
    assert not is_auxiliary_title('Indexing and retrieval methods')
