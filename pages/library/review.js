/* Durable OCR review UI; job records remain read-only. */
(() => {
'use strict';
window.ZlibReviewUI={create({api,safe,node,onPreview,onResume,onRefresh,notify}){
const $=id=>document.getElementById(id);
let current='',detail=null,page=0,ticket=0,retrying='',busy=false,history=[],historyIndex=-1;
const labels={running:'处理中',review:'待确认',ready:'已确认待导入',importing:'正在入库',imported:'已导入',interrupted:'已中断，可接续',pending:'未处理',text:'原始文本',ok:'识图成功',empty:'已确认空白',failed:'待复核',retry_ready:'重试已返回，待人工确认',manual:'已补录',skipped:'人工跳过'};
const editable=()=>detail&&['review','interrupted'].includes(detail.status);
function renderList(rows){
 const area=$('analysis-cache-list');area.replaceChildren();
 if(!rows?.length){area.append(node('p','暂无分析缓存。新分析会逐页保存，不会因关闭页面而清空。','hint'));return;}
 for(const r of rows){const card=node('article','','cache-card');card.append(node('h3',r.title),node('span',labels[r.status]||r.status,'badge'),node('p',`OCR 已处理 ${r.processed_count||0}/${r.expected_count||0} 页 · 待复核 ${r.unresolved_count||0} 页`));
 const open=node('button','打开分析缓存');open.onclick=()=>safe(()=>openReview(r.id));card.append(open);area.append(card);}
}
function filteredPages(){return (detail?.pages||[]).filter(p=>$('review-filter').value==='all'||($('review-filter').value==='unresolved'?['failed','retry_ready','pending'].includes(p.status):p.failure_reason));}
function renderDetail(){
 $('review-title').textContent=detail.title+' · 逐页复核';
 $('review-summary').textContent=`${labels[detail.status]||detail.status}；OCR ${detail.processed_count}/${detail.expected_count} 页，曾失败 ${detail.failed_count} 页，待复核 ${detail.unresolved_count} 页，未处理 ${detail.pending_count} 页；已人工补录 ${detail.manual_count} 页、跳过 ${detail.skipped_count} 页。`;
 const pages=filteredPages();if(page&&!pages.some(p=>p.page===page)){const previous=detail.pages.find(p=>p.page===page);if(previous)pages.push(previous);}$('review-pages').replaceChildren();for(const p of pages){const o=node('option',`第 ${p.page} 页 · ${labels[p.status]||p.status}${p.reason?' · '+p.reason:''}`);o.value=p.page;$('review-pages').append(o);}
 if(pages.some(p=>p.page===page))$('review-pages').value=String(page);
 else page=pages.length?pages[0].page:0;
 $('review-pages').value=page?String(page):'';
 $('review-resume').hidden=detail.status!=='interrupted';
 $('review-reopen').hidden=!['ready','imported'].includes(detail.status);
 $('review-finalize').disabled=busy||detail.status==='running'||detail.status==='importing'||detail.status==='imported'||detail.pending_count>0||detail.unresolved_count>0||!$('review-ack').checked;
 $('review-confirm-note').textContent=detail.pending_count?'仍有页面未处理，请接续分析。':detail.unresolved_count?'仍有失败页未经复核，暂不可进入入库预览。':'确认后仅生成入库预览；仍需选择知识库并点击确认导入。';
 $('review-page-panel').hidden=!page;
 if(!page)$('review-page-empty').textContent='此筛选下没有页面。可切换为“曾失败的全部页面”或“全部页面”查看。';else $('review-page-empty').textContent='';
 $('review-clear').disabled=busy||['running','importing'].includes(detail.status);
}
async function refreshDetail(){if(!current)return;const key=current;const data=await api('POST','review-detail',{analysis_id:key});if(current!==key)return;detail=data;renderDetail();}
async function selectPage(next,remember=true){
 page=Number(next);if(!page||!current||!detail)return;
 if(remember&&history[historyIndex]!==page){history=history.slice(0,historyIndex+1);history.push(page);historyIndex=history.length-1;}
 if(!Array.from($('review-pages').options).some(o=>Number(o.value)===page)){const info=detail.pages.find(p=>p.page===page);const option=node('option',`第 ${page} 页 · ${labels[info?.status]||info?.status||'返回查看'}`);option.value=page;$('review-pages').append(option);}
 $('review-pages').value=String(page);$('review-page-panel').hidden=false;$('review-page-empty').textContent='';const request=++ticket,key=current;$('review-page-label').textContent=`物理页 ${page}`;$('review-image').removeAttribute('src');$('review-image').hidden=true;$('review-page-state').textContent='正在读取缓存及原页…';$('review-text').value='';$('review-raw').textContent='';$('review-note').value='';setActions(false);
 const result=await api('POST','review-page',{analysis_id:key,page});if(request!==ticket||key!==current)return;
 $('review-image').hidden=!result.image;if(result.image)$('review-image').src=result.image;
 $('review-page-state').textContent=[labels[result.status]||result.status,result.reason,result.review_note].filter(Boolean).join(' · ');
 $('review-raw').textContent=result.raw_response||(result.failure_kind==='budget'||/DailyBudgetExceeded|每日预算/.test(result.reason||'')?'本地每日预算插件已拦截，请求未提交模型，因此没有模型回复。':'未取得模型回复（可能超时、未调用或页面已有文本层）');
 $('review-text').value=result.text||'';$('review-note').value=result.status==='skipped'?result.review_note||'':'';
 $('review-retry').textContent='单页重试 OCR（会调用模型）';
 setActions(editable()&&Boolean(result.failure_reason));
}
function setActions(enabled){for(const id of ['review-save-text','review-skip','review-retry'])$(id).disabled=!enabled||busy;$('review-text').readOnly=!enabled;}
async function openReview(key){
 current=key;$('review-delete-prompt').hidden=true;page=0;history=[];historyIndex=-1;ticket++;$('review-ack').checked=false;$('review-filter').value='unresolved';
 if($('import-dialog').open)$('import-dialog').close();if(!$('review-dialog').open)$('review-dialog').showModal();
 await refreshDetail();await selectPage(page);
}
async function resolve(action){
 if(!current||!page)return;
 busy=true;setActions(false);try{await api('POST','review-save',{analysis_id:current,page,action,text:$('review-text').value,note:$('review-note').value});
 $('review-ack').checked=false;page=0;await refreshDetail();const next=detail.pages.find(p=>['failed','retry_ready'].includes(p.status));if(next){$('review-filter').value='unresolved';page=next.page;renderDetail();}await onRefresh();
 }finally{busy=false;await refreshDetail();if(page)await selectPage(page);}
}
$('close-review').onclick=()=>$('review-dialog').close();$('review-dialog').addEventListener('close',()=>{current='';detail=null;ticket++;});
$('review-filter').onchange=()=>safe(async()=>{page=0;renderDetail();await selectPage(page);});
$('review-pages').onchange=()=>safe(()=>selectPage($('review-pages').value));
function step(delta){
 if(!detail||busy)return;
 if(delta<0){if(history.length){const target=page===0?historyIndex:historyIndex-1;if(target>=0){historyIndex=target;safe(()=>selectPage(history[historyIndex],false));return;}}if(page>1){history.unshift(page-1);historyIndex=0;safe(()=>selectPage(history[historyIndex],false));}return;}
 if(delta>0&&historyIndex<history.length-1){historyIndex++;safe(()=>selectPage(history[historyIndex],false));return;}
 const pages=filteredPages(),index=pages.findIndex(p=>p.page===page);
 const next=pages[index+delta];
 if(next)safe(()=>selectPage(next.page));
 else if(page+delta>=1&&page+delta<=detail.page_count)safe(()=>selectPage(page+delta));
}
$('review-prev').onclick=()=>step(-1);$('review-next').onclick=()=>step(1);
$('review-save-text').onclick=()=>safe(()=>resolve('manual'));
$('review-skip').onclick=()=>safe(()=>resolve('skip'));
$('review-retry').onclick=()=>safe(async()=>{if(retrying)throw Error('请等待当前单页重试结束');const result=await api('POST','review-retry',{analysis_id:current,page});retrying=result.job_id;$('review-retry').disabled=true;notify('已提交单页重试，最长等待30秒；完成后可查看结果。');await onRefresh();});
$('review-refresh').onclick=()=>safe(async()=>{await refreshDetail();await selectPage(page);});
$('review-resume').onclick=()=>safe(async()=>{const meta=detail;const result=await api('POST','review-resume',{analysis_id:current});$('review-dialog').close();onResume(result.job_id,meta.file_id);await onRefresh();});
$('review-reopen').onclick=()=>safe(async()=>{await api('POST','review-reopen',{analysis_id:current});$('review-ack').checked=false;await refreshDetail();await selectPage(page);await onRefresh();});
$('review-ack').onchange=renderDetail;
$('review-finalize').onclick=()=>safe(async()=>{busy=true;renderDetail();try{const result=await api('POST','review-finalize',{analysis_id:current,confirmed:$('review-ack').checked});$('review-dialog').close();onPreview(result);await onRefresh();}finally{busy=false;if(detail)renderDetail();}});
$('review-clear').onclick=()=>{if(!detail||busy)return;$('review-delete-prompt').hidden=false;$('review-delete-confirm').focus();};
$('review-delete-cancel').onclick=()=>{$('review-delete-prompt').hidden=true;};
$('review-delete-confirm').onclick=()=>safe(async()=>{
 if(!current||busy)return;const key=current;busy=true;$('review-delete-confirm').disabled=true;$('review-clear').disabled=true;
 try{await api('POST','review-delete',{analysis_id:key,confirmed:true});if(current===key)$('review-dialog').close();await onRefresh();notify('分析缓存已删除，原文件和已入库资料保留');}
 finally{busy=false;$('review-delete-confirm').disabled=false;if(detail)renderDetail();}
});
async function syncJobs(jobs){if(!retrying)return;const job=jobs.find(j=>j.id===retrying);if(job&&['completed','failed','interrupted'].includes(job.status)){retrying='';if(current){await refreshDetail();await selectPage(page);}notify(job.status==='completed'?'单页处理结束，请查看复核状态。':job.message,job.status!=='completed');}}
return {renderList,open:openReview,syncJobs};
}};
})();
