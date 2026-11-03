const {JSDOM}=require('jsdom'),fs=require('node:fs'),assert=require('node:assert/strict');
const dom=new JSDOM('<main></main>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window;
const sleep=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 w.eval(fs.readFileSync('agent_service/static/chat-business-debug.js','utf8'));w.nexusSecureUUID=()=> 'request_1';let rev=2,posts=[];
 const fid='bf_'+'a'.repeat(40),sha='b'.repeat(64),commit='c'.repeat(40),oldHash='d'.repeat(64),state=()=>({projectId:'p1',viewEpoch:1,authSealed:false});
 const doc={nodes:[{id:'read',kind:'source_table',title:'资料',inputs:{source:{source:'input',name:'sales'}}}]},detail={projectId:'p1',role:'candidate',flow:{id:fid,candidate_digest:sha,candidate_commit:commit,candidate_document:doc}};
 const api=async(url,options)=>{if(url.includes('input-sources'))return {items:[{id:'s1',name:'资料.csv',revision:rev,content_hash:rev===2?oldHash:'e'.repeat(64)}]};if(options?.method==='POST'){posts.push(JSON.parse(JSON.stringify(options.body)));throw Error('request outcome unknown')}throw Error('unexpected')};
 let panel=w.createChatBusinessDebug({container:w.document.querySelector('main'),api,getState:state});w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail}));
 for(let i=0;i<40&&!w.document.querySelector('select');i++)await sleep();w.document.querySelector('input[type=checkbox]').checked=true;w.document.querySelector('select').value='s1';
 [...w.document.querySelectorAll('button')].find(b=>b.textContent==='运行所选步骤').click();for(let i=0;i<40&&!posts.length;i++)await sleep();
 panel.dispose();rev=3;panel=w.createChatBusinessDebug({container:w.document.querySelector('main'),api,getState:state});w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail}));
 for(let i=0;i<40&&!w.document.querySelector('select');i++)await sleep();assert.match(w.document.body.textContent,/原资料版本已变化或不可用/);assert(w.document.querySelector('select').disabled);
 [...w.document.querySelectorAll('button')].find(b=>b.textContent==='运行所选步骤').click();for(let i=0;i<40&&posts.length<2;i++)await sleep();assert.deepEqual(posts[1],posts[0]);assert.equal(posts[1].inputs.sales.revision,2);assert.equal(posts[1].inputs.sales.content_hash,oldHash);
 panel.dispose();console.log('Reload and retry preserve original source pin even after source changes');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>dom.window.close());
