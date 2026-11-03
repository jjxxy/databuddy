'use strict';
/* Read-only exploration of one explicitly adopted, immutable result. */
window.nexusExplore=(()=>{
 let key='',generation=0,filters=[],busy=false;
 const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
 function sync(context){
  const host=document.getElementById('data-explore-panel');if(!host)return;
  const {project,workflow,result,resultId,request,isCurrent}=context;
  const published=typeof workflow?.published_run==='string'?workflow.published_run:workflow?.published_run?.id||workflow?.published_run?.run_id;
  const schema=result?.context?.output_semantics?.columns;
  const current=!!project&&!!result&&result.run_id===resultId&&published===resultId&&/^[a-f0-9]{64}$/.test(result.manifest_sha256||'')&&Array.isArray(schema);
  const next=current?JSON.stringify([project,resultId,result.manifest_sha256]):'';
  if(next===key)return;
  key=next;generation++;filters=[];busy=false;host.replaceChildren();host.hidden=!current;
  if(!current)return;
  const dims=schema.filter(c=>['text','date','boolean'].includes(c.data_type));
  const measures=schema.filter(c=>['decimal','integer'].includes(c.data_type)&&c.aggregation==='additive'&&c.value_format==='native'&&c.unit);
  const heading=el('h2','筛选、分组与下钻'),intro=el('p','按当前已采用版本的完整记录计算。分组最多 500 项，数据上限 20 MiB；原始资料、业务口径和期间仍需核对。');
  host.append(heading,intro);
  if(!dims.length){host.append(el('p','当前结果没有可分组的文本、日期或布尔字段。'));return;}
  function choice(label,options,blank){const wrapper=el('label',label+' '),select=el('select');select.setAttribute('aria-label',label);if(blank!==undefined){const o=el('option',blank);o.value='';select.append(o)}for(const c of options){const o=el('option',c.label||c.name);o.value=c.name;select.append(o)}wrapper.append(select);host.append(wrapper);return select}
  const dimension=choice('分组字段',dims),metric=el('select');metric.setAttribute('aria-label','统计方式');
  for(const [v,t] of [['count','记录数'],['sum','数值求和']]){let o=el('option',t);o.value=v;metric.append(o)}
  const metricLabel=el('label','统计方式 ');metricLabel.append(metric);host.append(metricLabel);
  const measure=choice('求和字段',measures,'请选择可加数值字段');measure.disabled=true;
  const monthField=choice('月份字段',dims.filter(x=>x.data_type!=='boolean'),'不按月筛选');
  const monthLabel=el('label','选择月份 '),month=el('input');month.type='month';month.setAttribute('aria-label','选择月份');monthLabel.append(month);host.append(monthLabel);
  metric.onchange=()=>{measure.disabled=metric.value!=='sum'};
  const filterBox=el('p'),run=el('button','开始分析'),clear=el('button','清除下钻筛选'),status=el('p'),table=el('div');
  run.type=clear.type='button';status.setAttribute('role','status');status.setAttribute('aria-live','polite');host.append(filterBox,run,clear,status,table);
  const name=n=>schema.find(c=>c.name===n)?.label||n;
  const display=v=>v===null?'空值':v===''?'空字符串':typeof v==='string'&&!v.trim()?'空白字符':String(v);
  function drawFilters(){filterBox.textContent=filters.length?'下钻范围：'+filters.map(f=>name(f.column)+' = '+display(f.value)).join('；'):'当前没有下钻筛选。';clear.disabled=!filters.length||busy}
  drawFilters();
  function valid(token){return token===generation&&key===next&&isCurrent()}
  function invalidate(){generation++;table.replaceChildren();status.textContent='条件已变化，请重新分析。';busy=false;run.disabled=false;drawFilters()}
  for(const input of [dimension,metric,measure,monthField,month])input.addEventListener('change',invalidate);
  clear.onclick=()=>{filters=[];invalidate()};
  run.onclick=async()=>{
   if(busy||!isCurrent())return;
   if(!dimension.value){status.textContent='请选择分组字段。';return}
   if(metric.value==='sum'&&!measure.value){status.textContent='请选择已明确单位和可加性的数值字段。';return}
   if(Boolean(monthField.value)!==Boolean(month.value)){status.textContent='请选择月份字段和月份，或全部留空。';return}
   if(monthField.value&&filters.some(f=>f.column===monthField.value)){status.textContent='月份字段与下钻筛选重复，请清除下钻后重新选择。';return}
   const token=++generation;busy=true;run.disabled=true;clear.disabled=true;status.textContent='正在核验并计算完整结果…';table.replaceChildren();
   const body={expected_handoff_sha256:result.manifest_sha256,group_by:[dimension.value],filters:filters.map(f=>({...f})),metric:metric.value};
   if(metric.value==='sum')body.measure=measure.value;
   if(monthField.value){body.period_column=monthField.value;body.period=month.value}
   try{
    const data=await request('/api/projects/'+encodeURIComponent(project)+'/data-results/'+encodeURIComponent(resultId)+'/explore',{method:'POST',body});
    if(!valid(token))return;
    if(data.run_id!==resultId||data.handoff_sha256!==result.manifest_sha256||data.complete!==true||!Array.isArray(data.groups)||data.groups.length>500||
       !Number.isSafeInteger(data.total_rows)||data.total_rows<0||!Number.isSafeInteger(data.selected_rows)||data.selected_rows<0||data.selected_rows>data.total_rows||
       data.metric!==body.metric||data.measure!==(body.measure||null)||data.period!==(body.period||null)||data.period_column!==(body.period_column||null)||
       JSON.stringify(data.group_by)!==JSON.stringify(body.group_by)||JSON.stringify(data.filters)!==JSON.stringify(body.filters))throw Error('分析范围、版本或完整性不一致，请刷新后重试。');
    if(data.groups.some(g=>!Array.isArray(g.values)||g.values.length!==1||!Number.isSafeInteger(g.count)||g.count<1||
       ![null,'string','boolean'].includes(g.values[0]===null?null:typeof g.values[0])||
       body.metric==='sum'&&(typeof g.amount!=='string'||!/^[-]?[0-9]+(?:\.[0-9]+)?$/.test(g.amount)||typeof g.unit!=='string'||!g.unit))||
       data.groups.reduce((n,g)=>n+g.count,0)!==data.selected_rows)throw Error('分析结果记录不完整，请刷新后重试。');
    status.textContent='共读取 '+data.total_rows+' 条，筛选后 '+data.selected_rows+' 条；'+data.notice;
    const t=el('table'),head=el('thead'),hr=el('tr');for(const col of [name(dimension.value),'记录数',...(metric.value==='sum'?[name(measure.value)]:[]),'下钻'])hr.append(el('th',col));head.append(hr);t.append(head);
    const tbody=el('tbody');for(const group of data.groups){
      if(!Array.isArray(group.values)||group.values.length!==1||!Number.isSafeInteger(group.count))throw Error('分组结果格式不一致');
      const tr=el('tr'),v=group.values[0];tr.append(el('td',display(v)),el('td',String(group.count)));
      if(metric.value==='sum')tr.append(el('td',String(group.amount)+' '+String(group.unit||'')));
      const td=el('td'),button=el('button','查看此范围');button.type='button';button.disabled=filters.some(f=>f.column===dimension.value)||filters.length>=10;
      button.onclick=()=>{filters=[...filters,{column:dimension.value,value:v}];const nextDim=dims.find(c=>!filters.some(f=>f.column===c.name));if(nextDim)dimension.value=nextDim.name;invalidate();status.textContent='已选择范围，请选择分组字段并再次分析。'};td.append(button);tr.append(td);tbody.append(tr);
    }t.append(tbody);table.append(t);
   }catch(error){if(valid(token))status.textContent=error.message||'分析未完成，请检查资料和口径。'}finally{if(valid(token)){busy=false;run.disabled=false;drawFilters()}}
  };
 }
 return {sync};
})();
