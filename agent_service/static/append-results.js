'use strict';
/* Explicit append, whole-row upsert or checked deletion of two adopted results. */
window.nexusAppendResults=(()=>{
 let key='',generation=0,busy=false,pending=null,pendingUnknown=false;
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
 function sync({project,workflow,result,resultId,request,isCurrent}){
  const host=document.getElementById('data-append-panel');if(!host)return;
  const adopted=typeof workflow?.published_run==='string'?workflow.published_run:workflow?.published_run?.id||workflow?.published_run?.run_id;
  const ready=!!project&&!!result&&result.run_id===resultId&&adopted===resultId&&/^[a-f0-9]{64}$/.test(result.manifest_sha256||'');
  const next=ready?JSON.stringify([project,resultId,result.manifest_sha256]):'';
  if(next===key)return;key=next;generation++;busy=false;pending=null;pendingUnknown=false;host.replaceChildren();host.hidden=!ready;if(!ready)return;
  const title=el('h2','合并已核对的变更明细'),note=el('p','选择另一项已采用的数据结果作为变更明细，系统核对完整数据、字段语义和业务唯一键，生成新的待核对候选。结果固定使用选中的两个版本，不自动采用。未出现在变更中的历史行会保留；删除只针对明确清单且整行原值一致的记录。这不是自动采集或 CDC，也不能证明变更资料完整。');
  const selector=el('select');selector.setAttribute('aria-label','变更明细正式结果');const blank=el('option','正在读取正式结果…');blank.value='';selector.append(blank);
  const label=el('label','选择变更明细 ');label.append(selector);const modeLabel=el('label','处理方式 '),mode=el('select');mode.setAttribute('aria-label','处理方式');for(const [v,t] of [['append','仅追加新增记录'],['upsert','新增及按业务键整行替换'],['delete','按明确清单删除记录']]){const o=el('option',t);o.value=v;mode.append(o)}modeLabel.append(mode);const verify=el('label'),confirm=el('input'),confirmText=document.createTextNode('我已核对新增明细范围完整，且只包含本次新增记录');confirm.type='checkbox';verify.append(confirm,confirmText);
  const evidenceLabel=el('label','核对依据（至少 4 个字） '),evidence=el('textarea');evidence.rows=2;evidence.maxLength=500;evidenceLabel.append(evidence);
  const button=el('button','创建追加候选'),status=el('p'),link=el('a','打开候选任务');button.type='button';status.setAttribute('role','status');status.setAttribute('aria-live','polite');link.hidden=true;
  host.append(title,note,label,modeLabel,verify,evidenceLabel,button,status,link);
  const valid=t=>t===generation&&key===next&&isCurrent();
  const reset=()=>{generation++;pending=null;pendingUnknown=false;busy=false;button.disabled=false;link.hidden=true;status.textContent='设置已变化，请重新确认。'};
  selector.addEventListener('change',()=>{confirm.checked=false;reset()});
  for(const input of [confirm,evidence])input.addEventListener('change',reset);
  const labelForMode=()=>mode.value==='delete'?'创建删除候选':mode.value==='upsert'?'创建变更候选':'创建追加候选';
  mode.addEventListener('change',()=>{confirm.checked=false;confirmText.textContent=mode.value==='delete'?'我已逐行核对明确删除清单及原始值，未列出记录会保留，删除候选须复核后采用。':mode.value==='upsert'?'我已核对业务键和变更范围，同键记录会用变更明细整行替换，缺席的历史记录保留。':'我已核对新增明细范围完整，且只包含本次新增记录';button.textContent=labelForMode();reset()});
  async function loadOptions(){const token=++generation;button.disabled=true;try{
    const catalog=await request('/api/projects/'+encodeURIComponent(project)+'/data-workflows');if(!valid(token))return;
    if(!catalog||!Array.isArray(catalog.items))throw Error('正式结果列表不可用');selector.replaceChildren();const opt=el('option','请选择');opt.value='';selector.append(opt);
    for(const item of catalog.items){const id=typeof item.published_run==='string'?item.published_run:item.published_run?.id;if(!id||id===resultId)continue;const opt=el('option',(item.name||'未命名任务')+' · '+id.slice(0,12));opt.value=id;selector.append(opt)}
    status.textContent=selector.options.length>1?'请选择已单独采用的变更明细结果。':'项目中暂无另一份已采用的变更明细结果。';
   }catch(error){if(valid(token))status.textContent=error.message||'读取结果列表失败。'}finally{if(valid(token))button.disabled=false}}
  button.onclick=async()=>{
   if(busy||!isCurrent())return;
   if(!pending&&(!selector.value||!confirm.checked||evidence.value.trim().length<4)){status.textContent='请选择结果，确认处理方式并填写核对依据。';return}
   const token=++generation;busy=true;button.disabled=true;selector.disabled=mode.disabled=confirm.disabled=evidence.disabled=true;status.textContent='正在核对输入版本和完整业务键…';link.hidden=true;
   try{
    if(!pending){
      const delta=await request('/api/projects/'+encodeURIComponent(project)+'/data-results/'+encodeURIComponent(selector.value)+'?offset=0&limit=1');
      if(!valid(token))return;
      if(delta.run_id!==selector.value||!/^[a-f0-9]{64}$/.test(delta.manifest_sha256||''))throw Error('变更结果版本无法核对，请刷新后重试。');
      if(typeof window.nexusSecureUUID!=='function')throw Error('当前页面无法生成安全请求编号，请更新浏览器或使用 HTTPS。');
      pending={baseline_run_id:resultId,baseline_sha256:result.manifest_sha256,delta_run_id:delta.run_id,delta_sha256:delta.manifest_sha256,request_id:window.nexusSecureUUID(),mode:mode.value,confirm_append_only:mode.value==='append',confirm_replace:mode.value==='upsert',confirm_delete:mode.value==='delete',note:evidence.value.trim()};
    }
    const value=await request('/api/projects/'+encodeURIComponent(project)+'/data-append-candidates',{method:'POST',body:pending});
    if(!valid(token))return;
    if(!value||!/^wf_[a-f0-9]{40}$/.test(value.workflow_id||'')||typeof value.run_id!=='string'||value.baseline_run_id!==pending.baseline_run_id||value.baseline_sha256!==pending.baseline_sha256||value.delta_run_id!==pending.delta_run_id||value.delta_sha256!==pending.delta_sha256||value.mode!==pending.mode||!['baseline_rows','delta_rows','total_rows','added_rows','replaced_rows'].every(k=>Number.isSafeInteger(value[k])&&value[k]>=0)||(pending.mode==='delete'?!Number.isSafeInteger(value.deleted_rows)||value.deleted_rows<1||value.deleted_rows!==value.delta_rows||value.deleted_rows>value.baseline_rows||value.total_rows!==value.baseline_rows-value.deleted_rows||value.added_rows!==0||value.replaced_rows!==0:value.added_rows+value.replaced_rows!==value.delta_rows||value.replaced_rows>value.baseline_rows||value.total_rows!==value.baseline_rows+value.added_rows||(pending.mode==='append'&&value.replaced_rows!==0)))throw Error('候选任务回执或输入版本不一致，请先核对任务列表。');
    const runState=({'queued':'已排队','running':'正在运行','retrying':'等待重试','validated':'已运行，待核对','succeeded':'已采用','failed':'运行未完成','cancelled':'已取消'})[value.status]||'状态待核对';
    status.textContent='候选任务'+runState+'：历史 '+value.baseline_rows+' 条（版本 '+value.baseline_run_id+'），变更 '+value.delta_rows+' 条（版本 '+value.delta_run_id+'）。'+(pending.mode==='delete'?'预计删除 '+value.deleted_rows+' 条，未列出的历史行保留；'+(value.total_rows===0?'删除清单覆盖全部记录，候选结果为空，请特别核对；':''):pending.mode==='upsert'?'预计新增 '+value.added_rows+' 条、整行替换 '+value.replaced_rows+' 条，未出现的历史行保留；':'')+'请打开任务查看实际状态并核对；输入版本不会自动切换。';
    link.href='/data-workbench.html?'+new URLSearchParams({project,workflow:value.workflow_id});link.hidden=false;pending=null;pendingUnknown=false;
   }catch(error){if(valid(token)){
     if(error.status>=400&&error.status<500){pending=null;status.textContent=(pendingUnknown?'原请求是否已创建尚不确定，请先在任务列表核对；':'')+(error.message||'未创建候选，请核对资料。');pendingUnknown=false;}
     else if(pending){pendingUnknown=true;status.textContent='没有确认提交结果。再次点击会使用相同输入和请求编号；如果正式版本已变化，重试可能失败，请先在任务列表核对原请求是否已创建。';}
     else status.textContent=error.message||'读取变更结果失败，请检查后重试。';
   }}finally{if(valid(token)){busy=false;button.disabled=false;selector.disabled=mode.disabled=confirm.disabled=evidence.disabled=!!pending;button.textContent=pending?'重试同一请求':labelForMode()}}
  };
  void loadOptions();
 }
 return {sync};
})();
