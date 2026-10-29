'use strict';
/* Collaboration is task state and evidence, never a second unbounded chat feed. */
(()=>{
 const b=window.atlasBusiness;if(!b)return;
 const el=s=>document.querySelector(s),enc=encodeURIComponent;
 const labels={queued:'等待处理',submitting:'正在提交',submitted:'已提交',working:'正在处理',input_required:'需要补充',auth_required:'需要授权',succeeded:'已返回成果',failed:'处理失败',cancel_requested:'正在取消',cancelled:'已取消',unknown:'提交结果未知',rejected:'未接受'};
 const terminal=new Set(['succeeded','failed','cancelled','unknown','rejected']);
 const downloadsInFlight=new Set();
 let projectEpoch=0,loadEpoch=0,peers=[],tasks=[],models=[],detailId='',detailTicket=0,loading=false;
 const state=()=>b.getState(),dialog=()=>el('#collaboration-dialog');
 function project(){const id=state().projectId;if(!id)throw Error('请先打开一个项目');return id}
 const endpoint=(pid,suffix)=>'/projects/'+enc(pid)+suffix;
 const current=(pid,epoch)=>pid===state().projectId&&epoch===projectEpoch;
 function error(message){el('#collaboration-error').textContent=message||'';el('#collaboration-error').hidden=!message}
 function clearDetail(){detailTicket++;detailId='';el('#collaboration-detail').hidden=true;el('#collaboration-detail-result').textContent='';el('#collaboration-detail-evidence').replaceChildren();el('#collaboration-detail-artifacts').replaceChildren()}
 function resetPeer(){el('#agent-peer-form').reset();el('#agent-peer-id').value='';el('#agent-peer-revision').value='';el('#agent-peer-probe-result').textContent='';el('#agent-peer-save').textContent='保存连接';el('#agent-peer-save').disabled=false;el('#agent-peer-probe').disabled=false;renderTransport();renderModels()}
 function reset(){downloadsInFlight.clear();projectEpoch++;loadEpoch++;peers=[];tasks=[];models=[];loading=false;error('');clearDetail();resetPeer();el('#collaboration-tasks').replaceChildren();el('#collaboration-peers').replaceChildren();el('#collaboration-usage').hidden=true;el('#collaboration-updated').textContent='';if(dialog().open)dialog().close()}
 async function projectChanged(){downloadsInFlight.clear();projectEpoch++;loadEpoch++;peers=[];tasks=[];models=[];loading=false;clearDetail();resetPeer();error('');el('#collaboration-tasks').replaceChildren();el('#collaboration-peers').replaceChildren();if(dialog().open)await load().catch(e=>error(e.message))}
 async function action(button,fn){if(button?.disabled)return;const pid=project(),epoch=projectEpoch;if(button)button.disabled=true;error('');try{return await fn(pid,epoch)}catch(e){if(current(pid,epoch)){error(e.message);b.toast(e.message)}}finally{if(button&&current(pid,epoch))button.disabled=false}}
 function stateLabel(task){if(task.state==='succeeded'&&!task.evidence?.integrity_verified)return task.verification_status==='advisory_unverified'?'已返回复核意见':'已返回说明';return labels[task.state]||'状态待核对'}
 function builtin(peer){return peer.origin==='builtin'||peer.transport==='local'||peer.requires_configuration===false}
 function renderPeers(){el('#collaboration-peers').innerHTML=peers.map(peer=>`<article class="feature-entry"><h3>${b.esc(peer.name)}</h3><small>${builtin(peer)?'内置专长 Agent':peer.transport==='mcp'?'我的 MCP Agent':'我的 A2A Agent'} · ${peer.enabled===false?'已停用':'可用'}${peer.protocol_profile?' · '+b.esc(peer.protocol_profile):''}</small><p>${b.esc(peer.description||'按明确任务范围协作，返回结果需结合来源核对。')}</p>${!builtin(peer)?`<p class="collaboration-evidence">${b.esc(peer.remote_project_id?'对方项目：'+peer.remote_project_id:'')}${peer.remote_model_id?' · 对方模型：'+b.esc(peer.remote_model_id):''}${peer.private_data_approved!==undefined?' · '+(peer.private_data_approved?'私有资料发送已获管理员批准':'私有资料发送需管理员批准'):''}</p>`:''}${peer.can_edit?`<div class="collaboration-actions"><button class="inline-action" data-agent-peer-edit="${b.esc(peer.id)}">修改连接</button>${peer.enabled!==false?`<button class="inline-action" data-agent-peer-disable="${b.esc(peer.id)}" data-revision="${b.esc(peer.revision||'')}">停用</button>`:''}</div>`:''}</article>`).join('')||'<p class="dialog-description">当前项目没有可用协作者，请联系管理员检查配置。</p>'}
 function taskNote(task){if(task.state==='unknown')return '对方可能已经接收任务。请先在对方系统核对；这里不会自动重发。';if(task.state==='input_required')return task.status_note||'请回到原对话补充资料，再继续核对这项协作。';if(task.state==='auth_required')return task.status_note||'请核对连接和授权；未完成授权前不会继续。';if(task.state==='cancel_requested')return '取消请求已记录，仍在确认执行状态；这不代表外部操作已经撤销。';if(task.state==='succeeded')return task.evidence?.integrity_verified?'返回内容已完成完整性核对；财务含义和口径仍需验收。':'对方报告任务完成；尚无经过本方核验的完整成果。';return task.status_note||task.error||''}
 function renderTasks(){el('#collaboration-tasks').innerHTML=tasks.map(task=>`<article class="feature-entry"><div class="collaboration-task-top"><h3>${b.esc(task.peer_name||task.peer_id||'协作 Agent')}</h3><span class="collaboration-state" data-state="${b.esc(task.state)}">${b.esc(stateLabel(task))}</span></div><p>${b.esc(task.goal||'已委派任务')}</p><small>${b.esc(task.deadline_at?'处理期限：'+task.deadline_at:'')}${task.updated_at?' · 更新于 '+b.esc(task.updated_at):''}</small>${taskNote(task)?`<p class="${task.state==='unknown'?'collaboration-warning':'collaboration-evidence'}">${b.esc(taskNote(task))}</p>`:''}<div class="collaboration-actions"><button class="inline-action" data-agent-task="${b.esc(task.id)}">查看结果与依据</button>${!terminal.has(task.state)&&task.state!=='cancel_requested'?`<button class="inline-action" data-agent-task-cancel="${b.esc(task.id)}">取消这项协作</button>`:''}</div></article>`).join('')||'<p class="dialog-description">本项目还没有协作任务。在对话中说明需要哪些复核，Atlas 会保留任务进度和返回依据。</p>'}
 function renderModels(selected=new Set([...el('#agent-peer-models').querySelectorAll('[data-agent-grant-model]:checked')].map(n=>n.dataset.agentGrantModel))){el('#agent-peer-models').innerHTML='<legend>可以处理返回内容的本方模型</legend>'+models.map(model=>`<label class="feature-check"><input type="checkbox" data-agent-grant-model="${b.esc(model.id)}" ${selected.has(model.id)?'checked':''}>${b.esc(model.name)}</label>`).join('');if(!models.length)el('#agent-peer-models').append(document.createTextNode('当前没有可用模型，请先联系管理员配置。'))}
 function renderTransport(){el('#agent-peer-mcp-fields').hidden=el('#agent-peer-transport').value!=='mcp'}
 function configuration(){const value={name:el('#agent-peer-name').value.trim(),transport:el('#agent-peer-transport').value,url:el('#agent-peer-url').value.trim(),remote_project_id:el('#agent-peer-project').value.trim(),remote_model_id:el('#agent-peer-model').value.trim(),data_sharing_approved:el('#agent-peer-data-approved').checked,submit_approved:el('#agent-peer-submit-approved').checked,model_approved:el('#agent-peer-model-approved').checked,approved_model_ids:[...el('#agent-peer-models').querySelectorAll('[data-agent-grant-model]:checked')].map(n=>n.dataset.agentGrantModel)};if(el('#agent-peer-credential').value)value.credential=el('#agent-peer-credential').value;if(value.transport==='mcp'){value.submit_tool=el('#agent-peer-submit-tool').value.trim();value.get_tool=el('#agent-peer-get-tool').value.trim();value.cancel_tool=el('#agent-peer-cancel-tool').value.trim()}return value}
 function validate(value,grants=false){if(!value.name||!value.url||!value.remote_project_id)throw Error('请填写名称、服务地址和对方项目标识');if(grants&&(!value.data_sharing_approved||!value.submit_approved))throw Error('请明确核对资料发送范围及任务提交权限');if(grants&&(!value.model_approved||!value.approved_model_ids.length))throw Error('请选择获准处理返回内容的本方模型')}
 async function load(){const pid=project(),epoch=projectEpoch,ticket=++loadEpoch;loading=true;try{const [peerData,taskData,modelData]=await Promise.all([b.api(endpoint(pid,'/agent-peers')),b.api(endpoint(pid,'/agent-tasks')),b.api('/models')]);if(!current(pid,epoch)||ticket!==loadEpoch)return;peers=peerData.items||[];tasks=taskData.items||[];models=modelData.models||[];renderPeers();renderTasks();renderModels();el('#collaboration-updated').textContent='已更新当前项目的协作进度';el('#collaboration-usage').hidden=true;el('#collaboration-usage').textContent='';if(detailId){const task=tasks.find(t=>t.id===detailId);if(task)renderDetail(task);else clearDetail()}}catch(e){if(current(pid,epoch)&&ticket===loadEpoch)throw e}finally{if(current(pid,epoch)&&ticket===loadEpoch)loading=false}}
 function evidenceText(evidence,task){evidence=evidence||{};const lines=[evidence.integrity_verified?'完整性：已核对返回内容摘要':'完整性：待核对','财务正确性：需依据来源与实际结果验收'];for(const key of ['manifest_sha256','bundle_sha256','sha256'])if(typeof evidence[key]==='string')lines.push('摘要：'+evidence[key]);if(Number.isInteger(evidence.files_count??evidence.file_count))lines.push('文件数：'+(evidence.files_count??evidence.file_count));if(Number.isInteger(evidence.bytes))lines.push('字节数：'+evidence.bytes);for(const artifact of (evidence.artifacts||[]).slice(0,20)){if(artifact&&typeof artifact==='object')lines.push([artifact.name||artifact.id||'成果',artifact.sha256?'SHA256 '+artifact.sha256:''].filter(Boolean).join(' · '))}const coverage=task?.input_read_coverage;if(coverage)lines.push(coverage.all_assigned_inputs_read?'已记录对全部分配输入的读取；读取记录不能替代财务验收。':'仍缺少输入读取记录：来源 '+(coverage.missing_sources??0)+' 份、结果 '+(coverage.missing_results??0)+' 份。');if(Number.isInteger(task?.evidence_count))lines.push('读取及复核记录：'+task.evidence_count+' 项');const files=task?.output_artifacts||[];if(files.length){lines.push('已接收成果文件：'+files.length+' 个');for(const file of files.slice(0,20))lines.push([file.name||'未命名成果',file.bytes!=null?file.bytes+' 字节':'',file.sha256?'SHA256 '+file.sha256:''].filter(Boolean).join(' · '))}if(task?.received_result_ids?.length)lines.push('已接收完整数据结果：'+task.received_result_ids.length+' 个，业务口径仍待验收。');return lines.join('\n')}
 async function sha256(bytes){
  if(!crypto.subtle?.digest)throw Error('浏览器无法校验成果，请使用 HTTPS 或本机安全地址打开工作空间');
  const digest=await crypto.subtle.digest('SHA-256',bytes);
  return [...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('');
 }
 async function downloadTaskFile(task,file,pid,epoch,button){
  const total=file.bytes,expected=String(file.sha256||'').toLowerCase();
  if(!Number.isSafeInteger(total)||total<0||total>32*1024*1024||!/^[a-f0-9]{64}$/.test(expected))throw Error('成果大小或摘要无效，未下载文件');
  const bytes=new Uint8Array(total);let offset=0;
  for(let page=0;page<=Math.ceil(total/65536);page++){
   if(!current(pid,epoch))return;
   button.textContent='正在校验成果 '+Math.round(offset/Math.max(1,total)*100)+'%';
   const result=await b.api(endpoint(pid,'/agent-tasks/'+enc(task.id)+'/artifacts/'+enc(file.id))+'?offset='+offset+'&limit=65536');
   if(!current(pid,epoch))return;
   if(result.id!==file.id||result.task_id!==task.id||result.bytes!==total||result.offset!==offset||String(result.sha256).toLowerCase()!==expected||result.integrity_verified!==true||result.encoding!=='base64'||typeof result.content_base64!=='string'||result.content_base64.length>87384)throw Error('成果分页与已核对的文件不一致，未下载文件');
   let decoded;try{decoded=atob(result.content_base64)}catch{throw Error('成果文件编码无效，未下载文件')}
   const chunk=Uint8Array.from(decoded,char=>char.charCodeAt(0)),end=offset+chunk.length;
   if(chunk.length>65536||result.length!==chunk.length||end>total||await sha256(chunk)!==String(result.page_sha256).toLowerCase())throw Error('成果片段摘要校验失败，未下载文件');
   if(!current(pid,epoch))return;
   bytes.set(chunk,offset);
   if(result.eof===true){
    if(end!==total||await sha256(bytes)!==expected)throw Error('完整成果摘要校验失败，未下载文件');
    if(!current(pid,epoch))return;
    const blob=new Blob([bytes],{type:file.media_type||'application/octet-stream'}),url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download=String(file.name||'协作成果').split(/[\\/]/).pop().replace(/[\u0000-\u001f\u007f]/g,'_').slice(0,180)||'协作成果';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);b.toast('完整文件已校验，已开始下载；业务口径仍需验收');return;
   }
   if(end<=offset||end>=total||result.next_offset!==end)throw Error('成果分页不连续，未下载文件');
   offset=end;
  }
  throw Error('成果分页未完整结束，未下载文件');
 }
 function renderDetail(task){
  detailId=task.id;el('#collaboration-detail').hidden=false;el('#collaboration-detail-title').textContent=task.peer_name||task.peer_id||'协作结果';el('#collaboration-detail-state').textContent=stateLabel(task)+' · '+taskNote(task);
  const value=task.result_summary;el('#collaboration-detail-result').textContent=typeof value==='string'?value:value?JSON.stringify(value,null,2):task.error||'暂无返回结果。';el('#collaboration-detail-evidence').textContent=evidenceText(task.evidence,task);el('#collaboration-detail-evidence').className='collaboration-evidence context-summary';
  const actions=el('#collaboration-detail-artifacts'),pid=state().projectId,epoch=projectEpoch;actions.replaceChildren();
  for(const file of (task.output_artifacts||[]).slice(0,20)){
   const button=document.createElement('button'),label='下载完整成果：'+(file.name||'未命名文件'),key=pid+':'+task.id+':'+file.id;button.className='inline-action';button.textContent=label;button.disabled=downloadsInFlight.has(key);button.dataset.artifactDownload=key;
   button.onclick=()=>action(button,async()=>{if(downloadsInFlight.has(key))return;downloadsInFlight.add(key);try{await downloadTaskFile(task,file,pid,epoch,button)}finally{downloadsInFlight.delete(key);for(const currentButton of actions.querySelectorAll('[data-artifact-download]'))if(currentButton.dataset.artifactDownload===key){currentButton.textContent=label;currentButton.disabled=false}}});actions.append(button);
  }
  if(task.received_result_ids?.length){
   const button=document.createElement('button');button.className='inline-action';button.textContent='在项目成果中核对数据';button.onclick=()=>{if(!current(pid,epoch))return;if(!window.atlasShell){b.toast('请从顶部成果面板核对完整数据结果');return}dialog().close();window.atlasShell.openArtifacts('result').catch(e=>{if(current(pid,epoch))error(e.message)})};actions.append(button);
  }
 }
 function editPeer(identity){const peer=peers.find(p=>p.id===identity);if(!peer||!peer.can_edit)throw Error('此协作者不能在这里修改');resetPeer();el('#agent-peer-add').open=true;el('#agent-peer-id').value=peer.id;el('#agent-peer-revision').value=peer.revision||'';for(const [suffix,key] of [['name','name'],['transport','transport'],['project','remote_project_id'],['url','url'],['model','remote_model_id'],['submit-tool','submit_tool'],['get-tool','get_tool'],['cancel-tool','cancel_tool']])if(peer[key]!=null)el('#agent-peer-'+suffix).value=peer[key];el('#agent-peer-data-approved').checked=!!peer.data_sharing_approved;el('#agent-peer-submit-approved').checked=!!peer.submit_approved;el('#agent-peer-model-approved').checked=!!peer.model_approved;renderModels(new Set(peer.approved_model_ids||[]));renderTransport();el('#agent-peer-save').textContent='核对后保存修改';el('#agent-peer-probe-result').textContent='凭据不回显；服务地址不变时，留空保留已保存凭据。'}
 el('#agent-collaboration').onclick=()=>{try{project();dialog().showModal();load().catch(e=>error(e.message))}catch(e){b.toast(e.message)}};
 el('#collaboration-refresh').onclick=e=>action(e.currentTarget,()=>load());
 el('#collaboration-detail-close').onclick=clearDetail;
 el('#agent-peer-transport').onchange=renderTransport;
 el('#agent-peer-reset').onclick=resetPeer;
 el('#agent-peer-probe').onclick=e=>action(e.currentTarget,async(pid,epoch)=>{const value=configuration();validate(value);const result=await b.api(endpoint(pid,'/agent-peers/probe'),{method:'POST',body:value});if(!current(pid,epoch))return;el('#agent-peer-probe-result').textContent='连接检查完成'+(result.protocol_profile?' · '+result.protocol_profile:'')+'。尚未提交任务或自动授权。'});
 el('#agent-peer-form').onsubmit=e=>{e.preventDefault();action(e.submitter||el('#agent-peer-save'),async(pid,epoch)=>{const value=configuration();validate(value,true);const id=el('#agent-peer-id').value,revision=el('#agent-peer-revision').value;await b.api(endpoint(pid,'/agent-peers'+(id?'/'+enc(id):''))+(id&&revision?'?expected_revision='+enc(revision):''),{method:id?'PUT':'POST',body:value});if(!current(pid,epoch))return;resetPeer();el('#agent-peer-add').open=false;await load();if(current(pid,epoch))b.toast('协作连接已保存；后续由对话安排明确任务')})};
 el('#collaboration-peers').onclick=e=>{const edit=e.target.closest('[data-agent-peer-edit]'),disable=e.target.closest('[data-agent-peer-disable]');if(edit){try{editPeer(edit.dataset.agentPeerEdit)}catch(err){error(err.message)}}if(disable)action(disable,async(pid,epoch)=>{await b.api(endpoint(pid,'/agent-peers/'+enc(disable.dataset.agentPeerDisable))+'?expected_revision='+enc(disable.dataset.revision),{method:'DELETE'});if(current(pid,epoch))await load()})};
 el('#collaboration-tasks').onclick=e=>{const taskButton=e.target.closest('[data-agent-task]'),cancel=e.target.closest('[data-agent-task-cancel]');if(taskButton)action(taskButton,async(pid,epoch)=>{const ticket=++detailTicket,task=await b.api(endpoint(pid,'/agent-tasks/'+enc(taskButton.dataset.agentTask)));if(current(pid,epoch)&&ticket===detailTicket)renderDetail(task)});if(cancel)action(cancel,async(pid,epoch)=>{await b.api(endpoint(pid,'/agent-tasks/'+enc(cancel.dataset.agentTaskCancel)+'/cancel'),{method:'POST'});if(current(pid,epoch))await load()})};
 setInterval(()=>{if(dialog().open&&!loading&&!document.hidden&&state().projectId)load().catch(e=>error(e.message))},5000);
 window.atlasCollaboration={projectChanged,reset,load,configuration};
})();
