// The real shared component: picker changes, partial failures and source identity.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const script=fs.readFileSync(path.resolve(__dirname,'../../agent_service/static/local-data.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<100;i++){await tick();if(check())return}throw Error('Batch UI did not settle')}
const reply=value=>({ok:true,status:200,json:async()=>value});
async function boot({probeFailed=false}={}){
 const dom=new JSDOM('<main id="view"></main>',{url:'https://atlas.fixture/',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[],sources=[];let fail=true,delay=false,release,previewSource;
 w.sessionStorage.setItem('atlas_token','batch-test-token');
 w.fetch=async(url,options={})=>{
  const method=options.method||'GET';calls.push({url,method,options});
  if(url==='/api/desktop/runtime')return reply({notice:'本地处理'});
  if(url==='/api/pipelines'&&method==='GET')return reply(previewSource?[{id:'pipe',project_id:'p',candidate_run:'run'}]:[]);
  if(url==='/api/projects/p/attachments'){
   const file=options.body.get('file');assert.equal(options.body.get('local_processing'),'true');
   if(delay)await new Promise(resolve=>release=resolve);
   if(file.size===2&&fail)throw Error('暂时无法上传第二份文件');
   const id='s_'+file.size;const existing=sources.find(s=>s.id===id);if(existing)existing.status='ready';if(!existing)sources.push({id,name:file.name,status:'ready',profile:{sample_rows:file.size,fields:[{name:'field_'+file.size,label:'字段'+file.size,numeric:true}]}});
   if(probeFailed&&file.size===1){sources.find(s=>s.id===id).status='failed';probeFailed=false}return reply({id,name:file.name});
  }
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
 const app=await boot();try{
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
 for(const files of [w=>Array.from({length:21},()=>new w.File(['x'],'f.csv')),w=>[new w.File(['x'],'valid.csv'),new w.File([],'empty.csv')],w=>[{name:'valid.csv',size:1},{name:'large.csv',size:25*1024*1024+1}]]){
  const app=await boot();try{choose(app,files(app.w));submit(app);await until(()=>!app.view.isBusy());assert.equal(uploads(app).length,0,'entire selection validated before any upload')}finally{app.dom.window.close()}
 }
 const probe=await boot({probeFailed:true});try{
  choose(probe,[new probe.w.File(['x'],'probe.csv')]);submit(probe);await until(()=>!probe.view.isBusy());assert.equal(probe.d.querySelector('#batch-rules').hidden,true);
  const request=uploads(probe)[0].options.body.get('request_id');submit(probe);await until(()=>!probe.view.isBusy());assert.equal(uploads(probe).length,2,'failed probe is re-enqueued via the idempotent attachment endpoint');assert.equal(uploads(probe)[1].options.body.get('request_id'),request);assert.equal(probe.d.querySelector('#batch-rules').hidden,false);
 }finally{probe.dom.window.close()}
 const stale=await boot();try{
  stale.delay();choose(stale,[new stale.w.File(['x'],'private.csv'),new stale.w.File(['xx'],'other.csv')]);submit(stale);await until(stale.waiting);
  assert.equal(stale.d.querySelector('#batch-file').disabled,true);assert.equal(stale.d.querySelector('#batch-source-select').disabled,true);
  stale.view.reset();stale.release();await tick();await tick();assert.equal(uploads(stale).length,1,'logout stops batch before next file');assert.equal(stale.d.querySelector('#batch-import-files').textContent,'');assert.equal(stale.d.querySelector('#batch-rules').hidden,true);
 }finally{stale.dom.window.close()}
 console.log(JSON.stringify({native_multiple_attribute:true,selection_lists_all_files_without_uploading:true,batch_continues_after_partial_failure:true,retry_preserves_ids_and_successful_files:true,same_names_retained:true,processing_uses_selected_source:true,invalid_batch_blocked_before_upload:true,logout_discards_late_batch_and_stops_next_file:true}));
})().catch(error=>{console.error(error);process.exitCode=1});
