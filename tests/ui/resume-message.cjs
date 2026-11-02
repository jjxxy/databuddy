// Actual rendered resume button -> submission, and safe rejection recovery.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const until=async predicate=>{const end=Date.now()+4000;while(!predicate()){if(Date.now()>end)throw Error('resume UI did not settle');await new Promise(resolve=>setTimeout(resolve,5))}};
const response=(value,status=200)=>({ok:status>=200&&status<300,status,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
const turnId='t_25300e8519874c4ebf0dbad07be20b1365030869';
async function boot({failureStatus=0,statusResult=false,statusFailure=false,message='资源不存在',includeResumed=false,originalMode='work',historicalPlanError=false}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,posts=[],statusQueries=[];
 let fail=failureStatus;
 w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;w.sessionStorage.setItem('nexus_token','fixture-owner');
 w.fetch=async(url,options={})=>{
  const u=new URL(url,w.location.href),route=u.pathname;
  if(route==='/api/models')return response({models:[{id:'careful',name:'业务模型'}],default_model_id:'careful'});
  if(route==='/api/projects')return response([{id:'p',name:'月结亲测'}]);
  if(route.endsWith('/conversation'))return response({project:{id:'p',name:'月结亲测'},session_id:'c',sessions:[{id:'c',name:'原对话'}],
   sources:[{id:'s_1',name:'业务源表.xlsx'},{id:'s_2',name:'口径.txt'}],applications:[],mode:'llm',
   turns:[{id:turnId,text:'分析原始完整资料',answer:historicalPlanError?'此前模型没有正常调用工作计划工具，这项任务尚未完成。可以继续原任务，已有工作记录会保留。':'AI运行服务暂不可用',state:'failed',outcome:'paused',resume_available:true,model_id:'careful',interaction_mode:originalMode,source_ids:['s_1','s_2'],created_at:'2026-01-01T00:00:00Z'},
    ...(includeResumed?[{id:'t_resumed',text:'继续',state:'queued',resume_turn_id:turnId,source_ids:['s_1','s_2'],created_at:'2026-01-01T00:01:00Z'}]:[])]});
  if(route.endsWith('/message-status')){statusQueries.push(u.searchParams.get('request_id'));return response({accepted:statusResult},statusFailure?503:200)}
  if(route.endsWith('/messages')){posts.push(JSON.parse(options.body));return fail?response({detail:message},fail):response({id:'t_accepted'})}
  throw Error('unexpected route '+route);
 };
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.resumeTest={state:()=>({pending,sending,selectedModelId,attached}),attach};');
 await until(()=>d.querySelector('[data-continue]'));
 w.nexusBusiness.attachSources([{id:'s_1',name:'业务源表.xlsx'},{id:'s_2',name:'口径.txt'}]);
 return {dom,w,d,posts,statusQueries,accept:()=>{fail=0},async click(){d.querySelector('[data-continue]').click();await until(()=>posts.length>0&&!w.resumeTest.state().sending)}};
}
(async()=>{
 const legacy=await boot({historicalPlanError:true});try{assert(legacy.d.querySelector(`[data-turn="${turnId}"]`).textContent.includes('没有正常调用工作计划工具'));await legacy.click();assert.equal(legacy.posts[0].resume_turn_id,turnId);assert.deepEqual(legacy.posts[0].source_ids,[])}finally{legacy.dom.window.close()}
 const inherited=await boot({includeResumed:true});try{
  const original=inherited.d.querySelector(`[data-turn="${turnId}"] .user-row`);
  const resumed=inherited.d.querySelector('[data-turn="t_resumed"] .user-row');
  assert(original.textContent.includes('业务源表.xlsx'));
  assert(!resumed.textContent.includes('业务源表.xlsx'),'continued task inherits sources internally without rendering them as a fresh upload');
 }finally{inherited.dom.window.close()}
 const chat=await boot({originalMode:'chat'});try{
  await chat.click();assert.equal(chat.posts[0].interaction_mode,'chat','continue uses the original task mode even when the composer is in another mode');
 }finally{chat.dom.window.close()}
 const normal=await boot();try{
  const button=normal.d.querySelector('[data-continue]');
  assert.equal(button.dataset.continue,turnId,'button must contain the actual opaque ID, not a literal interpolation');
  assert(!button.outerHTML.includes('${'));
  normal.d.querySelector('#message').value='留待发送的草稿';
  normal.w.resumeTest.attach([new normal.w.File(['example'],'草稿资料.txt',{type:'text/plain'})]);
  await normal.click();assert.equal(normal.posts.length,1);
  assert.equal(normal.posts[0].resume_turn_id,turnId);assert.equal(normal.posts[0].text,'继续');
  assert.equal(normal.posts[0].model_id,null,'the server continues with the original task model');
  assert.deepEqual(normal.posts[0].source_ids,[],'continuing must not upload or attach the composer draft');
  assert.deepEqual(Array.from(normal.w.resumeTest.state().attached,a=>a.source_id),['s_1','s_2',null],'draft attachments remain available after continuing');
  assert.equal(normal.d.querySelector('#message').value,'留待发送的草稿');
 }finally{normal.dom.window.close()}

 for(const test of [{failureStatus:404},{failureStatus:400,message:'待继续的任务不存在，请刷新对话后再点原任务的“继续”。输入和附件已保留。'},
   {failureStatus:400,message:'要继续的任务已不存在，请发起新的消息'},
   {failureStatus:400,message:'该任务当前不需要继续，请查看最新进度'},
   {failureStatus:422,message:'参数不正确'}]){
  const rejected=await boot(test);try{
   rejected.d.querySelector('#message').value='尚未发送的原草稿';await rejected.click();
   assert.equal(rejected.posts.length,1,'definitive rejection never auto-submits another message');
   assert.deepEqual(rejected.statusQueries,[rejected.posts[0].request_id]);
   assert.equal(rejected.w.resumeTest.state().pending,null);
   assert.equal(rejected.d.querySelector('#message').readOnly,false);
   assert.equal(rejected.d.querySelector('#message').value,'尚未发送的原草稿');
   assert.deepEqual(Array.from(rejected.w.resumeTest.state().attached,a=>a.source_id),['s_1','s_2']);
   assert.equal(rejected.w.resumeTest.state().selectedModelId,'careful');
   assert(rejected.d.querySelector('#connection-error').textContent.includes('尚未接收'));
   rejected.accept();await rejected.click();assert.equal(rejected.posts.length,2);
   assert.notEqual(rejected.posts[1].request_id,rejected.posts[0].request_id,'new UUID only after authoritative unaccepted status');
   assert.equal(rejected.posts[1].resume_turn_id,turnId);assert.deepEqual(rejected.posts[1].source_ids,rejected.posts[0].source_ids);
  }finally{rejected.dom.window.close()}
 }
 const emptyDraft=await boot({failureStatus:404});try{
  await emptyDraft.click();assert.equal(emptyDraft.d.querySelector('#message').value,'继续','empty editor restores the rejected continuation text');
  assert.equal(emptyDraft.posts.length,1);assert.equal(emptyDraft.w.resumeTest.state().pending,null);
 }finally{emptyDraft.dom.window.close()}
 for(const test of [{failureStatus:404,statusResult:true},{failureStatus:404,statusResult:null},{failureStatus:404,statusFailure:true},{failureStatus:503}]){
  const uncertain=await boot(test);try{
   await uncertain.click();const original=JSON.parse(JSON.stringify(uncertain.w.resumeTest.state().pending));
   assert(original,'uncertain or accepted submission keeps its frozen receipt');
   assert.equal(uncertain.d.querySelector('#message').readOnly,true);assert.equal(uncertain.d.querySelector('#model-select').disabled,true);
   uncertain.accept();await uncertain.click();assert.equal(uncertain.posts.length,2);
   assert.deepEqual(uncertain.posts[1],original,'explicit retry must reuse complete original payload and UUID');
  }finally{uncertain.dom.window.close()}
 }
 console.log(JSON.stringify({actual_paused_button_id:true,real_dom_click_posts_exact_resume_id:true,definitive_unaccepted_resume_unlocks_without_resending:true,original_draft_sources_and_model_preserved:true,unknown_or_accepted_submission_keeps_uuid:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
