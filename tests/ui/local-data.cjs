// Contract tests for the actual offline data UI. No layout/OS claims.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const compatibility=fs.readFileSync(path.join(root,'local-data.html'),'utf8');
assert(compatibility.includes('content="0;url=/"'));assert(!compatibility.includes('id="upload"'));
const html='<!doctype html><html><body><main id="fixture"></main></body></html>';
const script=fs.readFileSync(path.join(root,'local-data.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<30;i++){await tick();if(check())return;}throw Error('UI did not settle');}
function response(value,status=200){return {ok:status>=200&&status<300,status,json:async()=>value,blob:async()=>new Blob(['complete,exact\nresult,118.15\n'])};}
const exact='1234567890123456.123456789012';
async function boot({publishConflict=false,delayPublish=false,token=true}={}){
 const dom=new JSDOM(html,{url:'http://127.0.0.1:8123/local-data.html',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 if(token)w.sessionStorage.setItem('atlas_token','local-test-owner-token');
 let sequence=0,pipeline=false,published=false,releasePublish,downloaded=null;
 w.setTimeout=()=>++sequence;w.clearTimeout=()=>{};
 w.URL.createObjectURL=()=> 'blob:http://127.0.0.1:8123/result';w.URL.revokeObjectURL=()=>{};
 w.HTMLAnchorElement.prototype.click=function(){downloaded={name:this.download,url:this.href};};
 const fields=[{name:'c_1',label:'唯一编号',numeric:false},{name:'c_2',label:'客户<img src=x onerror=alert(1)>',numeric:false},
  {name:'c_3',label:'金额<script>bad</script>',numeric:true},{name:'c_4',label:'退款',numeric:true},{name:'c_5',label:'状态',numeric:false}];
 const result={run_id:'r_1',manifest:{records:2,commit:'frozen-commit',fields,spec:{group_by:'c_2',amount_label:'净收入'},summary:[{category:'<img src=x>',amount_total:exact}]},
  rows:[{c_2:'<img src=x onerror=alert(1)>',c_3:exact}]};
 const listing=()=>pipeline?[{id:'p_1',name:'任务<script>bad</script>',candidate_run:'r_1',published_run:published?'r_1':null}]:[];
 w.fetch=async(url,options={})=>{
  const u=new URL(url,w.location.href),method=options.method||'GET';calls.push({path:u.pathname+u.search,method,options});
  assert.equal(options.headers.Authorization,'Bearer local-test-owner-token');
  if(u.pathname==='/api/desktop/runtime')return response({notice:'云端模型不可用，本地能力可用。'});
  if(u.pathname==='/api/pipelines'&&method==='GET')return response(listing());
  if(u.pathname==='/api/projects'&&method==='POST')return response({id:'project_1'});
  if(u.pathname==='/api/projects/project_1/attachments'&&method==='POST'){
   assert.equal(options.body.get('local_processing'),'true','offline import must select the backend no-model path');
   assert(options.body.get('file'));return response({id:'s_1',name:'input.csv'});
  }
  if(u.pathname==='/api/sources')return response([{id:'s_1',status:'ready',profile:{fields,sample_rows:3}}]);
  if(u.pathname==='/api/pipelines'&&method==='POST'){pipeline=true;return response({id:'p_1'});}
  if(u.pathname==='/api/pipelines/p_1/preview')return response(result);
  if(u.pathname==='/api/pipelines/p_1/result')return response(result);
  if(u.pathname==='/api/pipelines/p_1/publish'){
   assert.deepEqual(JSON.parse(options.body),{run_id:'r_1'},'publish must bind the exact previewed run');
   const finish=()=>{if(publishConflict)return response({detail:'候选已变化，请重新核对。'},409);published=true;return response({published_run:'r_1'});};
   return delayPublish?new Promise(resolve=>{releasePublish=()=>resolve(finish());}):finish();
  }
  if(u.pathname==='/api/pipelines/p_1/export')return response({});
  throw Error('Unexpected request (including any model/network path): '+method+' '+u.pathname);
 };
 w.eval(script);const view=w.createAtlasLocalData({root:d.querySelector('#fixture')});void view.open();await tick();await tick();
 return {dom,w,d,calls,release:()=>releasePublish(),downloaded:()=>downloaded};
}
function submit(app,id){app.d.querySelector(id).dispatchEvent(new app.w.Event('submit',{bubbles:true,cancelable:true}));}
async function importFile(app){
 Object.defineProperty(app.d.querySelector('#file'),'files',{configurable:true,value:[new app.w.File(['id,customer,amount,refund,status\n1,A,100,10,paid\n'],'input.csv',{type:'text/csv'})]});
 submit(app,'#upload');await until(()=>!app.d.querySelector('#rules').hidden&&!app.d.querySelector('#process-button').disabled);
}
async function preview(app){
 await importFile(app);
 for(const [key,value] of [['primary','c_1'],['group','c_2'],['amount','c_3'],['refund','c_4'],['filter-field','c_5']])app.d.querySelector('#'+key).value=value;
 app.d.querySelector('#filter-value').value='paid';submit(app,'#process');
 await until(()=>!app.d.querySelector('#result').hidden&&!app.d.querySelector('#publish').disabled);
}
(async()=>{
 const empty=await boot();submit(empty,'#upload');await until(()=>empty.d.querySelector('#status').textContent.includes('非空'));
 assert.equal(empty.calls.filter(c=>c.method==='POST').length,0,'empty input must not create an orphan project');empty.dom.window.close();
 const large=await boot();Object.defineProperty(large.d.querySelector('#file'),'files',{configurable:true,value:[{name:'huge.csv',size:25*1024*1024+1}]});
 submit(large,'#upload');await until(()=>large.d.querySelector('#status').textContent.includes('25MB'));assert.equal(large.calls.filter(c=>c.method==='POST').length,0);large.dom.window.close();
 const invalid=await boot();await importFile(invalid);invalid.d.querySelector('#group').value='c_2';submit(invalid,'#process');
 await until(()=>invalid.d.querySelector('#status').textContent.includes('同时选择'));assert.equal(invalid.calls.filter(c=>c.path==='/api/pipelines'&&c.method==='POST').length,0);invalid.dom.window.close();
 const app=await boot({delayPublish:true});await preview(app);
 const created=app.calls.find(c=>c.path==='/api/pipelines'&&c.method==='POST'),body=JSON.parse(created.options.body);
 assert.equal(body.release_mode,'manual');assert.equal(body.spec.amount_field,'m_net');
 assert.deepEqual(body.spec.computations,[{name:'m_net',label:'净金额',operation:'subtract',left_field:'c_3',right_field:'c_4'}]);
 assert.deepEqual(body.spec.filters,[{field:'c_5',operator:'eq',data_type:'text',value:'paid'}]);
 assert.equal(app.calls.some(c=>c.path.endsWith('/publish')),false,'preview alone must never publish');
 assert.equal(app.d.querySelector('#download').hidden,true);assert.equal(app.d.querySelector('#publish').hidden,false);
 assert.equal(app.d.querySelectorAll('#table img,#table script,#history script,#group img').length,0,'untrusted cells/labels/names must remain text');
 assert(app.d.querySelector('#table').textContent.includes(exact),'decimal strings must not be coerced to Number');
 const summary=app.d.querySelector('#summary').textContent;assert(summary.includes('净收入：'+exact));assert(summary.includes('客户<img src=x onerror=alert(1)>：'));assert(!summary.includes('amount_total')&&!summary.includes('category'));assert.equal(app.d.querySelectorAll('#summary img').length,0);
 app.d.querySelector('#publish').click();await tick();app.d.querySelector('#publish').click();
 assert.equal(app.calls.filter(c=>c.path.endsWith('/publish')).length,1,'concurrent publish clicks must produce one request');
 app.release();await until(()=>!app.d.querySelector('#download').hidden&&!app.d.querySelector('#download').disabled);
 app.d.querySelector('#download').click();await until(()=>app.downloaded());assert.equal(app.downloaded().name,'Atlas-完整结果.csv');
 assert.equal(app.calls.filter(c=>c.path.endsWith('/export')).length,1);app.dom.window.close();
 const conflict=await boot({publishConflict:true});await preview(conflict);conflict.d.querySelector('#publish').click();
 await until(()=>conflict.d.querySelector('#status').textContent.includes('候选已变化'));
 assert.equal(conflict.d.querySelector('#download').hidden,true);assert.equal(conflict.calls.filter(c=>c.path.endsWith('/publish')).length,1,'conflicts must not retry automatically');
 assert.equal(conflict.calls.some(c=>c.path.endsWith('/result')),false,'conflict cannot display a false successful publication');conflict.dom.window.close();
 const unauthenticated=await boot({token:false});assert.equal(unauthenticated.calls.length,0);assert(unauthenticated.d.querySelector('#status').textContent.includes('工作空间'));unauthenticated.dom.window.close();
 console.log(JSON.stringify({local_import_no_model_path:true,empty_and_oversize_blocked:true,explicit_processing_choices:true,manual_frozen_publication:true,concurrent_publish_guard:true,conflict_not_retried:true,untrusted_values_are_text:true,exact_decimal_strings:true,full_export:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
