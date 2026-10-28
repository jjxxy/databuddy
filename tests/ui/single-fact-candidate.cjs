'use strict';
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),hash='a'.repeat(64),sourceHash='b'.repeat(64),plan='c'.repeat(64);
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
async function until(fn){for(let i=0;i<150;i++){if(fn())return;await tick()}throw Error('candidate UI did not settle')}
const reply=(value,status=200)=>({ok:status<400,status,json:async()=>value});
async function boot(options={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'data-workbench.html'),'utf8'),{url:'https://atlas.test/data-workbench.html?project=p_1&tab=semantics',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','fixture-token');w.setTimeout=()=>1;w.clearTimeout=()=>{};let releaseCreate;
 const text={id:'fact_1',status:options.pending?'pending':'approved',content_sha256:hash,source_revision:1,source_file_sha256:sourceHash,output_source_id:'out_1',approval_blockers:options.blocked?[{error:'missing'}]:[],facts:[{quote:'收入 12 元',value_as_printed:'12',normalized_value:'12',measure:'收入',unit:'元',period:'2026-10',dimensions:{地区:'合肥'}}]};
 if(options.multiple)text.facts.push({...text.facts[0]});if(options.noHash)delete text.source_file_sha256;
 const visual={...text,id:'visual_1',page:1,source_id:'source_1',ocr_executed:false,pixels_independently_verified:false};
 const task={workflow_id:'wf_generated',name:'单条原值',draft_sha256:options.wrongPlan?'f'.repeat(64):plan,candidate_run:options.validated?'r_generated':null,steps:[],inputs:{},document:{plan:{stages:[]},bindings:{facts:{source_id:options.wrongSource?'out_other':'out_1'}}}};
 w.fetch=async(url,request={})=>{
  const u=new URL(url,w.location.href),method=request.method||'GET';calls.push({path:u.pathname,method,body:request.body?JSON.parse(request.body):null});
  if(u.pathname.endsWith('/data-workflows'))return reply({items:[]});
  if(u.pathname.endsWith('/data-workflows/wf_generated'))return reply(task);
  if(u.pathname.endsWith('/data-results/r_generated'))return reply({run_id:'r_generated',plan_sha256:plan,manifest_sha256:'d'.repeat(64),rows:[],schema:[],total:1,stages:[],context:{requires_source_review:true,semantic_review_reasons:['FACT_SEMANTIC_ALIGNMENT_REQUIRED']}});
  if(u.pathname.endsWith('/semantic-proposals')||u.pathname.endsWith('/semantic-catalogs'))return reply({items:[],next_offset:null});
  if(u.pathname==='/api/runs')return reply(options.validated?[]:[{id:'r_generated',target:'wf_generated',status:'queued'}]);
  if(u.pathname.endsWith('/data-facts'))return reply({items:[{id:text.id,status:text.status,source_id:'source_1'}],next_offset:null});
  if(u.pathname.endsWith('/data-facts/fact_1'))return reply(text);
  if(u.pathname==='/api/sources')return reply([{id:'source_1',project_id:'p_1',kind:'file',name:'原件.png',revision:1,content_hash:sourceHash}]);
  if(u.pathname.endsWith('/visual-facts'))return reply({items:[{id:visual.id,status:visual.status,page:1,source_id:'source_1'}],next_offset:null});
  if(u.pathname.endsWith('/visual-facts/visual_1'))return reply(visual);
  if(u.pathname.endsWith('/single-fact-candidates')){
   const value=options.createFail?reply({detail:'来源版本已变化'},400):reply({workflow_id:'wf_generated',run_id:'r_generated',status:options.validated?'validated':'queued',plan_sha256:plan});
   return options.deferCreate?new Promise(resolve=>{releaseCreate=()=>resolve(value)}):value;
  }
  throw Error('unexpected '+method+' '+u.pathname);
 };
 w.eval(fs.readFileSync(path.join(root,'data-workbench.js'),'utf8')+';window.test={ready:workbenchReady,state:wbState};');await w.test.ready;
 await until(()=>!!d.querySelector('[data-facts]')&&!!d.querySelector('[data-visual-record]'));
 return {dom,w,d,calls,releaseCreate:()=>releaseCreate?.()};
}
async function expand(app,kind='text'){
 const details=app.d.querySelector(kind==='text'?'[data-facts]':'[data-visual-record]');details.open=true;details.dispatchEvent(new app.w.Event('toggle'));
 await until(()=>!!details.querySelector('.proposal-body')&&details.querySelector('.proposal-body').textContent.trim()!=='正在读取原文与数值…'&&details.querySelector('.proposal-body').textContent.trim()!=='正在读取待核对内容…');return details;
}
(async()=>{
 const a=await boot(),details=await expand(a),button=details.querySelector('[data-single-candidate-create]');assert(button,'approved single fact gets a natural candidate action');
 assert(!a.calls.some(c=>c.method==='POST'),'loading the record never creates or confirms');button.click();await until(()=>!!details.querySelector('[data-single-candidate-open]')&&!details.querySelector('[data-single-candidate-open]').hidden);
 const call=a.calls.find(c=>c.path.endsWith('/single-fact-candidates'));assert.deepEqual(call.body,{kind:'text',fact_review_id:'fact_1',expected_fact_sha256:hash,expected_original_revision:1,expected_original_sha256:sourceHash});
 assert(details.textContent.includes('已接收'));assert(!a.calls.some(c=>c.path.endsWith('/confirm')||c.path.endsWith('/publish')));
 details.querySelector('[data-single-candidate-open]').click();await until(()=>a.d.querySelector('#workbench-title').textContent==='单条原值');
 assert(a.d.querySelector('#workbench-notice').textContent.includes('查看运行进度'));assert.equal(a.d.querySelector('#workbench-panel-task').hidden,false);a.dom.window.close();
 const visual=await boot(),vd=await expand(visual,'visual');vd.querySelector('[data-single-candidate-create]').click();await until(()=>!vd.querySelector('[data-single-candidate-open]').hidden);
 assert.equal(visual.calls.find(c=>c.path.endsWith('/single-fact-candidates')).body.kind,'visual');visual.dom.window.close();
 for(const options of [{pending:true},{multiple:true},{blocked:true},{noHash:true}]){const b=await boot(options),part=await expand(b);assert(!part.querySelector('[data-single-candidate-create]'));b.dom.window.close()}
 const failed=await boot({createFail:true}),fd=await expand(failed);fd.querySelector('[data-single-candidate-create]').click();await until(()=>fd.textContent.includes('候选请求未通过'));
 assert(fd.querySelector('[data-single-candidate-create]').disabled);assert(fd.querySelector('[data-single-candidate-open]').hidden);failed.dom.window.close();
 const stale=await boot({deferCreate:true}),sd=await expand(stale);sd.querySelector('[data-single-candidate-create]').click();await until(()=>stale.calls.some(c=>c.path.endsWith('/single-fact-candidates')));
 stale.w.history.replaceState({},'','?project=p_2');stale.releaseCreate();await tick();await tick();assert(sd.querySelector('[data-single-candidate-open]').hidden);stale.dom.window.close();
 const validated=await boot({validated:true}),part=await expand(validated);part.querySelector('[data-single-candidate-create]').click();await until(()=>!part.querySelector('[data-single-candidate-open]').hidden);
 part.querySelector('[data-single-candidate-open]').click();await until(()=>!validated.d.querySelector('#workbench-panel-result').hidden);
 assert.equal(validated.d.querySelector('#single-fact-panel').hidden,false);assert(!validated.calls.some(c=>c.path.endsWith('/confirm')||c.path.endsWith('/publish')));validated.dom.window.close();
 const dirty=await boot(),dd=await expand(dirty);dd.querySelector('[data-single-candidate-create]').click();await until(()=>!dd.querySelector('[data-single-candidate-open]').hidden);
 dirty.w.test.state.editorDirty=true;dd.querySelector('[data-single-candidate-open]').click();assert(dd.textContent.includes('未保存'));assert(!dirty.calls.some(c=>c.path.endsWith('/data-workflows/wf_generated')));dirty.dom.window.close();
 const wrong=await boot({wrongSource:true}),wd=await expand(wrong);wd.querySelector('[data-single-candidate-create]').click();await until(()=>!wd.querySelector('[data-single-candidate-open]').hidden);
 wd.querySelector('[data-single-candidate-open]').click();await until(()=>wd.textContent.includes('无法核对或打开'));assert(wrong.d.querySelector('#workbench-panel-result').hidden);wrong.dom.window.close();
 process.stdout.write('single-fact-candidate UI: passed\n');
})().catch(error=>{console.error(error);process.exitCode=1});
