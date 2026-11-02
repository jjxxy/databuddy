/* Isolated read-only DOCX layout preview; no remote service or external network. */
window.nexusDocxPreview=function({container,blob,isCurrent}){
 if(!container||!(blob instanceof Blob)||blob.size>25*1024*1024)throw Error('文档过大或无法读取，请下载原件查看。');
 const frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-scripts');frame.setAttribute('referrerpolicy','no-referrer');frame.title='Word 文档版式预览';frame.style.cssText='display:block;width:100%;height:70vh;min-height:360px;border:0;background:#fff';frame.src='/docx-frame.html';
 const note=document.createElement('p');note.textContent='此预览由浏览器排版，字体、分页和复杂内容可能与原文件不同；请下载原件核对。';
 let closed=false,timer,resolveReady;const ready=new Promise(resolve=>{resolveReady=resolve});
 function finish(message){if(closed)return;note.textContent=message;resolveReady(false)}
 function close(){if(closed)return;closed=true;clearTimeout(timer);window.removeEventListener('message',onMessage);frame.remove();note.remove();resolveReady(false)}
 async function onMessage(event){if(closed||event.source!==frame.contentWindow||!event.data||typeof event.data!=='object')return;if(typeof isCurrent==='function'&&!isCurrent()){close();return}
  if(event.data.nexusDocx==='loaded'){try{const bytes=await blob.arrayBuffer();if(closed||typeof isCurrent==='function'&&!isCurrent()){close();return}frame.contentWindow.postMessage({nexusDocx:'render',bytes},'*',[bytes])}catch{finish('文档无法读取，请下载原件查看。')}}
  else if(event.data.nexusDocx==='ready'){clearTimeout(timer);resolveReady(true)}else if(event.data.nexusDocx==='error'){clearTimeout(timer);finish('文档无法安全预览，请下载原件查看。')}
 }
 window.addEventListener('message',onMessage);container.append(note,frame);timer=setTimeout(()=>{if(!closed){frame.remove();finish('文档预览超时，请下载原件查看。')}},30000);
 return {ready,close};
};
