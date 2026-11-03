const {JSDOM}=require('jsdom'),fs=require('node:fs'),assert=require('node:assert/strict');
const dom=new JSDOM('<main></main>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window;
const sleep=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 w.eval(fs.readFileSync('agent_service/static/chat-business-debug.js','utf8'));w.nexusSecureUUID=()=> 'request_1';let rev=2;
 const fid='bf_'+'a'.repeat(40),sha='b'.repeat(64),commit='c'.repeat(40),hash='d'.repeat(64),state=()=>({projectId:'p1',viewEpoch:1,authSealed:false});
 const doc={nodes:[{id:'read',kind:'source_table',title:'资料',inputs:{source:{source:'input',name:'sales'}}}]},detail={projectId:'p1',role:'candidate',flow:{id:fid,candidate_digest:sha,candidate_commit:commit,candidate_document:doc}};
 const api=async(url,options)=>{if(url.includes('input-sources'))return {items:[{id:'s1',name:'资料.csv',revision:rev,content_hash:rev===2?hash:'e'.repeat(64)}]};if(options?.method==='POST')return {run_id:'r1',plan_digest:sha,commit,publishable:false};return {id:'r1',flow_id:fid,plan_digest:sha,commit,publishable:false,status:'running',steps:[]}};
 let panel=w.createChatBusinessDebug({container:w.document.querySelector('main'),api,getState:state});w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail}));
 for(let i=0;i<40&&!w.document.querySelector('select');i++)await sleep();w.document.querySelector('input[type=checkbox]').checked=true;w.document.querySelector('select').value='s1';
 [...w.document.querySelectorAll('button')].find(b=>b.textContent==='运行所选步骤').click();for(let i=0;i<40&&!w.sessionStorage.getItem('nexus_debug_active:p1:'+fid+':candidate:'+sha);i++)await sleep();
 assert(w.document.querySelector('select').disabled);panel.dispose();rev=3;
 panel=w.createChatBusinessDebug({container:w.document.querySelector('main'),api,getState:state});w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail}));
 for(let i=0;i<40&&!w.document.querySelector('select');i++)await sleep();assert(w.document.querySelector('select').disabled);assert.match(w.document.querySelector('select').textContent,/原资料版本已变化或不可用/);
 panel.dispose();console.log('Running debug restores and locks its pinned source across reload');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>dom.window.close());
