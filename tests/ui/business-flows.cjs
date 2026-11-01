const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(r=>setImmediate(r));
async function until(test){for(let i=0;i<100;i++){await tick();if(test())return}throw Error('business flow page did not settle')}
const reply=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(value)});
(async()=>{
 const dom=new JSDOM(fs.readFileSync(path.join(root,'business-flows.html'),'utf8'),{url:'https://atlas.test/business-flows.html?project=p_one',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','fixture');const digest='a'.repeat(64),other='b'.repeat(64),commit='c'.repeat(40);
 let delayResolve,attempt=0,modeUnknown=false;const bodies=[];const delayed=new Promise(resolve=>delayResolve=resolve);
 w.atlasSecureUUID=()=> 'safe-request-1';w.confirm=()=>true;
 w.fetch=async (url,opts={})=>{const route=new URL(url,w.location.href).pathname;calls.push({route,method:opts.method||'GET'});
  if(route.endsWith('/business-flows'))return reply({items:[{id:'first',name:'第一个'},{id:'second',name:'第二个'}]});
  if(route.endsWith('/business-flows/first')){await delayed;return reply({candidate_digest:digest,candidate_commit:commit,candidate_run:'r_old',candidate_document:{name:'旧页面',nodes:[{id:'a',kind:'source',title:'旧内容'}]}})}
  if(route.endsWith('/business-flows/second'))return reply({candidate_digest:digest,candidate_commit:commit,candidate_run:'r_new',latest_run:modeUnknown?{id:'r_unknown',plan_digest:digest,commit,status:'unknown'}:null,candidate_document:{name:'收入分析',inputs:[],nodes:[{id:'a',kind:'source',title:'读取资料'}]}});
  if(route.endsWith('/business-flows/second/runs/r_new')||route.endsWith('/business-flows/second/runs/r_run'))return reply({plan_digest:digest,commit,status:'succeeded',steps:[{node_id:'a',status:'succeeded'}],artifacts:[{name:'恶意',download_url:'https://evil.example/api/projects/p_one/artifacts/a/download'},{name:'合法',download_url:'/api/projects/p_one/artifacts/a/download'}]});
  if(route.endsWith('/business-flows/second/runs/r_unknown'))return reply({plan_digest:digest,commit,status:'failed',steps:[{node_id:'a',status:'unknown'}]});
  if(route.endsWith('/business-flows/second/runs')&&opts.method==='POST'){bodies.push(JSON.parse(opts.body));if(++attempt===1)throw Error('network lost');return reply({run_id:'r_run'})}
  if(route.endsWith('/business-flows/second/publish')&&opts.method==='POST'){bodies.push(JSON.parse(opts.body));return reply({status:'published'})}
  throw Error('unexpected '+route);
 };
 w.eval(fs.readFileSync(path.join(root,'business-flow-view.js'),'utf8'));
 w.eval(fs.readFileSync(path.join(root,'business-flows.js'),'utf8'));
 try{
  await until(()=>d.querySelectorAll('#flow-list button').length===2);
  d.querySelectorAll('#flow-list button')[1].click();await until(()=>d.querySelector('#flow-definition').textContent.includes('收入分析'));
  await until(()=>d.querySelector('#flow-definition').textContent.includes('执行完成'));
  delayResolve();await tick();await tick();
  assert(!d.querySelector('#flow-definition').textContent.includes('旧内容'),'late result for earlier selection discarded');
  assert.equal(d.querySelectorAll('#flow-output-list button').length,1,'only project-scoped same-origin result');
  assert(d.querySelector('#flow-output-list').textContent.includes('合法'));
  assert(!calls.some(x=>x.method!=='GET'),'view never starts tasks or publishes');
  assert(!calls.some(x=>x.route.endsWith('/runs/r_old')),'stale result never fetched');
  d.querySelector('#flow-run').click();await until(()=>d.querySelector('#flow-notice').textContent.includes('没有确认'));
  d.querySelector('#flow-run').click();await until(()=>attempt===2);await until(()=>!d.querySelector('#flow-publish').disabled);
  assert.equal(bodies[0].request_id,bodies[1].request_id,'unknown network result reuses the same request');
  assert.deepEqual(bodies[1].inputs,{});assert.equal(bodies[1].expected_digest,digest);assert.equal(bodies[1].use_published,false);
  d.querySelector('#flow-publish').click();await until(()=>bodies.length===3);
  assert.equal(bodies[2].run_id,'r_run');assert.equal(bodies[2].expected_digest,digest);
  await until(()=>!d.querySelector('#flow-refresh').disabled);modeUnknown=true;d.querySelector('#flow-refresh').click();await until(()=>d.querySelector('#flow-definition').textContent.includes('结果待核对'));
  assert(d.querySelector('#flow-run').disabled,'an unknown step must never be rerun blindly');assert(d.querySelector('#flow-publish').disabled);
  console.log(JSON.stringify({project_scoped:true,actual_receipt_bound:true,stale_selection_ignored:true,untrusted_urls_rejected:true,no_automatic_mutation:true,explicit_run_and_publish:true,uncertain_retry_idempotent:true,unknown_step_blocks_retry:true,real_browser_layout_verified:false}));
 }finally{dom.window.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
