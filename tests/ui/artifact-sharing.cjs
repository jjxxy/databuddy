'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
const delay=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 const html=fs.readFileSync('agent_service/static/shared-artifact.html','utf8');
 const d=new JSDOM(html,{url:'https://fixture.test/shared-artifact.html?id=as_'+('a'.repeat(40)),runScripts:'outside-only'}),w=d.window;
 w.fetch=async()=>({ok:true,json:async()=>({name:'<img src=x onerror=alert(1)>',sha256:'a'.repeat(64),render:'html',allow_download:false,text:'<html><head><meta http-equiv="refresh" content="0;url=https://evil.test"><link href="https://evil.test/css"></head><body><script>alert(1)</script><form action="https://evil.test"><input></form><iframe src="https://evil.test"></iframe><a href="https://evil.test">link</a><img src="https://evil.test/x" onerror="alert(1)"><img src="data:image/png;base64,AA"><div style="background:url(https://evil.test/x)">报告</div></body></html>',notice:'test'})});
 w.eval(fs.readFileSync('agent_service/static/workspace-html-view.js','utf8'));w.eval(fs.readFileSync('agent_service/static/shared-artifact.js','utf8'));await delay();
 const frame=w.document.querySelector('#preview iframe');assert(frame);assert.equal(frame.getAttribute('sandbox'),'');assert.equal(frame.getAttribute('referrerpolicy'),'no-referrer');const safe=new w.DOMParser().parseFromString(frame.srcdoc,'text/html');
 assert.equal(safe.querySelector('script,iframe,form,link,meta[http-equiv=refresh]'),null);assert.equal(safe.querySelector('a').getAttribute('href'),null);assert.equal(safe.querySelector('img').getAttribute('src'),null);assert.equal(safe.querySelector('img').getAttribute('onerror'),null);const policy=safe.querySelector('meta[http-equiv="Content-Security-Policy"]').getAttribute('content');for(const directive of ["default-src 'none'","connect-src 'none'","frame-src 'none'","form-action 'none'","object-src 'none'"])assert(policy.includes(directive));assert.equal(w.document.querySelector('#title').textContent,'<img src=x onerror=alert(1)>');assert.equal(w.document.querySelector('#title img'),null);
 w.dispatchEvent(new w.Event('blur'));assert.equal(w.document.querySelector('#content').hidden,true);assert.equal(w.document.querySelector('#preview').children.length,0);
 console.log('artifact HTML preview sandbox, CSP, XSS and identity suspension verified');
})().catch(error=>{console.error(error);process.exit(1)});
