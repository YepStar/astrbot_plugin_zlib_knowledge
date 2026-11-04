"""Explainable first-pass classification; ambiguity never auto-creates or imports."""
import re

TOPICS = {
 '社会': ['social psychology','社会心理','social influence','群体','偏见','conformity'],
 '应用': ['organizational','industrial','应用心理','组织心理','职业','工作心理'],
 '发展': ['developmental','lifespan','发展心理','儿童','adolescence','child development'],
 '犯罪': ['forensic','criminal','犯罪','司法'],
 '进化': ['evolutionary','进化心理','evolution'],
 '认知': ['cognitive','personality','research methods','认知','人格','研究方法'],
}


def recommend(title, author, sample, bases):
    title = (title + ' ' + author).casefold()
    sample = sample[:20000].casefold()
    result = []
    for kb in bases:
        name = kb['kb_name']
        score, reasons = 0, []
        core = name.replace('冷月·','').replace('心理学','').strip()
        if core and core.casefold() in title:
            score += 8; reasons.append('书名与库名对应')
        for topic, terms in TOPICS.items():
            if topic not in name:
                continue
            hits = [w for w in terms if w in title]
            secondary = [w for w in terms if w in sample]
            score += 5 * bool(hits) + min(2,len(secondary))
            if hits or secondary:
                reasons.append('主题词：'+ '、'.join((hits+secondary)[:4]))
        if score:
            result.append({'kb_id':kb['kb_id'],'kb_name':name,'score':score,'reason':'；'.join(reasons)})
    result.sort(key=lambda x:x['score'],reverse=True)
    selected = result[0]['kb_id'] if result and result[0]['score'] >= 5 and (len(result)==1 or result[0]['score'] - result[1]['score'] >= 3) else ''
    return {'recommended_kb_id':selected,'candidates':result,'message':'已预选明确匹配的知识库，请确认' if selected else '没有足够明确的匹配，请选择已有知识库或新建'}
