const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(r=>setImmediate(r)),sha='a'.repeat(64),commit='b'.repeat(40);
async function until(fn){for(let i=0;i<200;i++){await tick();if(fn())return}throw Error('page did not settle')}
function reply(v,status=200){return {ok:status>=200&&status<300,status,json:async()=>structuredClone(v)}}
async function boot({count=1,mode='ok'}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'business-flows.html'),'utf8'),{url:'https://atlas.test/business-flows.html?project=p_one',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','x');const detail={published_digest:sha,published_commit:commit,published_run:'r_one',published_document:{name:'业务流程',inputs:[],nodes:[]},timezone:'Asia/Shanghai',cron:'0 9 * * *',schedule_uncertain_run:'job_1'};
 w.fetch=async(url,options={})=>{const u=new URL(url,w.location.href),p=u.pathname;calls.push({path:p,method:options.method||'GET',body:options.body});
  if(p==='/api/projects/p_one/business-flows')return reply({items:[{id:'bf_one',name:'业务流程'}]});
  if(p.endsWith('/business-flows/bf_one'))return reply(detail);
  if(p.endsWith('/runs/r_one'))return reply({id:'r_one',plan_digest:sha,commit,status:'succeeded',steps:[],artifacts:[]});
  if(p.endsWith('/scheduled-runs/job_1/resolve')){if(mode==='network')throw Error('network');if(mode==='conflict')return reply({detail:'核对运行已变化'},409);detail.schedule_uncertain_run=count>1?'job_2':null;detail.schedule_resume_required=true;return reply({run_id:'job_1',resolved:true,resume_required:true,remaining_count:count-1})}
  throw Error('unexpected '+p);
 };
 w.eval(fs.readFileSync(path.join(root,'business-flow-view.js'),'utf8'));w.eval(fs.readFileSync(path.join(root,'business-flows.js'),'utf8'));await until(()=>!d.querySelector('#flow-schedule-review').hidden);return {dom,w,d,calls,detail};
}
(async()=>{
 let env=await boot(),d=env.d,p=d.querySelector('#flow-schedule-review');try{
  assert(d.querySelector('#flow-run').disabled);assert(p.textContent.includes('无法确认或只执行了一部分'));assert(p.textContent.includes('不能代替核对'));d.querySelector('#flow-schedule-review-submit').click();await tick();assert.equal(env.calls.filter(c=>c.path.includes('/resolve')).length,0);
  d.querySelector('#flow-schedule-review-outcome').value='not_executed';d.querySelector('#flow-schedule-review-note').value='已经查目标系统回执';d.querySelector('#flow-schedule-review-submit').click();d.querySelector('#flow-schedule-review-submit').click();await until(()=>p.hidden);
  const calls=env.calls.filter(c=>c.path.includes('/resolve'));assert.equal(calls.length,1);assert.deepEqual(JSON.parse(calls[0].body),{outcome:'not_executed',note:'已经查目标系统回执'});assert(d.querySelector('#flow-notice').textContent.includes('请重新设置定时'));assert(d.querySelector('#flow-schedule-current').textContent.includes('请重新设置定时'));assert(!env.calls.some(c=>c.method==='POST'&&c.path.endsWith('/runs')));
 }finally{env.dom.window.close()}
 env=await boot({count:2});d=env.d;try{d.querySelector('#flow-schedule-review-outcome').value='executed';d.querySelector('#flow-schedule-review-note').value='已查业务系统回执';d.querySelector('#flow-schedule-review-submit').click();await until(()=>d.querySelector('#flow-schedule-review-run').textContent.includes('job_2'));assert(d.querySelector('#flow-notice').textContent.includes('仍有其他运行待核对'));assert(!d.querySelector('#flow-schedule-review').hidden)}finally{env.dom.window.close()}
 env=await boot({mode:'network'});d=env.d;try{d.querySelector('#flow-schedule-review-outcome').value='executed';d.querySelector('#flow-schedule-review-note').value='已查业务系统回执';d.querySelector('#flow-schedule-review-submit').click();await until(()=>d.querySelector('#flow-schedule-review-message').textContent.includes('先刷新状态'));assert(d.querySelector('#flow-schedule-review-submit').disabled);assert.equal(env.calls.filter(c=>c.path.includes('/resolve')).length,1)}finally{env.dom.window.close()}
 env=await boot({mode:'conflict'});d=env.d;try{d.querySelector('#flow-schedule-review-outcome').value='executed';d.querySelector('#flow-schedule-review-note').value='已查业务系统回执';d.querySelector('#flow-schedule-review-submit').click();await until(()=>d.querySelector('#flow-schedule-review-message').textContent.includes('已变化'));assert(!d.querySelector('#flow-schedule-review').hidden)}finally{env.dom.window.close()}
 console.log(JSON.stringify({explicit_external_evidence:true,never_blind_retry:true,per_run:true,remaining_runs:true,reset_after_review:true,ambiguous_submit_requires_refresh:true}));
})().catch(e=>{console.error(e);process.exitCode=1});
