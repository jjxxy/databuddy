/* A suggested chat draft is not a command and must never replace user work. */
window.createFlowEditHandoff=function({projectId,flowId,digest,api,getState,setDraft,container,kind='business'}){
 const isData=kind==='data';if(kind!=='business'&&!isData)return null;
 if(typeof projectId!=='string'||!projectId||!(isData?/^wf_[a-f0-9]{40}$/:/^bf_[a-f0-9]{40}$/).test(flowId||'')||!/^[a-f0-9]{64}$/.test(digest||'')||!container||container.querySelector('.flow-edit-handoff'))return null;
 const box=document.createElement('div'),message=document.createElement('span'),button=document.createElement('button');
 box.className='flow-edit-handoff';box.setAttribute('role','status');
 message.textContent='可在对话中描述要修改的业务规则；修改只会形成候选方案。';
 button.type='button';button.className='inline-action';button.textContent='在输入框准备修改请求';
 box.append(message,button);container.prepend(box);let busy=false;
 function blocked(s){return !s||s.projectId!==projectId||s.authSealed||s.sending||s.pending||s.navigating||s.activeTurn||s.hasAttachments||s.hasDraft||s.readOnly}
 button.addEventListener('click',async()=>{
  if(busy)return;const before=getState();
  if(blocked(before)){message.textContent='当前对话有草稿、附件或进行中的任务，请先处理，再准备修改请求。';return}
  busy=true;button.disabled=true;
  try{
   const flow=await api('/projects/'+encodeURIComponent(projectId)+(isData?'/data-workflows/':'/business-flows/')+encodeURIComponent(flowId));
   const after=getState();
   if(blocked(after)||before.viewEpoch!==after.viewEpoch){message.textContent='对话状态已变化，原有内容已保留；请重新操作。';return}
   if(!flow||(isData?flow.workflow_id:flow.id)!==flowId||(isData?flow.draft_sha256:flow.candidate_digest)!==digest){message.textContent='流程候选版本已变化，请回到流程页面重新查看。';return}
   const name=String(flow.name||'未命名流程').replace(/[\x00-\x1f\x7f「」]/g,' ').trim().slice(0,100)||'未命名流程';
   const text=isData?'请修改数据工作流程「'+name+'」（任务编号 '+flowId+'，用于准确定位）。请先读取并向我展示当前草稿版本，询问我的修改需求；确认需求后，在修改前重新读取并核对当前草稿，说明改动，再创建待核对方案。未经我确认不要采用结果或发布。':'请修改业务流程「'+name+'」（流程编号 '+flowId+'，用于准确定位）。请先读取并向我展示当前候选版本，询问我的修改需求，确认需求后再创建候选方案并说明变化；未经我确认不要发布。';
   if(setDraft(text)===true){message.textContent=isData?'已核对插入时的草稿版本，并将修改请求放入输入框；请补充要求后自行发送。修改前请重新核对当前草稿。':'已核对插入时的候选版本，并将修改请求放入输入框；请补充要求后自行发送。正式修改时会再次校验版本。';button.disabled=true;box.dataset.complete='true'}
   else message.textContent='当前对话暂不能编辑，原有内容已保留；请稍后重试。';
  }catch(e){message.textContent='暂时无法核对流程版本，请回到流程页面重新查看。'}
  finally{busy=false;if(!box.dataset.complete)button.disabled=false}
 });
 return box;
};
