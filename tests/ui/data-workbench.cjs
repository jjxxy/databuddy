// DOM/API contract regression. Does not claim native browser layout verification.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const html=fs.readFileSync(path.join(root,'data-workbench.html'),'utf8');
const script=fs.readFileSync(path.join(root,'data-workbench.js'),'utf8');
const sha='a'.repeat(64), semanticSha='c'.repeat(64);
const field=(id,name,unit='CNY')=>({id,name,meaning:'已付款订单扣除退款后的金额',kind:'measure',unit,period:'2026-10',grain:['office'],value_format:'decimal',aggregation:'sum'});
const proposal={id:'sem_1',kind:'alignment',status:'proposed',content_sha256:semanticSha,source_name:'收入<img src=x onerror=alert(1)>',
 payload:{source:field('source','来源收入'),candidates:[
   {target:field('good','净收入<script>bad</script>'),state:'review_required',blockers:[],review_reasons:['business_meaning_requires_human_review']},
   {target:field('blocked','美元收入','USD'),state:'blocked',blockers:['unit_mismatch_requires_explicit_conversion'],review_reasons:[]},
 ]}};
const catalogSha='d'.repeat(64),sourceSha='e'.repeat(64);
function catalogRecord(id,status='draft',version=1){
 const source={source_id:'definitions_1',locator:'口径文档第 2 页 <img src=x onerror=alert(1)>',content_sha256:sourceSha};
 return {id,key:'finance-income',version,status,content_sha256:catalogSha,
  created_at:'2026-10-21T09:00:00Z',created_actor_sha256:'1'.repeat(64),
  source_snapshot:{source_id:source.source_id,source_revision:4,source_revision_id:'frozen_definitions_4',content_sha256:sourceSha},
  payload:{key:'finance-income',name:'收入口径<img src=x onerror=alert(1)>',description:'完整定义<script>bad</script>',
   source,effective_from:'2026-01-01',effective_to:'2026-12-31',fields:[
    {...field('revenue','净收入<script>bad</script>'),temporal_kind:'interval',aliases:['收入别名<img src=x>'],number_format:'#,##0.00',
     provenance:[{...source,locator:'字段原件第 3 条 <script>bad</script>'}]},
    {id:'office',name:'办事处',meaning:'业务发生的办事处',kind:'dimension',grain:[],unit:null,
     temporal_kind:'timeless',period:null,value_format:'text',aggregation:'none',provenance:[]}]},
  approval:status==='draft'?null:{approved_at:'2026-10-21T09:30:00Z',actor_sha256:'2'.repeat(64),trust_level:'owner_reviewed'},
  retirement:status==='retired'?{retired_at:'2026-10-21T10:00:00Z'}:null};
}
function response(value,status=200){return {ok:status>=200&&status<300,status,json:async()=>value,blob:async()=>new Blob(['complete-result']),headers:{get:()=>null}};}
function clone(value){return JSON.parse(JSON.stringify(value));}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function boot({token=true,project=true,putConflict=false,reviewConflict=false,empty=false,factsBlocked=false,factsReviewConflict=false,latestJob=null,tab='',runGetError=false,schedule=null,refreshError='',catalogFixture=null,catalogBinding=null}={}){
 const url='https://atlas.test/data-workbench.html'+(project?'?project=p_1&workflow=wf_1'+(tab?'&tab='+tab:''):'');
 const dom=new JSDOM(html,{url,runScripts:'outside-only'}),w=dom.window,calls=[],timers=[];
 if(token)w.sessionStorage.setItem('atlas_token','test-token-that-must-not-be-logged');
 w.setTimeout=(fn,delay)=>{timers.push({fn,delay});return timers.length;};w.clearTimeout=()=>{};
 w.URL.createObjectURL=()=> 'blob:https://atlas.test/local-result';w.URL.revokeObjectURL=()=>{};
 const workflow={workflow_id:'wf_1',name:'<img src=x onerror=alert(1)> 收入核对',draft_sha256:sha,published_sha256:'b'.repeat(64),published_run:'old_result',candidate_run:'r_1',
 inputs:{draft:{binding:{source_id:'draft_source_that_must_not_appear_as_result_evidence'},schema:{meaning:'新的草稿来源',grain:['office']}}},
 steps:[{id:'filtered',kind:'filter',input:'orders',conditions:[]},{id:'summed',kind:'aggregate',input:'filtered',group_by:['office']}],
 document:{version:1,plan:{name:'收入核对',stages:[],inputs:{orders:{}},output:'summed'},bindings:{orders:{source_id:'s_1'}}},cron:schedule?.cron||null,watch_sources:schedule?.watch_sources||false,next_due:schedule?.next_due||null,refresh_error:refreshError};
 const semantic=clone(proposal);
 if(catalogBinding)semantic.catalog_binding=clone(catalogBinding);
 const catalogSettings=catalogFixture||{},catalogs=new Map((catalogSettings.records||[]).map(record=>[record.id,clone(record)])),pendingCatalogActions=[];
 const facts={id:'facts_1',name:'来源 <img src=x onerror=alert(1)>',status:'proposed',content_sha256:'f'.repeat(64),
 facts:[{value_as_printed:'1234567890123456.123456789012元',normalized_value:'1234567890123456.123456789012',measure:'净收入<script>bad</script>',unit:'CNY',period:'2026-10',dimensions:{office:'合肥'},quote:'2026年10月，合肥净收入1234567890123456.123456789012元。<img src=x>',start:12,end:83}],approval_blockers:factsBlocked?['请补全来源单位']:[]};
 let putBody=null,jobStatus=null,currentJob=latestJob,releaseSecond=null;
 w.fetch=async(url,options={})=>{
  const u=new URL(url,w.location.href),method=options.method||'GET';calls.push({url:u.pathname+u.search,method,options});
  assert.equal(options.headers.Authorization,'Bearer '+w.sessionStorage.getItem('atlas_token'));
  const catalogRoute=u.pathname.match(/\/semantic-catalogs(?:\/([^/]+)(?:\/(approve|retire))?)?$/);
  if(catalogRoute){
   const [,id,operation]=catalogRoute;
   if(!id){
    assert.equal(method,'GET');
    if(catalogSettings.listStatus)return response({detail:'catalog endpoint unavailable <script>raw</script>'},catalogSettings.listStatus);
    const offset=+(u.searchParams.get('offset')||0),page=catalogSettings.pages?.[offset];
    assert.equal(u.searchParams.get('limit'),'20');
    const ids=page?.ids||Array.from(catalogs.keys()).slice(offset,offset+20);
    return response({items:ids.map(key=>clone(catalogs.get(key))),total:page?.total??catalogs.size,next_offset:page?.next_offset??null});
   }
   const record=catalogs.get(id);if(!record)return response({detail:'语义包不存在'},404);
   if(operation){
    assert.equal(method,'POST');const body=JSON.parse(options.body);
    assert.deepEqual(body,{expected_sha256:record.content_sha256},'catalog authority and actor must never come from the frontend');
    const finish=()=>{
     if(catalogSettings.actionStatus)return response({detail:'定义或状态已变化'},catalogSettings.actionStatus);
     record.status=operation==='approve'?'approved':'retired';
     if(operation==='approve')record.approval={approved_at:'2026-10-21T10:30:00Z',actor_sha256:'3'.repeat(64),trust_level:'owner_reviewed'};
     else record.retirement={retired_at:'2026-10-21T10:45:00Z'};
     return response(clone(record));
    };
    return catalogSettings.deferActions?new Promise(resolve=>pendingCatalogActions.push(()=>resolve(finish()))):finish();
   }
   assert.equal(method,'GET');
   return catalogSettings.detailStatus?response({detail:'版本暂时不可读'},catalogSettings.detailStatus):response(clone(record));
  }
  if(u.pathname.endsWith('/data-workflows'))return response({items:empty?[]:[{id:'wf_1',name:workflow.name,candidate_run:workflow.candidate_run,published_run:workflow.published_run}]});
  if(u.pathname.endsWith('/data-workflows/wf_1')&&method==='PUT'){
   putBody=JSON.parse(options.body);return putConflict?response({detail:'方案已变化'},409):response({workflow_id:'wf_1',draft_sha256:'d'.repeat(64)});
  }
  if(u.pathname.endsWith('/data-workflows/wf_1'))return response(clone(workflow));
  if(u.pathname.endsWith('/data-workflows/wf_2'))return new Promise(resolve=>{releaseSecond=()=>resolve(response({...clone(workflow),workflow_id:'wf_2',name:'第二项任务'}));});
  if(u.pathname.endsWith('/data-workflows/wf_1/run')){jobStatus='queued';return response({id:'job_1',status:'queued'});}
  if(u.pathname.endsWith('/data-results/r_1/publish')){workflow.candidate_run=null;workflow.published_run='r_1';return response({published_run:'r_1'});}
  if(u.pathname.endsWith('/data-results/r_1/download'))return response({});
  if(u.pathname.endsWith('/data-results/r_1')){
   const offset=+(u.searchParams.get('offset')||0),limit=+(u.searchParams.get('limit')||50),count=Math.min(limit,603-offset);
   return response({run_id:'r_1',plan_sha256:sha,total:603,next_offset:offset+count<603?offset+count:null,
    schema:[{name:'office',label:'办事处<script>bad</script>',meaning:'业务办事处',data_type:'text'},{name:'amount',label:'净收入',meaning:'收入减退款',unit:'CNY',period:'2026-10',data_type:'decimal'}],
    rows:Array.from({length:count},(_,i)=>({office:offset+i===0?'<img src=x onerror=alert(1)>':'O'+(offset+i),amount:'1234567890123456.123456789012'})),
    context:{input_evidence:{orders:{source_id:'frozen_source',source_revision:2,rows:1200,sheet:'明细'}},output_semantics:{grain:['office']},requires_semantic_review:false},
    stages:[{stage_id:'summed',kind:'aggregate',output_rows:603,checks:{complete_table:true},lineage:{kind:'aggregate',group_by:['office']}}]});
  }
  if(u.pathname.endsWith('/semantic-proposals/sem_1/review')){
   if(reviewConflict)return response({detail:'定义已变化'},409);
   const payload=JSON.parse(options.body);semantic.status=payload.decision;return response(clone(semantic));
  }
  if(u.pathname.endsWith('/semantic-proposals/sem_1'))return response(clone(semantic));
  if(u.pathname.endsWith('/semantic-proposals'))return response({items:empty?[]:[{id:semantic.id,kind:semantic.kind,status:semantic.status,content_sha256:semantic.content_sha256,source_name:semantic.source_name}],total:empty?0:1,next_offset:null});
  if(u.pathname.endsWith('/data-facts/facts_1/review')){
   if(factsReviewConflict)return response({detail:'原文已变化'},409);
   const value=JSON.parse(options.body);facts.status=value.decision;return response(clone(facts));
  }
  if(u.pathname.endsWith('/data-facts/facts_1'))return response(clone(facts));
  if(u.pathname.endsWith('/data-facts'))return response({items:[clone(facts)]});
  if(u.pathname==='/api/runs')return runGetError?response({detail:'Traceback: db password and SQL should not be in the main flow'},500):response(currentJob?[clone(currentJob)]:jobStatus?[{id:'job_1',status:jobStatus}]:[]);
  throw Error('Unexpected request '+method+' '+u.pathname);
 };
 w.eval(script+';window.wbTest={ready:workbenchReady,state:wbState,save:wbSave,loadResult:wbLoadResult,loadWorkflow:wbLoadWorkflow,run:wbRun,poll:wbPoll,publish:wbPublish,loadProposals:wbLoadProposals,download:wbDownload,loadFacts:wbLoadFacts};');
 await w.wbTest.ready;await tick();
 return {dom,w,calls,timers,workflow,semantic,catalogs,catalogSettings,releaseCatalogAction:()=>{assert(pendingCatalogActions.length);pendingCatalogActions.shift()();},releaseSecond:()=>releaseSecond(),setJob:value=>{currentJob=value;},putBody:()=>putBody};
}
async function expandProposal(w){const details=w.document.querySelector('[data-proposal]');details.open=true;details.dispatchEvent(new w.Event('toggle'));await tick();await tick();return details;}
async function expandCatalog(w,id){const details=w.document.querySelector('[data-catalog="'+id+'"]');assert(details);details.open=true;details.dispatchEvent(new w.Event('toggle'));await tick();await tick();return details;}
async function openSemantics(w){w.document.querySelector('#workbench-tab-semantics').click();await tick();await tick();}
(async()=>{
 const app=await boot(),{w,calls}=app,d=w.document;
 assert.equal(d.querySelector('#workbench-shell').hidden,false);
 assert.equal(d.querySelector('#workbench-panel-task').hidden,false,'the landing view explains a saved task before opening a data table');
 assert.equal(d.querySelector('#workbench-panel-result').hidden,true);
 assert.equal(d.querySelector('#workbench-tab-task').getAttribute('aria-selected'),'true');
 assert.equal(d.querySelector('#workbench-editor-details').open,false,'technical rule editing stays behind an explicit expansion');
 assert.equal(d.querySelector('#workbench-source-count').textContent,'1 份');
 assert.equal(d.querySelector('#workbench-task-state').textContent,'新结果待确认');
 assert.equal(d.querySelector('#workbench-schedule').textContent,'按需运行');
 assert(d.querySelector('#workbench-task-summary').textContent.includes('2 个保存的步骤'));
 assert(!d.querySelector('#workbench-overview').textContent.includes(sha.slice(0,12)),'version hashes are not the primary task status');
 d.querySelector('#workbench-open-result').click();assert.equal(d.querySelector('#workbench-panel-result').hidden,false);assert.equal(d.activeElement,d.querySelector('#workbench-tab-result'));
 assert.equal(d.querySelectorAll('#workbench-shell img').length,0);
 assert.equal(d.querySelectorAll('#workbench-table-body script').length,0);
 assert.equal(d.querySelector('#workbench-table-body tr td:nth-child(2)').textContent,'1234567890123456.123456789012');
 assert.equal(d.querySelectorAll('#workbench-table-body tr').length,50);
 assert(d.querySelector('#workbench-page-info').textContent.includes('共 603 条'));
 assert(d.querySelector('#workbench-result-help').textContent.includes('仅展示这一页'));
 assert(d.querySelector('#workbench-sources').textContent.includes('frozen_source'));
 assert(!d.querySelector('#workbench-sources').textContent.includes('draft_source_that_must_not_appear'));
 assert.equal(d.querySelector('#workbench-back').getAttribute('href'),'/?project=p_1');
 assert.equal(calls.filter(c=>c.method!=='GET').length,0,'opening the review page cannot approve, run or publish');
 const csvSaves=[];w.HTMLAnchorElement.prototype.click=function(){csvSaves.push({name:this.download,href:this.href});};
 assert.equal(d.querySelector('#workbench-download-csv').disabled,false);
 d.querySelector('#workbench-download-csv').click();await tick();await tick();
 assert(calls.some(call=>call.url.endsWith('/data-results/r_1/download?format=csv')&&call.method==='GET'));
 assert.deepEqual(csvSaves,[{name:'数据结果-r_1.csv',href:'blob:https://atlas.test/local-result'}]);
 assert.equal(calls.filter(c=>c.method!=='GET').length,0,'CSV download cannot publish the candidate');
 const resultTab=d.querySelector('#workbench-tab-result');resultTab.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));assert.equal(d.activeElement,d.querySelector('#workbench-tab-rules'));assert.equal(d.querySelector('#workbench-panel-rules').hidden,false);
 await w.wbTest.loadResult(600);
 assert.equal(d.querySelectorAll('#workbench-table-body tr').length,3);
 assert(d.querySelector('#workbench-table-body').textContent.includes('O602'));
 assert.equal(d.querySelector('#workbench-next').disabled,true);
 const details=await expandProposal(w),select=details.querySelector('select');
 assert(details.querySelector('.catalog-binding').textContent.includes('临时目标定义，未绑定语义包'));
 assert(details.querySelector('.catalog-binding').textContent.includes('不能视为 owner 已核阅'));
 assert.equal(select.value,'');assert.equal(details.querySelector('[data-review="approved"]').disabled,true);
 select.value='blocked';select.dispatchEvent(new w.Event('change'));
 assert.equal(details.querySelector('[data-review="approved"]').disabled,true);
 assert(details.textContent.includes('单位不同'));
 assert.equal(details.querySelector('[data-review="rejected"]').disabled,false);
 select.value='good';select.dispatchEvent(new w.Event('change'));
 assert.equal(details.querySelectorAll('script').length,0);
 assert.equal(details.querySelector('[data-review="approved"]').disabled,false);
 details.querySelector('[data-review="approved"]').click();await tick();await tick();
 const reviewCall=calls.find(c=>c.url.endsWith('/review'));
 assert.deepEqual(JSON.parse(reviewCall.options.body),{expected_sha256:semanticSha,decision:'approved',target_id:'good'});
 assert.equal(JSON.parse(reviewCall.options.body).reviewer,undefined,'review actor must come from server authentication');
 await w.wbTest.publish();
 const publishCall=calls.find(c=>c.url.endsWith('/publish'));
 assert.deepEqual(JSON.parse(publishCall.options.body),{run_id:'r_1',expected_plan_sha256:sha});
 assert.equal(d.querySelector('#workbench-candidate').hidden,true);
 assert.equal(w.localStorage.getItem('atlas_token'),null);
 app.dom.window.close();

 const running=await boot();assert.equal(running.w.document.querySelector('#workbench-run').disabled,false);await running.w.wbTest.run();assert.equal(running.w.wbTest.state.activeJob,'job_1');assert.equal(running.w.document.querySelector('#workbench-run').disabled,true);assert(running.calls.some(c=>c.method==='POST'&&c.url.endsWith('/run')));assert(running.timers.length>0);running.dom.window.close();

 const failedJob={id:'failed_run',status:'failed',finished_at:'2026-10-21T10:20:30Z',error_code:'TASK_FAILED',error:'A general task failure',
 workflow_document:{plan:{stages:[{id:'filtered',kind:'filter'},{id:'summed',kind:'aggregate',label:'当次按月份汇总'}]}},
 workflow_failure:{stage_id:'summed',code:'NON_ADDITIVE',message:'Summation requires a declared additive measure <img src=x>'}};
 const failed=await boot({latestJob:failedJob}),fd=failed.w.document;
 assert.equal(fd.querySelector('#workbench-task-state').textContent,'需要检查');
 assert.equal(fd.querySelector('#workbench-failure').hidden,false);
 assert(fd.querySelector('#workbench-failure-reason').textContent.includes('尚未确认可以直接相加'));
 assert(fd.querySelector('#workbench-failure-next').textContent.includes('汇总口径'));
 assert(fd.querySelector('#workbench-failure-stage').textContent.includes('第 2 步'));
 assert(fd.querySelector('#workbench-failure-stage').textContent.includes('当次按月份汇总'),'historical failure must use its frozen steps, not the edited current draft');
 assert.equal(fd.querySelector('#workbench-failure details').open,false);
 assert(fd.querySelector('#workbench-failure-details').textContent.includes('NON_ADDITIVE'));
 assert.equal(fd.querySelector('#workbench-failure-details img'),null);
 assert.equal(fd.querySelector('#workbench-table-body tr td:nth-child(2)').textContent,'1234567890123456.123456789012','a failed newer run leaves the prior result available');
 assert.equal(failed.calls.filter(c=>c.method!=='GET').length,0,'restoring a failure must not retry or alter financial rules');
 assert.equal(fd.querySelector('#workbench-failure-chat').getAttribute('href'),'/?project=p_1');
 failed.dom.window.close();

 const budget=await boot({latestJob:{...failedJob,workflow_failure:{stage_id:null,code:'BYTE_BUDGET',message:'budget exceeded'}}});
 assert(budget.w.document.querySelector('#workbench-failure-next').textContent.includes('调整方案'));
 assert(!budget.w.document.querySelector('#workbench-failure-next').textContent.includes('重试'));
 budget.dom.window.close();

 for(const [code,reason,next] of [
  ['SOURCE_HASH_MISSING','旧来源缺少可核对的原件摘要','重新上传原件以建立带摘要的来源修订'],
  ['SOURCE_CONTENT_CHANGED','登记后的来源原件内容发生了变化','重新读取并登记新的来源修订'],
 ]){
  const sourceFailure=await boot({latestJob:{...failedJob,workflow_failure:{stage_id:null,code,message:'internal source integrity detail'}}}),sd=sourceFailure.w.document;
  assert(sd.querySelector('#workbench-failure-reason').textContent.includes(reason));
  assert(sd.querySelector('#workbench-failure-next').textContent.includes(next));
  assert(!sd.querySelector('#workbench-failure-next').textContent.includes('重试'),'source integrity failures require registration, not a blind retry');
  assert.equal(sourceFailure.calls.filter(c=>c.method==='POST').length,0,'restoring a source integrity failure never automatically reruns the task');
  sourceFailure.dom.window.close();
 }

 const unknown=await boot({latestJob:{...failedJob,workflow_failure:{stage_id:null,code:'UNRECOGNIZED',message:'Raw technical engine detail'}}});
 assert(unknown.w.document.querySelector('#workbench-failure-next').textContent.includes('Agent'));
 assert(!unknown.w.document.querySelector('#workbench-failure-reason').textContent.includes('Raw technical'));
 assert(unknown.w.document.querySelector('#workbench-failure-details').textContent.includes('Raw technical'));
 unknown.dom.window.close();

 const oldFailure=await boot({latestJob:{...failedJob,workflow_document:undefined,commit:'older-version'}});
 assert(!oldFailure.w.document.querySelector('#workbench-failure-stage').textContent.includes('第 2 步'),'an unverified historical stage must not be resolved against the current draft');
 assert(oldFailure.w.document.querySelector('#workbench-failure-details').textContent.includes('summed'));oldFailure.dom.window.close();

 const polled=await boot();await polled.w.wbTest.run();polled.setJob({...failedJob,id:'job_1'});await polled.w.wbTest.poll();
 assert.equal(polled.w.wbTest.state.activeJob,'');
 assert.equal(polled.w.document.querySelector('#workbench-failure').hidden,false);
 assert(polled.w.document.querySelector('#workbench-notice').textContent.includes('任务概况'));
 assert.equal(polled.w.document.querySelector('#workbench-download-xlsx').disabled,false);
 polled.dom.window.close();

 const directResult=await boot({tab:'result'});assert.equal(directResult.w.document.querySelector('#workbench-panel-result').hidden,false);directResult.dom.window.close();
 const stateError=await boot({runGetError:true});assert(stateError.w.document.querySelector('#workbench-notice').textContent.includes('稍后刷新'));assert(!stateError.w.document.querySelector('#workbench-notice').textContent.includes('Traceback'));stateError.dom.window.close();
 const scheduled=await boot({schedule:{cron:'0 6 * * *',watch_sources:true,next_due:'2026-10-22T06:00:00Z'}});
 assert.equal(scheduled.w.document.querySelector('#workbench-schedule').textContent,'按计划重复');
 assert(scheduled.w.document.querySelector('#workbench-schedule-note').textContent.includes('2026'));
 assert(!scheduled.w.document.querySelector('#workbench-schedule-note').textContent.includes('0 6 * * *'));
 assert(scheduled.w.document.querySelector('#workbench-panel-task').textContent.includes('重复执行沿用已采用的做法'));
 scheduled.dom.window.close();
 const refreshFailed=await boot({refreshError:'来源不可用'});assert.equal(refreshFailed.w.document.querySelector('#workbench-task-state').textContent,'需要检查来源');refreshFailed.dom.window.close();

 const switching=await boot(),switchPromise=switching.w.wbTest.loadWorkflow('wf_2');
 assert.equal(switching.w.wbTest.state.workflow,null,'old task must be cleared while a different task loads');
 assert.equal(switching.w.document.querySelector('#workbench-run').disabled,true);
 assert.equal(switching.w.document.querySelector('#workbench-save').disabled,true);
 assert.equal(switching.w.document.querySelector('#workbench-publish').disabled,true);
 await switching.w.wbTest.run();assert.equal(switching.calls.filter(c=>c.method!=='GET').length,0,'loading a task cannot submit actions from the previous task UI');
 switching.releaseSecond();await switchPromise;
 assert.equal(switching.w.document.querySelector('#workbench-title').textContent,'第二项任务');
 assert.equal(switching.w.document.querySelector('#workbench-run').disabled,false);switching.dom.window.close();

 const conflict=await boot({putConflict:true}),editor=conflict.w.document.querySelector('#workbench-plan');
 const modified=JSON.stringify({version:1,plan:{name:'我的修改'},bindings:{}});editor.value=modified;editor.dispatchEvent(new conflict.w.Event('input'));
 assert.equal(conflict.w.document.querySelector('#workbench-run').disabled,true);
 await conflict.w.wbTest.save();
 assert.equal(editor.value,modified);assert.equal(conflict.putBody().expected_sha256,sha);
 assert.equal(conflict.w.document.querySelector('#workbench-save').disabled,true);
 assert(conflict.w.document.querySelector('#workbench-notice').textContent.includes('没有覆盖'));
 assert(conflict.w.document.querySelector('#workbench-next-title').textContent.includes('合并'));
 conflict.dom.window.close();

 const stale=await boot({reviewConflict:true}),staleDetails=await expandProposal(stale.w),choice=staleDetails.querySelector('select');
 choice.value='good';choice.dispatchEvent(new stale.w.Event('change'));staleDetails.querySelector('[data-review="approved"]').click();await tick();await tick();
 assert(staleDetails.querySelector('.proposal-message').textContent.includes('本次没有确认'));
 assert.equal(staleDetails.querySelector('[data-review="approved"]').disabled,true);
 stale.dom.window.close();

 const login=await boot({token:false});assert.equal(login.w.document.querySelector('#workbench-login').hidden,false);assert.equal(login.calls.length,0);login.dom.window.close();
 const missing=await boot({project:false});assert.equal(missing.w.document.querySelector('#workbench-missing').hidden,false);assert.equal(missing.calls.length,0);missing.dom.window.close();
 const factsApp=await boot();await factsApp.w.wbTest.loadFacts();
 const factDetails=factsApp.w.document.querySelector('[data-facts]');factDetails.open=true;factDetails.dispatchEvent(new factsApp.w.Event('toggle'));await tick();await tick();
 assert.equal(factDetails.querySelectorAll('img,script').length,0);
 assert(factDetails.querySelector('blockquote').textContent.includes('1234567890123456.123456789012'));
 assert(factDetails.textContent.includes('2026-10'));
 assert.equal(factsApp.calls.filter(c=>c.method==='POST').length,0);
 const factApprove=factDetails.querySelector('[data-facts-decision="approved"]');assert.equal(factApprove.disabled,false);
 factApprove.click();await tick();await tick();
 const factCall=factsApp.calls.find(c=>c.url.endsWith('/data-facts/facts_1/review'));
 assert.deepEqual(JSON.parse(factCall.options.body),{expected_sha256:'f'.repeat(64),decision:'approved'});
 assert.equal(JSON.parse(factCall.options.body).reviewer,undefined);
 factsApp.dom.window.close();
 const blockedFacts=await boot({factsBlocked:true});await blockedFacts.w.wbTest.loadFacts();
 const blockedFactDetails=blockedFacts.w.document.querySelector('[data-facts]');blockedFactDetails.open=true;blockedFactDetails.dispatchEvent(new blockedFacts.w.Event('toggle'));await tick();await tick();
 assert.equal(blockedFactDetails.querySelector('[data-facts-decision="approved"]').disabled,true);
 assert(blockedFactDetails.textContent.includes('请补全来源单位'));
 blockedFactDetails.querySelector('[data-facts-decision="rejected"]').click();await tick();await tick();
 assert.equal(JSON.parse(blockedFacts.calls.find(c=>c.url.endsWith('/data-facts/facts_1/review')).options.body).decision,'rejected');blockedFacts.dom.window.close();
 const staleFacts=await boot({factsReviewConflict:true});await staleFacts.w.wbTest.loadFacts();
 const staleFactDetails=staleFacts.w.document.querySelector('[data-facts]');staleFactDetails.open=true;staleFactDetails.dispatchEvent(new staleFacts.w.Event('toggle'));await tick();await tick();
 staleFactDetails.querySelector('[data-facts-decision="approved"]').click();await tick();await tick();
 assert(staleFactDetails.querySelector('.proposal-message').textContent.includes('本次没有确认'));
 assert.equal(staleFactDetails.querySelector('[data-facts-decision="approved"]').disabled,true);staleFacts.dom.window.close();

 const catalogApp=await boot({catalogFixture:{records:[catalogRecord('cat_draft'),catalogRecord('cat_approved','approved',2),catalogRecord('cat_retired','retired',3)],
  pages:{0:{ids:['cat_draft'],total:3,next_offset:20},20:{ids:['cat_draft','cat_approved','cat_retired'],total:3,next_offset:null}},deferActions:true}}),cd=catalogApp.w.document;
 assert.equal(catalogApp.calls.filter(c=>c.url.includes('/semantic-catalogs')).length,0,'task landing does not read semantic catalogs');
 await openSemantics(catalogApp.w);
 assert.equal(catalogApp.calls.filter(c=>c.url.includes('/semantic-catalogs?')).length,1);
 assert.equal(catalogApp.calls.filter(c=>/\/semantic-catalogs\/[^/?]+$/.test(c.url)).length,0,'list navigation does not automatically load or approve full definitions');
 assert.equal(catalogApp.calls.filter(c=>c.method==='POST').length,0);
 assert.equal(cd.querySelectorAll('[data-catalog]').length,1);
 assert.equal(cd.querySelector('#workbench-more-catalogs').hidden,false);
 cd.querySelector('#workbench-more-catalogs').click();await tick();await tick();
 assert.equal(cd.querySelectorAll('[data-catalog]').length,3,'overlapping pages deduplicate by immutable catalog id');
 assert.equal(catalogApp.w.wbTest.state.catalogs.size,3);
 assert(cd.querySelector('#workbench-catalog-page-info').textContent.includes('已加载 3 个版本，共 3 个'));
 assert.equal(cd.querySelector('#workbench-more-catalogs').hidden,true);
 assert(catalogApp.calls.some(c=>c.url.endsWith('/semantic-catalogs?offset=20&limit=20')));
 const catalogDetails=await expandCatalog(catalogApp.w,'cat_draft');
 assert.equal(catalogApp.calls.filter(c=>c.url.endsWith('/semantic-catalogs/cat_draft')).length,1);
 for(const expected of ['完整字段定义（2）','已付款订单扣除退款后的金额','CNY','2026-10','office','2026-01-01','2026-12-31',
  '口径文档第 2 页 <img src=x onerror=alert(1)>','字段原件第 3 条 <script>bad</script>',sourceSha,catalogSha,'frozen_definitions_4','标量（没有维度）']){
  assert(catalogDetails.textContent.includes(expected),'full catalog review must show '+expected);
 }
 assert.equal(catalogDetails.querySelectorAll('.catalog-field').length,2);
 assert.equal(cd.querySelectorAll('#workbench-catalogs img,#workbench-catalogs script').length,0,'catalog metadata remains inert text');
 const acknowledge=catalogDetails.querySelector('[data-catalog-acknowledge="approve"]'),approveButton=catalogDetails.querySelector('[data-catalog-action="approve"]');
 assert.equal(acknowledge.checked,false);assert.equal(approveButton.disabled,true);
 approveButton.click();assert.equal(catalogApp.calls.filter(c=>c.method==='POST').length,0);
 acknowledge.checked=true;acknowledge.dispatchEvent(new catalogApp.w.Event('change'));
 assert.equal(approveButton.disabled,false);
 approveButton.click();approveButton.click();
 assert.equal(catalogApp.calls.filter(c=>c.url.endsWith('/semantic-catalogs/cat_draft/approve')).length,1,'double activation posts a single approval while pending');
 assert.equal(approveButton.disabled,true);assert.equal(acknowledge.disabled,true);
 assert.equal(cd.querySelector('#workbench-refresh-catalogs').disabled,true);
 const catalogApproval=catalogApp.calls.find(c=>c.url.endsWith('/semantic-catalogs/cat_draft/approve'));
 assert.deepEqual(JSON.parse(catalogApproval.options.body),{expected_sha256:catalogSha});
 catalogApp.releaseCatalogAction();await tick();await tick();
 assert(cd.querySelector('#workbench-catalog-message').textContent.includes('已批准此版本'));
 const retirementCheck=catalogDetails.querySelector('[data-catalog-acknowledge="retire"]'),retirementButton=catalogDetails.querySelector('[data-catalog-action="retire"]');
 assert.equal(retirementCheck.checked,false);assert.equal(retirementButton.disabled,true,'a prior approval checkbox does not authorize retirement');
 retirementButton.click();assert.equal(catalogApp.calls.filter(c=>c.url.endsWith('/retire')).length,0);
 retirementCheck.checked=true;retirementCheck.dispatchEvent(new catalogApp.w.Event('change'));
 retirementButton.click();retirementButton.click();
 assert.equal(catalogApp.calls.filter(c=>c.url.endsWith('/semantic-catalogs/cat_draft/retire')).length,1);
 assert.deepEqual(JSON.parse(catalogApp.calls.find(c=>c.url.endsWith('/semantic-catalogs/cat_draft/retire')).options.body),{expected_sha256:catalogSha});
 catalogApp.releaseCatalogAction();await tick();await tick();
 assert.equal(catalogDetails.querySelector('[data-catalog-action]'),null);
 assert.equal(catalogDetails.querySelector('[data-catalog-acknowledge]'),null);
 assert(catalogDetails.textContent.includes('历史提案、核对记录和结果继续保留'));
 const history=await expandCatalog(catalogApp.w,'cat_retired');
 assert.equal(history.querySelector('[data-catalog-action]'),null);
 assert(history.textContent.includes('第 3 版')&&history.textContent.includes('此版本已退休'));
 assert.equal(cd.querySelectorAll('#workbench-catalogs img,#workbench-catalogs script').length,0);
 catalogApp.dom.window.close();

 const missingCatalogs=await boot({tab:'semantics',catalogFixture:{listStatus:404}}),mcd=missingCatalogs.w.document;
 assert(mcd.querySelector('#workbench-catalog-message').textContent.includes('暂时无法读取业务语义包'));
 assert(!mcd.querySelector('#workbench-catalog-message').textContent.includes('catalog endpoint unavailable'));
 assert.equal(mcd.querySelector('#workbench-run').disabled,false,'a missing catalog API does not break the existing task');
 assert.equal(mcd.querySelectorAll('#workbench-table-body tr').length,50);
 await missingCatalogs.w.wbTest.run();
 assert(missingCatalogs.calls.some(c=>c.method==='POST'&&c.url.endsWith('/data-workflows/wf_1/run')));
 missingCatalogs.dom.window.close();

 const unreadable=await boot({tab:'semantics',catalogFixture:{records:[catalogRecord('cat_detail')],detailStatus:409}});
 const unreadableDetails=await expandCatalog(unreadable.w,'cat_detail');
 assert(unreadableDetails.textContent.includes('暂时无法读取此版本'));
 assert.equal(unreadableDetails.querySelector('[data-catalog-action]'),null);
 unreadable.catalogSettings.detailStatus=0;
 Array.from(unreadableDetails.querySelectorAll('button')).find(button=>button.textContent==='重新读取语义包').click();await tick();await tick();
 assert.equal(unreadableDetails.querySelector('[data-catalog-action="approve"]').disabled,true);
 assert.equal(unreadable.calls.filter(c=>c.url.endsWith('/semantic-catalogs/cat_detail')).length,2);
 unreadable.dom.window.close();

 const staleCatalog=await boot({tab:'semantics',catalogFixture:{records:[catalogRecord('cat_stale')],actionStatus:409}});
 const staleCatalogDetails=await expandCatalog(staleCatalog.w,'cat_stale'),sc=staleCatalogDetails.querySelector('[data-catalog-acknowledge="approve"]'),sb=staleCatalogDetails.querySelector('[data-catalog-action="approve"]');
 sc.checked=true;sc.dispatchEvent(new staleCatalog.w.Event('change'));sb.click();await tick();await tick();
 assert(staleCatalogDetails.querySelector('.proposal-message').textContent.includes('本次未获批准'));
 assert.equal(sb.disabled,true);assert.equal(sc.disabled,true,'a failed action invalidates the old acknowledgment');
 sc.dispatchEvent(new staleCatalog.w.Event('change'));sb.click();
 assert.equal(staleCatalog.calls.filter(c=>c.method==='POST').length,1,'old action remains disabled until the version is re-read');
 staleCatalog.catalogSettings.actionStatus=0;staleCatalog.catalogs.get('cat_stale').content_sha256='9'.repeat(64);
 Array.from(staleCatalogDetails.querySelectorAll('button')).find(button=>button.textContent==='重新读取语义包').click();await tick();await tick();
 const refreshedCheck=staleCatalogDetails.querySelector('[data-catalog-acknowledge="approve"]'),refreshedButton=staleCatalogDetails.querySelector('[data-catalog-action="approve"]');
 assert.equal(refreshedCheck.checked,false);assert.equal(refreshedButton.disabled,true);
 refreshedCheck.checked=true;refreshedCheck.dispatchEvent(new staleCatalog.w.Event('change'));refreshedButton.click();await tick();await tick();
 const retried=staleCatalog.calls.filter(c=>c.url.endsWith('/semantic-catalogs/cat_stale/approve'));
 assert.equal(retried.length,2);assert.deepEqual(JSON.parse(retried[1].options.body),{expected_sha256:'9'.repeat(64)});
 staleCatalog.dom.window.close();

 const bound=await boot({catalogBinding:{name:'已核阅收入<script>bad</script>',version:7,as_of_date:'2026-10-01',content_sha256:catalogSha,trust_level:'owner_reviewed'}});
 const boundDetails=await expandProposal(bound.w),binding=boundDetails.querySelector('.catalog-binding');
 for(const expected of ['目标来自 owner 已核阅语义包','已核阅收入<script>bad</script>','第 7 版','2026-10-01',catalogSha,'不代表这次字段匹配已确认'])assert(binding.textContent.includes(expected));
 assert.equal(binding.querySelectorAll('script,img').length,0);
 assert.equal(bound.calls.filter(c=>c.method==='POST').length,0);
 bound.dom.window.close();
 console.log(JSON.stringify({task_centric_landing:true,saved_task_source_result_states:true,deterministic_failure_next_action:true,no_blind_financial_retry:true,data_review:true,exact_numeric_strings:true,paginated_complete_result:true,frozen_source_evidence:true,untrusted_values_escaped:true,no_automatic_approval:true,optimistic_conflict_preserves_edits:true,review_digest_and_actor_scope:true,fact_quote_and_amount_review:true,fact_blockers_disable_approval:true,catalog_lazy_loading_and_pagination:true,catalog_full_definition_safe_text:true,catalog_explicit_owner_actions:true,catalog_double_submit_guard:true,catalog_failure_requires_fresh_read:true,catalog_binding_and_legacy_scope:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
