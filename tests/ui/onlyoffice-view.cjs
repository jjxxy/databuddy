const {JSDOM}=require('jsdom'),fs=require('node:fs'),assert=require('node:assert/strict');
(async()=>{
 const dom=new JSDOM('<main></main>',{runScripts:'outside-only',url:'http://127.0.0.1:8000/'}),w=dom.window;
 try{
  w.eval(fs.readFileSync('agent_service/static/workspace-onlyoffice-view.js','utf8'));
  assert.throws(()=>w.nexusOnlyOfficeView({container:w.document.querySelector('main'),value:{server_origin:'http://127.0.0.1:8000'},isCurrent:()=>true}));
  const config={document:{key:'fixture'}},handle=w.nexusOnlyOfficeView({container:w.document.querySelector('main'),value:{server_origin:'http://127.0.0.1:18080',config},isCurrent:()=>true});
  const frame=w.document.querySelector('iframe');assert.equal(frame.getAttribute('sandbox'),'allow-scripts allow-same-origin');let sent=[];frame.contentWindow.postMessage=(message,origin)=>sent.push({message,origin});
  w.dispatchEvent(new w.MessageEvent('message',{source:frame.contentWindow,origin:'http://outside.invalid',data:{nexusOnlyOffice:'loaded'}}));assert.equal(sent.length,0);
  w.dispatchEvent(new w.MessageEvent('message',{source:frame.contentWindow,origin:'http://127.0.0.1:18080',data:{nexusOnlyOffice:'loaded'}}));assert.equal(sent.length,1);assert.equal(sent[0].origin,'http://127.0.0.1:18080');
  w.dispatchEvent(new w.MessageEvent('message',{source:frame.contentWindow,origin:'http://127.0.0.1:18080',data:{nexusOnlyOffice:'ready'}}));assert.equal(await handle.ready,true);handle.close();assert.equal(w.document.querySelector('iframe'),null);
  const stale=w.nexusOnlyOfficeView({container:w.document.querySelector('main'),value:{server_origin:'http://127.0.0.1:18080',config},isCurrent:()=>false}),old=w.document.querySelector('iframe');let leaked=false;old.contentWindow.postMessage=()=>{leaked=true};w.dispatchEvent(new w.MessageEvent('message',{source:old.contentWindow,origin:'http://127.0.0.1:18080',data:{nexusOnlyOffice:'loaded'}}));assert.equal(leaked,false);assert.equal(await stale.ready,false);assert.equal(w.document.querySelector('iframe'),null);
  console.log('ONLYOFFICE viewer: different origin required and messages pinned to frame and origin');
 }finally{dom.window.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
