'use strict';
/* Account- and project-scoped template registrations; all labels are text. */
(function(){
 const project=new URLSearchParams(location.search).get('project')||'';
 const $=s=>document.querySelector(s), state={items:[],sources:[],selected:null,fields:[],busy:false,generation:0,dirty:false,uploadUncertain:false,renderUncertain:false};
 const enc=encodeURIComponent,root='/api/projects/'+enc(project),token=sessionStorage.getItem('nexus_token')||'';
 function node(tag,text){const e=document.createElement(tag);if(text!=null)e.textContent=String(text);return e}
 function notice(message,error=false){const p=$('#status');p.textContent=message||'';p.classList.toggle('error',error)}
 function changed(){return !state.dirty||window.confirm('尚有未生成的填写内容。切换后这些内容将丢失，继续吗？')}
 function invalidate(){state.generation++;state.selected=null;state.fields=[];state.dirty=false;state.renderUncertain=false;$('#editor').hidden=true;$('#positions').replaceChildren();$('#result').replaceChildren();$('#selected').textContent='从左侧选择模板，查看可以填写的文字位置。'}
 async function request(path,opts={}){
  const headers=token?{Authorization:'Bearer '+token}:{};let body=opts.body;
  if(body && !(body instanceof FormData)){headers['Content-Type']='application/json';body=JSON.stringify(body)}
  let response;try{response=await fetch(path,{method:opts.method||'GET',headers,body,credentials:'same-origin',cache:'no-store'})}
  catch(e){throw Object.assign(Error('网络连接中断，请刷新并核对操作结果。'),{uncertain:true})}
  if(!response.ok){let detail='操作未完成，请刷新后重试。';try{const data=await response.json();if(typeof data.detail==='string')detail=data.detail}catch(_){}
   if(response.status===401)detail='登录已失效，请返回工作空间重新登录。';throw Object.assign(Error(detail),{status:response.status})}
  return opts.binary?response:response.json();
 }
 function busy(value){state.busy=value;for(const b of document.querySelectorAll('button,select,input,textarea')){
  if(value){b.dataset.priorDisabled=b.disabled?'1':'0';b.disabled=true}
  else if(b.dataset.priorDisabled!==undefined){b.disabled=b.dataset.priorDisabled==='1';delete b.dataset.priorDisabled}
 }$('#upload').disabled=value||state.uploadUncertain;$('#render').disabled=value||state.renderUncertain}
 function sourceOptions(){const select=$('#source');select.replaceChildren(node('option','请选择文件'));select.firstChild.value='';
  for(const s of state.sources){const o=node('option',s.name+' · 版本 '+s.revision);o.value=s.id;select.append(o)} }
 function templateCards(){const list=$('#templates');list.replaceChildren();$('#empty').hidden=state.items.length>0;
  for(const item of state.items){const card=node('div');card.className='template-card';const title=node('strong',item.title);const detail=node('small',item.available?'来源版本 '+item.source_revision:'来源版本已变化或不可用，请重新登记');const open=node('button','填写');open.type='button';open.disabled=!item.available;open.addEventListener('click',()=>selectTemplate(item));
   const rename=node('button','改名');rename.type='button';rename.addEventListener('click',()=>renameTemplate(item));const remove=node('button','移除');remove.type='button';remove.addEventListener('click',()=>removeTemplate(item));card.append(title,detail,open,rename,remove);list.append(card)}
 }
 async function refresh(){if(state.busy||!changed())return;busy(true);notice('正在读取模板…');invalidate();try{
  const [templates,sources]=await Promise.all([request(root+'/personal-ppt-templates'),request(root+'/personal-ppt-template-sources')]);
  state.items=templates.items||[];state.sources=sources.items||[];sourceOptions();templateCards();state.uploadUncertain=false;
  notice(templates.truncated||sources.truncated?'当前只显示前 500 项，请整理项目资料后再查看。':'');
 }catch(e){notice(e.message,true)}finally{busy(false)} }
 async function selectTemplate(item){if(state.busy||!changed())return;const generation=++state.generation;busy(true);notice('正在核对模板版本并读取可填写位置…');
  try{const detail=await request(root+'/personal-ppt-templates/'+enc(item.id));if(generation!==state.generation)return;
   state.selected=detail.template;state.fields=[];state.dirty=false;$('#result').replaceChildren();$('#selected').textContent=detail.template.title+' · 来源版本 '+detail.template.source_revision;
   const positions=$('#positions');positions.replaceChildren();
   for(const slide of detail.structure.slides||[]){const fields=[...(slide.placeholders||[]),...(slide.text_shapes||[])];if(!fields.length)continue;const page=node('section');page.className='page';page.append(node('h3','第 '+slide.page+' 页'));
    for(const field of fields){const box=node('div');box.className='position';const label=node('label');const check=document.createElement('input');check.type='checkbox';label.append(check,document.createTextNode(field.name||'文字位置'));const old=node('small',(field.text_truncated?'原文摘录：':'原文：')+(field.text||'（空）'));const input=document.createElement('textarea');input.maxLength=1000;input.value=field.text_truncated?'':field.text||'';input.setAttribute('aria-label','填写 '+(field.name||'文字位置'));input.disabled=true;
     check.addEventListener('change',()=>{input.disabled=!check.checked;state.dirty=state.fields.some(x=>x.check.checked)});input.addEventListener('input',()=>{if(check.checked)state.dirty=true});box.append(label,old,input);page.append(box);state.fields.push({page:slide.page,shape_id:field.shape_id,check,input})}
    positions.append(page)}
   if(!state.fields.length)positions.append(node('p','此模板没有可直接填写的文字位置。'));
   $('#editor').hidden=false;notice('');
  }catch(e){invalidate();notice(e.message,true)}finally{busy(false);for(const field of state.fields)field.input.disabled=!field.check.checked} }
 async function removeTemplate(item){if(state.busy||!changed())return;if(!window.confirm('从我的清单移除“'+item.title+'”？项目原文件和已有成果会保留。'))return;
  let removed=false;busy(true);try{await request(root+'/personal-ppt-templates/'+enc(item.id)+'/remove',{method:'POST',body:{version:item.version}});state.dirty=false;removed=true}catch(e){notice(e.uncertain?'未确认移除结果，请刷新清单核对。':e.message,true)}finally{busy(false)}if(removed){await refresh();notice('已从清单移除，原文件保留。')} }
 async function renameTemplate(item){if(state.busy||!changed())return;const title=window.prompt('新的模板名称',item.title);if(title===null)return;if(!title.trim()||title.length>100){notice('请输入 1 至 100 个字符的名称。',true);return}
  let renamed=false;busy(true);try{await request(root+'/personal-ppt-templates/'+enc(item.id),{method:'PUT',body:{version:item.version,title}});state.dirty=false;renamed=true}catch(e){notice(e.uncertain?'未确认改名结果，请刷新清单核对。':e.message,true)}finally{busy(false)}if(renamed){await refresh();notice('模板名称已更新。')} }
 $('#source').addEventListener('change',()=>{const s=state.sources.find(x=>x.id===$('#source').value);if(s&&!$('#title').value)$('#title').value=s.name.replace(/\.pptx$/i,'')});
 $('#register-form').addEventListener('submit',async e=>{e.preventDefault();if(state.busy)return;const s=state.sources.find(x=>x.id===$('#source').value);if(!s){notice('请选择项目文件。',true);return}
  let saved=false;busy(true);try{await request(root+'/personal-ppt-templates',{method:'POST',body:{source_id:s.id,revision:s.revision,sha256:s.sha256,title:$('#title').value}});$('#title').value='';saved=true}catch(e){notice(e.uncertain?'未确认登记结果，请刷新清单核对。':e.message,true)}finally{busy(false)}if(saved){await refresh();notice('模板已登记。')} });
 $('#upload').addEventListener('click',async()=>{if(state.busy||state.uploadUncertain)return;const file=$('#upload-file').files?.[0];if(!file||!file.name.toLowerCase().endsWith('.pptx')){notice('请选择 PPTX 文件。',true);return}
  let id;try{id=window.nexusSecureUUID()}catch(e){notice(e.message,true);return}const body=new FormData();body.append('file',file);body.append('request_id',id);body.append('local_processing','true');busy(true);notice('正在上传模板…');
  let uploaded=false;try{await request(root+'/attachments',{method:'POST',body});$('#upload-file').value='';uploaded=true}
  catch(e){if(e.uncertain){state.uploadUncertain=true;notice('未确认上传结果。请先刷新清单，核对项目文件后再操作。',true)}else notice(e.message,true)}finally{busy(false)}
  if(uploaded){await refresh();notice('文件已上传，请在项目文件中选择并保存为模板。')} });
 $('#render').addEventListener('click',async()=>{if(state.busy||state.renderUncertain||!state.selected)return;const selected=state.selected,fields=state.fields.filter(f=>f.check.checked).map(f=>({page:f.page,shape_id:f.shape_id,text:f.input.value}));
  if(!fields.length){notice('请至少勾选一个需要填写的位置。',true);return}busy(true);notice('正在生成文件并核对模板版本…');
  try{const value=await request(root+'/personal-ppt-templates/'+enc(selected.id)+'/render',{method:'POST',body:{version:selected.version,name:$('#output-name').value,replacements:fields}});
   if(!/^of_[0-9a-f]{40}$/.test(value.artifact_id||'')||!Number.isInteger(value.template_revision)||value.template_revision!==selected.source_revision||!/^[0-9a-f]{64}$/.test(value.template_sha256||'')||value.template_sha256!==selected.source_sha256)throw Object.assign(Error('成果版本信息不完整，请到项目成果中核对。'),{uncertain:true});state.dirty=false;const url=root+'/deliverables/'+enc(value.artifact_id),button=node('button','下载 PPTX');button.type='button';button.addEventListener('click',async()=>{button.disabled=true;try{const response=await request(url,{binary:true});const blob=await response.blob();const link=node('a');link.href=URL.createObjectURL(blob);link.download=$('#output-name').value+'.pptx';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(link.href),1000)}catch(e){notice(e.message,true)}finally{button.disabled=false}});$('#result').replaceChildren(node('span','文件已生成。使用模板版本 '+value.template_revision+'，SHA-256：'+value.template_sha256+'。来源可能已更新，成果不会自动切换版本；请核对版式与数据。 '),button);notice('');
  }catch(e){if(e.uncertain)state.renderUncertain=true;notice(e.uncertain?'未确认生成结果，请在项目成果中核对；刷新状态后才能再次生成。':e.message,true)}finally{busy(false);for(const field of state.fields)field.input.disabled=!field.check.checked} });
 $('#refresh').addEventListener('click',refresh);window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue=''}});
 if(!project){notice('请从项目成果打开模板清单。',true);for(const b of document.querySelectorAll('button,input,select'))b.disabled=true}else refresh();
})();
