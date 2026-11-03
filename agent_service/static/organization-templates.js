'use strict';
/* Instance catalog uses only verified OIDC and explicit per-project copies. */
(()=>{
 const project=new URLSearchParams(location.search).get('project')||'',panel=document.getElementById('organization-templates'),list=document.getElementById('organization-template-list'),status=document.getElementById('organization-template-status');
 if(!panel||!list||!status||!project)return;
 const token=sessionStorage.getItem('nexus_token')||'';let epoch=0,busy=false,pending=null;
 const node=(tag,text)=>{const value=document.createElement(tag);if(text!==undefined)value.textContent=text;return value};
 async function request(path,options={}){
  const headers=token?{Authorization:'Bearer '+token}:{};if(options.body)headers['Content-Type']='application/json';
  let result;try{result=await fetch(path,{method:options.method||'GET',headers,body:options.body?JSON.stringify(options.body):undefined,credentials:'same-origin',cache:'no-store'})}
  catch(error){throw Object.assign(Error('网络中断，操作状态未确认。'),{uncertain:true})}
  if(!result.ok){let message=result.status===404?'当前工作空间未开放实例模板库。':'当前实例模板库不可用。';try{const body=await result.json();if(result.status!==404&&typeof body.detail==='string')message=body.detail}catch(_){}throw Object.assign(Error(message),{status:result.status,uncertain:options.method==='POST'})}
  try{return await result.json()}catch(error){throw Object.assign(Error('返回结果无法核对。'),{uncertain:options.method==='POST'})}
 }
 function controls(){for(const b of list.querySelectorAll('button'))b.disabled=busy||!!pending}
 async function copy(item,repeat=false){
  if(busy||pending&&!repeat)return;
  if(!repeat){let id;try{id=window.nexusSecureUUID()}catch(e){status.textContent=e.message;return}pending={item,id}}
  const current=pending;busy=true;controls();const ticket=++epoch;status.textContent='正在复制固定版本模板…';
  try{
   const value=await request('/api/projects/'+encodeURIComponent(project)+'/organization-ppt-templates/'+encodeURIComponent(current.item.id)+'/copy',{
    method:'POST',body:{version:current.item.version,sha256:current.item.sha256,request_id:current.id}});
   if(ticket!==epoch)return;
   if(value.organization_template_id!==current.item.id||value.template?.source_sha256!==current.item.sha256)throw Object.assign(Error('无法确认复制结果，请核对我的模板。'),{uncertain:true});
   pending=null;retry.hidden=true;status.textContent='已复制固定版本。请点击上方“刷新清单”后，在我的模板中填写；源模板撤下后，已复制文件仍保留。';
  }catch(error){if(ticket!==epoch)return;if(error.uncertain){status.textContent='未确认复制是否成功。可用相同请求再次核对，或刷新我的模板后确认；不要重复创建。';retry.hidden=false}else{pending=null;status.textContent=error.message;retry.hidden=true}}
  finally{if(ticket===epoch){busy=false;controls()}}
 }
 const retry=node('button','重试同一复制请求');retry.type='button';retry.hidden=true;retry.onclick=()=>copy(null,true);panel.append(retry);
 async function load(){const ticket=++epoch;status.textContent='正在读取实例模板…';try{
  const data=await request('/api/organization/ppt-templates');if(ticket!==epoch)return;if(!Array.isArray(data.items))throw Error('模板清单格式不正确');list.replaceChildren();
  if(!data.items.length)list.append(node('p','当前没有已发布模板。'));
  for(const item of data.items){if(!/^ot_[a-f0-9]{40}$/.test(item.id)||!/^[a-f0-9]{64}$/.test(item.sha256)||!Number.isSafeInteger(item.version))throw Error('模板版本信息不完整');
   const card=node('div');card.className='template-card';card.append(node('strong',item.title),node('small','来源版本 '+item.source_revision+' · SHA-256 '+item.sha256));const button=node('button','复制到我的模板');button.type='button';button.onclick=()=>copy(item);card.append(button);list.append(card)}
  status.textContent=(data.truncated?'仅显示前 500 项。':'')+'仅当前实例授权账号可使用；模板内容与版式仍需核对。';controls();
 }catch(error){if(ticket===epoch){list.replaceChildren();status.textContent=error.message||'当前实例模板库不可用。'}}}
 window.addEventListener('beforeunload',e=>{if(pending){e.preventDefault();e.returnValue=''}});
 void load();
})();
