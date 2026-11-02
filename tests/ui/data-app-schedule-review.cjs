const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(r=>setImmediate(r));
async function until(fn){for(let i=0;i<200;i++){await tick();if(fn())return}throw Error('page did not settle')}
const reply=(v,status=200)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(v)});
async function boot({count=1,ready=true,mode='ok'}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'data-app.html'),'utf8'),{url:'https://nexus.test/data-app.html?project=p_1&app=app_1',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('nexus_token','fixture');w.setInterval=()=>0;
 const config={name:'核对应用',status:'ready',schedule:'0 9 * * *',timezone:'Asia/Shanghai',schedule_uncertain_run:'job_1',...(ready?{run_id:'r_1',config:{page_size:25,default_sort:'amount',descending:false}}:{})};
 w.fetch=async(url,options={})=>{const u=new URL(url,w.location.href),p=u.pathname;calls.push({path:p,method:options.method||'GET',body:options.body});
  if(p.endsWith('/business-plan'))return reply({detail:'无方案'},404);
  if(p.endsWith('/applications/app_1'))return reply(config);
  if(p.endsWith('/data'))return reply({application:{title:'核对应用',layout:'table'},total:1,metric:{total:null},fields:[{name:'amount',label:'金额'}],rows:[{amount:'12.34'}],summary:[],run_id:'r_1',commit:'frozen'});
  if(p.endsWith('/scheduled-runs/job_1/resolve')){if(mode==='network')throw Error('网络不可用');if(mode==='conflict')return reply({detail:'核对运行已变化'},409);config.schedule_uncertain_run=count>1?'job_2':null;config.schedule_resume_required=true;return reply({run_id:'job_1',resolved:true,resume_required:true,remaining_count:count-1})}
  throw Error('unexpected '+p);
 };
 w.eval(fs.readFileSync(path.join(root,'data-app.js'),'utf8'));await until(()=>!d.querySelector('#schedule-review').hidden);return {dom,w,d,calls,config};
}
(async()=>{
 let env=await boot(),d=env.d,p=d.querySelector('#schedule-review');try{
  await until(()=>d.querySelector('#record-count').textContent==='1');assert.equal(d.querySelector('#schedule').textContent,'自动更新待核对');assert(d.querySelector('#rerun').disabled);assert(p.textContent.includes('无法确认或只执行了一部分'));assert(p.textContent.includes('不能代替核对'));
  d.querySelector('#schedule-review-submit').click();await tick();assert.equal(env.calls.filter(c=>c.path.includes('/resolve')).length,0);
  d.querySelector('#schedule-review-outcome').value='executed';d.querySelector('#schedule-review-note').value='已查目标系统完整回执';d.querySelector('#schedule-review-submit').click();d.querySelector('#schedule-review-submit').click();await until(()=>p.hidden);
  const calls=env.calls.filter(c=>c.path.includes('/resolve'));assert.equal(calls.length,1);assert.deepEqual(JSON.parse(calls[0].body),{outcome:'executed',note:'已查目标系统完整回执'});await until(()=>d.querySelector('#schedule-note').textContent.includes('重新设置定时'));assert(!env.calls.some(c=>c.method==='POST'&&c.path.endsWith('/messages')));
 }finally{env.dom.window.close()}
 env=await boot({ready:false});try{assert(!env.d.querySelector('#schedule-review').hidden);assert(env.d.querySelector('#schedule-review-run').textContent.includes('job_1'))}finally{env.dom.window.close()}
 env=await boot({count:2});d=env.d;try{d.querySelector('#schedule-review-outcome').value='not_executed';d.querySelector('#schedule-review-note').value='已查目标系统完整回执';d.querySelector('#schedule-review-submit').click();await until(()=>d.querySelector('#schedule-review-run').textContent.includes('job_2'));assert(d.querySelector('#notice').textContent.includes('仍有其他运行待核对'))}finally{env.dom.window.close()}
 env=await boot({mode:'network'});d=env.d;try{d.querySelector('#schedule-review-outcome').value='executed';d.querySelector('#schedule-review-note').value='已查目标系统完整回执';d.querySelector('#schedule-review-submit').click();await until(()=>d.querySelector('#schedule-review-message').textContent.includes('不要重复提交'));assert(d.querySelector('#schedule-review-submit').disabled);assert.equal(env.calls.filter(c=>c.path.includes('/resolve')).length,1);d.querySelector('#schedule-review-refresh').click();await until(()=>!d.querySelector('#schedule-review-submit').disabled);assert.equal(env.calls.filter(c=>c.path.includes('/resolve')).length,1)}finally{env.dom.window.close()}
 env=await boot({mode:'conflict'});d=env.d;try{d.querySelector('#schedule-review-outcome').value='not_executed';d.querySelector('#schedule-review-note').value='已查目标系统完整回执';d.querySelector('#schedule-review-submit').click();await until(()=>d.querySelector('#schedule-review-message').textContent.includes('已变化'));assert(d.querySelector('#schedule-review-submit').disabled)}finally{env.dom.window.close()}
 console.log(JSON.stringify({pipeline_review:true,external_evidence_required:true,remaining_runs:true,reset_after_review:true,ambiguous_submit_requires_refresh:true,no_replay:true}));
})().catch(e=>{console.error(e);process.exitCode=1});
