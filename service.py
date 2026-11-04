"""Application orchestration. Provider, extraction, ledger and native KB stay independent."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import time
import uuid
from pathlib import Path
from .store import Store, atomic_json
from .providers.zlibrary import ZLibrary, base_url
from .ingestion.extract import extract
from .ingestion.quality import audit_book
from .ingestion.review import AnalysisCache
from .matching import recommend
from .retrieval import Books
from .browser_session import BrowserSession
from .covers import CoverCache
from .presentation import plain_description

DEFAULTS = {'base_url':'https://z-library.sk', 'download_directory':'/AstrBot/data/books',
            'corpus_directory':'/AstrBot/data/knowledge_base/chapter_sources', 'chunk_size':1800,'chunk_overlap':250,
            'max_download_mb':512,'ocr_provider_id':''}
SUPPORTED = {'.pdf','.epub','.txt','.md','.mobi','.azw3','.djvu','.fb2'}


class LibraryService:
    def __init__(self, context, config, root, provider_factory=ZLibrary, extractor=extract):
        self.context, self.config = context, config
        self.settings = {k:config.get(k,v) for k,v in DEFAULTS.items()}
        self.store = Store(root)
        self.reviews = AnalysisCache(root,self.store)
        for job in self.store.list('job'):
            if isinstance(job.get('result'),dict) and 'preview_token' in job['result']:
                job['result'].pop('preview_token')
                self.store.put('job',job['id'],job)
        self.provider = provider_factory(self.settings,root)
        self.browser = BrowserSession(self.settings,root,self.provider) if isinstance(self.provider,ZLibrary) else None
        self.provider.browser = self.browser
        self.extractor = extractor
        self.books = Books(self.settings['corpus_directory'])
        self.tasks = set()
        self.import_lock = asyncio.Lock()
        self.download_lock = asyncio.Lock()
        self.previews = {}
        self.preview_jobs = {}
        self.covers = CoverCache()

    def bases(self):
        return [{k:getattr(h.kb,k) for k in ('kb_id','kb_name','description','embedding_provider_id','rerank_provider_id','chunk_size','chunk_overlap')} for h in self.context.kb_manager.kb_insts.values()]

    def state(self):
        jobs=self.store.list('job')[:40]
        for job in jobs:
            if isinstance(job.get('result'),dict):job['result'].pop('preview_token',None)
        return {'settings':{k:v for k,v in self.settings.items() if k not in ('chunk_size','chunk_overlap')},'session_saved':self.provider.configured(),'browser':self.browser.state() if self.browser else {},'knowledge_bases':self.bases(),
                'jobs':jobs, 'analyses':self.reviews.list(), 'chapter_books':[{'id':b['book_id'],'title':b['title'],'kb_id':b['kb_id'],'chapters':len(b['chapters'])} for b in self.books.all()]}

    async def save_settings(self, body):
        if self.tasks:
            raise ValueError('有任务运行中，请完成后再更改设置')
        if set(body) - set(DEFAULTS) or 'corpus_directory' in body:
            raise ValueError('配置项无效；章节目录由部署配置管理，防止丢失已有资料')
        value = dict(self.settings, **body)
        value['base_url'] = base_url(value['base_url'])
        p = Path(value['download_directory']).expanduser()
        if not p.is_absolute() or not p.is_dir() or p.is_symlink():
            raise ValueError('下载目录必须是已挂载、存在的绝对目录')
        for k in ('chunk_size','chunk_overlap','max_download_mb'):
            if type(value[k]) is not int:
                raise ValueError(k+' 必须为整数')
        if not 200 <= value['chunk_size'] <= 12000 or not 0 <= value['chunk_overlap'] < value['chunk_size'] or not 1 <= value['max_download_mb'] <= 2048:
            raise ValueError('分块或下载上限超出范围')
        value['download_directory'] = str(p)
        old = dict(self.config)
        try:
            self.config.update(value); self.config.save_config()
        except Exception:
            self.config.clear(); self.config.update(old)
            raise ValueError('配置保存失败') from None
        if self.browser and value['base_url']!=self.settings['base_url']:
            await self.browser.close()
        self.settings.clear(); self.settings.update(value)
        return {'ok':True}

    async def files(self):
        return await asyncio.to_thread(self._files)

    def _files(self):
        root = Path(self.settings['download_directory'])
        if not root.is_dir():
            raise ValueError('下载目录在容器中不可见，请检查挂载')
        imports = self.books.all()
        result = []
        for p in root.iterdir():
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in SUPPORTED or p.name.startswith('.'):
                continue
            st = p.stat()
            key = hashlib.sha256(str(p).encode()).hexdigest()[:24]
            fingerprints = self.store.get('fingerprint', key, {})
            if fingerprints.get('size')==st.st_size and fingerprints.get('mtime')==st.st_mtime_ns:
                digest = fingerprints['sha256']
            else:
                digest = self.hash_file(p)
                self.store.put('fingerprint',key,{'size':st.st_size,'mtime':st.st_mtime_ns,'sha256':digest})
            destinations = [{'kb_id':b['kb_id'],'kb_name':b['kb_name']} for b in imports if b.get('source_sha256')==digest]
            downloaded = self.store.get('download',p.name,{})
            result.append({'id':key,'name':p.name,'title':downloaded.get('title') or p.stem,'author':downloaded.get('author',''),
                           'size':st.st_size,'modified':st.st_mtime,'sha256':digest,'imported':bool(destinations),'destinations':destinations,
                           'importable':p.suffix.lower() in {'.pdf','.epub','.txt','.md'}})
        return sorted(result,key=lambda x:(x['imported'],-x['modified'],x['name']))

    @staticmethod
    def hash_file(path):
        h = hashlib.sha256()
        with Path(path).open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):
                h.update(block)
        return h.hexdigest()

    async def file(self, key):
        row = next((x for x in await self.files() if x['id']==key),None)
        if not row:
            raise ValueError('文件不存在或目录已变化，请刷新书架')
        return row,Path(self.settings['download_directory'])/row['name']

    async def prepare_browser(self):
        if self.browser:
            await self.browser.ensure()
            if not self.browser.ready:
                await self.browser.check_ready()

    async def search(self, query, extension='', page=1):
        await self.prepare_browser()
        books = await self.provider.search(query,extension,page)
        if self.browser:
            await self.browser.save_session()
            self.browser.ready=True
            self.browser.message='浏览器会话与图书搜索均已验证。'
        # Persist only identifiers/metadata; never arbitrary download URLs or credentials.
        for b in books:
            self.store.put('search',b['id'],b)
        public_books=[]
        for book in books:
            public={**{k:v for k,v in book.items() if k!='cover_url'},'has_cover':bool(book.get('cover_url'))}
            public['description']=plain_description(public.get('description',''))
            public_books.append(public)
        return {'books':public_books,'page':page,'query':query}

    async def book_details(self,book_id):
        book=self.store.get('search',str(book_id))
        if not book:raise ValueError('请刷新搜索结果后再查看详情')
        details={k:v for k,v in book.items() if k not in ('cover_url','hash')}
        details['description']=plain_description(details.get('description',''))
        details.update(cover='',cover_error='')
        url=book.get('cover_url')
        if not url:
            details['cover_error']='该版本暂未提供封面'
            return details
        try:
            await self.prepare_browser()
            details['cover']=await self.covers.get(url,self.browser.cover_bytes)
        except ValueError as e:details['cover_error']=str(e)
        return details

    def launch(self, kind, coro_factory):
        jobid = uuid.uuid4().hex
        job = {'id':jobid,'kind':kind,'status':'pending','message':'排队中','created':time.time()}
        self.store.put('job',jobid,job)
        async def report(message, **fields):
            job.update(message=message,**fields)
            self.store.put('job',jobid,job)
        async def runner():
            try:
                await report('处理中',status='running')
                result = await coro_factory(report,jobid)
                if kind=='preview':
                    result=dict(result);result.pop('preview_token',None)
                await report('分析完成，等待人工审核' if result.get('review_required') else '完成',status='completed',result=result)
            except asyncio.CancelledError:
                await report('任务被重载中断，请检查书架或导入记录',status='interrupted')
                raise
            except Exception as e:
                # Known errors deliberately contain no remote response or credential content.
                message = str(e) if isinstance(e,ValueError) else '任务失败，请检查脱敏日志或重新分析'
                await report(message[:700],status='failed')
                from astrbot.api import logger
                logger.warning('z-lib task %s failed (%s)',jobid,type(e).__name__)
        task = asyncio.create_task(runner())
        self.tasks.add(task);task.add_done_callback(self.tasks.discard)
        return {'job_id':jobid}

    def download(self, bookid):
        book = self.store.get('search',str(bookid))
        if not book:
            raise ValueError('请先搜索，再用结果中的书籍ID下载')
        async def work(report,jobid):
            async with self.download_lock:
                await self.prepare_browser()
                last = 0
                async def progress(count,total):
                    nonlocal last
                    if time.monotonic()-last > 1:
                        await report(f'已下载 {count//1024} KB',bytes=count,total=total)
                        last=time.monotonic()
                result = await self.provider.download(book,self.settings['download_directory'],progress)
                self.store.put('download',result['name'],result)
                return result
        return self.launch('download',work)

    async def preview(self, key, title='', edition='', chapter_map=None):
        row,path = await self.file(key)
        if row['size'] > int(self.settings['max_download_mb']) * 1024 * 1024:
            raise ValueError('本地文件超过配置的单本处理上限，请先调整设置')
        async def work(report,jobid):
            if self.extractor is extract:
                detail=await self.reviews.analyze(path,row,title.strip() or row['title'],edition.strip() or '未注明版本',chapter_map,self.context,self.settings,report)
                latest,_=await self.file(key)
                if latest['sha256']!=row['sha256']:
                    raise ValueError('分析期间源文件发生变化，缓存保留但禁止导入；请重新选择源文件')
                return {'review_required':True,'analysis_id':detail['id'],'title':detail['title'],
                        'processed_count':detail['processed_count'],'unresolved_count':detail['unresolved_count']}
            book = await self.extractor(path,title.strip() or row['title'],edition.strip() or '未注明版本',chapter_map,self.context,self.settings,report)
            if book['source_sha256'] != row['sha256']:
                raise ValueError('分析过程中文件发生变化，请重新分析')
            token = uuid.uuid4().hex
            self.previews[token] = {'book':book,'created':time.time(),'file_id':key}
            self.preview_jobs[jobid]=token
            if len(self.previews)>10:
                self.previews.pop(next(iter(self.previews)))
            info = recommend(book['title'],row['author'],'\n'.join(c['title']+' '+c['text'][:1800] for c in book['chapters']),self.bases())
            return {'preview_token':token,'title':book['title'],'chapters':[{'id':c['id'],'title':c['title'],'start_page':c['start_page'],'end_page':c['end_page'],'chunks':len(c['chunks'])} for c in book['chapters']],
                    'chunk_count':sum(len(c['chunks']) for c in book['chapters']),'quality':book['quality'],
                    'samples':[c['chunks'][0][:1600] for c in book['chapters'][:3]],**info}
        return self.launch('preview',work)

    def preview_result(self,job_id):
        job=self.store.get('job',job_id,{})
        if job.get('status')!='completed':raise ValueError('分析尚未完成')
        if job.get('result',{}).get('analysis_id'):
            self.reviews.meta(job['result']['analysis_id'])
            return job['result']
        token=self.preview_jobs.get(job_id)
        preview=self.previews.get(token)
        if not preview or time.time()-preview['created']>3600:
            raise ValueError('旧版预览已过期，请打开持久分析缓存；任务记录只能查看')
        return dict(job['result'],preview_token=token)

    def get_preview(self,token):
        persisted=self.reviews.get_preview(token)
        preview=persisted or self.previews.get(token)
        if preview and preview.get('analysis_id'):
            if persisted is None:
                raise ValueError('审核预览已失效，请重新确认审核结果')
            row=self.reviews.meta(preview['analysis_id'])
            if row['status']!='ready' or row.get('approved_at')!=preview.get('approved_at'):
                raise ValueError('审核状态已变化，请重新确认')
        elif preview and time.time()-preview['created']>3600:
            preview=None
        if preview is None:
            raise ValueError('导入预览不存在，请从分析缓存确认审核结果')
        return preview

    async def review_path(self,key):
        meta=self.reviews.meta(key)
        row,path=await self.file(meta['file_id'])
        if row['sha256']!=meta['source_sha256']:
            raise ValueError('源文件已变化，不能复用本次分析缓存')
        return row,path

    async def review_page(self,key,page):
        _,path=await self.review_path(key)
        return await self.reviews.page(key,page,path)

    async def review_resolve(self,key,page,action,text='',note=''):
        await self.review_path(key)
        return await self.reviews.resolve(key,page,action,text,note)

    async def review_retry(self,key,page):
        _,path=await self.review_path(key)
        provider=self.settings.get('ocr_provider_id','').strip()
        async def work(report,jobid):
            return await self.reviews.retry(key,page,path,provider,self.context,report)
        return self.launch('ocr_retry',work)

    async def review_resume(self,key):
        meta=self.reviews.meta(key)
        await self.review_path(key)
        if meta['status'] not in ('interrupted','review'):
            raise ValueError('当前缓存无需继续处理')
        return await self.preview(meta['file_id'],meta['title'],meta['edition'],meta.get('chapter_map'))

    async def review_finalize(self,key,confirmed=False):
        if confirmed is not True:
            raise ValueError('请明确确认全部失败页已复核')
        row,path=await self.review_path(key)
        book=await self.reviews.finalize(key,path,self.settings)
        meta=self.reviews.meta(key)
        self.reviews.invalidate_previews(key)
        token=uuid.uuid4().hex
        preview={'book':book,'created':time.time(),'file_id':row['id'],'analysis_id':key,'approved_at':meta['approved_at']}
        self.reviews.save_preview(token,preview)
        info=recommend(book['title'],row['author'],'\n'.join(c['title']+' '+c['text'][:1800] for c in book['chapters']),self.bases())
        return {'preview_token':token,'title':book['title'],'file_id':row['id'],'file_name':row['name'],'edition':book['edition'],
                'chapters':[{'id':c['id'],'title':c['title'],'start_page':c['start_page'],'end_page':c['end_page'],'chunks':len(c['chunks'])} for c in book['chapters']],
                'chunk_count':sum(len(c['chunks']) for c in book['chapters']),'quality':book['quality'],
                'samples':[c['chunks'][0][:1600] for c in book['chapters'][:3]],**info}

    @asynccontextmanager
    async def import_review_guard(self,token):
        preview=self.get_preview(token)
        key=preview.get('analysis_id')
        if key:
            async with self.reviews.lock(key):
                self.get_preview(token)
                meta=self.reviews.meta(key)
                meta.update(status='importing',message='已确认，正在导入知识库')
                self.reviews.save_meta(meta)
        try:
            yield
        finally:
            if key:
                meta=self.reviews.meta(key)
                if meta['status']=='importing':
                    meta.update(status='ready',message='导入未完成，审核缓存保留，可重试')
                    self.reviews.save_meta(meta)

    async def import_book(self, token, kb_id='', create=None):
        preview = self.get_preview(token)
        if bool(kb_id)==bool(create):
            raise ValueError('请选择已有知识库或填写新知识库，二者选一')
        if not create and not any(b['kb_id']==kb_id for b in self.bases()):
            raise ValueError('选择的知识库不存在')
        if create and (not str(create.get('kb_name','')).strip() or not create.get('embedding_provider_id')):
            raise ValueError('新知识库需要名称和嵌入模型')
        book = preview['book']
        async def work(report,jobid):
            async with self.import_lock, self.import_review_guard(token):
                row,path=await self.file(preview['file_id'])
                if row['sha256']!=book['source_sha256']:
                    raise ValueError('文件已变化，请重新分析')
                audit=await asyncio.to_thread(audit_book,book)
                if audit['invalid_count']:
                    raise ValueError('入库前逐块校验发现无效内容，请重新分析；尚未写入知识库')
                manager=self.context.kb_manager
                helper=await manager.get_kb(kb_id) if kb_id else None
                if create:
                    helper=await manager.create_kb(kb_name=str(create['kb_name']).strip()[:100],description=str(create.get('description',''))[:2000],
                       embedding_provider_id=create['embedding_provider_id'],rerank_provider_id=create.get('rerank_provider_id') or None,
                       chunk_size=self.settings['chunk_size'],chunk_overlap=self.settings['chunk_overlap'])
                if helper is None:
                    raise ValueError('知识库不可用')
                dest=Path(self.settings['corpus_directory'])/(book['book_id']+'-'+helper.kb.kb_id+'.json')
                if any(b['source_sha256']==book['source_sha256'] and b['kb_id']==helper.kb.kb_id for b in self.books.all()):
                    raise ValueError('本书已在目标知识库，拒绝重复导入')
                journalkey=book['book_id']+':'+helper.kb.kb_id
                journal=self.store.get('import',journalkey,{})
                if journal.get('status')!='completed':
                    for docid in journal.get('doc_ids',[]):
                        await helper.delete_document(docid)
                journal={'status':'running','doc_ids':[],'kb_id':helper.kb.kb_id,'book_id':book['book_id'],'job_id':jobid}
                self.store.put('import',journalkey,journal)
                # Own source copy. Legacy sources and existing KB documents are never modified.
                imported=json.loads(json.dumps(book));imported.update(kb_id=helper.kb.kb_id,kb_name=helper.kb.kb_name,ingested_at=time.time())
                analysis_meta=self.reviews.meta(preview['analysis_id']) if preview.get('analysis_id') else None
                if analysis_meta:
                    analysis_meta.update(status='importing',message='已人工确认，正在写入知识库')
                    self.reviews.save_meta(analysis_meta)
                published = False
                try:
                    for i,c in enumerate(imported['chapters']):
                        await report(f"入库 {i+1}/{len(imported['chapters'])}：{c['title']}")
                        async def callback(stage,current,total):
                            await report(f"{c['title']} · {stage}",current=current,total=total)
                        doc=await helper.upload_document(file_name=c['doc_name'],file_content=None,file_type='txt',pre_chunked_text=c['chunks'],batch_size=32,tasks_limit=2,max_retries=2,progress_callback=callback)
                        c['doc_id']=doc.doc_id
                        journal['doc_ids'].append(doc.doc_id);self.store.put('import',journalkey,journal)
                        if doc.chunk_count!=len(c['chunks']):
                            raise ValueError('章节索引数量校验不一致')
                    atomic_json(dest,imported)
                    published = True
                    journal['status']='completed';self.store.put('import',journalkey,journal)
                except BaseException:
                    if published:
                        # Registry publication is the commit point. Never remove committed
                        # chapter documents merely because the auxiliary ledger write failed.
                        raise
                    failures=[]
                    for docid in journal['doc_ids']:
                        try:
                            await asyncio.shield(helper.delete_document(docid))
                        except Exception:
                            failures.append(docid)
                    journal.update(status='needs_cleanup' if failures else 'rolled_back',doc_ids=failures)
                    self.store.put('import',journalkey,journal)
                    raise
                finally:
                    if analysis_meta:
                        analysis_meta.update(status='imported' if published else 'ready',message='已导入知识库' if published else '入库未完成，审核缓存保留，可重试')
                        self.reviews.save_meta(analysis_meta)
                if preview.get('analysis_id'):
                    self.reviews.invalidate_previews(preview['analysis_id'])
                self.previews.pop(token,None)
                return {'book_id':book['book_id'],'kb_id':helper.kb.kb_id,'kb_name':helper.kb.kb_name,'chapters':len(imported['chapters']),'chunks':sum(len(c['chunks']) for c in imported['chapters'])}
        return self.launch('import',work)

    async def close(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.browser:
            await self.browser.close()
        await self.provider.close()
