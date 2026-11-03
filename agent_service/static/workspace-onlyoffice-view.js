'use strict';
/* The viewer is hosted on the isolated document-server origin, never the app origin. */
window.nexusOnlyOfficeView=function({container,value,isCurrent}){
 const origin=new URL(value.server_origin);if(origin.origin!==value.server_origin||origin.origin===location.origin||!['http:','https:'].includes(origin.protocol))throw Error('办公服务地址无效');
 const frame=document.createElement('iframe');frame.setAttribute('sandbox','allow-scripts allow-same-origin');frame.setAttribute('referrerpolicy','no-referrer');frame.title='办公文件';frame.style.cssText='display:block;width:100%;height:70vh;min-height:360px;border:0;background:#fff';frame.src=origin.origin+'/web-apps/apps/api/documents/atlas-viewer.html';
 let closed=false,timer,resolveReady;const ready=new Promise(resolve=>{resolveReady=resolve});
 function finish(ok){if(closed)return;clearTimeout(timer);resolveReady(ok)}
 function close(){if(closed)return;closed=true;clearTimeout(timer);window.removeEventListener('message',onMessage);frame.remove();resolveReady(false)}
 function onMessage(event){if(closed||event.source!==frame.contentWindow||event.origin!==origin.origin||!event.data||typeof event.data!=='object')return;if(typeof isCurrent==='function'&&!isCurrent()){close();return}
  if(event.data.nexusOnlyOffice==='loaded')frame.contentWindow.postMessage({nexusOnlyOffice:'open',config:value.config},origin.origin);
  else if(event.data.nexusOnlyOffice==='ready')finish(true);else if(event.data.nexusOnlyOffice==='error')finish(false);
 }
 window.addEventListener('message',onMessage);container.append(frame);timer=setTimeout(()=>finish(false),30000);return {ready,close};
};
