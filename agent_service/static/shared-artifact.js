'use strict';
(function(){
 const id=new URLSearchParams(location.search).get('id')||'', $=s=>document.querySelector(s),content=$('#content'),preview=$('#preview');
 let ticket=0,scope=null,busy=false,offset=0,nextOffset=null,pageSpan=50,officePreview=null;
 const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);return n};
 function clear(message){officePreview?.close();officePreview=null;content.hidden=true;preview.replaceChildren();$('#title').textContent='查看分享成果';$('#version').textContent='';$('#page').textContent='';$('#status').textContent=message}
 function suspend(){ticket++;scope=null;busy=false;window.nexusSharedArtifactComments?.reset();clear('页面已离开，返回时将重新核对账号和分享权限。')}
 async function load(start){if(busy)return;busy=true;const n=++ticket;clear('正在验证身份和固定成果版本…');try{
  const response=await fetch('/api/shared-artifacts/'+encodeURIComponent(id)+'?offset='+start+'&limit=50',{credentials:'same-origin',cache:'no-store'});
  if(!response.ok)throw Error(response.status===401?'请从企业身份入口登录后访问。':'分享不可用或当前账号无权查看，请联系分享者。');
  const data=await response.json();if(n!==ticket)return;
  if(!/^[a-f0-9]{64}$/.test(data.sha256||'')||!/^[a-f0-9]{64}$/.test(data.viewer_hash||'')||typeof data.name!=='string'||!['document','text','html'].includes(data.render))throw Error('成果版本信息不完整。');
  if(scope&&(scope.sha!==data.sha256||scope.viewer!==data.viewer_hash))throw Error('成果版本或账号已变化，请重新打开链接。');
  if(data.render!=='html'&&data.offset!==start)throw Error('成果分页位置不一致。');
  scope={sha:data.sha256,viewer:data.viewer_hash};offset=start;nextOffset=data.next_offset;pageSpan=data.render==='text'?20000:50;$('#title').textContent=data.name;$('#version').textContent='文件 SHA-256 '+data.sha256+'。'+data.notice;
  if(data.render==='html'){
   if(typeof data.text!=='string'||typeof window.nexusSafeHtmlDocument!=='function')throw Error('HTML 安全预览不可用。');
   const frame=node('iframe');frame.setAttribute('sandbox','');frame.setAttribute('referrerpolicy','no-referrer');frame.title='安全静态 HTML 预览';frame.style.cssText='width:100%;height:70vh;border:1px solid #d8e1db;background:white';frame.srcdoc=window.nexusSafeHtmlDocument(data.text);preview.append(frame);
  }else if(data.render==='text'){if(typeof data.text!=='string')throw Error('文本预览不可用。');const pre=node('pre',data.text);pre.style.whiteSpace='pre-wrap';preview.append(pre)}
  else{if(!Array.isArray(data.blocks))throw Error('文档内容提取失败。');for(const block of data.blocks){if(block.type==='paragraph'){const p=node('p',block.text||'');if(block.slide_index)p.prepend(node('strong','第 '+block.slide_index+' 页 · '));preview.append(p)}else if(block.type==='table'){const table=node('table'),head=node('tr');for(const col of block.columns||[])head.append(node('th',col.label||col.name));table.append(head);for(const item of block.rows||[]){const tr=node('tr');for(const col of block.columns||[])tr.append(node('td',item[col.name]??''));table.append(tr)}preview.append(table)}}}
  if(data.render==='document'&&typeof window.nexusSharedOfficePreview==='function')officePreview=window.nexusSharedOfficePreview({container:preview,id,data,isCurrent:()=>n===ticket&&!!scope,onUnauthorized:()=>suspend()});
  $('#previous').hidden=start===0;$('#next').hidden=nextOffset==null;$('#download').hidden=data.allow_download!==true;$('#page').textContent=data.render==='html'?'静态安全预览':'当前显示 '+start+' 起的内容';content.hidden=false;if(data.allow_comments===true)window.nexusSharedArtifactComments?.activate({shareId:id,manifest:data.sha256,viewer:data.viewer_hash,onInvalidate:()=>{suspend();$('#status').textContent='身份或分享权限已变化，请重新打开并核对。'}});else window.nexusSharedArtifactComments?.reset();$('#status').textContent='已核对身份；显示内容不能因撤销而从接收者设备收回。';
 }catch(error){if(n===ticket){window.nexusSharedArtifactComments?.reset();clear(error.message||'成果暂不可查看。')}}finally{if(n===ticket)busy=false}}
 $('#previous').onclick=()=>load(Math.max(0,offset-pageSpan));$('#next').onclick=()=>{if(Number.isSafeInteger(nextOffset))load(nextOffset)};
 $('#download').onclick=()=>{const a=node('a');a.href='/api/shared-artifacts/'+encodeURIComponent(id)+'/download';a.download='';a.target='_blank';a.rel='noopener noreferrer';a.click();$('#status').textContent='已发起下载，服务端会再次验证账号和下载权限。'};
 if(location.protocol!=='https:'&&!['localhost','127.0.0.1','[::1]'].includes(location.hostname)){clear('请使用 HTTPS 安全地址查看分享。');return}
 if(!/^as_[a-f0-9]{40}$/.test(id)){clear('分享链接格式无效。');return}
 document.addEventListener('visibilitychange',()=>{if(document.hidden)suspend();else if(!scope&&!busy)void load(0)});window.addEventListener('storage',event=>{if(event.key==='nexus_gateway_auth_changed')suspend()});window.addEventListener('blur',suspend);window.addEventListener('focus',()=>{if(!document.hidden&&!scope&&!busy)void load(0)});void load(0);
})();
