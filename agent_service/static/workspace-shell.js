'use strict';
/* Projects, conversations and real deliverables form the daily work path. */
(()=>{
 const b=window.atlasBusiness;if(!b)return;
 const el=s=>document.querySelector(s),enc=encodeURIComponent,artifactMedia=matchMedia('(max-width:1100px)');
 let epoch=0,listTicket=0,previewTicket=0,resource=null,artifacts=[],listNext=null,listTotal=0;
 let preview=null,previewOffset=0,previewNext=null,previewPrevious=[],previewSheet='',previewBlob=null,trashItems={projects:[],conversations:[]};
 let desktopActionsSealed=false,explanationTicket=0,explanationLoading=false,explanationLoaded=false;
 const state=()=>b.getState(),pid=()=>{if(!state().projectId)throw Error('请先打开一个项目');return state().projectId};
 const root=(id=pid())=>'/projects/'+enc(id);
 const current=(id,mark)=>state().projectId===id&&epoch===mark;
 function message(selector,text){el(selector).textContent=text||'';el(selector).hidden=!text}
 function resourceRoot(value){return root(value.project_id)+(value.kind==='conversation'?'/sessions/'+enc(value.id):'')}
 function releaseBlob(){if(previewBlob){URL.revokeObjectURL(previewBlob);previewBlob=null}}
 function clearPreview(){clearExplanation();previewTicket++;preview=null;previewNext=null;previewPrevious=[];previewSheet='';releaseBlob();el('#artifact-preview').hidden=true;el('#artifact-preview-body').replaceChildren();el('#artifact-preview-actions').replaceChildren();el('#artifact-provenance').hidden=true;el('#artifact-provenance').open=false;el('#artifact-provenance-body').replaceChildren()}
 function closeArtifacts(focus=true){const wasOpen=!el('#workspace-artifacts-panel').hidden;el('#main').inert=document.body.classList.contains('sidebar-open');el('#sidebar').inert=false;document.body.classList.remove('artifacts-open');el('#workspace-artifacts-panel').hidden=true;el('#workspace-artifacts').setAttribute('aria-expanded','false');clearPreview();if(wasOpen&&focus)el('#workspace-artifacts').focus({preventScroll:true})}
 function reset(){desktopActionsSealed=true;el('#desktop-lineage').hidden=true;epoch++;listTicket++;resource=null;artifacts=[];listNext=null;clearPreview();closeArtifacts(false);el('#workspace-settings-menu').hidden=true;el('#workspace-settings').setAttribute('aria-expanded','false');for(const id of ['resource-actions-dialog','workspace-trash-dialog'])if(el('#'+id).open)el('#'+id).close();el('#artifact-list').replaceChildren();el('#workspace-trash-items').replaceChildren();el('#resource-name').value='';trashItems={projects:[],conversations:[]}}
 function projectChanged(){epoch++;listTicket++;resource=null;artifacts=[];listNext=null;clearPreview();for(const id of ['resource-actions-dialog'])if(el('#'+id).open)el('#'+id).close();el('#artifact-list').replaceChildren();if(!el('#workspace-artifacts-panel').hidden){if(state().projectId)loadArtifacts().catch(e=>message('#artifact-error',e.message));else closeArtifacts()}}
 async function action(button,fn,target){if(button?.disabled)return;const mark=epoch;if(button)button.disabled=true;if(target)message(target,'');try{return await fn()}catch(e){if(mark===epoch){if(target)message(target,e.message);b.toast(e.message)}}finally{if(button)button.disabled=false}}
 function openResource(kind,id){if(state().sending||state().pending)throw Error('请先确认上一条消息的发送结果');const item=(kind==='project'?b.projectItems():b.sessionItems()).find(x=>x.id===id);if(!item)throw Error('项目或对话已经变化，请刷新后再试');resource={...item,kind,project_id:kind==='project'?id:pid()};el('#resource-actions-title').textContent=kind==='project'?'项目设置':'对话设置';el('#resource-actions-description').textContent=kind==='project'?'修改项目名称不会改变已有资料、处理逻辑或成果。此工作空间的项目成员共享这些内容。':'对话属于当前项目；修改名称方便下次继续工作。';el('#resource-name').value=item.name||'';el('#resource-delete-confirmation').hidden=true;message('#resource-actions-error','');el('#resource-actions-dialog').showModal();el('#resource-name').focus()}
 el('#project-list').addEventListener('click',e=>{const button=e.target.closest('[data-project-menu]');if(button){try{openResource('project',button.dataset.projectMenu)}catch(err){b.toast(err.message)}}});
 el('#session-list').addEventListener('click',e=>{const button=e.target.closest('[data-session-menu]');if(button){try{openResource('conversation',button.dataset.sessionMenu)}catch(err){b.toast(err.message)}}});
 el('#resource-rename-form').onsubmit=e=>{e.preventDefault();action(e.submitter||el('#resource-rename'),async()=>{if(!resource)return;const value={...resource},mark=epoch,name=el('#resource-name').value.trim();if(!name)throw Error('名称不能为空');await b.api(resourceRoot(value),{method:'PATCH',body:{name,...(value.revision!=null?{expected_revision:value.revision}:{})}});if(mark!==epoch||resource?.id!==value.id)return;el('#resource-actions-dialog').close();if(value.kind==='project')await b.reloadProjects();else await b.reloadSessions();b.toast('名称已保存')},'#resource-actions-error')};
 el('#resource-delete').onclick=()=>{if(!resource)return;el('#resource-delete-note').textContent=resource.kind==='project'?'将“'+resource.name+'”及所属对话移到回收站，并停止相关任务。原资料、代码和成果保留，可以恢复。':'将这段对话移到回收站，并停止它尚未完成的任务。项目资料与其他对话保留，可以恢复。';el('#resource-delete-confirmation').hidden=false};
 el('#resource-delete-cancel').onclick=()=>el('#resource-delete-confirmation').hidden=true;
 el('#resource-delete-confirm').onclick=e=>action(e.currentTarget,async()=>{if(!resource)return;const value={...resource},mark=epoch;await b.api(resourceRoot(value),{method:'DELETE',body:value.revision!=null?{expected_revision:value.revision}:{}});if(mark!==epoch||resource?.id!==value.id)return;el('#resource-actions-dialog').close();resource=null;if(value.kind==='project')await b.reloadProjects();else await b.reloadSessions();b.toast('已移到回收站，可以在“工作设置与工具”中恢复')},'#resource-actions-error');
 const desktop=window.atlasDesktop;
 const lineageButton=el('#desktop-lineage');
 lineageButton.hidden=typeof desktop?.openLineage!=='function';
 lineageButton.onclick=e=>{if(desktopActionsSealed||typeof desktop?.openLineage!=='function')return;action(e.currentTarget,()=>desktop.openLineage())};
 el('#workspace-settings').onclick=()=>{const menu=el('#workspace-settings-menu');menu.hidden=!menu.hidden;el('#workspace-settings').setAttribute('aria-expanded',String(!menu.hidden))};
 el('#workspace-settings-menu').addEventListener('click',e=>{if(e.target.closest('button')){el('#workspace-settings-menu').hidden=true;el('#workspace-settings').setAttribute('aria-expanded','false')}});
 document.addEventListener('click',e=>{if(!e.target.closest('.workspace-utilities')){el('#workspace-settings-menu').hidden=true;el('#workspace-settings').setAttribute('aria-expanded','false')}});
 async function loadTrash(){const mark=epoch,value=await b.api('/trash');if(mark!==epoch)return;trashItems={projects:value.projects||[],conversations:value.conversations||[]};const active=new Set(b.projectItems().map(p=>p.id));const entries=[...trashItems.projects.map(p=>({...p,kind:'project'})),...trashItems.conversations.map(c=>({...c,kind:'conversation'}))];el('#workspace-trash-items').innerHTML=entries.map(item=>{const parentMissing=item.kind==='conversation'&&!active.has(item.project_id);return `<article class="feature-entry"><h3>${b.esc(item.name||'未命名')}</h3><small>${item.kind==='project'?'项目':'对话'}${item.deleted_at?' · '+b.esc(item.deleted_at):''}</small>${parentMissing?'<p class="dialog-description">先恢复所属项目，再恢复这段对话。</p>':`<button class="inline-action" data-restore-resource="${b.esc(item.id)}" data-kind="${item.kind}">恢复${item.kind==='project'?'项目':'对话'}</button>`}</article>`}).join('')||'<p class="dialog-description">回收站是空的。</p>'}
 el('#workspace-trash').onclick=()=>{el('#workspace-trash-dialog').showModal();message('#workspace-trash-error','');loadTrash().catch(e=>message('#workspace-trash-error',e.message))};
 el('#workspace-trash-items').onclick=e=>{const button=e.target.closest('[data-restore-resource]');if(button)action(button,async()=>{const value=(button.dataset.kind==='project'?trashItems.projects:trashItems.conversations).find(x=>x.id===button.dataset.restoreResource);if(!value)throw Error('回收站内容已变化，请重新打开');const resource={...value,kind:button.dataset.kind,project_id:button.dataset.kind==='project'?value.id:value.project_id};await b.api(resourceRoot(resource)+'/restore',{method:'POST',body:value.revision!=null?{expected_revision:value.revision}:{}});await b.reloadProjects(resource.project_id);if(resource.kind==='conversation')await b.openSession(resource.id);await loadTrash();b.toast('已恢复；未完成的工作不会自动重新提交')},'#workspace-trash-error')};

 function safeUrl(value,id=pid()){if(typeof value!=='string'||!value.startsWith('/api/projects/'+enc(id)+'/'))throw Error('成果地址不在当前项目范围');const url=new URL(value,location.origin);if(url.origin!==location.origin||!url.pathname.startsWith('/api/projects/'+enc(id)+'/')||url.username||url.password||url.hash||!/^artifacts\/[A-Za-z0-9_-]{1,100}\/(?:preview|download)$/.test(url.pathname.slice(('/api/projects/'+enc(id)+'/').length)))throw Error('成果地址无效');return url.pathname+url.search}
 async function binary(value,id){const url=safeUrl(value,id),response=await b.authFetch(url);if(!response.ok)throw Error('成果读取失败，请核对权限或刷新后再试');return response.blob()}
 async function download(value){const id=pid(),mark=epoch,blob=await binary(value.download_url,id);if(!current(id,mark))return;const link=document.createElement('a'),url=URL.createObjectURL(blob);link.href=url;link.download=value.name||'项目成果';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
 function category(value){return ({source:'来源资料',code:'加工代码与文本',office:'办公文件',report:'分析报告',result:'完整数据结果',application:'业务应用',chart:'数据图表',agent:'协作成果'})[value.kind]||'工作成果'}
 function artifactStatus(value){return ({adopted:'已采用',candidate:'待确认版本',needs_review:'外部结果待验收',historical:'历史版本',draft:'可修改代码',verified_result:'已保存报告',source_snapshot:'来源快照'})[value.status]||'已保存'}
 function renderArtifacts(){el('#artifact-list').innerHTML=artifacts.map(value=>`<article class="artifact-item"><div><h3>${b.esc(value.name)}</h3><small>${b.esc(category(value))}${value.format?' · '+b.esc(String(value.format).toUpperCase()):''} · ${b.esc(artifactStatus(value))} · ${value.integrity_verified||value.verified?'文件完整性已核对':'完整性待核对'}${value.version?' · 版本 '+b.esc(String(value.version).slice(0,12)):''}${value.updated_at?' · '+b.esc(new Date(value.updated_at).toLocaleString('zh-CN',{hour12:false})):''}</small></div><div class="artifact-item-actions"><button class="inline-action" data-preview-artifact="${b.esc(value.id)}">查看</button>${value.download_url?`<button class="inline-action" data-download-artifact="${b.esc(value.id)}">下载</button>`:''}</div></article>`).join('')||'<p class="dialog-description">还没有这类成果。完成数据加工或报告后，真实文件和结果会出现在这里。</p>';el('#artifact-more').hidden=listNext==null}
 async function loadArtifacts(append=false){const id=pid(),mark=epoch,ticket=++listTicket,filter=el('#artifact-filter').value;if(!append){artifacts=[];listNext=null;clearPreview()}const offset=append?listNext:0;if(offset==null)return;message('#artifact-error','');const query=new URLSearchParams({offset:String(offset),limit:'40'});if(filter!=='all')query.set('kind',filter);try{const value=await b.api(root(id)+'/artifacts?'+query);if(!current(id,mark)||ticket!==listTicket)return;const incoming=value.items||[];artifacts=append?[...artifacts,...incoming]:incoming;listNext=value.next_offset??null;listTotal=value.total??artifacts.length;if(value.unavailable_count)message('#artifact-error',String(value.unavailable_count)+'项成果暂时无法完整读取，请刷新或核对原始文件。');el('#artifact-project-name').textContent=(b.projectItems().find(p=>p.id===id)?.name||'当前项目')+' · '+listTotal+'项';renderArtifacts()}catch(e){if(current(id,mark)&&ticket===listTicket)throw e}}
 function textNode(tag,text,className){const n=document.createElement(tag);n.textContent=text??'';if(className)n.className=className;return n}
 function renderMarkdown(text){return window.atlasMarkdown?.render(text)||textNode('pre',text,'artifact-text')}
 function renderChart(chart){const box=document.createElement('section'),data=Array.isArray(chart.points)?chart.points:Array.isArray(chart.data)?chart.data:[];box.className='artifact-chart';box.append(textNode('h4',chart.title||'成果图表'));if(chart.type!=='bar'||!data.length||data.length>100){box.append(textNode('p','当前图表格式请下载原文件查看。'));return box}const values=data.map(row=>Number(row.value));if(data.some(row=>!['string','number'].includes(typeof row.value)||!/^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?$/i.test(String(row.value)))||values.some(value=>!Number.isFinite(value))){box.append(textNode('p','图表数值超出当前可视范围，请查看完整数据表。'));return box}const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg'),width=560,height=Math.max(220,data.length*28+45),min=Math.min(0,...values),max=Math.max(0,...values),scale=Math.max(Math.abs(min),Math.abs(max),1),range=max/scale-min/scale||1,x=value=>130+(value/scale-min/scale)/range*300;svg.setAttribute('viewBox','0 0 '+width+' '+height);svg.setAttribute('role','img');svg.setAttribute('aria-label',chart.title||'真实成果数据图表');const baseline=document.createElementNS(ns,'line');for(const [key,value]of Object.entries({x1:x(0),x2:x(0),y1:10,y2:height-25,stroke:'#c4d0d9'}))baseline.setAttribute(key,String(value));svg.append(baseline);data.forEach((row,i)=>{const y=25+i*28,label=document.createElementNS(ns,'text'),value=document.createElementNS(ns,'text'),rect=document.createElementNS(ns,'rect');label.setAttribute('x','4');label.setAttribute('y',String(y+11));label.textContent=String(row.label??'').slice(0,25);const title=document.createElementNS(ns,'title');title.textContent=String(row.label??'')+'：'+String(row.value);rect.append(title);value.setAttribute('x','438');value.setAttribute('y',String(y+11));value.textContent=String(row.value);for(const [key,val]of Object.entries({x:Math.min(x(0),x(values[i])),y,width:Math.max(1,Math.abs(x(values[i])-x(0))),height:16,fill:'#4e826b'}))rect.setAttribute(key,String(val));svg.append(label,rect,value)});box.append(svg);if(chart.scope_note)box.append(textNode('p',chart.scope_note,'dialog-description'));if(chart.unit)box.append(textNode('p','单位：'+chart.unit,'dialog-description'));box.append(textNode('p','图形用于展示；精确数值保留为原始标签，计算以完整结果为准。','dialog-description'));return box}
 function previewTable(data){const wrapper=document.createElement('div');wrapper.className='table-scroll artifact-table';wrapper.tabIndex=0;const columns=(data.columns||data.schema?.columns||[]).map(c=>typeof c==='string'?{name:c,label:c}:c),rows=data.rows||[];if(!columns.length){wrapper.append(textNode('p','此结果没有可展示的字段。'));return wrapper}const table=document.createElement('table'),head=document.createElement('thead'),hr=document.createElement('tr');for(const column of columns){const th=textNode('th',column.label||column.name);th.scope='col';if(column.unit)th.append(textNode('small',' · '+column.unit));hr.append(th)}head.append(hr);table.append(head);const body=document.createElement('tbody');for(const row of rows){const tr=document.createElement('tr');columns.forEach((column,i)=>{const value=Array.isArray(row)?row[i]:row[column.name];tr.append(textNode('td',value==null?'—':typeof value==='object'?JSON.stringify(value):String(value)))});body.append(tr)}table.append(body);wrapper.append(table);if(!rows.length)wrapper.append(textNode('p','当前页没有记录。'));return wrapper}
 function renderBlocks(blocks){const box=document.createElement('div');box.className='artifact-document';for(const block of blocks){const type=block.type||block.kind;if(type==='table')box.append(previewTable(block.table||block));else if(type==='heading')box.append(textNode('h4',block.text));else if(type==='paragraph'||type==='text'||block.text!=null)box.append(textNode('p',block.text));else box.append(textNode('p','此部分暂不支持预览，请下载原文件。','dialog-description'))}return box}
 function draftFor(value,reuse){const reference=[value.name,'成果标识 '+value.id,value.version?'版本 '+String(value.version).slice(0,12):''].filter(Boolean).join('；');return reuse?'请复用本项目成果及对应处理逻辑：'+reference+'。先核对来源、口径和当前输入，再说明哪些可以沿用；我希望接下来完成：':'请修改本项目成果：'+reference+'。保留原版本，先核对实际文件与处理逻辑。我要调整的是：'}
 function previewActions(value,result){const box=el('#artifact-preview-actions');box.replaceChildren();const variants=Array.isArray(value.downloads)&&value.downloads.length?value.downloads:value.download_url?[{url:value.download_url}]:[];for(const variant of variants){const label=value.kind==='source'?'下载原文件':variant.format==='handoff'?'下载完整数据包 ZIP':variant.format?'下载 '+String(variant.format).toUpperCase():'下载完整文件',button=textNode('button',label,'inline-action');button.onclick=()=>action(button,()=>download({...value,download_url:variant.url,name:variant.format?value.name.replace(/\.[a-z0-9]{1,8}$/i,'')+'.'+(variant.format==='handoff'?'zip':variant.format):value.name}),'#artifact-error');box.append(button)}if(value.kind==='source')return;if(value.kind==='code'&&typeof result?.text==='string'){const copy=textNode('button','复制本页代码','inline-action');copy.onclick=()=>action(copy,async()=>{if(!navigator.clipboard?.writeText)throw Error('此浏览器不支持复制，请选择代码或下载文件');await navigator.clipboard.writeText(result.text);b.toast('已复制当前预览页，完整代码请下载')},'#artifact-error');box.append(copy)}for(const [label,reuse] of [['在对话中修改',false],['复用做法',true]]){const button=textNode('button',label,'inline-action');button.onclick=()=>{if(b.setDraft(draftFor(value,reuse))){if(artifactMedia.matches)closeArtifacts();b.toast('已放入对话草稿，补充要求后发送')}};box.append(button)}}
 function clearExplanation(){
  explanationTicket++;explanationLoading=false;explanationLoaded=false;
  const details=el('#artifact-explanation');details.hidden=true;details.open=false;delete details.dataset.artifactId;
  el('#artifact-explanation-status').textContent='';el('#artifact-explanation-body').replaceChildren();
  el('#artifact-evidence-note').hidden=true;el('#artifact-evidence-note').textContent='';
 }
 function evidenceText(value){return typeof value==='string'?value:typeof value==='number'&&Number.isSafeInteger(value)?String(value):''}
 function evidenceCount(value){return Number.isSafeInteger(value)&&value>=0?String(value):''}
 function evidenceList(value){return Array.isArray(value)?value.filter(x=>x&&typeof x==='object'&&!Array.isArray(x)):[]}
 function evidenceSelection(value){
  if(!value||typeof value!=='object'||Array.isArray(value))return '';
  const pieces=[],sheet=evidenceText(value.sheet);if(sheet)pieces.push('页签：'+sheet);
  const range=value.range;
  if(Array.isArray(range)&&range.length===4&&range.every(Number.isSafeInteger)&&range[0]>=1&&range[0]<=range[2]&&range[2]<=16384&&range[1]>=1&&range[1]<=range[3]&&range[3]<=1048576){
   const column=value=>{let label='';for(let n=value;n>0;n=Math.floor((n-1)/26))label=String.fromCharCode(65+(n-1)%26)+label;return label};
   pieces.push('区域：'+column(range[0])+range[1]+':'+column(range[2])+range[3]);
  }else if(evidenceText(range))pieces.push('范围：'+evidenceText(range));
  const rows=Array.isArray(value.header_rows)?value.header_rows.map(evidenceText).filter(Boolean).join('、'):evidenceText(value.header_rows);
  if(rows)pieces.push('表头行：'+rows);return pieces.join('；');
 }
 function evidenceChecks(parent,checks){
  const values=evidenceList(checks);if(!values.length){parent.append(textNode('p','没有记录可展示的专项检查。','evidence-check'));return}
  const list=document.createElement('ul');for(const check of values){
   const item=document.createElement('li'),state=({passed:'已记录通过',failed:'未通过',needs_review:'待复核',not_checked:'未检查'})[check.status]||'状态未记录';
   item.append(textNode('strong',(evidenceText(check.label)||'检查项')+'：'+state));
   if(evidenceText(check.detail))item.append(document.createTextNode('。'+evidenceText(check.detail)));list.append(item);
  }parent.append(list);
 }
 function evidenceSection(parent,title){const section=document.createElement('section');section.append(textNode('h4',title));parent.append(section);return section}
 function renderExplanation(value){
  const box=el('#artifact-explanation-body');box.replaceChildren();
  box.append(textNode('p','以下内容来自这份报告及其关联结果的保存记录。文件完整性与检查结果不能代替业务口径、来源完整性和财务复核。','dialog-description'));
  if(evidenceText(value?.report?.period))box.append(textNode('p','报告期间：'+evidenceText(value.report.period)));
  const integrity=value?.integrity||{};
  box.append(textNode('p',integrity.status==='verified'?'报告与关联结果的文件摘要已核对；这不代表财务口径或数字已获批准。':'文件完整性状态未确认，请先核对原始文件。','evidence-check'));
  const resultId=value?.result_artifact_id;
  if(typeof resultId==='string'&&/^wa_[a-f0-9]{44}$/.test(resultId)){
   const button=textNode('button','查看本次加工结果','inline-action');button.type='button';button.dataset.openReportResult=resultId;
   button.onclick=()=>action(button,async()=>{
    const id=pid(),mark=epoch,prior=preview,request=previewTicket+1;
    const target={id:resultId,name:'本次报告使用的数据结果',kind:'result',format:'data',preview_url:'/api/projects/'+enc(id)+'/artifacts/'+enc(resultId)+'/preview'};
    previewPrevious=[];previewSheet='';
    try{await loadPreview(target,0,false,true)}catch(error){
     if(!current(id,mark)||previewTicket!==request)return;
     if(preview?.id===prior?.id){setupExplanation(prior);renderExplanation(value);explanationLoaded=true;el('#artifact-explanation').open=true}
     throw error;
    }
   },'#artifact-error');
   box.append(button);
  }else box.append(textNode('p','本次加工结果暂不能从这里打开；请核对历史结果记录。','dialog-description'));

  const sources=evidenceSection(box,'1. 使用了哪些资料'),sourceItems=evidenceList(value?.sources);
  if(!sourceItems.length)sources.append(textNode('p','没有记录可展示的来源，请核对原始资料。'));
  for(const source of sourceItems){
   const item=document.createElement('div');item.className='evidence-step';item.append(textNode('strong',evidenceText(source.name)||(source.source_id?'来源名称未记录':'上一份结果（未记录原始来源）')));
   const facts=[];if(Number.isSafeInteger(source.revision)&&source.revision>0)facts.push('版本 '+source.revision);
   const count=evidenceCount(source.rows);if(count!=='')facts.push('读取记录 '+count+' 条');
   const selection=evidenceSelection(source.selection);facts.push(selection||'读取区域未记录');
   if(facts.length)item.append(textNode('p',facts.join('；')));
   item.append(textNode('p',source.availability==='recorded'?'已记录该来源版本；打开时仍需确认原件可用。':'来源版本未完整记录，请核对原件。','dialog-description'));
   const versionNote=source.source_version_state==='changed'?'该来源后来有新版本；本报告仍使用当次版本，请按业务期间核对。':source.source_version_state==='current'?'已登记的来源版本未变；这不能证明上游业务数据已经更新。':'当前来源版本状态未知，无法据此判断是否为最新。';
   item.append(textNode('p',versionNote,'dialog-description'));
   if(typeof source.source_id==='string'&&/^[-A-Za-z0-9_]{1,100}$/.test(source.source_id)&&Number.isSafeInteger(source.revision)&&source.revision>0&&typeof source.content_hash==='string'&&/^[a-f0-9]{64}$/i.test(source.content_hash)){
    const button=textNode('button','查看当次来源','inline-action');button.type='button';button.onclick=()=>action(button,()=>openSource(source.source_id,source.revision,source.content_hash),'#artifact-error');item.append(button);
   }
   sources.append(item);
  }
  const steps=evidenceSection(box,'2. 怎样处理资料'),stepItems=evidenceList(value?.steps);
  if(!stepItems.length)steps.append(textNode('p','没有记录可展示的处理步骤，请核对保存的做法。'));
  else {const list=document.createElement('ol');for(const step of stepItems){
   const item=document.createElement('li');item.className='evidence-step';item.append(textNode('strong',evidenceText(step.label)||'步骤名称未记录'));
   if(evidenceText(step.description))item.append(textNode('p',evidenceText(step.description)));
   const counts=[],input=evidenceCount(step.input_rows),output=evidenceCount(step.output_rows);
   if(input!=='')counts.push('输入 '+input+' 条');else if(!evidenceList(step.inputs).length)counts.push('输入记录数未记录');
   counts.push(output!==''?'输出 '+output+' 条':'输出记录数未记录');item.append(textNode('p',counts.join('；')));
   if(input===''&&evidenceList(step.inputs).length){const inputs=evidenceList(step.inputs).map((entry,index)=>{const count=evidenceCount(entry.rows);return (evidenceText(entry.name)||'输入 '+(index+1))+'：'+(count===''?'记录数未记录':count+' 条')});item.append(textNode('p','输入：'+inputs.join('；')))}
   if(evidenceList(step.checks).length){const more=document.createElement('details');more.append(textNode('summary','查看此步骤的检查'));evidenceChecks(more,step.checks);item.append(more)}
   else item.append(textNode('p','没有记录可展示的单独检查。','dialog-description'));list.append(item);
  }steps.append(list)}
  const metrics=evidenceSection(box,'3. 报告数字如何计算'),metricItems=evidenceList(value?.metrics);
  if(!metricItems.length)metrics.append(textNode('p','没有记录可展示的指标计算，请核对报告原文及结果。'));
  for(const metric of metricItems){
   const item=document.createElement('div');item.className='evidence-step';item.append(textNode('strong',evidenceText(metric.label)||'指标名称未记录'));
   const amount=evidenceText(metric.value);item.append(textNode('p',amount?amount+(evidenceText(metric.unit)?' '+evidenceText(metric.unit):''):'精确值未记录，请核对报告原文。'));
   item.append(textNode('p',evidenceText(metric.description)||'计算口径未记录，请核对报告与保存的规则。'));
   const count=evidenceCount(metric.rows);if(count!=='')item.append(textNode('p','涉及记录 '+count+' 条'));metrics.append(item);
  }
  const checks=evidenceSection(box,'4. 做了哪些检查');evidenceChecks(checks,value?.checks);
  const limits=evidenceSection(box,'仍需注意');limits.className='evidence-limitations';
  const limitations=Array.isArray(value?.limitations)?value.limitations.filter(v=>typeof v==='string'&&v):[];
  if(limitations.length){const list=document.createElement('ul');for(const limit of limitations)list.append(textNode('li',limit));limits.append(list)}
  else limits.append(textNode('p','当前记录不能证明来源齐全或业务口径正确，请结合原件和业务要求复核。'));
 }
 async function loadExplanation(){
  const value=preview;if(!value||value.kind!=='report'||explanationLoading||explanationLoaded)return;
  const id=pid(),mark=epoch,ticket=++explanationTicket;explanationLoading=true;
  el('#artifact-explanation-status').textContent='正在读取这份报告的保存记录…';el('#artifact-explanation-body').replaceChildren();
  try{
   if(!/^[A-Za-z0-9_-]{1,100}$/.test(value.id))throw Error('invalid artifact id');
   const result=await b.api(root(id)+'/artifacts/'+enc(value.id)+'/explanation');
   if(!current(id,mark)||ticket!==explanationTicket||preview?.id!==value.id)return;
   renderExplanation(result);explanationLoaded=true;el('#artifact-explanation-status').textContent='';
  }catch(error){
   if(!current(id,mark)||ticket!==explanationTicket||preview?.id!==value.id)return;
   const message=error.status===403?'当前账号无权查看这份报告的处理记录。':error.status===404||error.status===410?'这份报告或关联记录已不可用，请核对原件。':error.status===409?'报告依据暂时无法验证，请核对原件与保存记录。':'暂时无法读取处理说明，请稍后重试。';
   el('#artifact-explanation-status').textContent=message;
   const retry=textNode('button','重试读取','inline-action');retry.type='button';retry.onclick=()=>loadExplanation();el('#artifact-explanation-body').replaceChildren(retry);
  }finally{if(ticket===explanationTicket)explanationLoading=false}
 }
 function setupExplanation(value,result){
  const details=el('#artifact-explanation'),note=el('#artifact-evidence-note');
  if(value.kind!=='report'){
   clearExplanation();
   if(value.kind==='result'){
    const contract=result?.context?.data_contract,checks=Array.isArray(contract?.checks)?contract.checks:[],total=result?.table?.total??result?.total;
    const numericEvidence=contract?.numeric_policy==='exact_decimal_v1'&&Number.isSafeInteger(contract?.rows)&&contract.rows>=0&&Number.isSafeInteger(total)&&total>=0&&contract.rows===total;
    const legacy=checks.some(check=>typeof check?.name==='string'&&/^(range|total):/.test(check.name))&&!numericEvidence,substantive=checks.filter(check=>typeof check?.name==='string'&&check.name.trim()&&check.name!=='row_count');
    note.textContent=legacy?'数值范围或对账检查的精度或记录范围未确认，请从原始资料重新运行并复核；范围检查本身不检查缺失值。':!checks.length?'未记录可验证的业务验收规则；请核对缺失值、来源、计算口径与结果。':checks.some(check=>check?.status==='fail')?'保存记录显示部分检查未通过，请先核对来源、计算口径与结果。':!substantive.length?'未记录可核对的行数范围或其他业务验收规则；请核对缺失值、来源、计算口径与结果。':'已记录的检查不代表来源齐全或财务口径正确；范围检查本身不检查缺失值，请确认是否另有缺失值检查。';note.hidden=false;
   }else if(['office','agent','code'].includes(value.kind)){note.textContent='此成果的预览未提供完整计算过程，请另行核对来源和处理依据。';note.hidden=false}
   else if(['application','chart'].includes(value.kind)){note.textContent='文件完整性不代表数值和业务口径正确，请结合来源和处理依据复核。';note.hidden=false}
   return;
  }
  if(details.dataset.artifactId!==value.id){clearExplanation();details.dataset.artifactId=value.id}
  details.hidden=false;
 }
 el('#artifact-explanation').ontoggle=()=>{if(el('#artifact-explanation').open)loadExplanation()};
 function previewMeta(value,result){
  const semantic=({requires_review:'业务口径待复核',declared_by_workflow:'口径由处理流程声明',owner_confirmed_version:'用户已确认版本',not_confirmed:'业务口径未确认'})[value.semantic_status],metric=({confirmed:'指标定义已确认',draft:'指标仍为草稿',not_confirmed:'指标定义未确认'})[value.metric_status];
  setupExplanation(value,result);
  el('#artifact-preview-meta').textContent=[value.kind==='source'?'原件只读预览，不修改源文件':semantic,value.kind==='source'?null:metric,result.fidelity_note||'当前预览可供核对，完整文件可下载。'].filter(Boolean).join(' · ');
  const details=el('#artifact-provenance'),box=el('#artifact-provenance-body');if(details.dataset.artifactId!==value.id)details.open=false;details.dataset.artifactId=value.id;details.hidden=false;box.replaceChildren();
  const facts=[['完整性',value.integrity_verified||value.verified?'原件字节已核对':'仅登记服务端回执，原件待核对'],['版本',value.version],['更新时间',value.updated_at],['SHA256',value.sha256],['摘要范围',({file:'原始文件',complete_result_manifest:'完整结果清单',frozen_report_manifest:'固定报告清单',complete_parquet:'完整数据文件'})[value.sha256_scope]||value.sha256_scope]];
  for(const [label,text] of facts)if(text!=null&&text!==''){const row=document.createElement('p');row.append(textNode('strong',label+'：'),document.createTextNode(String(text)));box.append(row)}
  if(value.source_refs?.length){box.append(textNode('h4','来源记录'));box.append(textNode('pre',JSON.stringify(value.source_refs,null,2),'artifact-provenance-json'))}
  if(result.context){
   const context={...result.context},contract=context.data_contract;delete context.data_contract;
   box.append(textNode('h4','处理口径与记录'));box.append(textNode('pre',JSON.stringify(context,null,2),'artifact-provenance-json'));
   const checks=Array.isArray(contract?.checks)?contract.checks:[],total=result?.table?.total??result?.total;
   const numericEvidence=contract?.numeric_policy==='exact_decimal_v1'&&Number.isSafeInteger(contract?.rows)&&contract.rows>=0&&Number.isSafeInteger(total)&&total>=0&&contract.rows===total;
   if(checks.length){box.append(textNode('h4','记录的验收检查'));const entries=checks.map(check=>{
    const name=typeof check?.name==='string'?check.name:'名称未记录',numeric=/^(range|total):/.test(name);
    const label=name==='row_count'?'记录数检查':name.startsWith('range:')?'数值范围：'+name.slice(6):name.startsWith('total:')?'对账总额：'+name.slice(6):name.startsWith('not_null:')?'缺失值：'+name.slice(9):name.startsWith('unique:')?'重复检查：'+name.slice(7):name;
    return {label: name==='row_count'?'行数范围未记录，需核对':label,status:name==='row_count'?'not_checked':numeric&&!numericEvidence?'needs_review':check?.status==='pass'?'passed':check?.status==='fail'?'failed':'not_checked'};
   });evidenceChecks(box,entries)}
  }
 }
 async function loadPreview(value,offset=0,fromPrevious=false,reveal=false){const id=pid(),mark=epoch,ticket=++previewTicket;if(preview?.id!==value.id)clearExplanation();releaseBlob();message('#artifact-error','');const url=new URL(safeUrl(value.preview_url,id),location.origin);url.searchParams.set('offset',String(offset));url.searchParams.set('limit','50');url.searchParams.set('text_limit','12000');if(previewSheet)url.searchParams.set('sheet',previewSheet);const result=await b.api(url.pathname.slice(4)+url.search);if(!current(id,mark)||ticket!==previewTicket)return;return renderPreview(value,result,id,mark,ticket,offset,reveal)}
 async function renderPreview(value,result,id,mark,ticket,offset,reveal,preloadedBlob=null){if(result.artifact){value={...value,...result.artifact};artifacts=artifacts.map(item=>item.id===value.id?value:item);renderArtifacts()}preview=value;previewOffset=offset;previewNext=result.next_offset??result.table?.next_offset??null;el('#artifact-preview').hidden=false;el('#artifact-preview-title').textContent=value.name;previewMeta(value,result);const box=el('#artifact-preview-body');box.replaceChildren();const table=result.table||result;const render=result.render||result.type;if(result.chart)box.append(renderChart(result.chart));if(render==='table')box.append(previewTable(table));else if(result.blocks||result.document?.blocks)box.append(renderBlocks(result.blocks||result.document.blocks));else if(render==='image'&&value.download_url){box.append(textNode('p','正在读取图片…','dialog-description'));const blob=preloadedBlob??await binary(value.download_url,id);if(!current(id,mark)||ticket!==previewTicket)return;if(!['image/png','image/jpeg','image/webp','image/gif'].includes(blob.type)){box.replaceChildren(textNode('p','此图片格式请下载查看。','dialog-description'))}else{previewBlob=URL.createObjectURL(blob);const image=document.createElement('img');image.src=previewBlob;image.alt=value.name;image.className='artifact-image';box.replaceChildren(image)}}else if(result.chart){}else if(typeof result.text==='string'){const markdown=render==='markdown'||result.markdown===true||['md','markdown'].includes(String(value.format).toLowerCase())||value.name.toLowerCase().endsWith('.md');box.append(markdown?renderMarkdown(result.text):textNode('pre',result.text,'artifact-text'))}else box.append(textNode('p',render==='pdf'?'当前页面暂不还原 PDF 版式，请下载完整文件查看。':'此格式暂不支持页面预览，请下载完整文件查看。','dialog-description'));
 const sheets=result.sheet_names||result.sheets||table.sheet_names;if(Array.isArray(sheets)&&sheets.length){const label=textNode('label','页签 '),select=document.createElement('select');select.setAttribute('aria-label','选择成果页签');for(const sheet of sheets){const name=typeof sheet==='string'?sheet:sheet.name;const option=textNode('option',name);option.value=name;select.append(option)}select.value=result.current_sheet||result.sheet||previewSheet||select.options[0]?.value;select.onchange=()=>{previewSheet=select.value;previewPrevious=[];loadPreview(value).catch(e=>{if(current(id,mark))message('#artifact-error',e.message)})};label.append(select);box.prepend(label)}
 const rows=table.rows||[],total=table.total??result.total,pageOffset=Number(table.offset??result.offset??offset);el('#artifact-page-status').textContent=render==='table'?(rows.length?'第'+(pageOffset+1)+'–'+(pageOffset+rows.length)+'行':'暂无可展示行')+(total!=null?' / 共'+total+'行':'')+(previewNext!=null?'，仍有后续':''):previewNext!=null?'当前为内容摘录，仍有后续':'已显示本次可用预览';el('#artifact-previous-page').hidden=!previewPrevious.length;el('#artifact-next-page').hidden=previewNext==null;previewActions(value,result);if(reveal&&current(id,mark)&&ticket===previewTicket&&!el('#workspace-artifacts-panel').hidden)el('#artifact-preview-title').scrollIntoView?.({block:'start',inline:'nearest',behavior:'auto'})}
 function syncLayout(){const panel=el('#workspace-artifacts-panel'),open=!panel.hidden;if(open){el('#main').inert=artifactMedia.matches;el('#sidebar').inert=artifactMedia.matches;if(artifactMedia.matches&&!panel.contains(document.activeElement))el('#workspace-artifacts-close').focus({preventScroll:true})}else{el('#main').inert=document.body.classList.contains('sidebar-open');el('#sidebar').inert=false}}
 artifactMedia.addEventListener?.('change',syncLayout);
 async function openArtifacts(kind='all'){pid();b.closeSidebar();if([...el('#artifact-filter').options].some(option=>option.value===kind))el('#artifact-filter').value=kind;el('#workspace-artifacts-panel').hidden=false;syncLayout();el('#workspace-artifacts-close').focus();document.body.classList.add('artifacts-open');el('#workspace-artifacts').setAttribute('aria-expanded','true');await loadArtifacts()}
 async function openSource(sourceId,revision=null,expectedHash=null){
  if(typeof sourceId!=='string'||!/^[-A-Za-z0-9_]{1,100}$/.test(sourceId))throw Error('资料标识无效，请刷新对话后重新打开');
  const query=new URLSearchParams();if(revision!=null){const number=Number(revision);if(!Number.isSafeInteger(number)||number<1)throw Error('资料版本无效，请刷新对话后重新打开');query.set('revision',String(number))}
  if(expectedHash!=null){if(typeof expectedHash!=='string'||!/^[a-f0-9]{64}$/i.test(expectedHash)||revision==null)throw Error('资料依据不完整，请核对原始记录');query.set('expected_sha256',expectedHash.toLowerCase())}
  // Keep the current report visible until the exact source and its preview have
  // both been fetched. A stale request must never replace a different project.
  const id=pid(),mark=epoch;let ticket=++previewTicket;listTicket++;message('#artifact-error','');
  try{
   const value=await b.api(root(id)+'/sources/'+enc(sourceId)+'/artifact'+(query.size?'?'+query:''));
   if(!current(id,mark)||ticket!==previewTicket)return;
   if(value?.kind!=='source'||typeof value.id!=='string'||!/^[-A-Za-z0-9_]{1,100}$/.test(value.id)||!value.preview_url)throw Error('这份资料暂不能预览，请刷新后重试');
   const prefix='/api/projects/'+enc(id)+'/artifacts/'+enc(value.id),url=new URL(safeUrl(value.preview_url,id),location.origin);
   if(url.pathname!==prefix+'/preview')throw Error('来源预览与版本记录不一致');
   if(value.download_url&&new URL(safeUrl(value.download_url,id),location.origin).pathname!==prefix+'/download')throw Error('来源下载与版本记录不一致');
   if(expectedHash!=null){
    const sameRef=Array.isArray(value.source_refs)&&value.source_refs.some(ref=>ref?.source_id===sourceId&&ref?.revision===Number(revision));
    if(!sameRef||value.sha256!==expectedHash.toLowerCase())throw Error('来源版本与报告依据不一致');
   }
   url.searchParams.set('offset','0');url.searchParams.set('limit','50');url.searchParams.set('text_limit','12000');
   const result=await b.api(url.pathname.slice(4)+url.search);
   if(!current(id,mark)||ticket!==previewTicket)return;
   if(result?.artifact&&(result.artifact.id!==value.id||result.artifact.sha256!==value.sha256))throw Error('来源预览与版本记录不一致');
   const blob=result?.render==='image'&&value.download_url?await binary(value.download_url,id):null;
   if(!current(id,mark)||ticket!==previewTicket)return;
   b.closeSidebar();clearPreview();ticket=++previewTicket;
   artifacts=[value];listNext=null;listTotal=1;el('#artifact-filter').value='source';el('#artifact-more').hidden=true;renderArtifacts();
   el('#artifact-project-name').textContent=(b.projectItems().find(p=>p.id===id)?.name||'当前项目')+' · 来源资料';
   el('#workspace-artifacts-panel').hidden=false;syncLayout();el('#workspace-artifacts-close').focus();document.body.classList.add('artifacts-open');el('#workspace-artifacts').setAttribute('aria-expanded','true');
   await renderPreview(value,result,id,mark,ticket,0,true,blob);
  }catch(error){
   if(!current(id,mark)||ticket!==previewTicket)return;
   const friendly=error.status===403?'当前账号无权查看这份来源资料。':error.status===404||error.status===410?'资料原件不存在或该历史来源版本已不可用，请核对原始资料。':error.status===409?'资料版本无法验证，请核对报告依据。':'资料原件暂不可用，请核对原始资料后重试。';
   message('#artifact-error',friendly);throw Object.assign(Error(friendly),{status:error.status});
  }
 }
 el('#workspace-artifacts').onclick=()=>{if(!el('#workspace-artifacts-panel').hidden){closeArtifacts();return}openArtifacts().catch(e=>b.toast(e.message))};
 el('#workspace-artifacts-close').onclick=closeArtifacts;el('#artifact-preview-close').onclick=clearPreview;
 el('#artifact-refresh').onclick=e=>action(e.currentTarget,()=>loadArtifacts(),'#artifact-error');el('#artifact-filter').onchange=()=>loadArtifacts().catch(e=>message('#artifact-error',e.message));el('#artifact-more').onclick=e=>action(e.currentTarget,()=>loadArtifacts(true),'#artifact-error');
 el('#artifact-list').onclick=e=>{const show=e.target.closest('[data-preview-artifact]'),get=e.target.closest('[data-download-artifact]');if(show)action(show,()=>{const value=artifacts.find(a=>a.id===show.dataset.previewArtifact);if(!value?.preview_url)throw Error('此成果暂不支持预览，请下载查看');previewPrevious=[];previewSheet='';return loadPreview(value,0,false,true)},'#artifact-error');if(get)action(get,()=>download(artifacts.find(a=>a.id===get.dataset.downloadArtifact)),'#artifact-error')};
 el('#artifact-next-page').onclick=e=>action(e.currentTarget,async()=>{if(preview&&previewNext!=null){previewPrevious.push(previewOffset);await loadPreview(preview,previewNext)}},'#artifact-error');el('#artifact-previous-page').onclick=e=>action(e.currentTarget,async()=>{if(previewPrevious.length)await loadPreview(preview,previewPrevious.pop(),true)},'#artifact-error');
 document.addEventListener('keydown',e=>{if(e.key==='Escape'){el('#workspace-settings-menu').hidden=true;el('#workspace-settings').setAttribute('aria-expanded','false');if(!el('#workspace-artifacts-panel').hidden)closeArtifacts()}});
 window.atlasShell={projectChanged,reset,loadArtifacts,loadPreview,openArtifacts,openSource,closeArtifacts,syncLayout,safeUrl};
})();
