import asyncio
import json
from types import SimpleNamespace as NS
import pytest
from astrbot_plugin_zlib_knowledge.service import LibraryService
from astrbot_plugin_zlib_knowledge.store import atomic_json
from astrbot_plugin_zlib_knowledge.providers.zlibrary import safe_filename,base_url

class Config(dict):
    def save_config(self):pass

@pytest.fixture
def svc(tmp_path):
    downloads=tmp_path/'downloads';downloads.mkdir()
    context=NS(kb_manager=NS(kb_insts={}))
    return LibraryService(context,Config(download_directory=str(downloads),corpus_directory=str(tmp_path/'chapters')),tmp_path/'state')

def test_secret_is_owner_only_and_never_in_state(svc):
    atomic_json(svc.provider.secret,{'origin':'x','cookies':{'remix_userkey':'SECRET'}},private=True)
    assert svc.provider.secret.stat().st_mode & 0o777==0o600
    assert 'SECRET' not in json.dumps(svc.state())

def test_file_inventory_ignores_symlinks_and_tracks_moved_identical_content(svc,tmp_path):
    d=tmp_path/'downloads';(d/'one.txt').write_text('actual knowledge');(d/'alias.txt').symlink_to(d/'one.txt')
    digest=svc.hash_file(d/'one.txt')
    atomic_json(tmp_path/'chapters'/'book.json',{'schema':1,'book_id':'b','source_sha256':digest,'kb_id':'k','kb_name':'Test','chapters':[{'id':'1'}]})
    rows=asyncio.run(svc.files());assert len(rows)==1 and rows[0]['imported']
    (d/'two.txt').write_text('different knowledge')
    rows=asyncio.run(svc.files());assert not rows[0]['imported'] and rows[1]['imported']

def test_preview_has_no_kb_side_effect_and_cancel_never_imports(svc,tmp_path):
    (tmp_path/'downloads'/'notes.txt').write_text('A useful definition describes a concept clearly. '*50)
    async def scenario():
        row=(await svc.files())[0]
        job=await svc.preview(row['id'])
        await asyncio.gather(*svc.tasks)
        result=svc.store.get('job',job['job_id'])
        assert result['status']=='completed'
        assert result['result']['review_required'] and not svc.previews and not svc.books.all()
        analysis_id=result['result']['analysis_id']
        with pytest.raises(ValueError, match='明确确认'):await svc.review_finalize(analysis_id)
        preview=await svc.review_finalize(analysis_id,confirmed=True)
        assert svc.get_preview(preview['preview_token'])
        assert not svc.store.list('import')
        await svc.close()
    asyncio.run(scenario())

def test_missing_preview_or_both_destinations_rejected(svc):
    with pytest.raises(ValueError):asyncio.run(svc.import_book('invalid','id'))

def test_safe_filename_and_origin_validation():
    assert '/' not in safe_filename('../../x','123','pdf/../exe')
    for url in ['http://example.com','https://user:pass@example.com','https://example.com/path']:
        with pytest.raises(ValueError):base_url(url)

def test_failed_second_chapter_rolls_back_only_new_documents(svc,tmp_path):
    p=tmp_path/'downloads'/'notes.txt';p.write_text('content')
    docs=[];deleted=[]
    class Helper:
        kb=NS(kb_id='k',kb_name='test',description='',embedding_provider_id='e',rerank_provider_id='',chunk_size=1800,chunk_overlap=250)
        async def upload_document(self,**kwargs):
            if docs:raise ValueError('controlled indexing failure')
            docs.append('new-document');return NS(doc_id='new-document',chunk_count=1)
        async def delete_document(self,docid):deleted.append(docid)
    h=Helper();svc.context.kb_manager.kb_insts={'k':h}
    async def get_kb(key):return h
    svc.context.kb_manager.get_kb=get_kb
    async def scenario():
        row=(await svc.files())[0]
        book={'schema':1,'title':'test','book_id':'b','source_sha256':row['sha256'],'chapters':[{'doc_name':'one.txt','title':'one','chunks':['knowledge']},{'doc_name':'two.txt','title':'two','chunks':['more knowledge']}]}
        import time
        svc.previews['token']={'book':book,'created':time.time(),'file_id':row['id']}
        task=await svc.import_book('token','k')
        await asyncio.gather(*svc.tasks)
        assert svc.store.get('job',task['job_id'])['status']=='failed'
        assert deleted==['new-document'] and not svc.books.all()
        await svc.close()
    asyncio.run(scenario())

