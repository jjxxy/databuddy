'use strict';
/* An overview groups real consecutive plan nodes; it never invents dependencies. */
window.renderWorkflowOverview=function(container,{kind,workflow,plan,run,version,assets,assetError,onDetail,onGroup}){
 const doc=container.ownerDocument,el=(tag,text,cls)=>{const n=doc.createElement(tag);if(text!=null)n.textContent=String(text);if(cls)n.className=cls;return n};
 container.replaceChildren();const nodes=[];
 const stageLabels={select:'选择字段',drop:'整理字段',filter:'筛选记录',reclassify:'业务分类',compute:'计算指标',aggregate:'汇总数据',lookup:'关联资料',lookup_multi:'关联资料',lookup_effective:'按有效期匹配',fx_convert:'币种换算',union_rows:'合并资料',normalize:'规范数据',sql:'查询数据'};
 const businessLabels={source:'获取资料',source_table:'读取资料',api_capture:'采集接口数据',collect:'获取资料',parse:'解析资料',transform:'处理数据',data_workflow:'处理数据',table_workflow:'加工数据',quality_gate:'自动核对数据',human_review:'人工复核',model:'模型分析',skill:'执行工作方法',template:'应用模板',office:'生成办公文件',office_document:'生成办公文件',html:'生成报告',html_report:'生成报告',table_report_spec:'整理报告',output:'生成结果'};
 if(kind==='data'){
  const p=workflow?.document?.plan;if(!p||!p.inputs||!Array.isArray(p.stages)){container.append(el('p','暂无可展示的流程。'));return}
  for(const [id,v] of Object.entries(p.inputs))nodes.push({id,label:typeof v?.meaning==='string'&&v.meaning.trim()?v.meaning:'读取资料',deps:[]});
  for(const s of p.stages)nodes.push({id:s?.id,label:stageLabels[s?.kind]||'处理数据',deps:[...(typeof s?.input==='string'?[s.input]:[]),...(typeof s?.right==='string'?[s.right]:[]),...(Array.isArray(s?.inputs)?s.inputs:[])]});
  if(typeof p.output==='string')nodes.push({id:'__overview_result__',label:'生成结果',deps:[p.output]});
 }else if(kind==='business'&&Array.isArray(plan?.nodes)){
  const definitions=new Map(plan.nodes.filter(s=>s&&typeof s.id==='string').map(s=>[s.id,s]));
  for(const s of plan.nodes){
   let material='';
   if(s?.kind==='html_report')material='生成网页报告';
   else if(s?.kind==='office_document'){
    const ref=s.inputs?.spec,source=ref?.source==='node'?definitions.get(ref.name):null;
    material=source?.kind==='table_report_spec'&&source.config?.format==='presentation'?'生成汇报 PPT':'生成办公文件';
   }
   nodes.push({id:s?.id,label:typeof s?.title==='string'&&s.title.trim()?s.title:businessLabels[s?.kind]||'业务处理',material,deps:Array.isArray(s?.depends_on)?s.depends_on:[]});
  }
 }else{container.append(el('p','暂无可展示的流程。'));return}
 const warning=el('p',null,'workflow-overview-notice');let status='尚未运行';
 if(kind==='data'){
  const r=workflow?.latest_run;
  if(r){const names={queued:'等待执行',running:'正在处理',retrying:'等待重试',validated:'结果待核对',succeeded:'已运行',failed:'运行未完成',cancelled:'运行已停止'};status=(r.matches_current_draft===true?'当前做法：':r.matches_current_draft===false?'其他版本：':'版本关系待核对：')+(names[r.status]||'状态待核对');}
  if(workflow?.published_run&&workflow?.draft_sha256&&workflow?.published_sha256&&workflow.draft_sha256!==workflow.published_sha256)status+='；当前做法与已采用版本不同';
 }else if(run){const same=!!version?.digest&&!!version?.commit&&run.plan_digest===version.digest&&run.commit===version.commit;const names={queued:'等待执行',running:'正在处理',succeeded:'已运行',failed:'运行未完成',unknown:'运行状态待核对',cancelled:'运行已停止',awaiting_review:'等待人工复核'};status=same?(names[run.status]||'状态待核对'):'运行记录与当前版本不一致';if(same&&Array.isArray(run.steps)&&run.steps.some(s=>['failed','unknown','blocked'].includes(s?.status)))status+='；有步骤未完成或状态待核对，请查看完整流程';}
 warning.textContent=status+'；结果和财务口径仍需核对。';container.append(warning);
 if(!nodes.length){container.append(el('p','当前流程没有可展示的步骤。'));return}
 const materials=[...new Set(nodes.map(n=>n.material).filter(Boolean))];
 if(materials.length)container.append(el('p','流程包含的成果步骤：'+materials.join('、')+'。是否生成成功请查看运行结果。','workflow-overview-materials'));
 assets?.showProject(container);
 if(assetError)container.append(el('p','项目流程文件暂时无法核验，以下按已读取的方案展示步骤。','workflow-overview-warning'));
 const seen=new Set();let valid=true;for(const n of nodes){if(typeof n.id!=='string'||!n.id||seen.has(n.id)||n.deps.some(id=>typeof id!=='string'||!seen.has(id)))valid=false;seen.add(n.id)}
 if(!valid){container.append(el('p','步骤依赖不完整或存在循环，请查看完整流程。','workflow-overview-warning'));}
 else{
  let groups;
  if(assets){
   groups=assets.groups.map((group,index)=>({...group,caption:group.nodeIds.length+' 个步骤',direction:'vertical',x:0,y:index*112}));
  }else{
  const size=Math.max(1,Math.ceil(nodes.length/5)),owner=new Map();groups=[];
  for(let i=0;i<nodes.length;i+=size){const groupNodes=nodes.slice(i,i+size),index=groups.length,labels=[...new Set([...groupNodes.map(x=>x.material).filter(Boolean),...groupNodes.map(x=>x.label)])];const item={id:'group_'+index,label:labels.slice(0,2).join('、')+(labels.length>2?'等':''),caption:groupNodes.length+' 个步骤',nodeIds:groupNodes.map(n=>n.id),direction:'vertical',deps:[],x:0,y:index*112};groups.push(item);for(const n of groupNodes)owner.set(n.id,item.id)}
  for(const n of nodes){const target=groups.find(g=>g.id===owner.get(n.id));for(const dep of n.deps){const source=owner.get(dep);if(source&&source!==target.id&&!target.deps.includes(source))target.deps.push(source)}}
  }
  container.append(el('p','概览按步骤分组；组间连线表示至少存在一条依赖，具体关系请查看完整流程。','workflow-overview-note'));
  const canvas=el('div',null,'workflow-overview-graph');canvas.style.setProperty('--workflow-overview-height',String(Math.max(180,groups.length*112+40))+'px');container.append(canvas);
  if(window.NexusFlowGraph)window.NexusFlowGraph.mount(canvas,groups,id=>{const group=groups.find(g=>g.id===id);if(group)onGroup?.(group.nodeIds)});
  else{const list=el('ol');for(const g of groups){const item=el('li'),button=el('button',g.label+'（'+g.caption+'）');button.type='button';button.onclick=()=>onGroup?.(g.nodeIds);item.append(button);list.append(item)}canvas.append(list)}
 }
 const button=el('button','查看完整流程','workflow-overview-detail');button.type='button';button.addEventListener('click',()=>onDetail?.());container.append(button);
};

