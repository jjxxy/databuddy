// The real shared component: picker changes, partial failures and source identity.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const script=fs.readFileSync(path.resolve(__dirname,'../../agent_service/static/local-data.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){const end=Date.now()+6000;while(Date.now()<end){if(check())return;await new Promise(resolve=>setTimeout(resolve,5))}throw Error('Batch UI did not settle')}
const reply=value=>({ok:true,status:200,json:async()=>value});
async function boot({probeFailed=false,insecure=false,transient=false,persistentPoll=false,retryAfter='0.001'}={}){
 const dom=new JSDOM('<main id="view"></main>',{url:insecure?'http://192.168.1.7/':'https://atlas.fixture/',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[],sources=[];let fail=true,delay=false,release,previewSource,uploadFailures=0,pollFailures=0,historyFailures=0;
 if(insecure){Object.defineProperty(w.crypto,'randomUUID',{value:undefined});w.eval(fs.readFileSync(path.resolve(__dirname,'../../agent_service/static/secure-id.js'),'utf8'))}
 w.sessionStorage.setItem('atlas_token','batch-test-token');
 w.fetch=async(url,options={})=>{
  const method=options.method||'GET';calls.push({url,method,options});
  if(url==='/api/desktop/runtime')return reply({notice:'本地处理'});
  if(url==='/api/pipelines'&&method==='GET'&&transient&&sources.length&&!historyFailures++){return {ok:false,status:503,headers:{get:()=>retryAfter},json:async()=>({detail:'网关繁忙'})}}
  if(url==='/api/pipelines'&&method==='GET')return reply(previewSource?[{id:'pipe',project_id:'p',candidate_run:'run'}]:[]);
  if(url==='/api/projects/p/attachments'){
   const file=options.body.get('file');assert.equal(options.body.get('local_processing'),'true');
   if(transient&&!uploadFailures++)return {ok:false,status:503,headers:{get:()=>retryAfter},json:async()=>({detail:'网关繁忙'})};
   if(delay)await new Promise(resolve=>release=resolve);
   if(file.size===2&&fail)throw Error('暂时无法上传第二份文件');
   const id='s_'+file.size;const existing=sources.find(s=>s.id===id);if(existing)existing.status='ready';if(!existing)sources.push({id,name:file.name,status:'ready',profile:{sample_rows:file.size,fields:[{name:'field_'+file.size,label:'字段'+file.size,numeric:true}]}});
   if(probeFailed&&file.size===1){sources.find(s=>s.id===id).status='failed';probeFailed=false}return reply({id,name:file.name});
  }
  if(url==='/api/sources'&&(transient||persistentPoll)&&pollFailures++<(persistentPoll?3:1))return {ok:false,status:429,headers:{get:()=>retryAfter},json:async()=>({detail:'稍后重试'})};
  if(url==='/api/sources')return reply(sources);
  if(url==='/api/pipelines'&&method==='POST'){previewSource=JSON.parse(options.body).source_id;return reply({id:'pipe'})}
  if(url==='/api/pipelines/pipe/preview')return reply({run_id:'run',manifest:{fields:[],summary:[],records:1},rows:[]});
  throw Error('Unexpected '+method+' '+url);
 };
 w.eval(script);const view=w.createAtlasLocalData({root:d.querySelector('#view'),prefix:'batch-',projectId:()=> 'p'});await view.open();
 return {dom,w,d,calls,view,sources,fail:value=>fail=value,delay:()=>delay=true,waiting:()=>!!release,release:()=>release?.()};
}
function choose(app,files){Object.defineProperty(app.d.querySelector('#batch-file'),'files',{configurable:true,value:files});app.d.querySelector('#batch-file').dispatchEvent(new app.w.Event('change'))}
function submit(app,id='upload'){app.d.querySelector('#batch-'+id).dispatchEvent(new app.w.Event('submit',{cancelable:true}))}
const uploads=app=>app.calls.filter(call=>call.url.endsWith('/attachments'));
(async()=>{
 const app=await boot({insecure:true});try{
  assert.equal(app.d.querySelector('#batch-file').multiple,true);
  const a=new app.w.File(['a'],'同名.csv'),b=new app.w.File(['bb'],'同名.csv'),c=new app.w.File(['ccc'],'<img src=x>.csv');
  choose(app,[a,b,c]);assert(app.d.querySelector('#batch-selection-count').textContent.includes('已选3个'));assert.equal(uploads(app).length,0,'selection does not upload');
  submit(app);await until(()=>!app.view.isBusy());assert.equal(uploads(app).length,3,'later files still import after a failure');
  assert.equal(app.sources.length,2);assert.equal(app.d.querySelectorAll('#batch-source-select option').length,2);assert(app.d.querySelector('#batch-status').textContent.includes('已导入2个文件，1个未完成'));
  assert.equal(app.d.querySelectorAll('#batch-import-files img').length,0,'file names remain text');assert(app.d.querySelector('#batch-import-files').textContent.includes('<img src=x>'));
  const retryId=uploads(app)[1].options.body.get('request_id');app.fail(false);submit(app);await until(()=>!app.view.isBusy());
  assert.equal(uploads(app).length,4,'successful files are not uploaded again');assert.equal(uploads(app)[3].options.body.get('request_id'),retryId,'failed upload keeps its idempotency key');
  assert.equal(new Set(uploads(app).map(call=>call.options.body.get('request_id'))).size,3,'same-name files have separate identities');
  const selector=app.d.querySelector('#batch-source-select');selector.value='s_3';selector.dispatchEvent(new app.w.Event('change'));
  assert.equal(app.d.querySelector('#batch-amount option[value="field_3"]').textContent,'字段3');assert.equal(app.d.querySelector('#batch-amount option[value="field_1"]'),null);
  submit(app,'process');await until(()=>!app.view.isBusy());assert.equal(JSON.parse(app.calls.find(call=>call.url==='/api/pipelines'&&call.method==='POST').options.body).source_id,'s_3','processing uses selected file, not first file');
  selector.value='s_2';selector.dispatchEvent(new app.w.Event('change'));assert.equal(app.d.querySelector('#batch-result').hidden,true,'a previous source result cannot be published under another source');
  app.view.projectChanged();assert.equal(app.d.querySelectorAll('#batch-source-select option').length,0);assert.equal(app.d.querySelector('#batch-import-files').textContent,'');
 }finally{app.dom.window.close()}
 const late=await boot();try{
  const input=late.d.querySelector('#batch-file');input.dispatchEvent(new late.w.Event('click'));
  late.view.projectChanged();await late.view.open();choose(late,[new late.w.File(['x'],'stale.csv')]);
  assert.equal(late.d.querySelector('#batch-import-files').textContent,'','late picker result cannot enter a reopened project');
  input.dispatchEvent(new late.w.Event('click'));late.view.close();choose(late,[new late.w.File(['x'],'closed.csv')]);
  assert.equal(late.d.querySelector('#batch-import-files').textContent,'','late picker result cannot enter a closed view');
 }finally{late.dom.window.close()}
 const appended=await boot();try{
  const a=new appended.w.File(['a'],'one.csv'),b=new appended.w.File(['bb'],'two.csv'),c=new appended.w.File(['ccc'],'three.csv');
  choose(appended,[a]);choose(appended,[b]);assert(appended.d.querySelector('#batch-selection-count').textContent.includes('已选2个'),'separate single-file selections accumulate');
  submit(appended);await until(()=>!appended.view.isBusy());assert.equal(uploads(appended).length,2);const failedId=uploads(appended)[1].options.body.get('request_id');
  assert(appended.d.querySelector('#batch-import-files').textContent.includes('one.csv'));assert(appended.d.querySelector('#batch-import-files').textContent.includes('two.csv'));
  choose(appended,[c]);assert(appended.d.querySelector('#batch-selection-count').textContent.includes('已选3个'));assert.equal(appended.d.querySelectorAll('#batch-source-select option').length,1,'adding files does not lose imported sources');
  appended.fail(false);submit(appended);await until(()=>!appended.view.isBusy());assert.equal(uploads(appended).length,4,'previously imported file is not uploaded again');assert.equal(uploads(appended)[2].options.body.get('request_id'),failedId,'failed file keeps its ID after another selection');assert.equal(appended.sources.length,3);
  const remove=appended.d.querySelector('[aria-label="移除 one.csv"]');remove.click();assert.equal(appended.d.querySelectorAll('#batch-source-select option').length,2);assert.equal(appended.d.querySelector('#batch-source-select').value,'s_2','removing active source switches to another saved source');
  assert(appended.d.querySelector('#batch-selection-count').textContent.includes('已选2个'));assert.equal(appended.sources.length,3,'removing a selection does not delete imported sources');
 }finally{appended.dom.window.close()}
 for(const files of [w=>Array.from({length:21},()=>new w.File(['x'],'f.csv')),w=>[new w.File(['x'],'valid.csv'),new w.File([],'empty.csv')],w=>[{name:'valid.csv',size:1},{name:'large.csv',size:25*1024*1024+1}]]){
  const app=await boot();try{choose(app,files(app.w));submit(app);await until(()=>!app.view.isBusy());assert.equal(uploads(app).length,0,'entire selection validated before any upload')}finally{app.dom.window.close()}
 }
 const busyGateway=await boot({transient:true,insecure:true});try{
  busyGateway.fail(false);choose(busyGateway,[new busyGateway.w.File(['x'],'first.csv'),new busyGateway.w.File(['xx'],'second.csv')]);submit(busyGateway);await until(()=>!busyGateway.view.isBusy());
  assert.equal(uploads(busyGateway).length,3,'one automatic upload retry plus second file');
  assert.equal(uploads(busyGateway)[0].options.body.get('request_id'),uploads(busyGateway)[1].options.body.get('request_id'),'gateway retry retains idempotency key');
  assert.equal(busyGateway.sources.length,2);assert.equal(busyGateway.d.querySelectorAll('#batch-source-select option').length,2);
  assert.equal(busyGateway.calls.filter(call=>call.url==='/api/sources').length,3,'429 during source probe is retried without re-uploading');
  assert(busyGateway.calls.filter(call=>call.url==='/api/pipelines'&&call.method==='GET').length>=3,'503 while refreshing history is retried');
  assert(busyGateway.d.querySelector('#batch-status').textContent.includes('2个文件已保存在本机'));
 }finally{busyGateway.dom.window.close()}
 const exhaustedPoll=await boot({persistentPoll:true});try{
  exhaustedPoll.fail(false);choose(exhaustedPoll,[new exhaustedPoll.w.File(['x'],'probe.csv')]);submit(exhaustedPoll);await until(()=>!exhaustedPoll.view.isBusy());
  assert.equal(uploads(exhaustedPoll).length,1);assert(exhaustedPoll.d.querySelector('#batch-status').textContent.includes('1个未完成'));
  submit(exhaustedPoll);await until(()=>!exhaustedPoll.view.isBusy());assert.equal(uploads(exhaustedPoll).length,1,'source status retry must not re-upload a successfully stored file');
  assert.equal(exhaustedPoll.d.querySelector('#batch-rules').hidden,false);
 }finally{exhaustedPoll.dom.window.close()}
 const staleBackoff=await boot({transient:true,retryAfter:'0.1'});try{
  staleBackoff.fail(false);choose(staleBackoff,[new staleBackoff.w.File(['x'],'private.csv'),new staleBackoff.w.File(['xx'],'other.csv')]);submit(staleBackoff);await until(()=>uploads(staleBackoff).length===1);
  staleBackoff.view.projectChanged();await new Promise(resolve=>setTimeout(resolve,150));
  assert.equal(uploads(staleBackoff).length,1,'changing project during Retry-After cancels retry and subsequent uploads');assert.equal(staleBackoff.d.querySelector('#batch-import-files').textContent,'');
 }finally{staleBackoff.dom.window.close()}
 const probe=await boot({probeFailed:true});try{
  choose(probe,[new probe.w.File(['x'],'probe.csv')]);submit(probe);await until(()=>!probe.view.isBusy());assert.equal(probe.d.querySelector('#batch-rules').hidden,true);
  const request=uploads(probe)[0].options.body.get('request_id');submit(probe);await until(()=>!probe.view.isBusy());assert.equal(uploads(probe).length,2,'failed probe is re-enqueued via the idempotent attachment endpoint');assert.equal(uploads(probe)[1].options.body.get('request_id'),request);assert.equal(probe.d.querySelector('#batch-rules').hidden,false);
 }finally{probe.dom.window.close()}
 const stale=await boot();try{
  stale.delay();choose(stale,[new stale.w.File(['x'],'private.csv'),new stale.w.File(['xx'],'other.csv')]);submit(stale);await until(stale.waiting);
  assert.equal(stale.d.querySelector('#batch-file').disabled,true);assert.equal(stale.d.querySelector('#batch-source-select').disabled,true);
  stale.view.reset();stale.release();await tick();await tick();assert.equal(uploads(stale).length,1,'logout stops batch before next file');assert.equal(stale.d.querySelector('#batch-import-files').textContent,'');assert.equal(stale.d.querySelector('#batch-rules').hidden,true);
 }finally{stale.dom.window.close()}
 console.log(JSON.stringify({http_429_503_retry_for_upload_poll_and_history:true,poll_failure_never_reuploads_success:true,project_change_cancels_backoff:true,native_multiple_attribute:true,selection_lists_all_files_without_uploading:true,batch_continues_after_partial_failure:true,retry_preserves_ids_and_successful_files:true,same_names_retained:true,processing_uses_selected_source:true,invalid_batch_blocked_before_upload:true,logout_discards_late_batch_and_stops_next_file:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
