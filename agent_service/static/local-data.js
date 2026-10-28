'use strict';
/* Shared local-data view: same-origin web capability, also used by the desktop shell. */
(()=>{
 const markup="<h1>把资料变成可核对的结果</h1><p class=\"lead\">导入、画像、明确处理口径、预览后发布。此页面使用本地确定性规则，不调用云端 AI。</p><p id=\"status\" role=\"status\" aria-live=\"polite\">正在读取本机状态…</p>\n<section><h2>1　选择资料</h2><form id=\"upload\"><label>任务名称<input id=\"name\" maxlength=\"80\" value=\"本地数据核对\" required></label><label>资料文件<input id=\"file\" type=\"file\" accept=\".csv,.xlsx,.json,.jsonl,.txt,.pdf,.docx,.pptx\" required></label><button id=\"upload-button\" type=\"submit\">导入并查看字段</button></form><p id=\"profile\"></p></section>\n<section id=\"rules\" hidden><h2>2　明确处理口径</h2><p>选择字段即确定处理方案。留空分组和金额可保留明细；分组汇总须同时选择两项。</p><form id=\"process\"><div class=\"grid\"><label>唯一编号（可选）<select id=\"primary\"></select></label><label>按哪个字段汇总<select id=\"group\"></select></label><label>金额字段<select id=\"amount\"></select></label><label>扣除的退款字段（可选）<select id=\"refund\"></select></label><label>仅保留该字段的指定值<select id=\"filter-field\"></select></label><label>指定值<input id=\"filter-value\" maxlength=\"2000\" placeholder=\"例如：已付款\"></label></div><p class=\"note\">金额按精确十进制处理。此操作产生新候选，不覆盖已有正式结果。</p><button id=\"process-button\" type=\"submit\">执行本地加工</button></form></section>\n<section id=\"result\" hidden><h2>3　核对结果</h2><p id=\"result-info\"></p><p id=\"summary\"></p><div id=\"table\" class=\"table-scroll\"></div><label id=\"source-review-label\" hidden><input id=\"source-review-confirm\" type=\"checkbox\">我已对照原始资料逐项核对来源解释、金额和业务口径，同意采用这次候选结果。</label><div class=\"actions\"><button id=\"publish\" type=\"button\" hidden>已核对，确认发布此候选</button><button id=\"download\" type=\"button\" hidden>导出完整 CSV</button></div><p id=\"receipt\"></p></section>\n<section><h2>已保存的本地任务</h2><p>退出或断网后，已发布成果仍可打开和导出。</p><div id=\"history\"></div></section>\n";
 window.createAtlasLocalData=function(options){
 const root=options.root,prefix=options.prefix||'',token=options.token??sessionStorage.getItem('atlas_token')??'';
 if(!root||!/^[A-Za-z0-9_-]*$/.test(prefix))throw Error('本地工作区挂载参数不正确');
 root.classList.add('atlas-local-view');root.innerHTML=markup.replace(/id="([^"]+)"/g,(_match,id)=>'id="'+prefix+id+'"');
 const $=selector=>root.querySelector(selector.replace(/^#/,'#'+prefix));
 const fetcher=options.request||((url,requestOptions)=>fetch(url,requestOptions));
 const optionsOnUnauthorized=()=>{if(options.onUnauthorized)options.onUnauthorized();else{reset();message('登录已失效，请返回工作空间重新登录。',true)}};
let source,project,current,previewRun,previewManifest,previewNeedsReview=false,previewNeedsAlignment=false,busy=false,epoch=0,active=false,sealed=false,boundProject='';
 const controllers=new Set();
 const authorized=()=>!sealed&&(options.isAuthorized?options.isAuthorized():(token?sessionStorage.getItem('atlas_token')===token:!sessionStorage.getItem('atlas_token')));
 function ensure(generation=epoch){if(!active||generation!==epoch||!authorized()||(options.projectId&&String(options.projectId()||'')!==boundProject))throw Object.assign(Error('工作空间或登录状态已变化，请重新打开本地工作区。'),{stale:true})}
const message=(text,error=false)=>{$('#status').textContent=text;$('#status').classList.toggle('error',error)};
async function api(path,requestOptions={}){
 const generation=epoch;ensure(generation);const options=requestOptions;
 const controller=new AbortController();controllers.add(controller);const timer=setTimeout(()=>controller.abort(),options.body instanceof FormData?120000:20000);
 const headers={Authorization:'Bearer '+token};let body=options.body;
 if(body&&!(body instanceof FormData)){headers['Content-Type']='application/json';body=JSON.stringify(body)}
 try{const response=await fetcher('/api'+path,{...options,body,headers,signal:controller.signal});ensure(generation);if(response.status===401){optionsOnUnauthorized();ensure(generation);}if(!response.ok){let detail;try{detail=(await response.json()).detail}catch{}throw Error(typeof detail==='string'?detail:'本地请求未完成，请稍后重试')}return response}
 catch(error){if(error.name==='AbortError')throw Error('本地请求等待超时，请刷新状态后再操作；不要重复提交。');throw error}
 finally{clearTimeout(timer);controllers.delete(controller)}
}
const json=async(path,requestOptions)=>{const generation=epoch,response=await api(path,requestOptions),value=await response.json();ensure(generation);return value};
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function waitFor(check){const generation=epoch,end=Date.now()+240000;while(Date.now()<end){ensure(generation);const value=await check();ensure(generation);if(value)return value;await pause(1000)}throw Error('加工尚未完成。请保留任务，并在稍后查看已保存任务的状态。')}
async function guarded(action){if(busy||!active||!authorized())return;const generation=epoch;busy=true;root.querySelectorAll('button').forEach(b=>b.disabled=true);try{await action()}catch(error){if(active&&generation===epoch&&!error.stale)message(error.message||'操作未完成，原有资料和成果已保留。',true)}finally{if(generation===epoch){busy=false;root.querySelectorAll('button').forEach(b=>b.disabled=false)}}}
function choices(id,fields){const select=$(id);select.replaceChildren();const empty=document.createElement('option');empty.value='';empty.textContent='不选择';select.append(empty);for(const f of fields){const option=document.createElement('option');option.value=f.name;option.textContent=f.label;select.append(option)}}
function renderTable(value){const area=$('#table');area.replaceChildren();const fields=value.manifest.fields||[],labels=Object.fromEntries(fields.map(f=>[f.name,f.label]));const rows=value.rows||[];if(!rows.length){area.textContent='结果为空，请检查筛选条件。';return}const keys=Object.keys(rows[0]).filter(k=>!k.startsWith('_dlt'));
 const table=document.createElement('table'),head=document.createElement('tr');for(const key of keys){const th=document.createElement('th');th.textContent=labels[key]||key;head.append(th)}const thead=document.createElement('thead');thead.append(head);table.append(thead);const tbody=document.createElement('tbody');for(const row of rows){const tr=document.createElement('tr');for(const key of keys){const td=document.createElement('td');td.textContent=row[key]==null?'':String(row[key]);tr.append(td)}tbody.append(tr)}table.append(tbody);area.append(table);
}
function showResult(value,candidate){previewRun=candidate?value.run_id:null;previewManifest=candidate?value.manifest:null;previewNeedsAlignment=Boolean(candidate&&(value.fact_semantic_alignment_required===true||value.manifest.fact_semantic_alignment_required===true));previewNeedsReview=Boolean(candidate&&!previewNeedsAlignment&&(value.requires_source_review===true||value.manifest.requires_source_review===true));$('#source-review-label').hidden=!previewNeedsReview;$('#source-review-confirm').checked=false;$('#result').hidden=false;$('#result-info').textContent=`完整结果 ${value.manifest.records} 条；当前显示前 ${value.rows.length} 条。代码版本 ${value.manifest.commit||'见来源清单'}。`;
 const spec=value.manifest.spec||{},groupLabel=(value.manifest.fields||[]).find(field=>field.name===spec.group_by)?.label||'分组',amountLabel=spec.amount_label||'金额合计';$('#summary').textContent=(value.manifest.summary||[]).map(row=>[row.category!==undefined?`${groupLabel}：${String(row.category)}`:'',row.amount_total!==undefined?`${amountLabel}：${String(row.amount_total)}`:''].filter(Boolean).join('，')).join('；');renderTable(value);$('#publish').hidden=!candidate||previewNeedsAlignment;$('#download').hidden=candidate;$('#receipt').textContent=candidate?(previewNeedsAlignment?'事实资料尚缺少逐条业务语义和可加性的可信核对，本候选不能发布。':'这是候选结果，尚未发布。请核对完整条数、金额口径与来源。'):'这是已发布结果，可导出完整 CSV。';
}
async function history(){const rows=await json('/pipelines'),area=$('#history'),visible=rows.filter(p=>!p.archived&&(!boundProject||p.project_id===boundProject));area.replaceChildren();if(!visible.length){area.textContent=boundProject?'此项目还没有本地任务。先导入一份资料。':'当前工作空间还没有本地任务。先导入一份资料。';return}for(const p of visible){const row=document.createElement('div'),text=document.createElement('span');text.textContent=p.name+' · '+(p.published_run?'已发布':p.candidate_run?'候选待核对':'处理中');row.append(text);if(p.published_run||p.candidate_run){const button=document.createElement('button');button.textContent=p.published_run?'打开成果':'核对候选';button.onclick=()=>guarded(async()=>{current=p;showResult(await json(`/pipelines/${p.id}/`+(p.published_run?'result':'preview')),!p.published_run);message('已打开保存的本地结果。')});row.append(button)}area.append(row)}}
$('#upload').addEventListener('submit',event=>{event.preventDefault();guarded(async()=>{const file=$('#file').files[0],name=$('#name').value.trim();if(!file||!file.size)throw Error('请选择一份非空资料文件。');if(!name)throw Error('请填写任务名称。');if(file.size>25*1024*1024)throw Error('文件超过25MB，请拆分后导入。');$('#rules').hidden=true;$('#result').hidden=true;source=current=previewRun=null;
 project=boundProject?{id:boundProject}:await json('/projects',{method:'POST',body:{name,request_id:crypto.randomUUID()}});const form=new FormData();form.append('file',file);form.append('request_id',crypto.randomUUID());form.append('local_processing','true');message('正在本地导入并检查资料，全程不调用云端 AI。');const uploaded=await json(`/projects/${project.id}/attachments`,{method:'POST',body:form});
 source=await waitFor(async()=>{const s=(await json('/sources')).find(s=>s.id===uploaded.id);if(s?.status==='failed')throw Error(s.error||'文件检查未通过，请检查格式和内容。');return s?.status==='ready'?s:null});
 const fields=source.profile.fields;choices('#primary',fields);choices('#group',fields);choices('#amount',fields.filter(f=>f.numeric));choices('#refund',fields.filter(f=>f.numeric));choices('#filter-field',fields);$('#profile').textContent=`已识别 ${fields.length} 个字段，画像使用最多500条样本（本次 ${source.profile.sample_rows} 条）。正式加工会校验全部资料。`;$('#rules').hidden=false;message('资料已保存在本机。请明确分组、金额或筛选口径。');await history();
})});
$('#process').addEventListener('submit',event=>{event.preventDefault();guarded(async()=>{if(!source)throw Error('请先导入资料。');const group=$('#group').value,amount=$('#amount').value,refund=$('#refund').value,filter=$('#filter-field').value,value=$('#filter-value').value; if(Boolean(group)!==Boolean(amount))throw Error('分组和金额字段需要同时选择；都不选则保留明细。');if(refund&&!amount)throw Error('扣除退款前，请先选择金额和分组字段。');if(filter&&!value.trim())throw Error('请填写筛选字段需要保留的值。');
 const spec={object_label:'本地业务记录',primary_key:$('#primary').value||null,group_by:group||null,amount_field:refund?'m_net':amount||null,amount_label:refund?'净金额':'金额合计',filters:filter?[{field:filter,operator:'eq',data_type:'text',value}]:[],computations:refund?[{name:'m_net',label:'净金额',operation:'subtract',left_field:amount,right_field:refund}]:[]};
 $('#result').hidden=true;message('正在本地加工和执行质量检查，已有正式结果保持不变。');current=await json('/pipelines',{method:'POST',body:{source_id:source.id,name:$('#name').value.trim(),spec,release_mode:'manual'}});await waitFor(async()=>{const p=(await json('/pipelines')).find(p=>p.id===current.id);if(p?.candidate_run){current=p;return p}const runs=await json('/runs?target='+encodeURIComponent(current.id));const failed=runs.find(r=>r.status==='failed');if(failed)throw Error(failed.error||failed.message||'质量检查未通过，请查看任务记录并检查编号、金额或筛选条件。');return null});showResult(await json(`/pipelines/${current.id}/preview`),true);message('加工完成，请核对候选结果后再发布。');await history();
})});
$('#publish').onclick=()=>guarded(async()=>{if(!current||!previewRun)throw Error('请先重新读取候选结果。');if(previewNeedsAlignment)throw Error('事实资料尚缺少业务语义和可加性核对，不能发布。');const body={run_id:previewRun};if(previewNeedsReview){if(!$('#source-review-confirm').checked)throw Error('请先对照原始资料核对来源解释、金额和业务口径，并勾选确认。');if(!previewManifest?.commit||!/^[a-f0-9]{64}$/.test(previewManifest?.parquet_hash||''))throw Error('候选结果的版本依据不完整，请重新查看。');Object.assign(body,{confirm_source_review:true,expected_commit:previewManifest.commit,expected_parquet_sha256:previewManifest.parquet_hash})}await json(`/pipelines/${current.id}/publish`,{method:'POST',body});showResult(await json(`/pipelines/${current.id}/result`),false);message('已发布核对后的结果，可离线查看或导出。');await history()});
$('#download').onclick=()=>guarded(async()=>{if(!current)throw Error('请先打开一个已发布结果。');const response=await api(`/pipelines/${current.id}/export`),blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='Atlas-完整结果.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);message('已导出完整 CSV。金额保留精确十进制文本。')});

 function close(){epoch++;active=false;busy=false;for(const controller of controllers)controller.abort();controllers.clear();root.querySelectorAll('button').forEach(button=>button.disabled=false)}
 function clear(){source=project=current=previewRun=previewManifest=null;previewNeedsReview=false;previewNeedsAlignment=false;root.querySelectorAll('form').forEach(form=>form.reset());$('#file').value='';$('#rules').hidden=true;$('#result').hidden=true;for(const id of ['profile','table','summary','receipt','history'])$('#'+id).replaceChildren();for(const id of ['primary','group','amount','refund','filter-field'])$('#'+id).replaceChildren();message('选择工作资料后再导入，不会自动扫描或上传文件。')}
 function reset(){close();sealed=true;clear()}
 function projectChanged(){close();clear();sealed=false}
 function open(){if(sealed||!authorized())return Promise.resolve();active=true;boundProject=String(options.projectId?.()||'');return guarded(async()=>{if(!token&&!options.request)throw Error('请从 Atlas 工作空间打开本地处理页面。');const state=await json('/desktop/runtime');message(state.notice);await history()})}
 return {open,close,reset,projectChanged,isBusy:()=>busy};
 };

 window.installAtlasLocalWorkspace=function(b){
  const panel=document.getElementById('atlas-local-workspace-panel'),container=document.getElementById('atlas-local-workspace-content'),select=document.getElementById('work-environment'),control=document.getElementById('work-environment-control'),openButton=document.getElementById('open-local-work');
  if(!panel||!container||!select||!control||!openButton||!b)return null;
  const projectContext=document.getElementById('atlas-local-project-context'),label=document.getElementById('atlas-local-workspace-scope'),choose=document.getElementById('atlas-local-choose-folder'),note=document.getElementById('atlas-local-folder-note');
  let generation=0,sealed=false,mode='work';
  const processor=window.createAtlasLocalData({root:container,prefix:'atlas-local-',projectId:()=>b.getState().projectId,request:(url,options)=>b.authFetch(url,options),isAuthorized:()=>!sealed,onUnauthorized:()=>b.expireAuthentication?.()});
  const preference=()=> 'atlas_work_environment_'+(b.getState().projectId||'home');
  const contextStatus=document.getElementById('work-environment-status');
  function restore(){select.value=sessionStorage.getItem(preference())==='local'?'local':'';openButton.hidden=select.value!=='local';contextStatus.hidden=true}
  async function syncEnvironment(){
   const current=generation,pid=b.getState().projectId;
   try{const local=await b.api('/local-workspace');if(current!==generation||sealed||pid!==b.getState().projectId)return;
    const cloud=select.querySelector('[value="cloud"]');cloud.disabled=true;cloud.textContent='云端（后续阶段开放）';
    if(local.enabled){select.value='local';sessionStorage.setItem(preference(),'local');openButton.hidden=false;contextStatus.textContent='工作文件夹：'+(local.label||'所选文件夹');contextStatus.hidden=false;}
    else{select.value='';sessionStorage.removeItem(preference());openButton.hidden=true;contextStatus.textContent='请选择本地工作文件夹；云端后续阶段开放。';contextStatus.hidden=false;}
   }catch{if(current===generation&&!sealed){contextStatus.textContent='工作位置状态暂时无法确认。';contextStatus.hidden=false;}}
  }
  function close(){generation++;processor.close();if(panel.open)panel.close();if(!sealed)openButton.focus({preventScroll:true})}
  async function open(){
   if(sealed||mode!=='work')return;
   const state=b.getState();if(state.pending||state.sending){b.toast('请先确认上一条消息的发送结果。');return;}
   select.value='local';openButton.hidden=false;
   if(!panel.open)panel.showModal();
   const current=++generation,pid=state.projectId;const projectName=b.projectItems?.().find(item=>item.id===pid)?.name;projectContext.textContent=projectName?'当前项目：'+projectName:'尚未选择项目';label.textContent='正在核对当前工作空间…';
   choose.hidden=typeof window.atlasDesktop?.chooseWorkspace!=='function';
   note.textContent=choose.hidden?'浏览器本身不能执行本机加工。需要连接实际的本机工作文件夹后，才能读取你明确选择的资料。':'选择文件夹会切换整个本地工作空间，原有项目和资料不会自动移动。';
   container.hidden=true;
   try{const value=await b.api('/local-workspace');if(current!==generation||sealed||pid!==b.getState().projectId)return;label.textContent=value.enabled?'当前本地工作空间：'+(value.label||'所选文件夹'):'尚未选择本地工作文件夹。选择后会切换到该文件夹的独立工作空间。';if(value.enabled){note.textContent='资料在已核对的本地工作文件夹内处理；只读取你明确导入的文件。切换文件夹不会移动已有项目。';sessionStorage.setItem(preference(),'local');container.hidden=false;await processor.open();}else{sessionStorage.removeItem(preference());contextStatus.textContent='本地文件夹未选择';contextStatus.hidden=false;}}
   catch{if(current===generation&&!sealed)label.textContent='暂时无法核对文件夹状态。已保存资料仍保留，请稍后重试。'}
  }
  choose.onclick=async()=>{
   const state=b.getState();if(sealed||processor.isBusy()||state.pending||state.sending||state.activeTurn){b.toast('请先等待或停止正在进行的工作，再切换工作空间。');return;}
   if(typeof window.atlasDesktop?.chooseWorkspace!=='function')return;
   const current=++generation;choose.disabled=true;
   try{const value=await window.atlasDesktop.chooseWorkspace();if(current!==generation||sealed)return;if(value?.cancelled)note.textContent='已取消选择，当前工作空间保持不变。';else note.textContent='正在切换工作空间。请等待工作台重新载入，原有项目不会被自动移动。';}
   catch{if(current===generation&&!sealed)note.textContent='没有切换工作空间。请检查本机连接后重试。';}
   finally{if(current===generation)choose.disabled=false}
  };
  select.onchange=()=>{
   if(select.value==='local'){void open();return;}
   close();restore();contextStatus.textContent='云端工作空间后续阶段开放，本轮未切换工作位置。';contextStatus.hidden=false;b.toast(contextStatus.textContent);
  };
  openButton.onclick=()=>void open();document.getElementById('atlas-local-workspace-close').onclick=close;
  panel.addEventListener('cancel',event=>{event.preventDefault();close()});
  function modeChanged(value){mode=value;control.hidden=mode!=='work';if(mode!=='work')close();else void syncEnvironment()}
  function projectChanged(){generation++;projectContext.textContent='';processor.projectChanged();if(panel.open)panel.close();restore();if(mode==='work')void syncEnvironment()}
  function reset(){sealed=true;generation++;processor.reset();if(panel.open)panel.close();select.value='';openButton.hidden=true;projectContext.textContent='';label.textContent='';note.textContent='';}
  restore();modeChanged('work');
  return {open,close,reset,modeChanged,projectChanged};
 };
})();
