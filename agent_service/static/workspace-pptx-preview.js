/* Presentation files are rendered inside an isolated, network-disabled frame. */
window.nexusPptxPreview=function({container,blob,isCurrent}){
 if(!container||!(blob instanceof Blob)||blob.size>25*1024*1024)throw Error('演示文稿过大或无法读取，请下载原件查看。');
 const frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-scripts');frame.setAttribute('referrerpolicy','no-referrer');frame.title='演示文稿';frame.style.cssText='display:block;width:100%;height:70vh;min-height:360px;border:0;background:#fff';frame.src='/pptx-frame.html';
 const note=document.createElement('p');note.textContent='页面内显示原文件的内容；字体、动画和复杂图形可能与 PowerPoint 不同，请核对原件。';
 let closed=false,timer,resolveReady;const ready=new Promise(resolve=>{resolveReady=resolve});
 function finish(message){if(closed)return;note.textContent=message;resolveReady(false)}
 function close(){if(closed)return;closed=true;clearTimeout(timer);window.removeEventListener('message',onMessage);frame.remove();note.remove();resolveReady(false)}
 async function onMessage(event){if(closed||event.source!==frame.contentWindow||!event.data||typeof event.data!=='object')return;if(typeof isCurrent==='function'&&!isCurrent()){close();return}
  if(event.data.nexusPptx==='loaded'){try{const bytes=await blob.arrayBuffer();if(closed||typeof isCurrent==='function'&&!isCurrent()){close();return}frame.contentWindow.postMessage({nexusPptx:'render',bytes},'*',[bytes])}catch{finish('演示文稿无法读取，请下载原件查看。')}}
  else if(event.data.nexusPptx==='ready'){clearTimeout(timer);resolveReady(true)}else if(event.data.nexusPptx==='error'){clearTimeout(timer);finish('演示文稿无法安全显示，请下载原件查看。')}
 }
 window.addEventListener('message',onMessage);container.append(note,frame);timer=setTimeout(()=>{if(!closed){frame.remove();finish('演示文稿显示超时，请下载原件查看。')}},30000);
 return {ready,close};
};
