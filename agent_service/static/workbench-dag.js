/* Render only dependencies explicitly present in the saved workflow plan. */
window.renderBusinessWorkflowGraph = function (container, workflow, context = null) {
  container.replaceChildren();
  const plan = workflow?.document?.plan;
  if (!plan || !plan.inputs || !Array.isArray(plan.stages)) {
    container.textContent = '当前任务没有可展示的已保存流程。'; return;
  }
  const svgNS = 'http://www.w3.org/2000/svg';
  const create = (tag, attrs = {}) => {const el = document.createElementNS(svgNS, tag);for (const [key,value] of Object.entries(attrs)) el.setAttribute(key, String(value));return el;};
  const names = {select:'选择字段',drop:'移除字段',filter:'筛选记录',reclassify:'按规则分类',compute:'计算业务数值',aggregate:'汇总数据',lookup:'匹配资料',lookup_multi:'按多项条件匹配',lookup_effective:'按有效期匹配',fx_convert:'按汇率换算',union_rows:'合并记录',normalize:'规范数据格式',sql:'按保存的查询处理'};
  const fields=new Map();
  function addField(column) {if(!column || typeof column.name!=='string')return;const label=typeof column.label==='string'&&column.label.trim()?column.label.trim():typeof column.meaning==='string'&&column.meaning.trim()?column.meaning.trim():column.name;if(fields.has(column.name)&&fields.get(column.name)!==label)fields.set(column.name,null);else if(!fields.has(column.name))fields.set(column.name,label);}
  for(const input of Object.values(plan.inputs))for(const column of input?.columns||[])addField(column);
  for(const step of plan.stages){for(const c of step?.calculations||[])addField(c.output);for(const a of step?.aggregations||[])addField(a.output);for(const c of Object.values(step?.outputs||{}))addField(c);for(const c of step?.output_schema?.columns||[])addField(c);addField(step?.output);}
  const field=name=>typeof name==='string'?(fields.get(name)||name):'未指定字段';
  const list=items=>Array.isArray(items)&&items.length?items.map(field).join('、'):'未指定';
  const ops={eq:'等于',neq:'不等于',gt:'大于',gte:'大于或等于',lt:'小于',lte:'小于或等于',in:'属于',not_in:'不属于',contains:'包含',is_null:'为空',not_null:'不为空'};
  const aggs={sum:'求和',count:'计数',min:'最小值',max:'最大值',concat:'拼接'};
  const arithmetic={add:'加',subtract:'减',multiply:'乘',divide:'除以'};
  const shown=value=>value==null?'':Array.isArray(value)?value.map(String).join('、'):String(value);
  function describe(stage){
    const details=[];
    if(stage.kind==='filter'&&Array.isArray(stage.conditions)&&stage.conditions.length)details.push('筛选条件：'+stage.conditions.map(c=>field(c.column)+' '+(ops[c.operator]||'条件待核对')+(c.value==null?'':' '+shown(c.value))).join('；'));
    if(stage.kind==='select')details.push('保留字段：'+list(stage.columns));
    if(stage.kind==='drop')details.push('移除字段：'+list(stage.columns));
    if(stage.kind==='aggregate') {details.push('汇总范围：'+(Array.isArray(stage.by)&&stage.by.length?list(stage.by):'全部记录'));if(Array.isArray(stage.aggregations)&&stage.aggregations.length)details.push('汇总指标：'+stage.aggregations.map(a=>(aggs[a.function]||'按已保存规则处理')+(a.column?' '+field(a.column):'')+(a.output?.name?'，输出 '+field(a.output.name):'')).join('；'));}
    if(stage.kind==='compute'&&Array.isArray(stage.calculations)&&stage.calculations.length)details.push('计算：'+stage.calculations.map(c=>field(c.output?.name)+' = '+field(c.left)+' '+(arithmetic[c.operation]||'按保存的规则计算')+' '+field(c.right)+(c.rounding_scale!=null?'；每行保留 '+String(c.rounding_scale)+' 位（HALF_EVEN）':'')+(c.constant_scaling?'；固定比例需业务核对':'')).join('；'));
    if(stage.kind==='lookup'&&stage.left_key&&stage.right_key)details.push('关联条件：'+field(stage.left_key)+' 对应 '+field(stage.right_key));
    if((stage.kind==='lookup_multi'||stage.kind==='lookup_effective')&&Array.isArray(stage.keys)&&stage.keys.length)details.push('关联条件：'+stage.keys.map(k=>field(k.left)+' 对应 '+field(k.right)).join('；'));
    if(stage.kind==='lookup_effective')details.push('按 '+field(stage.business_date)+' 核对生效期间（起始：'+field(stage.valid_from)+'；结束：'+field(stage.valid_to)+'）');
    if(stage.kind==='fx_convert')details.push('金额字段：'+field(stage.amount_column)+'；按 '+field(stage.rate_column)+' 从 '+shown(stage.source_currency)+' 换算为 '+shown(stage.target_currency)+'；舍入方式：'+shown(stage.rounding_mode));
    if(stage.kind==='normalize'&&Array.isArray(stage.rules)&&stage.rules.length)details.push('规范字段：'+stage.rules.map(r=>field(r.column)).join('、'));
    if(stage.kind==='reclassify'&&stage.column)details.push('按已保存的映射规则处理 '+field(stage.column));
    if(stage.kind==='union_rows')details.push('合并 '+(Array.isArray(stage.inputs)?stage.inputs.length:'未指定数量的')+' 份输入记录');
    if(stage.kind==='sql')details.push('自定义查询；系统不能仅凭查询名称解释业务口径，请查看完整规则。');
    if(typeof stage.description==='string'&&stage.description.trim())details.push('保存的说明：'+stage.description.trim());
    return details.length?details.join('。'):'本步缺少可解释的信息，请查看完整规则。';
  }
  const nodes=[], lookup=new Map(), warnings=[];
  for (const [key, input] of Object.entries(plan.inputs)) {
    const label=typeof input?.meaning==='string'&&input.meaning.trim()?input.meaning.trim():key;
    const binding=workflow.document?.bindings?.[key];
    const description=binding?.source_id?'当前计划绑定的资料；名称尚未核实，运行时来源版本以运行记录为准。':binding?.result_id?'当前计划绑定的上游结果（编号：'+binding.result_id+'）；运行时版本以运行记录为准。':'未找到来源绑定，请查看完整规则。';
    const node={id:key,label,description,deps:[],rank:0,type:'source',sourceId:binding?.source_id};nodes.push(node);lookup.set(key,node);
  }
  for (const stage of plan.stages) {
    if (!stage || typeof stage.id!=='string' || lookup.has(stage.id)) {warnings.push('有步骤编号缺失或重复，无法完整展示。');continue;}
    const raw=[...(typeof stage.input==='string'?[stage.input]:[]),...(typeof stage.right==='string'?[stage.right]:[]),...(Array.isArray(stage.inputs)?stage.inputs:[])];
    const deps=[...new Set(raw.filter(v=>typeof v==='string'))];
    const missing=deps.filter(id=>!lookup.has(id));if(missing.length)warnings.push('部分步骤依赖未找到，流程图可能不完整。');
    const resolved=deps.filter(id=>lookup.has(id));
    const node={id:stage.id,label:names[stage.kind]||'处理业务数据',description:describe(stage),deps:resolved,rank:resolved.length?1+Math.max(...resolved.map(id=>lookup.get(id).rank)):1,type:'stage'};
    nodes.push(node);lookup.set(stage.id,node);
  }
  if (typeof plan.output==='string' && lookup.has(plan.output)) {
    const node={id:'__result__',label:'生成任务结果',description:'运行完成后仍需核对，不能视为已采用',deps:[plan.output],rank:lookup.get(plan.output).rank+1,type:'result'};nodes.push(node);
  } else warnings.push('未找到已保存的结果步骤。');
  const version=!workflow.published_run?'当前做法尚未采用':!workflow.published_sha256||!workflow.draft_sha256?'无法确认当前做法与已采用版本是否一致':workflow.published_sha256===workflow.draft_sha256?'当前做法与已采用版本一致':'当前做法与已采用版本不同，正式结果仍保留原版本';
  const run=workflow.latest_run;const runStates={queued:'等待执行',running:'正在运行',retrying:'等待重试',validated:'已运行，结果待核对',succeeded:'已运行',failed:'运行未完成',cancelled:'运行已取消'};
  const lastRun=run?(run.matches_current_draft===true?'当前做法最近运行：':run.matches_current_draft===false?'最近运行使用的是其他版本：':'最近运行与当前做法的关系待核对：')+(runStates[run.status]||'状态待核对'):'当前暂无运行记录';
  const warning=document.createElement('p');warning.className='workflow-graph-note';warning.textContent=version+'；'+lastRun+'。连线表示资料或上一步的输入关系，具体结果仍需核对。';container.append(warning);
  if (warnings.length) {const note=document.createElement('p');note.className='workflow-graph-warning';note.textContent=[...new Set(warnings)].join(' ');container.append(note);}
  if (!nodes.length) return;
  const cols=new Map();for (const node of nodes) {if(!cols.has(node.rank))cols.set(node.rank,[]);cols.get(node.rank).push(node);}
  const colWidth=260,rowHeight=114,boxWidth=214,boxHeight=80,pad=20;
  for (const [rank,col] of cols) col.forEach((node,i)=>{node.x=pad+rank*colWidth;node.y=pad+i*rowHeight;});
  const width=pad*2+(Math.max(...cols.keys())*colWidth)+boxWidth,height=pad*2+Math.max(...[...cols.values()].map(col=>col.length))*rowHeight;
  const scroller=document.createElement('div');scroller.className='workflow-graph-scroll';scroller.tabIndex=0;scroller.setAttribute('aria-label','任务流程图，可横向滚动');
  const svg=create('svg',{viewBox:`0 0 ${width} ${height}`,width,height,role:'group','aria-label':'已保存业务任务的来源、处理步骤与结果'});
  const defs=create('defs'),marker=create('marker',{id:'workflow-arrow',viewBox:'0 0 10 10',refX:'9',refY:'5',markerWidth:'7',markerHeight:'7',orient:'auto-start-reverse'});marker.append(create('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#53615b'}));defs.append(marker);svg.append(defs);
  for (const node of nodes) for (const id of node.deps) {const from=lookup.get(id);if(!from)continue;const x1=from.x+boxWidth,y1=from.y+boxHeight/2,x2=node.x,y2=node.y+boxHeight/2,mid=(x1+x2)/2;svg.append(create('path',{d:`M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,fill:'none',stroke:'#53615b','stroke-width':'1.5','marker-end':'url(#workflow-arrow)'}));}
  const detail=document.createElement('div');detail.className='workflow-graph-detail';detail.setAttribute('aria-live','polite');detail.textContent='选择流程节点，可以查看这一步的业务说明。';
  for (const node of nodes) {
    const group=create('g',{role:'button',tabindex:'0','aria-label':node.label+'：查看说明'}),rect=create('rect',{x:node.x,y:node.y,width:boxWidth,height:boxHeight,rx:'7',fill:'#fff',stroke:'#303a35','stroke-width':'1.2'});
    function showDetail(){detail.replaceChildren();const heading=document.createElement('strong');heading.textContent=node.label;const body=document.createElement('p');body.textContent=node.description||'请查看完整规则。';detail.append(heading,body);context?.onNode?.(detail,node.type==='result'?'__overview_result__':node.id);}
    group.addEventListener('click',showDetail);group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();showDetail();}});
    const title=create('title');title.textContent=node.label+(node.description?'：'+node.description:'');group.append(title,rect);
    const label=create('text',{x:node.x+12,y:node.y+25,'font-size':'14','font-weight':'600',fill:'#1d2a24'});label.textContent=node.label.length>13?node.label.slice(0,13)+'…':node.label;group.append(label);
    const description=create('text',{x:node.x+12,y:node.y+48,'font-size':'12',fill:'#59655f'});function paint(){title.textContent=node.label+'：'+node.description;group.setAttribute('aria-label',node.label+'：查看说明');label.textContent=node.label.length>13?node.label.slice(0,13)+'…':node.label;description.replaceChildren();const short=node.description.replace(/\s+/g,' ');const first=create('tspan',{x:node.x+12,dy:'0'});first.textContent=short.slice(0,16);description.append(first);if(short.length>16){const second=create('tspan',{x:node.x+12,dy:'16'});second.textContent=short.length>32?short.slice(16,32)+'…':short.slice(16);description.append(second);}}paint();node.paint=paint;group.append(description);svg.append(group);
  }
  scroller.append(svg);container.append(scroller,detail);
  if(window.NexusFlowGraph){
    let selected=null;
    const heading=document.createElement('strong'),body=document.createElement('p');
    const updateLabel=item=>{heading.textContent=item.label;body.textContent=item.description||'请查看完整规则。'};
    const show=id=>{const item=lookup.get(id)||nodes.find(value=>value.id===id);if(!item)return;selected=id;updateLabel(item);detail.replaceChildren(heading,body);context?.onNode?.(detail,item.type==='result'?'__overview_result__':item.id)};
    for(const item of nodes)item.caption=item.description;
    scroller.replaceChildren();const controller=window.NexusFlowGraph.mount(scroller,nodes,show);
    for(const item of nodes){const prior=item.paint;item.paint=()=>{prior();item.caption=item.description;controller.update();if(selected===item.id)updateLabel(item)}}
  }

  // Source names come only from the authenticated source list, constrained to
  // this project. A late response must never repaint a different workflow.
  const project=context?.projectId || new URLSearchParams(window.location.search).get('project');
  const request=context?.request || window.wbRequest;
  const current=context?.isCurrent || (()=>new URLSearchParams(window.location.search).get('project')===project && (typeof window.wbCurrentProject!=='function'||window.wbCurrentProject()));
  if(project&&typeof request==='function'&&nodes.some(n=>n.sourceId)){
    request('/api/sources').then(sources=>{
      if(!container.contains(scroller)||!current())return;
      const visible=new Map((Array.isArray(sources)?sources:[]).filter(s=>s?.project_id===project&&!s.archived&&typeof s.id==='string'&&typeof s.name==='string'&&s.name.trim()).map(s=>[s.id,s.name.trim()]));
      for(const item of nodes){if(!item.sourceId)continue;const name=visible.get(item.sourceId);if(name){const meaning=item.label;item.label=name;item.description='当前计划绑定的资料：'+name+'。'+(meaning!==name?'计划用途：'+meaning+'。':'')+'运行时来源版本以运行记录为准。';item.paint();}}
    }).catch(()=>{});
  }
};
