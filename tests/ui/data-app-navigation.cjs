// Real data-app page routes. Does not claim browser layout verification.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<60;i++){await tick();if(check())return}throw Error('application navigation did not settle')}
const reply=(value,status=200)=>({ok:status<400,status,json:async()=>value});
async function boot({status='building',error='',ready=false,missing=false,factBlocked=false,history=false,historicalRisk=false}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'data-app.html'),'utf8'),{url:'https://atlas.test/data-app.html'+(missing?'':'?project=p_1&app=app_1&session=c_1'),runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','fixture-owner');w.setInterval=()=>0;
 w.fetch=async(url,options={})=>{const u=new URL(url,w.location.href);calls.push({path:u.pathname,method:options.method||'GET'});
  if(u.pathname.endsWith('application.json'))return reply({},404);
  if(u.pathname.endsWith('/app_1'))return error==='config'?reply({detail:'应用暂时无法读取'},404):reply({name:'测试应用',status,can_publish:true,preview_type:history?'history':'candidate',...(ready?{run_id:'r_1',config:{page_size:25,default_sort:'c_1',descending:false}}:{})});
  if(u.pathname.endsWith('/data'))return error==='data'?reply({detail:'结果暂时无法读取'},500):reply({application:{title:'测试应用',layout:'chart',chart:'bar'},total:1,metric:{total:'123.45',status:'confirmed'},data_as_of:null,preview:factBlocked||history,bulk_blocked_reason:factBlocked?'需要业务语义对齐':null,semantic_warning:historicalRisk?'历史事实业务语义未核对':null,fields:[{name:'c_1',label:'金额'}],rows:[{c_1:'123.45'}],summary:[{category:'例子',amount_total:'123.45'}],run_id:'r_1',commit:'frozen'});
  throw Error('unexpected application route '+u.pathname);
 };
 w.eval(fs.readFileSync(path.join(root,'data-app.js'),'utf8')+';window.testDataApp={run};');
 await until(()=>!d.querySelector('#notice').hidden||d.querySelector('#record-count').textContent==='1');
 return {dom,w,d,calls};
}
(async()=>{
 for(const options of [{status:'building'},{status:'failed'},{error:'config'},{ready:true,error:'data'}]){
  const app=await boot(options);try{
   for(const [id,intent] of [['back',''],['revise','revise']]){const u=new URL(app.d.querySelector('#'+id).href);assert.equal(u.searchParams.get('project'),'p_1');assert.equal(u.searchParams.get('application'),'app_1');assert.equal(u.searchParams.get('session'),'c_1');assert.equal(u.searchParams.get('intent'),intent||null)}
   const ask=app.d.querySelector('#ask');assert.equal(ask.getAttribute('href'),null);assert.equal(ask.getAttribute('aria-disabled'),'true');assert(ask.textContent.includes('结果生成后可用'));assert(!app.calls.some(call=>call.method!=='GET'));
  }finally{app.dom.window.close()}
 }
 const ready=await boot({ready:true});try{const ask=ready.d.querySelector('#ask'),u=new URL(ask.href);assert.equal(ask.getAttribute('aria-disabled'),'false');assert.equal(u.searchParams.get('intent'),'ask');assert.equal(u.searchParams.get('session'),'c_1');assert.equal(ready.d.querySelector('.eyebrow').textContent,'你的数据应用')}finally{ready.dom.window.close()}
 const blocked=await boot({ready:true,factBlocked:true});try{
  assert.equal(blocked.d.querySelector('#semantic-warning').hidden,false);assert(blocked.d.querySelector('#semantic-warning').textContent.includes('可加性'));
  assert(blocked.d.querySelector('#amount-metric').hidden&&blocked.d.querySelector('#chart-panel').hidden,'even a stale metric must not look verified');
  assert.equal(blocked.d.querySelector('#amount-total').textContent,'—');assert.equal(blocked.d.querySelector('#chart').children.length,0,'blocked data must not populate hidden metric or chart elements');
  assert(blocked.d.querySelector('#export').hidden&&blocked.d.querySelector('#export').disabled);assert(blocked.d.querySelector('#publish').disabled);
  assert(blocked.d.querySelector('#table-body').textContent.includes('123.45'),'paginated rows remain available for read-only inspection');
  assert.equal(blocked.d.querySelector('#ask').getAttribute('aria-disabled'),'true');assert(blocked.d.querySelector('#ask').textContent.includes('待业务语义对齐'));blocked.d.querySelector('#export').click();await blocked.w.testDataApp.run('确认发布');
  assert(!blocked.calls.some(c=>c.path.endsWith('/export')||c.method==='POST'),'blocked candidate must not issue export or approval');
 }finally{blocked.dom.window.close()}
 const historical=await boot({ready:true,history:true});try{
  assert.equal(historical.d.querySelector('#semantic-warning').hidden,true);assert.equal(historical.d.querySelector('#export').hidden,false);assert.equal(historical.d.querySelector('#export').disabled,false);
  assert.equal(historical.d.querySelector('#amount-metric').hidden,false);assert.equal(historical.d.querySelector('#chart-panel').hidden,false);
 }finally{historical.dom.window.close()}
 const riskyHistory=await boot({ready:true,history:true,historicalRisk:true});try{
  assert.equal(riskyHistory.d.querySelector('#semantic-warning').hidden,false);
  assert(riskyHistory.d.querySelector('#semantic-warning').textContent.includes('未核对的汇总与图表已隐藏'));
  assert(riskyHistory.d.querySelector('#amount-note').textContent.includes('待核对'),'historical risk must override stale confirmed labels');
  assert.equal(riskyHistory.d.querySelector('#export').hidden,false);assert.equal(riskyHistory.d.querySelector('#export').disabled,false);
  assert.equal(riskyHistory.d.querySelector('#amount-metric').hidden,true);assert.equal(riskyHistory.d.querySelector('#chart-panel').hidden,true);
  assert.equal(riskyHistory.d.querySelector('#amount-total').textContent,'—');assert.equal(riskyHistory.d.querySelector('#chart').children.length,0);
  assert(riskyHistory.d.querySelector('#table-body').textContent.includes('123.45'),'historical rows remain viewable');
  assert(riskyHistory.d.querySelector('#publish').disabled);assert.equal(riskyHistory.d.querySelector('#ask').getAttribute('aria-disabled'),'true');
  await riskyHistory.w.testDataApp.run('确认发布');assert(!riskyHistory.calls.some(call=>call.method==='POST'));
 }finally{riskyHistory.dom.window.close()}
 const missing=await boot({missing:true});try{assert.equal(missing.d.querySelector('#back').getAttribute('href'),'/');for(const id of ['revise','ask']){assert.equal(missing.d.querySelector('#'+id).getAttribute('href'),null);assert.equal(missing.d.querySelector('#'+id).getAttribute('aria-disabled'),'true')}assert(missing.d.querySelector('#notice').textContent.includes('缺少应用标识'));assert(!missing.calls.some(call=>call.path.startsWith('/api/')))}finally{missing.dom.window.close()}
 console.log(JSON.stringify({unready_and_failed_apps_retain_project_session_links:true,initial_config_and_data_errors_navigable:true,ask_requires_result:true,ready_intents_preserved:true,missing_context_honest:true,no_automatic_submission:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1});
