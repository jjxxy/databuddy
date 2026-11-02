// Exercises the actual application page against fixed run-bound responses.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<150;i++){await tick();if(check())return}throw Error('business plan did not settle')}
const reply=(value,status=200)=>({ok:status<400,status,json:async()=>value});
async function boot({selectedRole='candidate',wrongCommit=false,unavailable=false,delay=false,omitRequest=false}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'data-app.html'),'utf8'),{url:'https://nexus.test/data-app.html?project=p1&app=a1&preview=1&run=r1',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('nexus_token','fixture');w.setInterval=()=>0;
 const evidence={run_id:'r1',commit:'c1',inputs:[{name:'收入明细',revision:3,records:100,input_hash:'hash-input'}],output_hash:'hash-output',quality:{available:true,passed:2,failed:1,other:0},replay:{available:false,reason:'尚未验证复现'}};
 const plan={version:1,origin:'stored',user_request:'统计 <img src=x> 收入',summary:'核对收入 <img src=x>',steps:[{title:'读取数据',details:['检查总数']}],sources:[{id:'s1',name:'收入表',design_revision:2,role:'主资料'}],rules:{filter:'已付款'},warnings:['金额口径待确认']};
 if(omitRequest)plan.user_request=null;
 let release;
 w.fetch=async(url,options={})=>{const u=new URL(url,w.location.href);calls.push({path:u.pathname,query:u.searchParams});
  if(u.pathname.endsWith('/business-plan')){if(delay)await new Promise(resolve=>release=resolve);return unavailable?reply({detail:'missing'},404):reply({selected:{role:selectedRole,run_id:'r1',commit:wrongCommit?'wrong':'c1',plan},changes:[{label:'过滤条件',before:'全部',after:'已付款'}]})}
  if(u.pathname.endsWith('/data'))return reply({application:{title:'收入分析',layout:'table'},total:1,metric:{total:null},preview:true,fields:[{name:'amount',label:'金额'}],rows:[{amount:'1'}],summary:[],run_id:'r1',commit:'c1'});
  if(u.pathname.endsWith('/a1'))return reply({name:'收入分析',run_id:'r1',preview_type:'candidate',can_publish:true,config:{page_size:25,default_sort:'amount',descending:false},run_evidence:evidence});
  throw Error('unexpected route '+u.pathname);
 };
 w.eval(fs.readFileSync(path.join(root,'data-app.js'),'utf8')+';window.businessTest={refresh};');
 await until(()=>d.querySelector('#record-count').textContent==='1');
 return {dom,w,d,calls,release:()=>release?.()};
}
(async()=>{
 const a=await boot();try{await until(()=>a.d.querySelector('#business-plan-status').textContent==='候选方案');const text=a.d.querySelector('#business-plan').textContent;
  assert(text.includes('读取数据')&&text.includes('检查总数')&&text.includes('设计时版本 2'));assert(text.includes('本次变更需求')&&text.includes('统计 <img src=x> 收入'));
  assert(text.includes('与正式方案的变化')&&text.includes('已付款'));assert(!a.d.querySelector('#business-plan img'),'untrusted plan text must remain text');
  assert(!a.d.querySelector('#run-evidence').hidden&&a.d.querySelector('#run-evidence').textContent.includes('尚未验证复现'));
  assert(a.d.querySelector('#run-evidence').textContent.includes('收入明细 · 版本 3 · 100 条'));assert(a.d.querySelector('#run-evidence').textContent.includes('hash-input'));assert(a.d.querySelector('#run-evidence').textContent.includes('hash-output'));
  const call=a.calls.find(x=>x.path.endsWith('/business-plan'));assert.equal(call.query.get('run_id'),'r1');
 }finally{a.dom.window.close()}
 const stale=await boot({wrongCommit:true});try{await until(()=>stale.d.querySelector('#business-plan-status').textContent==='未获取');assert(stale.d.querySelector('#business-plan-message').textContent.includes('版本'));assert.equal(stale.d.querySelector('#business-plan-content').textContent,'')}finally{stale.dom.window.close()}
 const history=await boot({selectedRole:'history'});try{await until(()=>history.d.querySelector('#business-plan-status').textContent==='历史方案');assert(!history.d.querySelector('#business-plan').textContent.includes('与正式方案的变化'))}finally{history.dom.window.close()}
 const noRequest=await boot({omitRequest:true});try{await until(()=>noRequest.d.querySelector('#business-plan-status').textContent==='候选方案');assert(noRequest.d.querySelector('#business-plan').textContent.includes('未记录原始需求'))}finally{noRequest.dom.window.close()}
 const noPlan=await boot({unavailable:true});try{await until(()=>noPlan.d.querySelector('#business-plan-status').textContent==='未获取');assert.equal(noPlan.d.querySelector('#record-count').textContent,'1');assert(noPlan.d.querySelector('#business-plan-message').textContent.includes('不可用'))}finally{noPlan.dom.window.close()}
 console.log(JSON.stringify({exact_run_and_commit_bound:true,escaped_untrusted_text:true,history_does_not_show_candidate_diff:true,optional_plan_failure_preserves_result:true,actual_run_evidence:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1});
