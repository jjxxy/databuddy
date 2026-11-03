'use strict';
/* An explicit numerical decomposition, never an inferred business cause. */
window.nexusFactorAnalysis=(()=>{
 let key='',generation=0,busy=false;
 const node=(tag,text)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el};
 function sync({project,workflow,result,resultId,request,isCurrent}){
  const host=document.getElementById('data-factor-panel');if(!host)return;
  const published=typeof workflow?.published_run==='string'?workflow.published_run:workflow?.published_run?.id||workflow?.published_run?.run_id;
  const schema=result?.context?.output_semantics?.columns;
  const ready=!!project&&!!result&&result.run_id===resultId&&published===resultId&&/^[a-f0-9]{64}$/.test(result.manifest_sha256||'')&&Array.isArray(schema);
  const next=ready?JSON.stringify([project,resultId,result.manifest_sha256]):'';
  if(next===key)return;
  key=next;generation++;busy=false;host.replaceChildren();host.hidden=!ready;if(!ready)return;
  host.append(node('h2','数量与单价差异分析'),node('p','按同一业务粒度逐条分解金额变化，数量变化按基准单价计算；这是数学归因，不自动认定业务原因。须先明确字段、单位和期间；可按字段精确筛选，日期输入完整值。'));
  const nums=schema.filter(c=>['decimal','integer'].includes(c.data_type)&&c.value_format==='native');
  const quantities=nums.filter(c=>c.aggregation==='additive'&&c.unit),prices=nums.filter(c=>c.aggregation==='non_additive'&&c.unit);
  if(quantities.length<2||prices.length<2){host.append(node('p','需要两个明确可加的数量字段和两个声明不可加的单价字段，请先在数据做法中定义。'));return}
  function choose(label,items,optional=false){const wrap=node('label',label+' '),select=node('select');select.setAttribute('aria-label',label);const blank=node('option',optional?(label==='筛选字段'?'不筛选':'全部汇总'):'请选择');blank.value='';select.append(blank);for(const c of items){const opt=node('option',(c.label||c.name)+(c.unit?' · '+c.unit:'')+(c.period?' · '+c.period:''));opt.value=c.name;select.append(opt)}wrap.append(select);host.append(wrap);return select}
  const q0=choose('基准数量',quantities),q1=choose('本期数量',quantities),p0=choose('基准单价',prices),p1=choose('本期单价',prices);
  const dims=schema.filter(c=>['text','date','boolean'].includes(c.data_type));const group=choose('按字段分组',dims,true),filterColumn=choose('筛选字段',dims,true);
  function input(label,type){const wrap=node('label',label+' '),field=node('input');field.type=type;field.setAttribute('aria-label',label);wrap.append(field);host.append(wrap);return field}
  const base=input('基准月份','month'),current=input('本期月份','month'),unit=input('金额单位','text');unit.maxLength=80;unit.placeholder='例如 CNY';
  const filterValue=input('筛选值','text');filterValue.maxLength=500;filterValue.placeholder='精确匹配；布尔值输入 true 或 false';
  const button=node('button','计算差异'),status=node('p'),container=node('div');button.type='button';status.setAttribute('role','status');status.setAttribute('aria-live','polite');host.append(button,status,container);
  const active=t=>t===generation&&key===next&&isCurrent();
  const changed=()=>{generation++;container.replaceChildren();busy=false;button.disabled=false;status.textContent='条件已变化，请重新计算。'};
  for(const field of [q0,q1,p0,p1,group,filterColumn,base,current,unit,filterValue])field.addEventListener('change',changed);
  for(const field of [base,current,unit,filterValue])field.addEventListener('input',changed);
  button.onclick=async()=>{
   if(busy||!isCurrent())return;
   if([q0,q1,p0,p1].some(field=>!field.value)||!base.value||!current.value||!unit.value.trim()){status.textContent='请选择四个字段、两个期间并填写金额单位。';return}
   let filters=[];if(filterColumn.value){const column=dims.find(c=>c.name===filterColumn.value);let value=filterValue.value;if(!value.trim()){status.textContent='请输入筛选值；当前页面不支持筛选空值。';return}if(column.data_type==='boolean'){if(!['true','false'].includes(value)){status.textContent='布尔筛选值请输入 true 或 false。';return}value=value==='true'}filters=[{column:column.name,value}];}
   const n=++generation;busy=true;button.disabled=true;container.replaceChildren();status.textContent='正在核对完整结果并逐条计算…';
   const body={expected_handoff_sha256:result.manifest_sha256,quantity_before:q0.value,quantity_after:q1.value,price_before:p0.value,price_after:p1.value,baseline_period:base.value,current_period:current.value,amount_unit:unit.value.trim(),group_by:group.value?[group.value]:[],filters};
   try{
    const value=await request('/api/projects/'+encodeURIComponent(project)+'/data-results/'+encodeURIComponent(resultId)+'/factor-analysis',{method:'POST',body});
    if(!active(n))return;
    if(value.run_id!==resultId||value.handoff_sha256!==result.manifest_sha256||value.plan_sha256!==result.plan_sha256||!/^[a-f0-9]{64}$/.test(value.plan_sha256||'')||value.baseline_period!==body.baseline_period||value.current_period!==body.current_period||value.unit!==body.amount_unit||JSON.stringify(value.columns)!==JSON.stringify([body.quantity_before,body.quantity_after,body.price_before,body.price_after])||JSON.stringify(value.group_by)!==JSON.stringify(body.group_by)||JSON.stringify(value.filters)!==JSON.stringify(body.filters)||!Array.isArray(value.groups)||value.groups.length>500||!Number.isSafeInteger(value.total_rows)||value.total_rows<1||!Number.isSafeInteger(value.input_rows)||value.input_rows<value.total_rows)throw Error('分析版本或范围不一致，请刷新后重试。');
    const decimal=/^-?\d+(?:\.\d+)?$/;
    if(value.groups.some(g=>!Array.isArray(g.values)||g.values.length!==body.group_by.length||g.values.some(v=>v!==null&&!['string','boolean'].includes(typeof v))||!Number.isSafeInteger(g.count)||g.count<1||!['before','after','quantity_effect','price_effect','delta'].every(k=>typeof g[k]==='string'&&decimal.test(g[k])))||value.groups.reduce((n,g)=>n+g.count,0)!==value.total_rows)throw Error('分析结果不完整，请刷新后重试。');
    status.textContent='已从 '+value.input_rows+' 条记录中计算 '+value.total_rows+' 条符合条件的记录，金额单位 '+value.unit+'。'+value.notice;
    const evidence=node('p','数据依据：结果 '+value.handoff_sha256.slice(0,12)+' · 方案 '+value.plan_sha256.slice(0,12)+' · '+value.total_rows+' / '+value.input_rows+' 条'+(filters.length?' · '+filters[0].column+' = '+String(filters[0].value):''));evidence.title='结果摘要：'+value.handoff_sha256+'；方案摘要：'+value.plan_sha256;container.append(evidence);
    const table=node('table'),head=node('thead'),row=node('tr');for(const label of ['分组','记录数','基准金额','本期金额','数量影响','单价影响','金额差异'])row.append(node('th',label));head.append(row);table.append(head);
    const tbody=node('tbody');for(const g of value.groups){if(!Array.isArray(g.values)||!Number.isSafeInteger(g.count))throw Error('分析结果格式不一致');const tr=node('tr');for(const v of [g.values.length?g.values.map(x=>x===null?'未填写':String(x)).join(' / '):'全部',g.count,g.before,g.after,g.quantity_effect,g.price_effect,g.delta])tr.append(node('td',String(v)));tbody.append(tr)}table.append(tbody);container.append(table);
   }catch(error){if(active(n))status.textContent=error.message||'差异分析未完成，请检查字段和口径。'}finally{if(active(n)){busy=false;button.disabled=false}}
  };
 }
 return {sync};
})();
