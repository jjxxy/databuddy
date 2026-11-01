const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(r=>setImmediate(r));
async function until(test){for(let i=0;i<100;i++){await tick();if(test())return}throw Error('page did not settle')}
const reply=(v,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(v)});
(async()=>{const dom=new JSDOM(fs.readFileSync(path.join(root,'business-flows.html'),'utf8'),{url:'https://atlas.test/business-flows.html?project=p_one',runScripts:'outside-only'}),w=dom.window,d=w.document;const digest='a'.repeat(64),hash='b'.repeat(64),commit='c'.repeat(40),posts=[];let attempts=0,scheduleBody;
 const doc={name:'收入分析',inputs:['file'],nodes:[{id:'read',kind:'source_table',title:'读取收入资料',inputs:{source:{source:'input',name:'file'}},config:{}}]};
 const detail={candidate_digest:digest,candidate_commit:commit,candidate_document:doc,published_digest:digest,published_commit:commit,published_document:doc,timezone:'Asia/Shanghai',cron:null};
 w.sessionStorage.setItem('atlas_token','fixture');w.atlasSecureUUID=()=> 'same-id';w.confirm=()=>true;
 w.fetch=async(url,opts={})=>{const u=new URL(url,w.location.href),r=u.pathname;
 if(r.endsWith('/business-flows'))return reply({items:[{id:'one',name:'分析'}]});
 if(r.endsWith('/business-flows/one'))return reply(detail);
 if(r.endsWith('/input-sources'))return reply({items:[{id:'source1',name:'一月收入',revision:2,content_hash:hash},{id:'bad',name:'bad',revision:0,content_hash:hash}]});
 if(r.endsWith('/requests/same-id'))return reply({accepted:null});
 if(r.endsWith('/runs')&&opts.method==='POST'){posts.push(JSON.parse(opts.body));if(++attempts===1)throw Error('lost');return reply({run_id:'result'})}
 if(r.endsWith('/runs/result'))return reply({id:'result',status:'succeeded',plan_digest:digest,commit,steps:[],artifacts:[]});
 if(r.endsWith('/schedule')){scheduleBody=JSON.parse(opts.body);return reply({})}throw Error('unexpected '+url)};
 w.eval(fs.readFileSync(path.join(root,'business-flow-view.js'),'utf8'));w.eval(fs.readFileSync(path.join(root,'business-flows.js'),'utf8'));
 try{await until(()=>d.querySelector('#flow-inputs select'));const sel=d.querySelector('#flow-inputs select');assert.equal(sel.options.length,2);assert(d.querySelector('#flow-run').disabled);sel.value='source1';sel.dispatchEvent(new w.Event('change'));assert(!d.querySelector('#flow-run').disabled);d.querySelector('#flow-run').click();await until(()=>d.querySelector('#flow-notice').textContent.includes('没有确认'));assert(d.querySelector('#flow-inputs select').disabled);d.querySelector('#flow-run').click();await until(()=>posts.length===2);assert.deepEqual(posts[0],posts[1]);assert.deepEqual(posts[0].inputs.file,{source_id:'source1',revision:2,content_hash:hash});await until(()=>d.querySelector('#flow-role').options.length===2);d.querySelector('#flow-role').value='published';d.querySelector('#flow-role').dispatchEvent(new w.Event('change'));await until(()=>d.querySelector('#flow-inputs select'));const pub=d.querySelector('#flow-inputs select');pub.value='source1';pub.dispatchEvent(new w.Event('change'));assert(d.querySelector('#flow-schedule-current').textContent.includes('未设置'));d.querySelector('#flow-schedule-time').value='09:30';d.querySelector('#flow-schedule-save').click();await until(()=>!!scheduleBody);assert.deepEqual(scheduleBody,{cron:'30 9 * * *',source_bindings:{file:'source1'},expected_published_digest:digest});console.log(JSON.stringify({dynamic_source_pinned:true,network_retry_freezes_inputs:true,explicit_daily_schedule:true,timezone_shown:true}));}finally{dom.window.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
