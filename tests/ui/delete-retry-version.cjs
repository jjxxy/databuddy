'use strict';
const {JSDOM}=require('jsdom');const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const d=new JSDOM('<section id="data-append-panel" hidden></section>',{runScripts:'outside-only',url:'https://fixture.test'}),w=d.window;
 w.nexusSecureUUID=()=> '11111111-1111-4111-8111-111111111111';w.eval(fs.readFileSync('agent_service/static/append-results.js','utf8'));
 const sha='a'.repeat(64),dsha='b'.repeat(64),calls=[];let n=0;
 const request=async(path,options)=>{calls.push([path,options]);if(path.endsWith('/data-workflows'))return {items:[{name:'删除清单',published_run:'delta'}]};if(path.includes('/data-results/delta'))return {run_id:'delta',manifest_sha256:dsha};if(n++===0)throw Error('network');let e=Error('输入结果已不是当前正式版本，请刷新');e.status=400;throw e};
 const panel=w.document.querySelector('#data-append-panel');w.nexusAppendResults.sync({project:'p',workflow:{published_run:'base'},result:{run_id:'base',manifest_sha256:sha},resultId:'base',request,isCurrent:()=>true});await new Promise(r=>setTimeout(r,0));
 const [target,mode]=panel.querySelectorAll('select');target.value='delta';mode.value='delete';mode.dispatchEvent(new w.Event('change'));panel.querySelector('input').checked=true;panel.querySelector('textarea').value='已核对删除范围';panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));
 assert(panel.textContent.includes('正式版本已变化，重试可能失败'));const first=calls.at(-1)[1].body;
 panel.querySelector('button').click();await new Promise(r=>setTimeout(r,0));assert.deepEqual(calls.at(-1)[1].body,first);assert(panel.textContent.includes('原请求是否已创建尚不确定'));assert(panel.textContent.includes('先在任务列表核对'));assert(panel.textContent.includes('输入结果已不是当前正式版本'));assert(panel.querySelector('a').hidden);
 d.window.close();console.log('uncertain retry with changed official version explains candidate outcome remains unknown');
})().catch(e=>{console.error(e);process.exit(1)});
