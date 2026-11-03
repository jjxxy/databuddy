'use strict';
/* A local review of the actual version-pinned editing draft, not approval. */
window.nexusOfficeChangeReview=({container,original,current,order})=>{
 if(!container||!original||!current||!Array.isArray(order))return;
 const node=(tag,text)=>{const item=document.createElement(tag);if(text!==undefined)item.textContent=String(text);return item};
 container.replaceChildren();
 const old=original.sections,now=current.sections,prior=old.map(s=>s.index);
 const removed=old.filter(s=>!order.includes(s.index));
 const added=order.filter(i=>i>=old.length);
 const orderChanged=JSON.stringify(order)!==JSON.stringify(prior);
 const changes=[];
 for(const s of old){
  if(!order.includes(s.index))continue;
  const next=now[s.index];
  for(const field of ['heading','body'])if(s[field]!==next[field])changes.push({section:s.index,field:field==='heading'?'标题':'正文',before:s[field],after:next[field]});
  s.bullets.forEach((text,i)=>{if(text!==next.bullets[i])changes.push({section:s.index,field:'要点 '+(i+1),before:text,after:next.bullets[i]})});
 }
 if(!orderChanged&&!changes.length){container.append(node('p','当前没有暂存修改。'));return}
 container.append(node('p','这里展示当前草稿相对原始文件的差异；已生成的修改也会保留在编辑页。原文件保持不变；这不是财务审批或版式预览。'));
 if(orderChanged){
  const list=node('ol');for(const i of order){const item=node('li',(i>=old.length?'新增 · ':'')+(now[i].heading||'未命名章节'));list.append(item)}
  container.append(node('h3','新文件的章节顺序'),list);
 }
 if(removed.length){
  const list=node('ul');for(const section of removed){let extra='';if(section.contains_table&&section.contains_chart)extra='（含表格和图表）';else if(section.contains_table)extra='（含表格）';else if(section.contains_chart)extra='（含图表）';list.append(node('li',(section.heading||'未命名章节')+extra+'：将从新文件移除'))}
  container.append(node('h3','移除内容'),list);
 }
 if(added.length){
  container.append(node('h3','新增内容'));
  for(const i of added){const section=now[i],details=node('details'),summary=node('summary',section.heading||'未命名章节');details.append(summary,node('p','新增正文'),node('pre',section.body));container.append(details)}
 }
 if(changes.length){
  container.append(node('h3','原文与修改后内容'));
  for(const change of changes){const section=old[change.section],details=node('details'),summary=node('summary',(section.heading||'未命名章节')+' · '+change.field),before=node('pre',change.before),after=node('pre',change.after);before.className=after.className='artifact-text';details.append(summary,node('p','原文'),before,node('p','修改后'),after);container.append(details)}
 }
};
