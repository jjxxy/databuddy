'use strict';
const stageLabels={select:'选择字段',drop:'整理字段',filter:'筛选记录',reclassify:'业务分类',compute:'计算指标',aggregate:'汇总数据',lookup:'关联资料',lookup_multi:'关联资料',lookup_effective:'按有效期匹配',fx_convert:'币种换算',union_rows:'合并资料',normalize:'规范数据',sql:'查询数据'};
function stageInputs(stage){
 return [stage.input,stage.right,...(Array.isArray(stage.inputs)?stage.inputs:[])].filter(id=>typeof id==='string');
}
// Expand exactly the operations in each saved table plan. The qualified ID is
// local to the graph; chat, debugging and run receipts still belong to its owner.
window.workflowDetailedBusinessItems=function(plan){
 const nodes=(plan?.nodes||[]).filter(node=>typeof node?.id==='string');
 const outputIds=new Map();
 for(const node of nodes){
  const p=node.kind==='table_workflow'&&node.config?.plan;
  if(p&&Array.isArray(p.stages)&&p.stages.some(s=>s.id===p.output))outputIds.set(node.id,node.id+':'+p.output);
 }
 const output=id=>outputIds.get(id)||id, items=[];
 for(const node of nodes){
  const deps=Array.isArray(node.depends_on)?node.depends_on:[], p=node.config?.plan;
  if(!outputIds.has(node.id)){
   items.push({...node,title:node.title||'业务处理',owner:node.id,deps:deps.map(output)});
   continue;
  }
  const local=new Set(p.stages.map(s=>s.id));
  const bound=new Map(Object.entries(node.inputs||{}).filter(([alias,ref])=>Object.hasOwn(p.inputs||{},alias)&&ref?.source==='node').map(([alias,ref])=>[alias,ref.name]));
  const used=new Set(p.stages.flatMap(stageInputs).map(id=>bound.get(id)));
  const orderOnly=deps.filter(id=>!used.has(id)).map(output);
  for(const rule of p.stages){
   const id=node.id+':'+rule.id, inputs=stageInputs(rule);
   const upstream=inputs.map(name=>local.has(name)?node.id+':'+name:bound.has(name)?output(bound.get(name)):null).filter(Boolean);
   // A prerequisite such as a review gate controls the owner without supplying table rows.
   if(!inputs.some(name=>local.has(name)))upstream.push(...orderOnly);
   const stage={id,owner_id:node.id,stage_id:rule.id,rule};
   const title=rule.description||stageLabels[rule.kind]||rule.id;
   items.push({id,owner:node.id,stage,title,kind:rule.kind,description:node.title,deps:[...new Set(upstream)]});
  }
 }
 return items;
};
window.showWorkflowStage=function(container,stage,assets){
 if(!assets?.showNode(container,stage.id)){
  const doc=container.ownerDocument,heading=doc.createElement('h6'),pre=doc.createElement('pre'),viewer=doc.createElement('div');
  viewer.className='workflow-file-viewer';
  const sql=stage.rule.kind==='sql'&&typeof stage.rule.sql==='string';
  heading.textContent=sql?'步骤定义中保存的 SQL':'保存的步骤规则';
  pre.textContent=sql?stage.rule.sql:JSON.stringify(stage.rule,null,2);pre.tabIndex=0;
  viewer.append(heading,pre);container.append(viewer);
 }
 const note=container.ownerDocument.createElement('p');
 note.textContent='此步随所属加工节点一起运行，运行记录按加工节点保存。';
 container.append(note);
};
/* An overview groups real consecutive plan nodes; it never invents dependencies. */
window.renderWorkflowOverview=function(container,{kind,workflow,plan,run,version,assets,assetError,onDetail,onGroup}){
 const doc=container.ownerDocument,el=(tag,text,cls)=>{const n=doc.createElement(tag);if(text!=null)n.textContent=String(text);if(cls)n.className=cls;return n};
 container.replaceChildren();const nodes=[];
 const businessLabels={source:'获取资料',source_table:'读取资料',api_capture:'采集接口数据',collect:'获取资料',parse:'解析资料',transform:'处理数据',data_workflow:'处理数据',table_workflow:'加工数据',quality_gate:'自动核对数据',human_review:'人工复核',model:'模型分析',skill:'执行工作方法',template:'应用模板',office:'生成办公文件',office_document:'生成办公文件',html:'生成报告',html_report:'生成报告',table_report_spec:'整理报告',output:'生成结果'};
 if(kind==='data'){
  const p=workflow?.document?.plan;if(!p||!p.inputs||!Array.isArray(p.stages)){container.append(el('p','暂无可展示的流程。'));return}
  for(const [id,v] of Object.entries(p.inputs))nodes.push({id,kind:'source',label:typeof v?.meaning==='string'&&v.meaning.trim()?v.meaning:'读取资料',deps:[]});
  for(const s of p.stages)nodes.push({id:s?.id,kind:s?.kind,label:s?.description||stageLabels[s?.kind]||'处理数据',deps:stageInputs(s)});
  if(typeof p.output==='string')nodes.push({id:'__overview_result__',kind:'output',label:'交付已选定的结果',deps:[p.output]});
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
  const stepCounts=new Map();
  if(kind==='business')for(const step of window.workflowDetailedBusinessItems(plan))stepCounts.set(step.owner,(stepCounts.get(step.owner)||0)+1);
  const caption=ids=>ids.reduce((total,id)=>total+(stepCounts.get(id)||1),0)+' 个步骤';
  let groups;
  if(assets&&!assets.usePlanGroups){
   groups=assets.groups.map((group,index)=>({...group,caption:caption(group.nodeIds),direction:'vertical',x:0,y:index*112}));
  }else{
  const owner=new Map();groups=[];
  let blocks=[];
  if(kind==='data'){
   const many=workflow.document.plan.stages.length>6;
   for(const n of nodes){
    let phase='整理与加工数据';
    if(n.kind==='source')phase='读取来源资料';
    else if(n.kind==='output')phase='交付结果';
    else if(many&&['lookup','lookup_multi','lookup_effective','union_rows'].includes(n.kind))phase='关联与合并资料';
    else if(many&&['compute','aggregate','fx_convert'].includes(n.kind))phase='计算与汇总指标';
    n.phase=phase;
    if(blocks.at(-1)?.[0].phase!==phase)blocks.push([]);
    blocks.at(-1).push(n);
   }
   // Match the persisted grouping's size cap without changing the frozen files.
   while(blocks.length>6){
    let index=0;
    for(let j=1;j<blocks.length-1;j++)if(blocks[j].length+blocks[j+1].length<blocks[index].length+blocks[index+1].length)index=j;
    blocks.splice(index,2,blocks[index].concat(blocks[index+1]));
   }
  }else{
   const size=Math.max(1,Math.ceil(nodes.length/5));
   for(let i=0;i<nodes.length;i+=size)blocks.push(nodes.slice(i,i+size));
  }
  for(const groupNodes of blocks){const index=groups.length,labels=[...new Set([...groupNodes.map(x=>x.material).filter(Boolean),...groupNodes.map(x=>x.label)])];const item={id:'group_'+index,label:labels.slice(0,2).join('、')+(labels.length>2?'等':''),caption:caption(groupNodes.map(node=>node.id)),nodeIds:groupNodes.map(n=>n.id),direction:'vertical',deps:[],x:0,y:index*112};if(kind==='data'&&groupNodes.length>1)item.label=groupNodes.every(n=>n.phase===groupNodes[0].phase)?groupNodes[0].phase:groupNodes[0].label+'至'+groupNodes.at(-1).label;
   groups.push(item);for(const n of groupNodes)owner.set(n.id,item.id)}
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
 if(kind==='data'&&p){for(const [id,v] of Object.entries(p.inputs||{}))items.push({id,title:v?.meaning||'读取资料',deps:[]});for(const s of p.stages||[])items.push({id:s.id,title:s.description||stageLabels[s.kind]||'处理数据',deps:stageInputs(s)});if(typeof p.output==='string')items.push({id:'__overview_result__',title:'生成结果',deps:[p.output]});}
 if(kind==='business')items.push(...window.workflowDetailedBusinessItems(p));
 const byId=new Map(items.map(x=>[x.id,x]));
 container.replaceChildren(el('h4','详细步骤'));
 const steps=items.filter(item=>wanted.has(item.owner||item.id));
 const visible=new Set(steps.map(item=>item.id));
 if(!steps.length){container.append(el('p','本环节没有可展示的步骤。'));return}
 const verified=kind==='business'&&!!version?.digest&&!!version?.commit&&run?.plan_digest===version.digest&&run?.commit===version.commit&&run?.flow_id===flowId;
 const receipts=new Map(verified&&Array.isArray(run.steps)?run.steps.map(step=>[step.node_id,step]):[]);
 const states={succeeded:'已完成',failed:'未完成',running:'运行中',blocked:'未执行',unknown:'待核对',awaiting_review:'等待人工复核'};
 const canvas=el('div'),inspector=el('div');
 canvas.className='workflow-group-graph';
 canvas.setAttribute('aria-label','本环节内的执行节点');
 container.append(canvas,inspector);
 function showReceipt(section,id,buttonLabel='查看这一步的实际输出'){
   const step=receipts.get(id);section.append(el('p',step?'本次步骤状态：'+(states[step.status]||'待核对'):'当前版本没有可核验的步骤运行记录。'));section.append(el('p','实际输入明细：运行记录未提供，请结合来源与运行记录核对。'));
   if(step?.status==='succeeded'&&step.table_available===true&&['succeeded','failed','cancelled','awaiting_review'].includes(run.status)&&/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(id)&&/^[a-zA-Z0-9_-]{1,128}$/.test(run.id||'')){const button=el('button',buttonLabel),result=el('div');button.type='button';button.onclick=async()=>{button.disabled=true;result.replaceChildren(el('p','正在读取…'));try{const path='/projects/'+encodeURIComponent(projectId)+'/business-flows/'+encodeURIComponent(flowId)+'/runs/'+encodeURIComponent(run.id)+'/tables/'+encodeURIComponent(id)+'?offset=0&limit=20';const data=await api(path);if(!isCurrent()||!container.contains(result))return;if(data.run_id!==run.id||data.node_id!==id||!Array.isArray(data.columns)||!Array.isArray(data.rows)||data.rows.length>20||data.offset!==0||!Number.isSafeInteger(data.total)||data.total<0||data.rows.length!==Math.min(20,data.total))throw Error('记录不一致');const table=el('table'),head=el('tr');for(const col of data.columns.slice(0,100))head.append(el('th',String(col.label||col.name).slice(0,200)));table.append(head);for(const row of data.rows){const tr=el('tr');for(const col of data.columns.slice(0,100)){const v=row?.[col.name];tr.append(el('td',(v==null?'—':typeof v==='object'?JSON.stringify(v):String(v)).slice(0,1000)))}table.append(tr)}result.replaceChildren(el('p',(run.status==='succeeded'?'':'整体流程未完成；以下是已完成步骤的输出。')+'实际输出：共 '+data.total+' 条，显示前 '+data.rows.length+' 条。'),table)}catch(_){if(isCurrent()&&container.contains(result))result.replaceChildren(el('p','输出暂时无法读取或核验。'))}finally{if(isCurrent()&&container.contains(button))button.disabled=false}};section.append(button,result)}else section.append(el('p','实际输出明细：此步骤没有可核验的表格记录。'));
 }
 function openStep(id){
  if(!visible.has(id)||!byId.has(id)||isCurrent?.()===false)return;
  const item=byId.get(id),section=el('section');
  section.className='workflow-step-files';
  section.append(el('h5',item.title));
  inspector.replaceChildren(section);
  if(item.stage){
   selectNode?.(item.owner,section,item.stage.stage_id);
   window.showWorkflowStage(section,item.stage,assets);
   if(receipts.has(item.owner)){
    const parentRecord=el('details');
    parentRecord.append(el('summary','查看所属加工节点「'+item.description+'」的运行记录'));
    showReceipt(parentRecord,item.owner,'查看所属加工节点的实际输出');
    section.append(parentRecord);
   }
   if(item.deps.length)section.append(el('p','依赖：'+item.deps.map(dep=>byId.get(dep)?.title||dep).join('、')));
   return;
  }
  selectNode?.(id,section);
  if(!assets)section.append(el('p','此版本暂无可查看的节点源码。'));
  (showNode||assets?.showNode)?.(section,item.id);if(item.deps.length)section.append(el('p','来自：'+item.deps.map(id=>byId.get(id)?.title||id).join('、')));
  if(kind==='business')showReceipt(section,id);
  else section.append(el('p','实际输入与中间输出：当前记录未提供逐步骤明细，请结合运行结果核对。'));
 }
 // Steps are stored in topological order. Put parallel branches side by side
 // and keep dependencies from other groups in the selected step's description.
 const ranks=new Map(),columns=new Map();
 const nodes=steps.map(item=>{
  const deps=item.deps.filter(id=>visible.has(id));
  const rank=deps.reduce((max,id)=>Math.max(max,(ranks.get(id)??-1)+1),0);
  ranks.set(item.id,rank);
  const column=columns.get(rank)||0;
  columns.set(rank,column+1);
  return {id:item.id,label:item.title,caption:states[receipts.get(item.id)?.status]||'查看步骤详情',
   direction:'vertical',deps,x:column*264,y:rank*112};
 });
 if(window.NexusFlowGraph){
  canvas.style.setProperty('--group-graph-height',Math.min(560,Math.max(180,columns.size*112+40))+'px');
  window.NexusFlowGraph.mount(canvas,nodes,openStep);
 }else{
  const list=el('ol'),buttons=new Map();
  for(const node of nodes){
   const entry=el('li'),button=el('button',node.label);
   button.type='button';button.setAttribute('aria-pressed','false');buttons.set(node.id,button);
   button.onclick=()=>{
    for(const [id,other] of buttons)other.setAttribute('aria-pressed',String(id===node.id));
    openStep(node.id);
   };
   entry.append(button);list.append(entry);
  }
  canvas.append(list);
 }
};
