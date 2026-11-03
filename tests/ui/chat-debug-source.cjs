const {JSDOM}=require('jsdom'),fs=require('node:fs'),assert=require('node:assert/strict');
const dom=new JSDOM('<main></main>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window;
const sleep=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 w.eval(fs.readFileSync('agent_service/static/chat-business-debug.js','utf8'));w.nexusSecureUUID=()=> 'request_1';
 const fid='bf_'+'a'.repeat(40),sha='b'.repeat(64),commit='c'.repeat(40),hash='d'.repeat(64),calls=[],state=()=>({projectId:'p1',viewEpoch:1,authSealed:false});
 const doc={nodes:[{id:'read',kind:'source_table',title:'销售资料',inputs:{source:{source:'input',name:'sales'}}},{id:'sum',kind:'table_workflow',title:'汇总',depends_on:['read'],inputs:{table:{source:'node',name:'read'}}}]};
 const api=async(url,options)=>{calls.push({url,options});if(url.includes('input-sources'))return {items:[{id:'s1',name:'销售.csv',revision:2,content_hash:hash}]};if(options?.method==='POST')return {run_id:'r1',plan_digest:sha,commit,publishable:false};return {id:'r1',flow_id:fid,plan_digest:sha,commit,publishable:false,status:'succeeded',steps:[]}};
 const panel=w.createChatBusinessDebug({container:w.document.querySelector('main'),api,getState:state});
 w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail:{projectId:'p1',role:'candidate',flow:{id:fid,candidate_digest:sha,candidate_commit:commit,candidate_document:doc}}}));
 for(let i=0;i<40&&!w.document.querySelector('select');i++)await sleep();const box=w.document.querySelector('input[type=checkbox]'),select=w.document.querySelector('select');assert(box&&!box.disabled);box.checked=true;
 const button=[...w.document.querySelectorAll('button')].find(b=>b.textContent==='运行所选步骤');button.click();assert.match(w.document.body.textContent,/选择资料/);assert(!calls.some(x=>x.options?.method==='POST'));
 select.value='s1';button.click();for(let i=0;i<40&&!calls.some(x=>x.options?.method==='POST');i++)await sleep();
 const post=calls.find(x=>x.options?.method==='POST');assert.deepEqual(JSON.parse(JSON.stringify(post.options.body.inputs)),{sales:{source_id:'s1',revision:2,content_hash:hash}});
 panel.dispose();console.log('Debug source input binds a selected authorized file revision and hash');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>dom.window.close());
