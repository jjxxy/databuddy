'use strict';
/* Project conversation result preview. Results always come from the frozen run. */
window.createChatWorkflowResults = function ({container, api, getState}) {
  let generation=0, selection=null, offset=0;
  const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);return n};
  const runId=v=>typeof v==='string'?v:v?.id||v?.run_id||'';
  const validId=v=>typeof v==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(v);
  function current(g,project,epoch){const s=getState();return g===generation&&!s.authSealed&&s.projectId===project&&s.viewEpoch===epoch;}
  async function load(start){
    if(!selection)return;const {project,workflow,epoch}=selection, g=++generation;
    const candidate=runId(workflow.candidate_run),published=runId(workflow.published_run),rid=selection.resultId||candidate||published;
    container.replaceChildren();
    if(!validId(rid)){container.append(el('p','这项流程暂时没有可查看的结果。'));return;}
    container.append(el('p','正在读取本次结果…'));
    try{
      const result=await api('/projects/'+encodeURIComponent(project)+'/data-results/'+encodeURIComponent(rid)+'?'+new URLSearchParams({offset:String(start),limit:'20'}));
      if(!current(g,project,epoch))return;
      if(result?.run_id!==rid||result.workflow_id!==workflow.workflow_id||!Array.isArray(result.schema)||!Array.isArray(result.rows)||!Number.isSafeInteger(result.total)||result.total<0||result.offset!==start||start>result.total||result.rows.length!==Math.min(20,result.total-start)||(start+result.rows.length<result.total?result.next_offset!==start+result.rows.length:result.next_offset!==null)){container.replaceChildren(el('p','结果或分页记录与所选流程不一致，请刷新后重试。'));return;}
      offset=start;container.replaceChildren();container.append(el('h3','处理结果'));
      if(validId(candidate)&&validId(published)&&candidate!==published){const chooser=el('select');chooser.setAttribute('aria-label','选择新结果或已采用结果');for(const [value,label] of [[candidate,'新结果（待核对）'],[published,'已采用结果']]){const option=el('option',label);option.value=value;chooser.append(option)}chooser.value=rid;chooser.addEventListener('change',()=>{selection.resultId=chooser.value;void load(0)});container.append(chooser)}
      const role=rid===candidate?'新结果（待核对），尚需确认':'已采用结果';
      const version=typeof result.plan_sha256==='string'&&typeof workflow.draft_sha256==='string'?(result.plan_sha256===workflow.draft_sha256?'与当前展示的做法版本一致':'使用其他做法版本，不能将当前流程图视为这次运行的过程'):'与当前做法的版本关系未确认';
      container.append(el('p',role+'；'+version+'。'));
      if(workflow.latest_run?.status==='failed')container.append(el('p','最近一次运行未完成；下方是此前保留的结果。')); 
      container.append(el('p',result.context?.requires_semantic_review===true&&!result.semantic_review_confirmed?'业务口径尚需复核；请核对来源、字段含义、期间、单位及结果。':'请根据实际用途核对来源、业务口径和数值；展示结果不代表财务验收通过。'));
      const schema=Array.isArray(result.schema)?result.schema:[], rows=Array.isArray(result.rows)?result.rows:[];
      if(schema.length){const table=el('table'),head=el('thead'),tr=el('tr');for(const f of schema){const th=el('th',(f.label||f.meaning||f.name)+(f.unit?'（'+f.unit+'）':''));th.scope='col';tr.append(th)}head.append(tr);table.append(head);const body=el('tbody');for(const record of rows){const row=el('tr');for(const f of schema){const value=record?.[f.name];row.append(el('td',value==null?'—':typeof value==='object'?JSON.stringify(value):String(value)))}body.append(row)}table.append(body);const scroll=el('div');scroll.style.overflowX='auto';scroll.append(table);container.append(scroll)}else container.append(el('p','本次结果没有可展示的字段。'));
      const total=Number.isSafeInteger(result.total)&&result.total>=0?String(result.total):'未记录';container.append(el('p','本页 '+rows.length+' 条；总记录数：'+total+'。'));
      const nav=el('div'),prev=el('button','上一页'),next=el('button','下一页');prev.type=next.type='button';prev.disabled=start<=0;next.disabled=result.next_offset==null||!Number.isSafeInteger(result.next_offset)||result.next_offset<=start;prev.addEventListener('click',()=>load(Math.max(0,offset-20)));next.addEventListener('click',()=>load(result.next_offset));nav.append(prev,next);container.append(nav);
      const evidence=result.context?.input_evidence;
      if(evidence&&typeof evidence==='object'&&!Array.isArray(evidence)){const title=el('h4','本次运行的来源依据'),list=el('ul');for(const [alias,proof] of Object.entries(evidence)){if(!proof||typeof proof!=='object')continue;const parts=[alias];if(typeof proof.file==='string'&&proof.file)parts.push('资料：'+proof.file);else if(proof.source_id)parts.push('来源编号：'+String(proof.source_id));if(proof.source_revision!=null)parts.push('来源版本：'+String(proof.source_revision));if(proof.sheet)parts.push('页签：'+String(proof.sheet));if(proof.rows!=null)parts.push('读取记录：'+String(proof.rows));list.append(el('li',parts.join('；')))}if(list.children.length)container.append(title,list);else container.append(el('p','本次运行未提供可展示的来源依据。'))}else container.append(el('p','本次运行未提供可展示的来源依据。'));
    }catch(_){if(current(g,project,epoch))container.replaceChildren(el('p','结果暂时无法读取，请刷新后再试。'));}
  }
  function selected(event){generation++;selection=null;container.replaceChildren();const detail=event.detail||{},s=getState(),w=detail.workflow;if(!w||!detail.projectId||s.authSealed||s.projectId!==detail.projectId)return;if(!validId(w.workflow_id))return;selection={project:detail.projectId,workflow:w,epoch:s.viewEpoch};void load(0)}
  async function business(event){
    generation++;selection=null;container.replaceChildren();const d=event.detail||{},s=getState(),flow=d.flow;
    if(!flow||!d.projectId||s.authSealed||s.projectId!==d.projectId)return;
    const role=d.role==='published'?'published':'candidate',fid=flow.id,rid=runId(flow[role+'_run']),digest=flow[role+'_digest'],commit=flow[role+'_commit'],epoch=s.viewEpoch,g=generation;
    if(!validId(fid)||!validId(rid)){container.append(el('p','所选版本暂无可展示的运行结果。'));return}
    if(!/^[a-f0-9]{64}$/.test(digest||'')||typeof commit!=='string'||!commit){container.append(el('p','版本依据不完整，暂不能关联运行结果。'));return}
    container.append(el('p','正在读取本次运行结果…'));
    try{
      const path='/projects/'+encodeURIComponent(d.projectId)+'/business-flows/'+encodeURIComponent(fid)+'/runs/'+encodeURIComponent(rid);
      const run=await api(path);if(!current(g,d.projectId,epoch))return;
      if(run?.id!==rid||run.flow_id!==fid||run.plan_digest!==digest||run.commit!==commit){container.replaceChildren(el('p','运行记录与当前流程版本不一致，不能展示为这次结果。'));return}
      container.replaceChildren(el('h3','本次运行结果'));const names={succeeded:'已完成',failed:'未完成',queued:'等待执行',running:'正在运行',unknown:'状态未知，需核对'};container.append(el('p','运行状态：'+(names[run.status]||'状态待核对')+'；财务口径与来源仍需复核。'));
      if(run.status!=='succeeded')return;
      const artifacts=Array.isArray(run.artifacts)?run.artifacts:[];
      if(artifacts.length){const list=el('ul');for(const artifact of artifacts){if(typeof artifact?.name!=='string')continue;const item=el('li',artifact.name+' '),aid=artifact.id;let previewPath='';
        if(typeof aid==='string'&&/^[A-Za-z0-9_-]{1,100}$/.test(aid)){try{const prefix='/api/projects/'+encodeURIComponent(d.projectId)+'/artifacts/'+encodeURIComponent(aid),preview=new URL(artifact.preview_url,location.origin),download=new URL(artifact.download_url,location.origin);if(preview.origin===location.origin&&!preview.username&&!preview.password&&!preview.search&&!preview.hash&&preview.pathname===prefix+'/preview'&&download.origin===location.origin&&!download.username&&!download.password&&!download.search&&!download.hash&&download.pathname===prefix+'/download')previewPath=preview.pathname}catch(_){}}
        if(previewPath){const button=el('button','查看'),message=el('span');button.type='button';button.addEventListener('click',async()=>{const stillCurrent=()=>container.contains(item)&&!getState().authSealed&&getState().projectId===d.projectId&&getState().viewEpoch===epoch;if(!stillCurrent())return;button.disabled=true;message.textContent=' 正在读取预览…';try{const preview=await api(previewPath.slice(4));if(!stillCurrent())return;if(preview?.artifact?.id&&preview.artifact.id!==aid)throw Error('预览与成果不一致');const supported=preview?.table||preview?.render==='table'||preview?.render==='image'||typeof preview?.text==='string'||preview?.chart||Array.isArray(preview?.blocks)||Array.isArray(preview?.document?.blocks)||preview?.visual_preview;if(!supported){message.textContent=' 此格式暂不支持页面预览，请在成果区下载查看。';return}if(typeof window.nexusShell?.openArtifact!=='function'){message.textContent=' 成果预览暂不可用，请稍后再试。';return}message.textContent='';await window.nexusShell.openArtifact(artifact)}catch(_){if(stillCurrent())message.textContent=' 成果预览暂时无法读取，请核对权限后重试。'}finally{if(stillCurrent())button.disabled=false}});item.append(button,message)}else item.append(el('span','（预览地址未通过核验，请在成果区查看）'));list.append(item)}if(list.children.length)container.append(el('p','本次生成的成果：'),list)}
      const tableSteps=(Array.isArray(run.steps)?run.steps:[]).filter(x=>x?.status==='succeeded'&&x.table_available===true&&typeof x.node_id==='string'&&/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(x.node_id));
      if(!tableSteps.length){if(!artifacts.length)container.append(el('p','本次运行没有可在此展示的数据结果。'));return}
      const node=tableSteps[tableSteps.length-1];container.append(el('p','以下为步骤“'+String(node.title||node.node_id)+'”的数据结果，后续步骤可能继续处理。'));
      async function tablePage(start){
        const pageMark=++generation;try{const data=await api(path+'/tables/'+encodeURIComponent(node.node_id)+'?'+new URLSearchParams({offset:String(start),limit:'20'}));if(!current(pageMark,d.projectId,epoch))return;
        if(data.run_id!==rid||data.node_id!==node.node_id||!Array.isArray(data.columns)||!Array.isArray(data.rows)||!Number.isSafeInteger(data.total)||data.total<0||data.offset!==start||start>data.total||data.rows.length!==Math.min(20,data.total-start)||(start+data.rows.length<data.total?data.next_offset!==start+data.rows.length:data.next_offset!==null))throw Error('结果分页不一致');
        const old=container.querySelector('.chat-business-table');if(old)old.remove();const section=el('div');section.className='chat-business-table';const wrap=el('div');wrap.style.overflowX='auto';const table=el('table'),head=el('tr');for(const col of data.columns)head.append(el('th',String(col.label||col.name)+(col.unit?'（'+col.unit+'）':'')));table.append(head);for(const row of data.rows){const tr=el('tr');for(const col of data.columns){const v=row?.[col.name];tr.append(el('td',v==null?'—':typeof v==='object'?JSON.stringify(v):String(v)))}table.append(tr)}wrap.append(table);section.append(wrap,el('p','共 '+data.total+' 条，本页 '+data.rows.length+' 条。'));const prev=el('button','上一页'),next=el('button','下一页');prev.type=next.type='button';prev.disabled=start===0;next.disabled=data.next_offset==null;prev.onclick=()=>void tablePage(Math.max(0,start-20));next.onclick=()=>void tablePage(data.next_offset);section.append(prev,next);container.append(section)
        }catch(_){if(current(pageMark,d.projectId,epoch))container.append(el('p','结果明细暂时无法读取或核验。'))}}
      await tablePage(0);
    }catch(_){if(current(g,d.projectId,epoch))container.replaceChildren(el('p','运行结果暂时无法读取，请刷新后再试。'));}
  }
  window.addEventListener('nexus-chat-workflow-selected',selected);
  window.addEventListener('nexus-chat-business-flow-selected',business);
  return {dispose(){generation++;selection=null;window.removeEventListener('nexus-chat-workflow-selected',selected);window.removeEventListener('nexus-chat-business-flow-selected',business);container.replaceChildren()}};
};
