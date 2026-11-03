'use strict';
let used=false;
const send=type=>parent.postMessage({nexusOnlyOffice:type},'*');
window.addEventListener('message',event=>{
 if(event.source!==parent||used||event.data?.nexusOnlyOffice!=='open')return;
 used=true;
 try{
  if(!event.data.config||!window.DocsAPI?.DocEditor)throw Error('config');
  const config={...event.data.config,events:{onDocumentReady:()=>send('ready'),onError:()=>send('error')}};
  new window.DocsAPI.DocEditor('office',config);
 }catch(_){send('error')}
});
send('loaded');