/* Details show only recorded plan nodes. Data values are loaded only on explicit request. */
window.renderWorkflowGroupDetail=function(container,{kind,ids,workflow,plan,run,version,projectId,flowId,api,isCurrent,assets,showNode,selectNode}){
 const doc=container.ownerDocument,el=(tag,text)=>{const n=doc.createElement(tag);if(text!=null)n.textContent=String(text);return n};
 const wanted=new Set(ids), p=kind==='data'?workflow?.document?.plan:plan, items=[];
 const labels={select:'选择字段',drop:'整理字段',filter:'筛选记录',reclassify:'业务分类',compute:'计算指标',aggregate:'汇总数据',lookup:'关联资料',lookup_multi:'关联资料',lookup_effective:'按有效期匹配',fx_convert:'币种换算',union_rows:'合并资料',normalize:'规范数据',sql:'查询数据'};
 if(kind==='data'&&p){for(const [id,v] of Object.entries(p.inputs||{}))items.push({id,title:v?.meaning||'读取资料',deps:[]});for(const s of p.stages||[])items.push({id:s.id,title:s.description||labels[s.kind]||'处理数据',deps:[s.input,s.right,...(Array.isArray(s.inputs)?s.inputs:[])].filter(x=>typeof x==='string')});if(typeof p.output==='string')items.push({id:'__overview_result__',title:'生成结果',deps:[p.output]});}
 if(kind==='business')for(const n of p?.nodes||[])items.push({id:n.id,title:n.title||'业务处理',deps:Array.isArray(n.depends_on)?n.depends_on:[]});
 const byId=new Map(items.map(x=>[x.id,x]));container.replaceChildren(el('h4','这部分的详细步骤'));
 const selectedItems=items.filter(item=>wanted.has(item.id));
 const sections=new Map();
 if(assets&&window.NexusFlowGraph&&selectedItems.length>1){
  const canvas=el('div');canvas.className='workflow-group-graph';
  const graphNodes=selectedItems.map((item,index)=>({
   id:item.id,label:item.title,caption:'查看节点文件',direction:'vertical',x:0,y:index*112,
   deps:item.deps.filter(id=>wanted.has(id))
  }));
  canvas.setAttribute('aria-label','本环节内的执行节点');
  container.append(canvas);
  window.NexusFlowGraph.mount(canvas,graphNodes,id=>{
   const section=sections.get(id);
   if(section)selectNode?.(id,section);
   const file=section?.querySelector('.workflow-file-links button');
   for(let folder=file?.closest('details');folder&&section.contains(folder);folder=folder.parentElement?.closest('details'))folder.open=true;
   section?.scrollIntoView?.({block:'nearest'});
   file?.focus({preventScroll:true});
  });
 }
 const verified=kind==='business'&&!!version?.digest&&!!version?.commit&&run?.plan_digest===version.digest&&run?.commit===version.commit&&run?.flow_id===flowId;
 for(const item of selectedItems){const section=el('section'),heading=el('h5',item.title);sections.set(item.id,section);section.className='workflow-step-files';section.append(heading);
  if(selectNode){
   const choose=el('button',item.title);choose.type='button';heading.replaceChildren(choose);
   section.addEventListener('click',()=>selectNode(item.id,section));
  }
  (showNode||assets?.showNode)?.(section,item.id);if(item.deps.length)section.append(el('p','来自：'+item.deps.map(id=>byId.get(id)?.title||id).join('、')));
  if(kind==='business'){const step=verified&&Array.isArray(run.steps)?run.steps.find(x=>x?.node_id===item.id):null;section.append(el('p',step?'本次步骤状态：'+({succeeded:'已完成',failed:'未完成',running:'运行中',blocked:'未执行',unknown:'待核对',awaiting_review:'等待人工复核'}[step.status]||'待核对'):'当前版本没有可核验的步骤运行记录。'));section.append(el('p','实际输入明细：运行记录未提供，请结合来源与运行记录核对。'));
   if(step?.status==='succeeded'&&step.table_available===true&&['succeeded','failed','cancelled','awaiting_review'].includes(run.status)&&/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(item.id)&&/^[a-zA-Z0-9_-]{1,128}$/.test(run.id||'')){const button=el('button','查看这一步的实际输出'),result=el('div');button.type='button';button.onclick=async()=>{button.disabled=true;result.replaceChildren(el('p','正在读取…'));try{const path='/projects/'+encodeURIComponent(projectId)+'/business-flows/'+encodeURIComponent(flowId)+'/runs/'+encodeURIComponent(run.id)+'/tables/'+encodeURIComponent(item.id)+'?offset=0&limit=20';const data=await api(path);if(!isCurrent()||!container.contains(result))return;if(data.run_id!==run.id||data.node_id!==item.id||!Array.isArray(data.columns)||!Array.isArray(data.rows)||data.rows.length>20||data.offset!==0||!Number.isSafeInteger(data.total)||data.total<0||data.rows.length!==Math.min(20,data.total))throw Error('记录不一致');const table=el('table'),head=el('tr');for(const col of data.columns.slice(0,100))head.append(el('th',String(col.label||col.name).slice(0,200)));table.append(head);for(const row of data.rows){const tr=el('tr');for(const col of data.columns.slice(0,100)){const v=row?.[col.name];tr.append(el('td',(v==null?'—':typeof v==='object'?JSON.stringify(v):String(v)).slice(0,1000)))}table.append(tr)}result.replaceChildren(el('p',(run.status==='succeeded'?'':'整体流程未完成；以下是已完成步骤的输出。')+'实际输出：共 '+data.total+' 条，显示前 '+data.rows.length+' 条。'),table)}catch(_){if(isCurrent()&&container.contains(result))result.replaceChildren(el('p','输出暂时无法读取或核验。'))}finally{if(isCurrent()&&container.contains(button))button.disabled=false}};section.append(button,result)}else section.append(el('p','实际输出明细：此步骤没有可核验的表格记录。'));
  }else section.append(el('p','实际输入与中间输出：当前记录未提供逐步骤明细，请结合运行结果核对。'));container.append(section);}
};
