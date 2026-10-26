// Actual rendered resume button -> submission, and safe rejection recovery.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const until=async predicate=>{const end=Date.now()+4000;while(!predicate()){if(Date.now()>end)throw Error('resume UI did not settle');await new Promise(resolve=>setTimeout(resolve,5))}};
const response=(value,status=200)=>({ok:status>=200&&status<300,status,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
const turnId='t_25300e8519874c4ebf0dbad07be20b1365030869';
async function boot({failureStatus=0,statusResult=false,statusFailure=false,message='资源不存在'}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,posts=[],statusQueries=[];
 let fail=failureStatus;
 w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;w.sessionStorage.setItem('atlas_token','fixture-owner');
 w.fetch=async(url,options={})=>{
  const u=new URL(url,w.location.href),route=u.pathname;
  if(route==='/api/models')return response({models:[{id:'careful',name:'业务模型'}],default_model_id:'careful'});
  if(route==='/api/projects')return response([{id:'p',name:'月结亲测'}]);
  if(route.endsWith('/conversation'))return response({project:{id:'p',name:'月结亲测'},session_id:'c',sessions:[{id:'c',name:'原对话'}],
   sources:[{id:'s_1',name:'业务源表.xlsx'},{id:'s_2',name:'口径.txt'}],applications:[],mode:'llm',
   turns:[{id:turnId,text:'分析原始完整资料',answer:'AI运行服务暂不可用',state:'failed',outcome:'paused',resume_available:true,model_id:'careful',source_ids:['s_1','s_2']}]});
  if(route.endsWith('/message-status')){statusQueries.push(u.searchParams.get('request_id'));return response({accepted:statusResult},statusFailure?503:200)}
  if(route.endsWith('/messages')){posts.push(JSON.parse(options.body));return fail?response({detail:message},fail):response({id:'t_accepted'})}
  throw Error('unexpected route '+route);
 };
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.resumeTest={state:()=>({pending,sending,selectedModelId,attached})};');
 await until(()=>d.querySelector('[data-continue]'));
 w.atlasBusiness.attachSources([{id:'s_1',name:'业务源表.xlsx'},{id:'s_2',name:'口径.txt'}]);
 return {dom,w,d,posts,statusQueries,accept:()=>{fail=0},async click(){d.querySelector('[data-continue]').click();await until(()=>posts.length>0&&!w.resumeTest.state().sending)}};
}
(async()=>{
 const normal=await boot();try{
  const button=normal.d.querySelector('[data-continue]');
  assert.equal(button.dataset.continue,turnId,'button must contain the actual opaque ID, not a literal interpolation');
  assert(!button.outerHTML.includes('${'));
  await normal.click();assert.equal(normal.posts.length,1);
  assert.equal(normal.posts[0].resume_turn_id,turnId);assert.equal(normal.posts[0].text,'继续');
  assert.equal(normal.posts[0].model_id,'careful');assert.deepEqual(normal.posts[0].source_ids,['s_1','s_2']);
 }finally{normal.dom.window.close()}

 for(const test of [{failureStatus:404},{failureStatus:400,message:'待继续的任务不存在，请刷新对话后再点原任务的“继续”。输入和附件已保留。'}]){
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
 for(const test of [{failureStatus:404,statusResult:true},{failureStatus:404,statusResult:null},{failureStatus:404,statusFailure:true},{failureStatus:503},{failureStatus:400,message:'其他参数不正确'}]){
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
