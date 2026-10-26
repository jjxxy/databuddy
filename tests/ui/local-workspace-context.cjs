// Boots the actual web app and shared local view; no Electron or layout claim.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<100;i++){await tick();if(check())return;}throw Error('Workspace context did not settle');}
const reply=(value,status=200)=>({ok:status<400,status,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value)),blob:async()=>new Blob(['exact'])});
async function boot({folder=true,delayPreview=false,otherProjectHistory=false}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://127.0.0.1:8123/',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','alice-owner-token');w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 let pipeline=false,releasePreview,reloads=0;
 const fields=[{name:'c_1',label:'唯一编号',numeric:false},{name:'c_2',label:'客户',numeric:false},{name:'c_3',label:'金额',numeric:true}];
 w.fetch=async(url,options={})=>{
  const route=new URL(url,w.location.href).pathname,method=options.method||'GET';calls.push({route,method,options});
  assert.equal(options.headers?.Authorization,'Bearer alice-owner-token');
  if(route==='/api/models')return reply({models:[],default_model_id:null,configured:false});
  if(route==='/api/projects'&&method==='GET')return reply([{id:'p_1',name:'项目一'},{id:'p_2',name:'项目二'}]);
  const conversation=route.match(/^\/api\/projects\/(p_[12])\/conversation$/);
  if(conversation)return reply({project:{id:conversation[1],name:conversation[1]==='p_1'?'项目一':'项目二'},session_id:'session_1',sessions:[{id:'session_1',name:'对话'}],turns:[],sources:[],applications:[],mode:'rules',has_more:false});
  if(route==='/api/local-workspace')return reply({enabled:folder,label:folder?'真实选择的业务文件夹':null});
  if(route==='/api/desktop/runtime')return reply({workspace_location:folder?'local':'unbound',cloud_workspace_configured:false,notice:'本地能力可用，云端 AI 断网时会明确提示。'});
  if(route==='/api/pipelines'&&method==='GET')return reply(pipeline?[{id:'pipe_1',project_id:'p_1',name:'已保存候选',candidate_run:'run_1'}]:otherProjectHistory?[{id:'pipe_other',project_id:'p_2',name:'其他项目任务',published_run:'run_other'}]:[]);
  if(route==='/api/projects/p_1/attachments'){
   assert.equal(options.body.get('local_processing'),'true');return reply({id:'source_1',name:'input.csv'});
  }
  if(route==='/api/sources')return reply([{id:'source_1',project_id:'p_1',status:'ready',profile:{fields,sample_rows:2}}]);
  if(route==='/api/pipelines'&&method==='POST'){pipeline=true;return reply({id:'pipe_1',project_id:'p_1'});}
  if(route==='/api/pipelines/pipe_1/preview'){
   if(delayPreview)await new Promise(resolve=>releasePreview=resolve);
   return reply({run_id:'run_1',manifest:{records:1,commit:'frozen',fields,summary:[]},rows:[{c_1:'OLD_OWNER_SECRET',c_3:'1234567890123456.123456789012'}]});
  }
  throw Error('Unexpected web route '+method+' '+route);
 };
 w.eval(fs.readFileSync(path.join(root,'local-data.js'),'utf8'));
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.contextTest={seal:sealAuthentication,setMode,api};window.contextReload=fn=>{reloadAuthDocument=fn};');
 w.contextReload(()=>{reloads++;});
 await until(()=>!d.querySelector('#shell').hidden&&w.atlasBusiness.getState().sessionId==='session_1');await tick();await tick();
 return {dom,w,d,calls,release:()=>releasePreview(),waiting:()=>!!releasePreview,reloads:()=>reloads};
}
async function openLocal(app){
 const selector=app.d.querySelector('#work-environment');selector.value='local';selector.dispatchEvent(new app.w.Event('change'));
 await until(()=>app.d.querySelector('#atlas-local-workspace-panel').open&&!app.d.querySelector('#atlas-local-workspace-content').hidden&&!app.d.querySelector('#atlas-local-upload-button').disabled);
}
async function importLocal(app){
 Object.defineProperty(app.d.querySelector('#atlas-local-file'),'files',{configurable:true,value:[new app.w.File(['id,customer,amount\n1,A,12.34\n'],'input.csv',{type:'text/csv'})]});
 app.d.querySelector('#atlas-local-upload').dispatchEvent(new app.w.Event('submit',{cancelable:true}));
 await until(()=>!app.d.querySelector('#atlas-local-rules').hidden&&!app.d.querySelector('#atlas-local-process-button').disabled);
}
(async()=>{
 const app=await boot({delayPreview:true,otherProjectHistory:true});
 try{
  assert.equal(app.w.atlasDesktop,undefined,'core web integration must not need an Electron bridge');
  assert(app.d.querySelector('#composer #work-environment'),'location choice belongs to work mode');
  assert.equal(app.d.querySelector('.header-actions #work-environment'),null);
  assert.equal(app.d.querySelector('#work-environment option[value="cloud"]').disabled,true);
  assert(app.d.querySelector('#work-environment option[value="cloud"]').textContent.includes('后续阶段'));
  await openLocal(app);assert(app.d.querySelector('#atlas-local-workspace-scope').textContent.includes('真实选择'));
  assert.equal(app.d.querySelector('#atlas-local-project-context').textContent,'当前项目：项目一');
  assert(app.d.querySelector('#atlas-local-history').textContent.includes('此项目还没有本地任务'));assert(!app.d.querySelector('#atlas-local-history').textContent.includes('其他项目任务'));
  assert(app.d.querySelector('.empty-state .eyebrow').textContent.includes('你的业务工作空间'));
  assert.equal(app.d.querySelectorAll('#file').length,1,'chat attachment control retains its original ID');
  assert.equal(app.d.querySelectorAll('#atlas-local-file').length,1,'embedded controls have scoped IDs');
  await importLocal(app);
  assert.equal(app.calls.some(call=>call.route==='/api/projects'&&call.method==='POST'),false,'local processing reuses the current project');
  app.d.querySelector('#atlas-local-name').value='PRIVATE ALICE DRAFT';
  app.d.querySelector('#atlas-local-process').dispatchEvent(new app.w.Event('submit',{cancelable:true}));await until(app.waiting);
  const pending=app.calls.find(call=>call.route.endsWith('/preview'));
  app.w.contextTest.seal();app.w.sessionStorage.setItem('atlas_token','bob-owner-token');
  assert.equal(app.d.querySelector('#atlas-local-workspace-panel').open,false);
  assert.equal(app.d.querySelector('#atlas-local-project-context').textContent,'');
  assert.equal(pending.options.signal.aborted,true,'authentication seal aborts local component requests');
  assert.equal(app.d.querySelector('#atlas-local-name').value,'本地数据核对');
  app.release();await tick();await tick();
  assert.equal(app.d.querySelector('#atlas-local-table').textContent,'','late old-owner response cannot render');
  assert(!app.d.body.textContent.includes('OLD_OWNER_SECRET'));
  const before=app.calls.length;await app.w.atlasLocalData.open();assert.equal(app.calls.length,before,'sealed old controller cannot open with a new token');
  assert(app.calls.every(call=>call.options.headers.Authorization==='Bearer alice-owner-token'));
 }finally{app.dom.window.close();}
 const changed=await boot();
 try{
  await openLocal(changed);await importLocal(changed);await changed.w.atlasBusiness.openProject('p_2');
  assert.equal(changed.d.querySelector('#atlas-local-workspace-panel').open,false);
  assert.equal(changed.d.querySelector('#atlas-local-rules').hidden,true);assert.equal(changed.d.querySelector('#atlas-local-project-context').textContent,'');
  await openLocal(changed);assert.equal(changed.d.querySelector('#atlas-local-project-context').textContent,'当前项目：项目二');changed.d.querySelector('#atlas-local-process').dispatchEvent(new changed.w.Event('submit',{cancelable:true}));
  await until(()=>changed.d.querySelector('#atlas-local-status').textContent.includes('先导入'));
  assert.equal(changed.calls.some(call=>call.route==='/api/pipelines'&&call.method==='POST'),false,'a source from the previous project cannot be processed in the new context');
  changed.w.contextTest.setMode('chat');assert.equal(changed.d.querySelector('#work-environment-control').hidden,true);assert.equal(changed.d.querySelector('#atlas-local-workspace-panel').open,false);
  changed.w.contextTest.setMode('work');assert.equal(changed.d.querySelector('#work-environment-control').hidden,false);
 }finally{changed.dom.window.close();}
 const unbound=await boot({folder:false});
 try{
  const selector=unbound.d.querySelector('#work-environment');selector.value='local';selector.dispatchEvent(new unbound.w.Event('change'));
  await until(()=>unbound.d.querySelector('#atlas-local-workspace-scope').textContent.includes('尚未选择'));
  assert.equal(unbound.d.querySelector('#atlas-local-workspace-content').hidden,true,'no fake folder selection permits processing');
  assert.equal(unbound.d.querySelector('#atlas-local-choose-folder').hidden,true,'plain browser cannot pretend it has a native directory chooser');
  assert.equal(unbound.calls.some(call=>call.method==='POST'),false,'choosing an environment never uploads or moves files');
 }finally{unbound.dom.window.close();}
 console.log(JSON.stringify({web_work_mode_context:true,no_electron_required_for_bound_backend:true,real_folder_status_required:true,cloud_future_stage_blocked:true,current_project_reused:true,prefixed_controls:true,project_switch_resets_sources:true,auth_seal_aborts_and_rejects_late_results:true,no_old_token_under_new_account:true,no_automatic_upload:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1;});
