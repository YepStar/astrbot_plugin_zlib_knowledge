"""Dashboard-authenticated API only, served through AstrBot's plugin-page bridge."""
from quart import jsonify, request


class WebAPI:
    def __init__(self, plugin):
        self.plugin = plugin
        self.service = plugin.service

    def register(self):
        for name, methods in [('browser-start',['POST']),('browser-frame',['POST']),('browser-input',['POST']),('browser-finish',['POST']),('browser-close',['POST']),('browser-setup',['POST']),('preview-result',['POST']),('review-detail',['POST']),('review-page',['POST']),('review-save',['POST']),('review-retry',['POST']),('review-resume',['POST']),('review-finalize',['POST']),('review-reopen',['POST']),('review-delete',['POST']),('book-details',['POST']),('state',['GET']),('files',['GET']),('settings',['POST']),('login',['POST']),('logout',['POST']),('session',['POST']),('search',['POST']),('download',['POST']),('preview',['POST']),('import',['POST'])]:
            async def view(_name=name):
                try:
                    data = {} if request.method=='GET' else await request.get_json()
                    if not isinstance(data,dict):
                        raise ValueError('请求必须为 JSON 对象')
                    return jsonify(await self.dispatch(_name,data))
                except ValueError as e:
                    return jsonify({'error':str(e)}),400
                except Exception as e:
                    from astrbot.api import logger
                    logger.warning('z-lib API %s failed (%s)',_name,type(e).__name__)
                    return jsonify({'error':'操作失败，请检查配置、任务状态或脱敏日志'}),500
            self.plugin.context.register_web_api('/astrbot_plugin_zlib_knowledge/'+name,view,methods,'电子书管理：'+name)

    async def dispatch(self, name, body):
        s = self.service
        if name=='state':
            result=s.state()
            result['providers']=[{'id':p.meta().id,'kind':'chat'} for p in self.plugin.context.get_all_providers()]+[{'id':p.meta().id,'kind':'embedding'} for p in self.plugin.context.get_all_embedding_providers()]
            return result
        if name=='browser-start': return await s.browser.start()
        if name=='browser-frame': return await s.browser.frame(body.get('viewer_id'))
        if name=='browser-input': return await s.browser.input(body.get('viewer_id'),body)
        if name=='browser-finish':
            s.browser.check_viewer(body.get('viewer_id'))
            return await s.browser.check_ready()
        if name=='browser-close': return await s.browser.end_view(body.get('viewer_id'))
        if name=='browser-setup':
            async def work(report,jobid): return await s.browser.setup(report)
            return s.launch('browser_setup',work)
        if name=='review-detail': return s.reviews.detail(str(body.get('analysis_id','')))
        if name=='review-page': return await s.review_page(str(body.get('analysis_id','')),body.get('page'))
        if name=='review-save': return await s.review_resolve(str(body.get('analysis_id','')),body.get('page'),body.get('action'),body.get('text',''),body.get('note',''))
        if name=='review-retry': return await s.review_retry(str(body.get('analysis_id','')),body.get('page'))
        if name=='review-resume': return await s.review_resume(str(body.get('analysis_id','')))
        if name=='review-finalize': return await s.review_finalize(str(body.get('analysis_id','')),body.get('confirmed'))
        if name=='review-reopen': return await s.reviews.reopen(str(body.get('analysis_id','')))
        if name=='review-delete':
            if body.get('confirmed') is not True: raise ValueError('请确认删除缓存；原文件和已入库内容不会删除')
            return await s.reviews.delete(str(body.get('analysis_id','')))
        if name=='preview-result': return s.preview_result(str(body.get('job_id','')))
        if name=='files': return {'files':await s.files()}
        if name=='settings': return await s.save_settings(body)
        if name=='login': raise ValueError('请刷新插件页面并使用“浏览器登录”，无需手动填写凭据')
        if name=='logout':
            if s.browser: await s.browser.logout()
            return await s.provider.logout()
        if name=='session':
            if s.browser:
                await s.browser.ensure()
                return await s.browser.check_ready()
            return await s.provider.status()
        if name=='book-details': return await s.book_details(str(body.get('book_id','')))
        if name=='search': return await s.search(body.get('query',''),body.get('extension',''),body.get('page',1))
        if name=='download': return s.download(str(body.get('book_id','')))
        if name=='preview': return await s.preview(str(body.get('file_id','')),str(body.get('title','')),str(body.get('edition','')),body.get('chapter_map'))
        if name=='import': return await s.import_book(str(body.get('preview_token','')),str(body.get('kb_id','')),body.get('create'))
        raise ValueError('未知操作')

    def unregister(self):
        apis = self.plugin.context.registered_web_apis
        apis[:] = [r for r in apis if not r[0].startswith('/astrbot_plugin_zlib_knowledge/')]
