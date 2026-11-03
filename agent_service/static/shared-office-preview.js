/* Visual previews are available only when the sharer permits the original file. */
'use strict';
window.nexusSharedOfficePreview=function({container,id,data,isCurrent,onUnauthorized}){
 if(!container||!/^as_[a-f0-9]{40}$/.test(id)||!data||data.allow_download!==true||!['pptx','docx'].includes(data.format)||!/^[a-f0-9]{64}$/.test(data.sha256||'')||!/^[a-f0-9]{64}$/.test(data.viewer_hash||''))return null;
 const renderer=data.format==='pptx'?window.nexusPptxPreview:window.nexusDocxPreview;
 if(typeof renderer!=='function'||!window.crypto?.subtle)return null;
 const wrap=document.createElement('section'),button=document.createElement('button'),message=document.createElement('p'),view=document.createElement('div');
 button.type='button';button.textContent='查看版式预览';message.textContent='预览会读取允许下载的原文件；复杂版式、图表和字体可能与原件不同。';wrap.append(button,message,view);container.prepend(wrap);
 let closed=false,busy=false,controller=null,handle=null;
 const current=()=>!closed&&(typeof isCurrent!=='function'||isCurrent());
 function close(){if(closed)return;closed=true;controller?.abort();handle?.close();wrap.remove()}
 button.onclick=async()=>{if(!current()||busy)return;busy=true;button.disabled=true;message.textContent='正在验证文件版本并加载版式…';controller=new AbortController();try{
  const response=await fetch('/api/shared-artifacts/'+encodeURIComponent(id)+'/download',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
  if(!current())return;if([401,403,404,410].includes(response.status)){onUnauthorized?.();return}if(!response.ok)throw Error('无法读取原文件，请重新核对分享权限。');
  const declared=Number(response.headers?.get('content-length'));if(Number.isFinite(declared)&&declared>25*1024*1024)throw Error('文件超过预览大小限制。');
  const bytes=await response.arrayBuffer();if(!current())return;if(bytes.byteLength>25*1024*1024)throw Error('文件超过预览大小限制。');
  const digest=await crypto.subtle.digest('SHA-256',bytes);if(!current())return;
  const hash=Array.from(new Uint8Array(digest),x=>x.toString(16).padStart(2,'0')).join('');if(hash!==data.sha256){onUnauthorized?.();return}
  const check=await fetch('/api/shared-artifacts/'+encodeURIComponent(id)+'?offset=0&limit=1',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
  if(!current())return;if([401,403,404,410].includes(check.status)){onUnauthorized?.();return}if(!check.ok)throw Error('无法确认账号和分享状态，请重试。');
  const latest=await check.json();if(!current())return;if(latest.sha256!==data.sha256||latest.viewer_hash!==data.viewer_hash||latest.allow_download!==true){onUnauthorized?.();return}
  const blob=new Blob([bytes],{type:'application/octet-stream'});handle=renderer({container:view,blob,isCurrent:current});
  message.textContent='已校验文件版本。版式预览可能与原件不同，请结合原文件核对。';button.hidden=true;
  if(handle?.ready){const ok=await handle.ready;if(current()&&!ok){handle.close();handle=null;button.hidden=false;message.textContent='暂不能安全显示版式，仍可查看下面的文字和表格。'}}
 }catch(e){if(current())message.textContent=e?.message||'版式预览暂不可用。'}finally{if(current()){busy=false;button.disabled=false}}};
 return {close};
};
