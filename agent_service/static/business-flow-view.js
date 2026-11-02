'use strict';
/* Render only actual saved definitions and run receipts. Never interpret input
   descriptions or filenames as HTML, and never infer completion from a plan. */
(function(root){
 const KIND_LABELS={source:'获取资料',source_table:'读取表格资料',collect:'获取资料',parse:'解析资料',transform:'数据处理',data_workflow:'数据处理',table_workflow:'数据处理',quality_gate:'自动核对数据',human_review:'人工复核',model:'AI 处理',skill:'使用工作方法',template:'使用模板',office:'生成办公文件',office_document:'生成办公文件',html:'生成网页报告',html_report:'生成网页报告',table_report_spec:'准备报告内容',output:'交付成果'};
 const STATE_LABELS={pending:'等待执行',queued:'等待执行',running:'正在执行',succeeded:'执行完成',completed:'执行完成',failed:'未完成',unknown:'结果待核对',cancelled:'已停止',skipped:'已跳过',blocked:'等待处理',awaiting_review:'等待人工复核'};
 function node(document,tag,value,cls){const e=document.createElement(tag);if(value!==undefined&&value!==null)e.textContent=String(value);if(cls)e.className=cls;return e}
 function text(value,fallback){return typeof value==='string'&&value.trim()?value.trim():fallback}
 function appendFailure(document,box,receipt){
  if(receipt?.status!=='failed')return;
  if(typeof receipt.error_message==='string'&&receipt.error_message.trim())box.append(node(document,'p','失败原因：'+receipt.error_message.trim().slice(0,500)));
  if(typeof receipt.error_code==='string'&&/^[A-Z0-9_]{1,80}$/.test(receipt.error_code))box.append(node(document,'p','错误编号：'+receipt.error_code));
 }
 // Office type is only known when its spec explicitly references a saved
 // table-report node. A title or filename is not proof of the output type.
 function kindLabel(item,byId){
  if(item.kind==='table_report_spec'){
   if(item.config?.format==='presentation')return '准备汇报 PPT 内容';
   if(item.config?.format==='html')return '准备网页报告内容';
  }
  if(item.kind==='office_document'){
   const ref=item.inputs?.spec,source=ref?.source==='node'?byId.get(ref.name):null;
   if(source?.kind==='table_report_spec'&&source.config?.format==='presentation')return '生成汇报 PPT';
  }
  return KIND_LABELS[item.kind]||'其他工作步骤';
 }
 function elapsed(start,end){const a=Date.parse(start),b=end?Date.parse(end):Date.now();if(!Number.isFinite(a)||!Number.isFinite(b)||b<a)return '';const seconds=Math.floor((b-a)/1000);return seconds>=3600?Math.floor(seconds/3600)+'小时'+Math.floor(seconds%3600/60)+'分'+seconds%60+'秒':seconds>=60?Math.floor(seconds/60)+'分'+seconds%60+'秒':seconds+'秒'}
 function graph(document,plan,receipts,matching){
  const wrap=node(document,'section',null,'flow-graph');wrap.append(node(document,'h3','业务处理流程'));
  const items=new Map(),nodes=[],issues=[],definitions=new Map(plan.nodes.filter(v=>v&&typeof v.id==='string').map(v=>[v.id,v]));
  for(const entry of plan.nodes){if(!entry||typeof entry.id!=='string'||items.has(entry.id)){issues.push('流程步骤编号缺失或重复，不能完整展示。');continue;}const item={id:entry.id,label:text(entry.title,text(entry.name,kindLabel(entry,definitions))),kindLabel:kindLabel(entry,definitions),description:text(entry.description,'暂无业务说明，请查看完整规则。'),deps:Array.isArray(entry.depends_on)?[...new Set(entry.depends_on.filter(v=>typeof v==='string'))]:[],rank:null};items.set(item.id,item);nodes.push(item);}
  if(!nodes.length){wrap.append(node(document,'p','当前方案没有可展示的步骤。'));return wrap;}
  let cyclic=false;const active=new Set();function rank(item){if(item.rank!==null)return item.rank;if(active.has(item.id)){cyclic=true;return 0;}active.add(item.id);const prev=item.deps.map(id=>{const upstream=items.get(id);if(!upstream){issues.push('部分依赖步骤未找到，无法展示完整关系。');return 0;}return rank(upstream)+1;});active.delete(item.id);return item.rank=prev.length?Math.max(...prev):0;}
  for(const item of nodes)rank(item);
  if(cyclic){wrap.append(node(document,'p','步骤依赖存在循环，无法可靠展示流程图。','flow-graph-warning'));return wrap;}
  if(issues.length)wrap.append(node(document,'p',[...new Set(issues)].join(' '),'flow-graph-warning'));
  const columns=new Map();for(const item of nodes){if(!columns.has(item.rank))columns.set(item.rank,[]);columns.get(item.rank).push(item)}
  const gap=260,row=110,w=214,h=78,pad=20;for(const [rank,col] of columns)col.forEach((v,i)=>{v.x=pad+rank*gap;v.y=pad+i*row;});
  const width=pad*2+Math.max(...columns.keys())*gap+w,height=pad*2+Math.max(...[...columns.values()].map(c=>c.length))*row;
  const ns='http://www.w3.org/2000/svg',el=(tag,attrs={})=>{const v=document.createElementNS(ns,tag);for(const [k,x] of Object.entries(attrs))v.setAttribute(k,String(x));return v};
  const scroll=node(document,'div',null,'flow-graph-scroll');scroll.tabIndex=0;scroll.setAttribute('aria-label','业务流程图，可横向滚动');
  const svg=el('svg',{viewBox:`0 0 ${width} ${height}`,width,height,role:'group','aria-label':'当前版本的业务步骤和依赖'}),defs=el('defs'),arrow=el('marker',{id:'business-flow-arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:7,markerHeight:7,orient:'auto-start-reverse'});arrow.append(el('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#53615b'}));defs.append(arrow);svg.append(defs);
  for(const item of nodes)for(const id of item.deps){const from=items.get(id);if(!from)continue;const x1=from.x+w,y1=from.y+h/2,x2=item.x,y2=item.y+h/2,mid=(x1+x2)/2;svg.append(el('path',{d:`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,fill:'none',stroke:'#53615b','stroke-width':1.5,'marker-end':'url(#business-flow-arrow)'}));}
  const detail=node(document,'div','选择节点可查看业务说明。','flow-graph-detail');detail.setAttribute('aria-live','polite');
  for(const item of nodes){const receipt=matching?receipts.get(item.id):null,state=receipt?(STATE_LABELS[receipt.status]||'状态未知'):matching?'本次未记录':'尚未选择对应运行';const g=el('g',{role:'button',tabindex:0,'aria-label':item.label+'：查看说明'});g.append(el('rect',{x:item.x,y:item.y,width:w,height:h,rx:7,fill:'#fff',stroke:'#303a35'}));const heading=el('text',{x:item.x+12,y:item.y+24,'font-size':14,'font-weight':600,fill:'#1d2a24'});heading.textContent=item.label.length>13?item.label.slice(0,13)+'…':item.label;g.append(heading);const status=el('text',{x:item.x+12,y:item.y+49,'font-size':12,fill:'#59655f'});status.textContent=state;g.append(status);function show(){detail.replaceChildren();detail.append(node(document,'strong',item.label),node(document,'p',item.kindLabel),node(document,'p',item.description),node(document,'p',state));appendFailure(document,detail,receipt);}g.addEventListener('click',show);g.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();show();}});svg.append(g)}
  scroll.append(svg);wrap.append(scroll,detail);
  if(root.NexusFlowGraph){
   for(const item of nodes){const receipt=matching?receipts.get(item.id):null;item.caption=receipt?(STATE_LABELS[receipt.status]||'状态未知'):matching?'本次未记录':'尚未选择对应运行';}
   scroll.replaceChildren();root.NexusFlowGraph.mount(scroll,nodes,id=>{const item=items.get(id);if(!item)return;detail.replaceChildren();detail.append(node(document,'strong',item.label),node(document,'p',item.kindLabel),node(document,'p',item.description),node(document,'p',item.caption));appendFailure(document,detail,matching?receipts.get(id):null);});
  }
  return wrap;
 }
 function render(container,options){
  const document=container.ownerDocument, plan=options?.plan, run=options?.run;
  container.replaceChildren();
  if(!plan||!Array.isArray(plan.nodes)){container.append(node(document,'p','尚无可展示的流程方案。','flow-empty'));return}
  const header=node(document,'div',null,'flow-view-header');
  header.append(node(document,'h2',text(plan.name,'业务流程')));
  const version=options?.version||{};
  const labels={candidate:'候选方案',published:'正式方案',history:'历史方案',draft:'草稿'};
  header.append(node(document,'span',labels[version.role]||'版本待确认','flow-version'));
  container.append(header);
  const notices=node(document,'div',null,'flow-notices');
  if(typeof version.commit==='string'&&version.commit)notices.append(node(document,'p','代码版本：'+version.commit));
  if(typeof version.digest==='string'&&version.digest)notices.append(node(document,'p','方案版本：'+version.digest));
  const matching=!!run&&!!(version.digest||version.commit)&&(!version.digest||run.plan_digest===version.digest)&&(!version.commit||run.commit===version.commit);
  if(run){
    notices.append(node(document,'p',matching?'运行状态：'+(STATE_LABELS[run.status]||'状态未知'):'所选运行与当前方案版本不一致，未展示步骤状态。'));
    if(matching&&typeof run.message==='string'&&run.message)notices.append(node(document,'p',run.message));
  }else notices.append(node(document,'p','当前展示的是保存的方案，尚未选择对应运行记录。'));
  notices.append(node(document,'p','模型及工作方法的输出可能变化；执行完成不等于业务结果已经核验。'));
  container.append(notices);
  const receipts=Array.isArray(run?.receipts)?run.receipts:Array.isArray(run?.steps)?run.steps:[];
  const sameRun=matching;
  const byId=new Map();if(sameRun)for(const receipt of receipts){const id=receipt?.node_id||receipt?.id;if(typeof id==='string'&&!byId.has(id))byId.set(id,receipt)}
  const ids=new Map(),definitions=new Map();for(const item of plan.nodes){if(item&&typeof item.id==='string'){ids.set(item.id,text(item.title,text(item.name,item.id)));definitions.set(item.id,item)}}
  container.append(graph(document,plan,byId,sameRun));
  const list=node(document,'ol',null,'flow-step-list');
  for(const item of plan.nodes){
   if(!item||typeof item.id!=='string')continue;
   const receipt=byId.get(item.id), li=node(document,'li',null,'flow-step');
   const top=node(document,'div',null,'flow-step-heading');
   top.append(node(document,'strong',text(item.title,text(item.name,kindLabel(item,definitions)))));
   top.append(node(document,'span',receipt?(STATE_LABELS[receipt.status]||'状态未知'):sameRun?'本次未记录':'尚未选择运行','flow-step-state'));
   li.append(top);if(receipt&&typeof receipt.started_at==='string'){const duration=elapsed(receipt.started_at,receipt.finished_at);if(duration){const time=node(document,'p','耗时 '+duration,'flow-step-elapsed');time.dataset.startedAt=receipt.started_at;if(receipt.finished_at)time.dataset.finishedAt=receipt.finished_at;li.append(time)}}
   if(item.kind)li.append(node(document,'p',kindLabel(item,definitions),'flow-step-kind'));
   if(typeof item.description==='string'&&item.description.trim())li.append(node(document,'p',item.description,'flow-step-description'));
   const dependencies=Array.isArray(item.depends_on)?item.depends_on.filter(x=>typeof x==='string'):[];
   if(dependencies.length)li.append(node(document,'p','依赖：'+dependencies.map(id=>ids.get(id)||id).join('、'),'flow-step-dependencies'));
   if(receipt){
    const details=node(document,'details',null,'flow-step-receipt');details.append(node(document,'summary','查看本步骤记录'));
    for(const [key,label] of [['started_at','开始时间'],['finished_at','结束时间'],['input_sha256','输入摘要'],['output_sha256','输出摘要']]){
     if(typeof receipt[key]==='string'&&receipt[key])details.append(node(document,'p',label+'：'+receipt[key]));
    }
    if(receipt.deterministic===false)details.append(node(document,'p','此步骤的输出可能因模型或外部服务变化。'));
    if(receipt.status==='unknown')details.append(node(document,'p','尚不能确认是否执行成功，请先核对结果；系统不会自动重复外部操作。'));
    appendFailure(document,details,receipt);
    if(typeof receipt.message==='string'&&receipt.message)details.append(node(document,'p',receipt.message));
    li.append(details);
   }
   list.append(li);
  }
  if(list.children.length)container.append(list);else container.append(node(document,'p','当前方案没有可展示的步骤。','flow-empty'));
 }
 if(root.document&&!root.__nexusFlowElapsedTimer){root.__nexusFlowElapsedTimer=root.setInterval(()=>{if(root.document.hidden)return;for(const item of root.document.querySelectorAll('.flow-step-elapsed[data-started-at]')){const value=elapsed(item.dataset.startedAt,item.dataset.finishedAt);if(value)item.textContent='耗时 '+value}},1000)}
 root.NexusBusinessFlowView={render};
 if(typeof module!=='undefined'&&module.exports)module.exports={render};
})(typeof window!=='undefined'?window:globalThis);