def test_successful_import_commits_registry_and_rejects_duplicates(svc,tmp_path):
    p=tmp_path/'downloads'/'notes.txt';p.write_text('content')
    class Helper:
        kb=NS(kb_id='k',kb_name='test',description='',embedding_provider_id='e',rerank_provider_id='',chunk_size=1800,chunk_overlap=250)
        async def upload_document(self,**kwargs):return NS(doc_id='new-doc',chunk_count=len(kwargs['pre_chunked_text']))
        async def delete_document(self,docid):raise AssertionError('Successful import must not delete documents')
    h=Helper();svc.context.kb_manager.kb_insts={'k':h}
    async def get_kb(key):return h
    svc.context.kb_manager.get_kb=get_kb
    async def scenario():
        import time
        row=(await svc.files())[0]
        b={'schema':1,'title':'test','book_id':'b','source_sha256':row['sha256'],'chapters':[{'doc_name':'one.txt','title':'one','chunks':['meaningful knowledge']}]}
        for token in ['one','two']:
            svc.previews[token]={'book':b,'created':time.time(),'file_id':row['id']}
        task=await svc.import_book('one','k');await asyncio.gather(*svc.tasks)
        assert svc.store.get('job',task['job_id'])['status']=='completed'
        assert svc.books.all()[0]['chapters'][0]['doc_id']=='new-doc'
        assert (await svc.files())[0]['imported']
        task=await svc.import_book('two','k');await asyncio.gather(*svc.tasks)
        assert svc.store.get('job',task['job_id'])['status']=='failed'
        await svc.close()
    asyncio.run(scenario())

def test_history_never_contains_import_authorization_token(svc,tmp_path):
    (tmp_path/'downloads'/'notes.txt').write_text('A definition describes useful knowledge. '*50)
    async def scenario():
        row=(await svc.files())[0]
        job=await svc.preview(row['id'])
        await asyncio.gather(*svc.tasks)
        stored=svc.store.get('job',job['job_id'])
        assert stored['status']=='completed' and 'preview_token' not in stored['result']
        assert 'preview_token' not in svc.state()['jobs'][0]['result']
        active=svc.preview_result(job['job_id'])
        assert active['review_required'] and 'preview_token' not in active
        approved=await svc.review_finalize(active['analysis_id'],confirmed=True)
        assert svc.get_preview(approved['preview_token'])
        assert 'preview_token' not in svc.preview_result(job['job_id'])
        svc.reviews.invalidate_previews(active['analysis_id'])
        with pytest.raises(ValueError,match='不存在'):svc.get_preview(approved['preview_token'])
        assert svc.store.get('job',job['job_id'])['status']=='completed'
        await svc.close()
    asyncio.run(scenario())


def test_normal_settings_do_not_expose_chunking_parameters(svc):
    assert 'chunk_size' not in svc.state()['settings']
    assert 'chunk_overlap' not in svc.state()['settings']
    assert svc.settings['chunk_size']==1800 and svc.settings['chunk_overlap']==250

def test_search_does_not_load_images_and_details_load_cover_on_demand(tmp_path):
    from test_covers import image_bytes
    calls=[]
    class Provider:
        def __init__(self,*args):pass
        async def search(self,*args):return [{'id':'1','title':'Book','hash':'abc','cover_url':'https://example.com/cover.png','description':'<p>Actual description.</p>'}]
        async def close(self):pass
    svc=LibraryService(NS(kb_manager=NS(kb_insts={})),Config(download_directory=str(tmp_path)),tmp_path/'state',provider_factory=Provider)
    async def scenario():
        result=await svc.search('Book')
        assert result['books'][0]['has_cover'] and 'cover_url' not in result['books'][0]
        assert not svc.covers.items and not calls
        async def ensure():pass
        async def load(url):calls.append(url);return image_bytes()
        async def close():pass
        svc.browser=NS(ready=True,ensure=ensure,cover_bytes=load,close=close)
        detail=await svc.book_details('1')
        assert detail['cover'].startswith('data:image/jpeg;base64,')
        assert detail['description']=='Actual description.' and 'hash' not in detail
        await svc.book_details('1');assert len(calls)==1
        await svc.close()
    asyncio.run(scenario())


def test_import_quality_gate_rejects_index_before_any_database_write(svc,tmp_path):
    p=tmp_path/'downloads'/'book.txt';p.write_text('source')
    async def scenario():
        import time
        row=(await svc.files())[0]
        b={'schema':1,'title':'Book','book_id':'b','source_sha256':row['sha256'],'chapters':[{'title':'Name Index','doc_name':'index.txt','chunks':['Bandura, A., 38']} ]}
        svc.previews['bad']={'book':b,'created':time.time(),'file_id':row['id']}
        svc.context.kb_manager.kb_insts={'k':NS(kb=NS(kb_id='k',kb_name='test',description='',embedding_provider_id='e',rerank_provider_id='',chunk_size=1800,chunk_overlap=250))}
        job=await svc.import_book('bad','k')
        await asyncio.gather(*svc.tasks)
        result=svc.store.get('job',job['job_id'])
        assert result['status']=='failed' and '逐块校验' in result['message']
        assert not svc.store.list('import') and not svc.books.all()
        await svc.close()
    asyncio.run(scenario())
