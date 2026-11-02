const {JSDOM}=require('jsdom'),fs=require('node:fs'),assert=require('node:assert/strict');
const dom=new JSDOM('<main></main>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window;
const wait=()=>new Promise(resolve=>setTimeout(resolve,10));
(async()=>{
 w.eval(fs.readFileSync('agent_service/static/chat-business-debug.js','utf8'));
 const fid='bf_'+'a'.repeat(40),newFlow='bf_'+'d'.repeat(40),sha='b'.repeat(64),commit='c'.repeat(40),newSha='e'.repeat(64),newCommit='f'.repeat(40);
 const doc={nodes:[{id:'read',kind:'source_table',title:'读取资料',inputs:{}}]},state=()=>({projectId:'p1',viewEpoch:1,authSealed:false});
 w.sessionStorage.setItem('nexus_debug_active:p1:'+fid+':candidate:'+sha,JSON.stringify({id:'run1',sha,commit,selected_nodes:['read']}));
 const api=async()=>({id:'run1',flow_id:fid,plan_digest:sha,commit,publishable:false,status:'failed',steps:[{node_id:'read',status:'failed',error_code:'SOURCE_VERSION_CHANGED'}],auto_repair:{flow_id:newFlow,run_id:'run2',plan_digest:newSha,commit:newCommit,selected_nodes:['read'],status:'succeeded',publishable:false}});
 const panel=w.createChatBusinessDebug({container:w.document.querySelector('main'),api,getState:state});let opened='';w.addEventListener('nexus-open-chat-flow',e=>{opened=e.detail.id});
 w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail:{projectId:'p1',role:'candidate',flow:{id:fid,candidate_digest:sha,candidate_commit:commit,candidate_document:doc}}}));
 for(let i=0;i<40&&![...w.document.querySelectorAll('button')].some(b=>b.textContent==='查看修复副本');i++)await wait();
 assert.match(w.document.body.textContent,/原流程与正式结果未更改/);assert.match(w.document.body.textContent,/已完成，仍需核对/);
 [...w.document.querySelectorAll('button')].find(b=>b.textContent==='查看修复副本').click();assert.equal(opened,newFlow);
 assert.equal(JSON.parse(w.sessionStorage.getItem('nexus_debug_active:p1:'+newFlow+':candidate:'+newSha)).id,'run2');
 panel.dispose();console.log('Auto repair displays a separate verified debug run and preserves the original');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>dom.window.close());
