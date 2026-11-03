'use strict';
/* Read-only, version-pinned workspace comparisons; never an approval. */
window.nexusVersionCompare={mount({dialog,versions,projectId,api,isCurrent}){
 const existing=dialog.querySelector('[data-version-compare]');if(existing)existing.remove();
 if(!Array.isArray(versions)||versions.length<2)return;
 const root=document.createElement('details');root.dataset.versionCompare='';
 const title=document.createElement('summary');title.textContent='对比两个版本';root.append(title);
 const description=document.createElement('p');description.textContent='比较已保存的项目文件变更，不代表财务数据或业务口径已确认。';root.append(description);
 function select(labelText,chosen){const label=document.createElement('label');label.textContent=labelText+' ';const field=document.createElement('select');field.setAttribute('aria-label',labelText);for(const item of versions){if(!/^[a-f0-9]{40}$/.test(item.commit))continue;const option=document.createElement('option');option.value=item.commit;option.textContent=item.commit.slice(0,12)+' · '+(item.reason||'工作版本');field.append(option)}field.value=chosen;label.append(field);root.append(label);return field}
 const base=select('旧版本',versions[1].commit),target=select('新版本',versions[0].commit);
 const button=document.createElement('button');button.type='button';button.className='inline-action';button.textContent='查看差异';root.append(button);
 const status=document.createElement('p');status.setAttribute('role','status');root.append(status);
 const files=document.createElement('div'),content=document.createElement('div');root.append(files,content);
 let ticket=0;
 const valid=(current)=>current===ticket&&dialog.open&&isCurrent();
 const changed=()=>{ticket++;files.replaceChildren();content.replaceChildren();status.textContent='';button.disabled=false};base.onchange=changed;target.onchange=changed;
 async function load(path){const current=++ticket;button.disabled=true;status.textContent='正在读取固定版本…';content.replaceChildren();
  try{const query=new URLSearchParams({base:base.value,target:target.value});if(path!==undefined)query.set('path',path);const value=await api('/projects/'+encodeURIComponent(projectId)+'/versions/compare?'+query);
   if(!valid(current))return;
   if(value.base!==base.value||value.target!==target.value)throw Error('比较版本已变化，请重新查看');
   if(path===undefined){files.replaceChildren();const list=document.createElement('ul');for(const file of value.files||[]){const li=document.createElement('li'),item=document.createElement('button');item.type='button';item.className='inline-action';item.textContent=({'A':'新增','M':'修改','D':'删除','T':'类型变更'}[file.status]||'变更')+' · '+file.path;item.onclick=()=>load(file.path);li.append(item);list.append(li)}files.append(list);status.textContent=value.files?.length?'选择文件查看逐行差异。':'这两个版本的文件没有差异。'}
   else{if(value.file?.path!==path)throw Error('文件差异与所选版本不一致');const note=document.createElement('p');note.textContent=value.file.note||'';content.append(note);if(value.file.kind==='text'){const pre=document.createElement('pre');pre.style.cssText='max-height:24em;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere';pre.textContent=value.file.diff||'文件内容没有文本差异。';content.append(pre)}status.textContent='正在查看：'+path}
  }catch(error){if(valid(current))status.textContent=error.message||'暂时无法读取版本差异，请重试。'}finally{if(valid(current))button.disabled=false}}
 button.onclick=()=>load();dialog.querySelector('#version-list').before(root);
}};
