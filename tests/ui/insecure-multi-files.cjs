// Real application + lifecycle in an HTTP non-secure-context simulation.
// The low-concurrency gateway, polling and responses are controlled fixtures.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),vm=require('node:vm');
const root=path.resolve(__dirname,'../../agent_service/static');
const read=name=>fs.readFileSync(path.join(root,name),'utf8');
const until=async condition=>{const end=Date.now()+6000;while(!condition()){if(Date.now()>end)throw Error('HTTP multi-file scenario timed out');await new Promise(r=>setTimeout(r,5))}};
const valid=/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
function withoutNativeUUID(w){Object.defineProperty(w.crypto,'randomUUID',{value:undefined,configurable:true});Object.defineProperty(w,'isSecureContext',{value:false,configurable:true})}
function verifyFallback(){
 const dom=new JSDOM('',{url:'http://192.168.1.7/',runScripts:'outside-only'}),w=dom.window;
 try{
  withoutNativeUUID(w);w.eval(read('secure-id.js'));
  const ids=Array.from({length:1000},()=>w.atlasSecureUUID());
  assert(ids.every(value=>valid.test(value)));assert.equal(new Set(ids).size,ids.length);
 }finally{dom.window.close()}
 let directCalls=0,randomCalls=0;
 const native={crypto:{randomUUID(){directCalls++;return 'existing-native-id'},getRandomValues(){randomCalls++;throw Error('unneeded')}}};
 vm.runInNewContext(read('secure-id.js'),{window:native});assert.equal(native.atlasSecureUUID(),'existing-native-id');assert.equal(directCalls,1);assert.equal(randomCalls,0);
 const noEntropy={crypto:{}};vm.runInNewContext(read('secure-id.js'),{window:noEntropy});assert.throws(()=>noEntropy.atlasSecureUUID(),/安全的请求标识/);
}
async function scenario(mode,status,persistent=false){
 const dom=new JSDOM(read('index.html'),{url:'http://192.168.1.7:8000/',runScripts:'outside-only'}),w=dom.window,d=w.document;
 try{
  withoutNativeUUID(w);w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;
  w.sessionStorage.setItem('atlas_token','fixture-only');let activeUploads=0,maxUploads=0,revision=0;
  const uploads=[],posts=[],registered=[],failures=new Map();
  const response=(value,code=200,retry=false)=>({ok:code<400,status:code,headers:{get:name=>name.toLowerCase()==='retry-after'&&retry?'0.001':null},json:async()=>value});
  w.fetch=async(url,options={})=>{
   const u=new URL(url,w.location.href),route=u.pathname,method=options.method||'GET';let value={};
   if(route==='/api/browser/register'){const input=JSON.parse(options.body);registered.push(input.device_id);value={desktop:false,quiet:false}}
   else if(route==='/api/models')value={models:[{id:'fixture',name:'业务模型'}],default_model_id:'fixture'};
   else if(route==='/api/projects')value=[{id:'p',name:'测试项目'}];
   else if(route.endsWith('/conversation'))value={project:{id:'p',name:'测试项目'},session_id:'c',sessions:[{id:'c',name:'对话'}],turns:[],sources:[],applications:[],mode:'llm'};
   else if(route.endsWith('/view')&&method==='GET')value={revision:0,draft:'',pending:null,at_bottom:true};
   else if(route.endsWith('/view')&&method==='PUT'){revision++;value={revision,draft_saved:true}}
   else if(route==='/api/activity')value={events:[],unread:[],unread_count:0,cursor:1,active:[],active_count:0,services:{agent:true,worker:true}};
   else if(route.endsWith('/attachments')){
    const file=options.body.get('file'),id=options.body.get('request_id');uploads.push({name:file.name,id});assert(valid.test(id));
    // One of two gateway slots is occupied by a background poll. Reject any
    // overlapping second upload, as the isolated production gateway does.
    if(activeUploads>=1)return response({detail:'网关繁忙'},503,true);
    if(file.name==='first.csv'&&(failures.get(id)||0)<(persistent?3:1)){failures.set(id,(failures.get(id)||0)+1);return response({detail:'网关繁忙'},status,true)}
    activeUploads++;maxUploads=Math.max(maxUploads,activeUploads);
    await new Promise(resolve=>setTimeout(resolve,15));activeUploads--;
    value={id:'source_'+file.name,name:file.name};
   }
   else if(route.endsWith('/messages')){posts.push(JSON.parse(options.body));value={id:'turn_1'}}
   else throw Error('Unexpected route '+route+' '+method);
   return response(value);
  };
  w.eval(read('secure-id.js'));w.eval(read('lifecycle.js')+';window.createAtlasLifecycle=createAtlasLifecycle;');
  w.eval(read('app.js')+';window.integrationTest={send,state:()=>({projectId,sessionId,sending,attached,pending})};');
  await until(()=>w.integrationTest.state().sessionId==='c'&&!d.querySelector('#shell').hidden);
  assert.equal(registered.length,1);assert(/^d_[a-f0-9]{32}$/.test(registered[0]));
  d.querySelector('[data-mode='+mode+']').click();
  const files=['first.csv','second.csv','third.csv'].map((name,i)=>new w.File([String(i)],name));
  d.querySelector('#attach').click();const picker=d.querySelector('#file');assert(picker.multiple);
  Object.defineProperty(picker,'files',{configurable:true,value:files});picker.dispatchEvent(new w.Event('change'));
  assert.equal(d.querySelectorAll('.attachment-chip').length,3,'all selected files enter the composer');
  d.querySelector('#message').value='处理这些文件';await w.integrationTest.send();
  if(persistent){
   assert.equal(posts.length,0,'never submit a partial message');assert.equal(uploads.length,5);
   assert.equal(w.integrationTest.state().attached[0].status,'failed');
   assert.equal(w.integrationTest.state().attached[1].source_id,'source_second.csv');assert.equal(w.integrationTest.state().attached[2].source_id,'source_third.csv');
   d.querySelector('[data-retry-attachment="0"]').click();await until(()=>!w.integrationTest.state().sending);
   await w.integrationTest.send();assert.equal(uploads.length,6);assert.equal(uploads[5].id,uploads[0].id,'manual retry preserves original id');
  }else assert.equal(uploads.length,4);
  assert.equal(posts.length,1);assert.equal(posts[0].interaction_mode,mode);assert.deepEqual(posts[0].source_ids,files.map(f=>'source_'+f.name));
  assert(valid.test(posts[0].request_id));assert.equal(maxUploads,1);
  assert.equal(uploads[0].name,'first.csv');assert.equal(uploads[1].name,'first.csv');assert.equal(uploads[0].id,uploads[1].id,'retry must use the same upload receipt');
  assert.equal(new Set(uploads.map(row=>row.id)).size,3);
  assert.equal(w.integrationTest.state().attached.length,0);
 }finally{dom.window.close()}
}
(async()=>{verifyFallback();await scenario('chat',503);await scenario('work',429);await scenario('work',503,true);console.log(JSON.stringify({http_nonsecure_randomuuid_missing:true,secure_random_v4_fallback:true,lifecycle_and_both_modes_boot:true,three_selected_files_submitted:true,gateway_two_slot_with_background_poll:true,one_upload_at_a_time:true,retry_503_and_429_same_id:true,persistent_failure_preserves_successes_and_manual_retry:true,actual_windows_shell_or_live_deployment:false}));})().catch(error=>{console.error(error);process.exitCode=1});
