'use strict';
window.createChatWorkflowPanel = function({container,api,getState,setDraft}) {
 const root=document.createElement('section');root.className='chat-workflows';root.hidden=true;
 const title=document.createElement('h3');title.textContent='本项目的业务流程与结果';
 const select=document.createElement('select');select.setAttribute('aria-label','选择本项目业务流程');
 const status=document.createElement('p');status.setAttribute('role','status');const listWarning=document.createElement('p');listWarning.setAttribute('role','status');
 const graph=document.createElement('div'),dataRun=document.createElement('div'),businessRun=document.createElement('div'),debugRun=document.createElement('div'),schedule=document.createElement('div'),businessSchedule=document.createElement('div'),result=document.createElement('div');
 root.append(title,select,listWarning,status,graph,dataRun,businessRun,debugRun,schedule,businessSchedule,result);container.append(root);
 const resultView=window.createChatWorkflowResults?.({container:result,api,getState});
 window.createChatWorkflowSchedule?.({container:schedule,api,getState,onUpdated:()=>refresh(true)});
 window.createChatBusinessSchedule?.({container:businessSchedule,api,getState,onUpdated:()=>refresh(true)});
 window.createChatBusinessWorkflowRun?.({container:businessRun,api,getState,onUpdated:()=>refresh(true)});
 window.createChatBusinessDebug?.({container:debugRun,api,getState});
 window.createChatDataWorkflowRun?.({container:dataRun,api,getState,onUpdated:()=>refresh(true)});
 let expandedButton=null;
 function setExpanded(on){document.body.classList.toggle('workflow-expanded',on);if(expandedButton){expandedButton.setAttribute('aria-pressed',String(on));expandedButton.textContent=on?'退出全屏':'全屏查看'}window.nexusShell?.syncLayout?.()}
 window.addEventListener('nexus-exit-workflow-expanded',()=>{setExpanded(false);expandedButton?.focus()});
 let items=[],generation=0,selection=0,project='',lastFetch=0,loading=false,listSignature='',selectedWorkflow=null,pendingOpen=null;
 const stateOk=(pid,epoch)=>{const s=getState();return !s.authSealed&&s.projectId===pid&&s.viewEpoch===epoch};
 function event(workflow){clearNode();selectedWorkflow=null;window.dispatchEvent(new CustomEvent('nexus-chat-business-flow-selected',{detail:{projectId:project,flow:null}}));window.dispatchEvent(new CustomEvent('nexus-chat-workflow-selected',{detail:{projectId:project,workflow}}))}
 function clear(){setExpanded(false);expandedButton=null;generation++;selection++;items=[];project='';lastFetch=0;loading=false;listSignature='';pendingOpen=null;root.hidden=true;select.replaceChildren();graph.replaceChildren();status.textContent='';listWarning.textContent='';event(null)}
 function clearNode(){if(selectedWorkflow)delete selectedWorkflow.node_id;nodeStatus.textContent='';nodeStatus.hidden=true}
 const nodeStatus=document.createElement('p');nodeStatus.hidden=true;nodeStatus.setAttribute('role','status');status.after(nodeStatus);
 function overview(options,renderDetail){
  if(typeof window.renderWorkflowOverview!=='function'){renderDetail(graph);return}
  const summary=document.createElement('div'),details=document.createElement('div');details.hidden=true;graph.replaceChildren(summary,details);
  let detailsMark=0;function show(render,label){clearNode();setExpanded(false);expandedButton=null;const token=++detailsMark;details.hidden=false;details.replaceChildren();const close=document.createElement('button');close.type='button';close.textContent='收起'+label;const content=document.createElement('div');details.append(close,content);close.onclick=()=>{clearNode();setExpanded(false);expandedButton=null;detailsMark++;details.hidden=true;details.replaceChildren()};if(label==='完整流程'){const full=document.createElement('button');full.type='button';full.textContent='全屏查看';full.setAttribute('aria-pressed','false');full.onclick=()=>setExpanded(!document.body.classList.contains('workflow-expanded'));expandedButton=full;details.insertBefore(full,content)}render(content,()=>token===detailsMark&&options.isCurrent());details.scrollIntoView?.({block:'nearest'})}
  window.renderWorkflowOverview(summary,{...options,onDetail:()=>show(content=>renderDetail(content),'完整流程'),onGroup:ids=>{show((content,isCurrent)=>window.renderWorkflowGroupDetail(content,{...options,ids,api,isCurrent}),'详细步骤');if(options.kind==='business')window.dispatchEvent(new CustomEvent('nexus-debug-select-group',{detail:{projectId:options.projectId,flowId:options.flowId,ids}}))}});
 }
 async function open(key){setExpanded(false);expandedButton=null;const current=items.find(x=>x.key===key);const pid=project,epoch=getState().viewEpoch,mark=++selection;graph.replaceChildren();event(null);if(!current)return;status.textContent='正在读取保存的流程…';
  try {const value=await api('/projects/'+encodeURIComponent(pid)+(current.kind==='data'?'/data-workflows/':'/business-flows/')+encodeURIComponent(current.id));if(mark!==selection||!stateOk(pid,epoch))return;
   const isData=current.kind==='data';
   if((isData?value.workflow_id:value.id)!==current.id)throw Error('流程不一致');
   const role=value.candidate_document?'candidate':'published';
   const plan=isData?value.document?.plan:(value.candidate_document||value.published_document);
   const version=isData?{digest:value.draft_sha256}:role==='candidate'?{role,digest:value.candidate_digest,commit:value.candidate_commit}:{role,digest:value.published_digest,commit:value.published_commit};
   const isCurrent=()=>mark===selection&&stateOk(pid,epoch);
   let assets=null,assetError=false;
   if(window.loadWorkflowCodeAssets&&plan){
    const expectedNodes=isData?[...Object.keys(plan.inputs||{}),...(plan.stages||[]).map(node=>node.id),...(typeof plan.output==='string'?['__overview_result__']:[])]:plan.nodes.map(node=>node.id);
    try {assets=await window.loadWorkflowCodeAssets({kind:current.kind,projectId:pid,flowId:current.id,...version,expectedNodes,api,isCurrent})}
    catch(_){assetError=true}
    if(!isCurrent())return;
   }
   const options={kind:current.kind,workflow:isData?value:null,plan,version,run:value.latest_run||null,projectId:pid,flowId:current.id,isCurrent,assets,assetError};
   const editableNodes=new Map();
   if(isData){
    for(const [id,schema] of Object.entries(plan?.inputs||{}))editableNodes.set(id,schema.meaning||id);
    for(const node of plan?.stages||[])editableNodes.set(node.id,node.description||node.id);
   }else for(const node of plan?.nodes||[])editableNodes.set(node.id,node.title||node.id);
   options.selectNode=(id,anchor)=>{
    if(!isCurrent()||!selectedWorkflow||!graph.contains(anchor))return;
    clearNode();
    if(!editableNodes.has(id))return;
    selectedWorkflow.node_id=id;
    nodeStatus.textContent='当前对话所指步骤：'+editableNodes.get(id);
    nodeStatus.hidden=false;
   };
   const openNode=(container,id)=>{options.selectNode(id,container);options.showNode(container,id)};
   options.showNode=(container,id)=>{
    assets?.showNode(container,id);
    window.renderWorkflowNodeIO?.(container,id,options);
   };
   if(isData){
    status.textContent='展示当前保存的任务做法，运行结果需结合对应版本核对。';
    overview(options,target=>window.renderBusinessWorkflowGraph(target,value,{projectId:pid,request:url=>api(url.replace(/^\/api/,'')),isCurrent,onNode:openNode}));
    event(value);
   }else{
    status.textContent='展示保存的业务流程；步骤完成不等于业务结果已核对。';
    overview(options,target=>{
     window.NexusBusinessFlowView.render(target,{plan,version,run:options.run,onNode:openNode});
     const label=target.querySelector('.flow-version');
     if(label&&role==='candidate')label.textContent='当前方案';
     const notices=target.querySelector('.flow-notices');
     if(notices){
      const versions=[...notices.querySelectorAll('p')].filter(p=>p.textContent.startsWith('代码版本：')||p.textContent.startsWith('方案版本：'));
      if(versions.length){
       const disclosure=document.createElement('details'),summary=document.createElement('summary');
       summary.textContent='查看版本依据';disclosure.append(summary,...versions);notices.prepend(disclosure);
      }
     }
    });
    window.dispatchEvent(new CustomEvent('nexus-chat-business-flow-selected',{detail:{projectId:pid,flow:value,role}}));
   }
   const digest=current.kind==='data'?value.draft_sha256:value.candidate_digest;
   if(/^[a-f0-9]{64}$/.test(digest||''))selectedWorkflow={kind:current.kind,id:current.id,digest,project:pid,epoch};
  } catch(e){if(mark===selection&&stateOk(pid,epoch))status.textContent='流程暂时无法读取，请稍后重试。';}
 }
 async function refresh(force=false){const s=getState(),pid=s.projectId,epoch=s.viewEpoch;if(!pid||s.authSealed){clear();return}if(project!==pid){const incoming=pendingOpen;clear();project=pid;if(incoming?.project===pid&&incoming.epoch===epoch)pendingOpen=incoming}if(loading||(!force&&Date.now()-lastFetch<10000))return;const mark=++generation;loading=true;
  try {const [data,business]=await Promise.allSettled([api('/projects/'+encodeURIComponent(pid)+'/data-workflows'),api('/projects/'+encodeURIComponent(pid)+'/business-flows')]);if(mark!==generation||!stateOk(pid,epoch))return;lastFetch=Date.now();const failedData=data.status==='rejected',failedBusiness=business.status==='rejected';listWarning.textContent=failedData&&failedBusiness?'暂时无法更新流程列表，已保留本项目已加载的列表，请稍后重试。':failedData?'数据任务暂时无法更新，已保留本项目已加载的列表，请稍后重试。':failedBusiness?'业务流程暂时无法更新，已保留本项目已加载的列表，请稍后重试。':'';const all=items.filter(x=>(x.kind==='data'&&failedData)||(x.kind==='business'&&failedBusiness));if(data.status==='fulfilled')for(const x of data.value?.items||[])if(/^wf_[a-f0-9]{40}$/.test(x.id||''))all.push({kind:'data',id:x.id,key:'data:'+x.id,name:x.name||'数据任务',version:JSON.stringify([x.draft_sha256,x.active_sha256,x.candidate_run,x.published_run])});if(business.status==='fulfilled')for(const x of (Array.isArray(business.value)?business.value:business.value?.items||[]))if(/^bf_[a-f0-9]{40}$/.test(x.id||''))all.push({kind:'business',id:x.id,key:'business:'+x.id,name:x.name||'业务流程',version:JSON.stringify([x.candidate_digest,x.published_digest,x.latest_run])});
   const previous=select.value,signature=JSON.stringify(all);const changed=signature!==listSignature;listSignature=signature;items=all;select.replaceChildren();for(const item of items){const option=document.createElement('option');option.value=item.key;option.textContent=item.name;select.append(option)}root.hidden=!items.length&&data.status==='fulfilled'&&business.status==='fulfilled';if(!items.length){graph.replaceChildren();event(null);status.textContent='';return}const key=items.some(x=>x.key===previous)?previous:items[0].key;select.value=key;const selected=items.find(x=>x.key===key);if((selected.kind==='data'&&failedData)||(selected.kind==='business'&&failedBusiness)){selection++;graph.replaceChildren();event(null);status.textContent='这项流程暂时无法更新，请稍后查看。';return}if(force||changed||key!==previous||!graph.childNodes.length)await open(key);
  }finally{if(mark===generation){loading=false;openRequested()}}
 }
 function openRequested(){const request=pendingOpen;if(!request||loading)return;if(!stateOk(request.project,request.epoch)||project!==request.project){pendingOpen=null;return}const item=items.find(x=>x.id===request.id);pendingOpen=null;if(!item){status.textContent='这项流程暂时无法读取，请稍后再试。';root.hidden=false;return}select.value=item.key;void open(item.key);root.scrollIntoView?.({block:'nearest'})}
 select.addEventListener('change',()=>void open(select.value));
 window.addEventListener('nexus-open-chat-flow',event=>{const id=event.detail?.id,s=getState();if(!/^(?:bf|wf)_[a-f0-9]{40}$/.test(id||'')||!s.projectId||s.authSealed)return;pendingOpen={id,project:s.projectId,epoch:s.viewEpoch};if(loading)return;void refresh(true).then(openRequested)});
 return {refresh,clear,getSelectedWorkflow(){const v=selectedWorkflow,s=getState();const dock=root.closest('#workspace-artifacts-panel'),workflowHost=root.closest('#workflow-panel');return v&&(!dock||!dock.hidden)&&(!workflowHost||!workflowHost.hidden)&&stateOk(v.project,v.epoch)&&!s.navigating&&!s.sending?{kind:v.kind,id:v.id,digest:v.digest,...(v.node_id?{node_id:v.node_id}:{})}:null}};
};
