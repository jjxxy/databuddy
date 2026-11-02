const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),JSZip=require('jszip'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),bundle=fs.readFileSync(path.join(root,'pptx-bundle/pptx-sandbox.js'),'utf8');
const tick=()=>new Promise(r=>setTimeout(r,5));
async function run(bytes){
 const dom=new JSDOM('<html><body><button id="previous"></button><span id="status"></span><button id="next"></button><img id="slide"></body></html>',{runScripts:'outside-only',url:'http://localhost/'}),w=dom.window,messages=[];
 w.setImmediate=setImmediate;w.clearImmediate=clearImmediate;w.TextDecoder=TextDecoder;w.TextEncoder=TextEncoder;w.URL.createObjectURL=()=> 'blob:slide';w.URL.revokeObjectURL=()=>{};
 w.postMessage=m=>{if(m&&m.nexusPptx)messages.push(m.nexusPptx)};let receive;const add=w.addEventListener.bind(w);w.addEventListener=(kind,fn,...rest)=>{if(kind==='message')receive=fn;else add(kind,fn,...rest)};
 w.eval(bundle);const arr=w.Uint8Array.from(bytes);receive({source:w.parent,data:{nexusPptx:'render',bytes:arr.buffer}});
 for(let i=0;i<1000&&!messages.includes('ready')&&!messages.includes('error');i++)await tick();return {dom,w,messages};
}
(async()=>{
 const good=await run(fs.readFileSync(process.argv[2]));assert(good.messages.includes('ready'),JSON.stringify(good.messages));assert.equal(good.w.document.querySelector('#slide').getAttribute('src'),'blob:slide');assert.match(good.w.document.querySelector('#status').textContent,/第 1 \/ 2 页/);good.w.document.querySelector('#next').click();assert.match(good.w.document.querySelector('#status').textContent,/第 2 \/ 2 页/);good.dom.window.close();
 const bad=await run(Buffer.from('invalid'));assert(bad.messages.includes('error'));bad.dom.window.close();
 const bomb=new JSZip();bomb.file('payload',Buffer.alloc(25*1024*1024));const oversized=await run(await bomb.generateAsync({type:'nodebuffer',compression:'DEFLATE'}));assert(oversized.messages.includes('error'));oversized.dom.window.close();
 console.log('PPTX sandbox: valid deck renders and paginates; invalid and oversized ZIP rejected');
})().catch(e=>{console.error(e);process.exitCode=1});
