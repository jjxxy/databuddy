'use strict';
/* Exact values come from a pinned result; bar sizes are server-computed approximations. */
window.nexusGapAnalysis=(()=>{
 let key='',generation=0,busy=false;
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
 const display=v=>v===null?'空值':v===''?'空字符串':typeof v==='string'&&!v.trim()?'空白字符':String(v);
 function sync({project,workflow,result,resultId,request,isCurrent}){
  const host=document.getElementById('data-gap-panel');if(!host)return;
  const pub=typeof workflow?.published_run==='string'?workflow.published_run:workflow?.published_run?.id||workflow?.published_run?.run_id;
  const schema=result?.context?.output_semantics?.columns;
  const ready=!!project&&!!result&&result.run_id===resultId&&pub===resultId&&/^[a-f0-9]{64}$/.test(result.manifest_sha256||'')&&Array.isArray(schema);
  const next=ready?JSON.stringify([project,resultId,result.manifest_sha256]):'';
  if(next===key)return;key=next;generation++;busy=false;host.replaceChildren();host.hidden=!ready;if(!ready)return;
  host.append(el('h2','基准与实际差异分析'),el('p','选择含明确单位、可加性和期间的两个数值字段，并自行核对业务含义和数据范围；单位相同不证明口径一致。可按字段精确筛选；日期字段输入完整值，版本更新后请刷新。图表只展示相对比例，差异不能自动证明业务原因。'));
  const nums=schema.filter(c=>['decimal','integer'].includes(c.data_type)&&c.value_format==='native'&&c.aggregation==='additive'&&c.unit&&c.period);
  if(nums.length<2){host.append(el('p','需要至少两个声明单位、期间和可加性的数值字段；请先核对数据做法。'));return}
  function choose(label,values,optional=false){const wrap=el('label',label+' '),select=el('select');select.setAttribute('aria-label',label);let blank=el('option',optional?'全部汇总':'请选择');blank.value='';select.append(blank);for(const c of values){let opt=el('option',(c.label||c.name)+(c.unit?' · '+c.unit:'')+(c.period?' · '+c.period:''));opt.value=c.name;select.append(opt)}wrap.append(select);host.append(wrap);return select}
  const baseline=choose('基准字段',nums),current=choose('实际或本期字段',nums),group=choose('按字段分组',schema.filter(c=>['text','date','boolean'].includes(c.data_type)),true);
  const filterColumn=choose('筛选字段',schema.filter(c=>['text','date','boolean'].includes(c.data_type)),true);
  function input(label,type){const wrap=el('label',label+' '),field=el('input');field.type=type;field.setAttribute('aria-label',label);wrap.append(field);host.append(wrap);return field}
  const baselinePeriod=input('基准月份','month'),currentPeriod=input('实际月份','month'),decimals=input('变化率小数位','number');decimals.min='0';decimals.max='8';decimals.step='1';decimals.value='2';
  const filterValue=input('筛选值','text');filterValue.placeholder='精确匹配；布尔值输入 true 或 false';
  const button=el('button','计算差异'),status=el('p'),container=el('div');button.type='button';status.setAttribute('role','status');status.setAttribute('aria-live','polite');host.append(button,status,container);
  const active=t=>t===generation&&key===next&&isCurrent();
  for(const field of [baseline,current,group,baselinePeriod,currentPeriod,decimals,filterColumn,filterValue])field.addEventListener('change',()=>{generation++;container.replaceChildren();busy=false;button.disabled=false;status.textContent='条件已变化，请重新计算。'});
  for(const field of [baselinePeriod,currentPeriod,decimals,filterValue])field.addEventListener('input',()=>{generation++;container.replaceChildren();busy=false;button.disabled=false;status.textContent='条件已变化，请重新计算。'});
  button.onclick=async()=>{
   if(busy||!isCurrent())return;
   const scale=Number(decimals.value);if(!baseline.value||!current.value||baseline.value===current.value||!baselinePeriod.value||!currentPeriod.value||decimals.value===''||!Number.isInteger(scale)||scale<0||scale>8){status.textContent='请选择不同的两个字段、明确两个期间和 0–8 位变化率小数。';return}
   let filters=[];if(filterColumn.value){const column=schema.find(c=>c.name===filterColumn.value);let value=filterValue.value;if(!value.trim()){status.textContent='请输入筛选值；当前页面不支持筛选空值或空白。';return}if(column.data_type==='boolean'){if(!['true','false'].includes(value)){status.textContent='布尔筛选值请输入 true 或 false。';return}value=value==='true'}filters=[{column:column.name,value}];}
   const n=++generation;busy=true;button.disabled=true;container.replaceChildren();status.textContent='正在核对完整数据并计算…';
   const body={expected_handoff_sha256:result.manifest_sha256,baseline_column:baseline.value,current_column:current.value,baseline_period:baselinePeriod.value,current_period:currentPeriod.value,group_by:group.value?[group.value]:[],filters,ratio_decimals:scale,rounding:'half_even'};
   try{
    const value=await request('/api/projects/'+encodeURIComponent(project)+'/data-results/'+encodeURIComponent(resultId)+'/gap-analysis',{method:'POST',body});
    if(!active(n))return;
    if(value.run_id!==resultId||value.handoff_sha256!==result.manifest_sha256||value.plan_sha256!==result.plan_sha256||!/^[a-f0-9]{64}$/.test(value.plan_sha256||'')||value.baseline_column!==body.baseline_column||value.current_column!==body.current_column||value.baseline_period!==body.baseline_period||value.current_period!==body.current_period||value.ratio_decimals!==scale||value.rounding!=='half_even'||JSON.stringify(value.group_by)!==JSON.stringify(body.group_by)||JSON.stringify(value.filters)!==JSON.stringify(body.filters)||!Array.isArray(value.groups)||value.groups.length>500||!Number.isSafeInteger(value.total_rows)||value.total_rows<1||!Number.isSafeInteger(value.input_rows)||value.input_rows<value.total_rows||typeof value.unit!=='string'||!value.unit)throw Error('分析版本或范围不一致，请刷新后重试。');
    const decimal=/^-?\d+(?:\.\d+)?$/;
    if(value.groups.some(g=>!Array.isArray(g.values)||g.values.length!==body.group_by.length||g.values.some(v=>v!==null&&!['string','boolean'].includes(typeof v))||!Number.isSafeInteger(g.count)||g.count<1||!['baseline','current','delta'].every(k=>typeof g[k]==='string'&&decimal.test(g[k]))||(g.relative_percent!==null&&(typeof g.relative_percent!=='string'||!decimal.test(g.relative_percent)))||!Number.isInteger(g.chart_basis_points)||g.chart_basis_points<0||g.chart_basis_points>10000||!['increase','decrease','equal'].includes(g.chart_direction))||value.groups.reduce((n,g)=>n+g.count,0)!==value.total_rows)throw Error('分析结果不完整，请刷新后重试。');
    status.textContent='已从 '+value.input_rows+' 条记录中计算 '+value.total_rows+' 条符合条件的记录，单位 '+value.unit+'。'+value.notice;
    const evidence=el('p','数据依据：结果 '+value.handoff_sha256.slice(0,12)+' · 方案 '+value.plan_sha256.slice(0,12)+' · '+body.baseline_column+'（'+body.baseline_period+'）对比 '+body.current_column+'（'+body.current_period+'） · '+value.total_rows+' / '+value.input_rows+' 条 · '+value.unit+(filters.length?' · '+filters[0].column+' = '+String(filters[0].value):''));evidence.title='结果摘要：'+value.handoff_sha256+'；方案摘要：'+value.plan_sha256;container.append(evidence);
    const note=el('p',value.chart_notice+' 变化率按四舍六入五取偶保留 '+scale+' 位；基准为零时不计算变化率。');container.append(note);
    const table=el('table'),head=el('thead'),hr=el('tr');for(const label of ['分组','记录数','基准值','实际值','差额','变化率','差额比例图'])hr.append(el('th',label));head.append(hr);table.append(head);
    const tbody=el('tbody');for(const g of value.groups){const tr=el('tr'),groupLabel=g.values.length?g.values.map(display).join(' / '):'全部';
      for(const v of [groupLabel,g.count,g.baseline,g.current,g.delta])tr.append(el('td',String(v)));
      const ratioCell=el('td',g.relative_percent===null?(g.ratio_unavailable_reason||'不适用'):g.relative_percent+'%');if(g.ratio_interpretation_warning){const warning=el('small',g.ratio_interpretation_warning);warning.className='gap-ratio-warning';ratioCell.append(warning)}tr.append(ratioCell);
      const chart=el('td'),track=el('div'),bar=el('div');track.className='gap-chart-track';bar.className='gap-chart-bar gap-'+g.chart_direction;bar.style.width=(g.chart_basis_points/100)+'%';track.setAttribute('role','img');track.setAttribute('aria-label',(g.chart_direction==='increase'?'增加':g.chart_direction==='decrease'?'减少':'无差异')+'，精确差额 '+g.delta+' '+value.unit+'，柱长为相对比例');track.append(bar);chart.append(track);tr.append(chart);tbody.append(tr)}table.append(tbody);container.append(table);
   }catch(error){if(active(n))status.textContent=error.message||'差异分析未完成，请检查字段和口径。'}finally{if(active(n)){busy=false;button.disabled=false}}
  };
 }
 return {sync};
})();
