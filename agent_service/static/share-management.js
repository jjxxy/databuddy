'use strict';
/* Explicit OIDC-subject sharing of one currently adopted data result. */
window.nexusShareUI=(()=>{
 let scope=null, serial=0, busy=false, uncertain=false, deployment=null;
 const root=()=>document.getElementById('data-share-panel');
 const node=(tag,text)=>{const n=document.createElement(tag);if(text!=null)n.textContent=String(text);return n};
 const valid=s=>scope===s&&s.isCurrent()&&root()&&!root().hidden;
 const runId=v=>typeof v==='string'?v:(v?.id||v?.run_id||'');
 function clear(){serial++;scope=null;busy=false;uncertain=false;const r=root();if(r){r.replaceChildren();r.hidden=true}}
 async function deploymentMode(){
  if(deployment)return deployment;
  if(location.protocol!=='https:'&&!['localhost','127.0.0.1','[::1]'].includes(location.hostname))throw Error('分享需要 HTTPS');
  // This gateway routes to the visitor's own workspace; it does not resolve
  // a cross-user share ID. Never advertise a link that cannot reach the owner.
  const res=await fetch('/health',{cache:'no-store',credentials:'same-origin'});
  if(!res.ok)throw Error('无法确认当前分享服务，请稍后再试');
  const value=await res.json();
  if(value?.scope==='isolated_workspace_gateway')return deployment='gateway';
  if(value?.status==='ok')return deployment='direct';
  throw Error('无法确认当前分享服务，请稍后再试');
 }
 function shareUrl(id){if(!/^share_[a-f0-9]{24}$/.test(id))throw Error('分享编号无效');const u=new URL('/shared-result.html',location.origin);u.searchParams.set('id',id);return u.href}
 function sync(value){
  const panel=root();if(!panel)return;
  const published=runId(value.workflow?.published_run), key=[value.project,value.workflow?.workflow_id,value.resultId,published,value.result?.manifest_sha256].join('|');
  if(!value.resultId||value.resultId!==published||value.result?.run_id!==published||!value.result?.manifest_sha256){clear();return}
  if(scope?.key===key)return;
  clear();const s={...value,key,published};scope=s;panel.hidden=false;
  const heading=node('h2','分享正式数据结果');const note=node('p','仅限指定企业账号查看当前已采用的结果。允许下载时，对方可取得全部结果数据；CSV 的空值与空字符串无法区分，类公式文本会加单引号，高精度数字和长编号须按文本导入。分享不会发送消息，版本更新后旧链接失效；不等同于财务审批。');
  const mode=node('p','正在检查分享服务…');mode.setAttribute('role','status');
  const body=node('div');body.hidden=true;
  const form=node('form');const label=node('label','接收账号的企业身份主体标识（每行一个）');const subjects=node('textarea');subjects.required=true;subjects.rows=3;subjects.maxLength=20000;subjects.placeholder='请从企业身份管理员处确认账号主体标识';label.append(subjects);
  const hoursLabel=node('label','有效时间（小时，1–720）');const hours=node('input');hours.type='number';hours.min='1';hours.max='720';hours.value='168';hours.required=true;hoursLabel.append(hours);
  const downloadLabel=node('label');const download=node('input');download.type='checkbox';downloadLabel.append(download,document.createTextNode('允许下载完整结果 CSV'));
  const commentLabel=node('label');const comments=node('input');comments.type='checkbox';commentLabel.append(comments,document.createTextNode('允许获准账号评论（所有分享接收者均可见，不构成财务审批）'));
  const create=node('button','创建分享链接');create.type='submit';const refresh=node('button','刷新分享记录');refresh.type='button';const message=node('p');message.setAttribute('role','status');const list=node('div');
  form.append(label,hoursLabel,downloadLabel,commentLabel,create,refresh);body.append(form,message,list);panel.append(heading,note,mode,body);
  function setBusy(value){busy=value;create.disabled=value||uncertain;refresh.disabled=value;for(const button of list.querySelectorAll('button'))button.disabled=value}
  async function refreshList(){
   const ticket=++serial;setBusy(true);message.textContent='正在读取分享记录…';
   try{const payload=await s.request('/api/projects/'+encodeURIComponent(s.project)+'/data-shares');if(!valid(s)||ticket!==serial)return;
    const records=payload?.items;if(!Array.isArray(records))throw Error('分享记录格式异常');list.replaceChildren();
    const relevant=records.filter(r=>r.result_id===s.published);
    if(!relevant.length)list.append(node('p','当前正式版本还没有分享记录。'));
    for(const record of relevant){
     const box=node('div'),info=node('p');const expiry=Date.parse(record.expires_at),active=record.state==='active'&&Number.isFinite(expiry)&&expiry>Date.now();info.textContent=(active?'有效':record.state==='revoked'?'已撤销':'已过期或状态不可确认')+' · 有效期至 '+(record.expires_at||'未记录')+' · '+(Number.isInteger(record.allowed_subject_count)?record.allowed_subject_count+' 个账号 · ':'')+(record.allow_download?'允许下载':'不允许下载')+' · '+(record.allow_comments?'允许评论':'不允许评论');box.append(info);
     if(active){
      const url=shareUrl(record.id), field=node('input');field.readOnly=true;field.value=url;field.setAttribute('aria-label','分享链接');box.append(field);
      const copy=node('button','复制链接');copy.type='button';copy.onclick=async()=>{try{if(!navigator.clipboard?.writeText)throw Error();await navigator.clipboard.writeText(url);if(valid(s))message.textContent='链接已复制，请自行发送给获准访问的账号。'}catch(_){field.focus();field.select();if(valid(s))message.textContent='无法自动复制，已选中链接，请手动复制。'}};box.append(copy);
      const revoke=node('button','撤销此链接');revoke.type='button';revoke.onclick=async()=>{const stamp=++serial;setBusy(true);message.textContent='正在撤销…';try{await s.request('/api/projects/'+encodeURIComponent(s.project)+'/data-shares/'+encodeURIComponent(record.id),{method:'DELETE'});if(!valid(s)||stamp!==serial)return;await refreshList()}catch(error){if(valid(s)&&stamp===serial){uncertain=true;message.textContent='未确认撤销结果，请刷新分享记录后核对。';setBusy(false)}}};box.append(revoke);
     }
     list.append(box);
    }
    uncertain=false;message.textContent='已读取当前正式版本的分享记录。';
   }catch(error){if(valid(s)&&ticket===serial){uncertain=true;message.textContent='暂时无法读取分享记录，请重试。'}}finally{if(valid(s)&&ticket===serial)setBusy(false)}
  }
  refresh.onclick=refreshList;
  form.onsubmit=async event=>{event.preventDefault();if(busy||uncertain||!valid(s))return;
   const allowed=subjects.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean),n=Number(hours.value);
   if(!allowed.length||allowed.length>100||new Set(allowed).size!==allowed.length||!Number.isInteger(n)||n<1||n>720){message.textContent='请检查账号主体标识和有效时间，账号不能重复。';return}
   const stamp=++serial;setBusy(true);message.textContent='正在创建分享…';
   try{const created=await s.request('/api/projects/'+encodeURIComponent(s.project)+'/data-results/'+encodeURIComponent(s.published)+'/shares',{method:'POST',body:{allowed_subjects:allowed,expires_hours:n,allow_download:download.checked,allow_comments:comments.checked}});
    if(!valid(s)||stamp!==serial)return;if(created.result_id!==s.published||created.state!=='active')throw Error('分享结果与当前版本不符');subjects.value='';await refreshList();
   }catch(error){if(valid(s)&&stamp===serial){if([400,403,404,409,422].includes(error.status)){uncertain=false;message.textContent=error.detail||error.message||'分享未创建，请核对后重试。'}else{uncertain=true;message.textContent='未确认是否创建成功。请先刷新分享记录核对，避免重复创建。'}setBusy(false)}}
  };
  deploymentMode().then(modeValue=>{if(!valid(s))return;if(modeValue==='gateway'){mode.textContent='当前独立工作空间网关尚不支持跨账号分享访问，不能创建可用链接。';return}mode.textContent='接收者须通过本服务配置的企业身份反向代理访问；如未接入，请先联系管理员。';body.hidden=false;void refreshList()}).catch(()=>{if(valid(s))mode.textContent='无法确认分享服务。请使用 HTTPS（本机可使用 localhost），并检查服务配置后重新打开。'});
 }
 return {sync,clear};
})();
