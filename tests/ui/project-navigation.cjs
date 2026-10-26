// Actual app navigation: delayed dialogs must stay bound to their project.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<100;i++){await tick();if(check())return}throw Error('project navigation did not settle')}
const reply=value=>({ok:true,status:200,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
async function boot({empty=false}={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','fixture-owner');w.setInterval=()=>0;w.matchMedia=()=>({matches:false,addEventListener(){}});w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 let release,delayed='';
 w.fetch=async(url,options={})=>{const u=new URL(url,w.location.href),route=u.pathname;calls.push({route,method:options.method||'GET'});
  if(route==='/api/models')return reply({models:[],configured:false});if(route==='/api/projects')return reply(empty?[]:[{id:'p_1',name:'项目一'},{id:'p_2',name:'项目二'}]);
  if(route.endsWith('/conversation')){const pid=route.split('/')[3];return reply({project:{id:pid,name:pid},session_id:u.searchParams.get('session_id')||'c_1',sessions:[{id:'c_1',name:'对话一'},{id:'c_2',name:'对话二'}],sources:[],applications:[],turns:[],mode:'rules',has_more:false})}
  if(route.endsWith('/versions')||route.endsWith('/applications')){if(delayed&&route.endsWith('/'+delayed))await new Promise(resolve=>release=resolve);return reply(route.endsWith('/versions')?[{commit:'OLD_PRIVATE_COMMIT',reason:'当前项目工作版本',created_at:null,changes:[]}]:[{id:'app_1',name:'OLD_PRIVATE_APPLICATION',url:'/data-app.html?project=p_1&app=app_1',status:'ready'}])}
  throw Error('unexpected route '+route);
 };
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.navigationTest={openProject,openSession,openVersions,openApplications,seal:sealAuthentication,state:()=>({projectId,sessionId})};');
 await until(()=>!d.querySelector('#shell').hidden&&(empty?d.querySelector('#composer').hidden:w.navigationTest.state().sessionId==='c_1'));
 return {dom,w,d,calls,delay:kind=>{delayed=kind;release=null},release:()=>{delayed='';release()},waiting:()=>!!release};
}
(async()=>{
 const app=await boot();try{
  for(const kind of ['versions','applications']){
   await app.w.navigationTest.openProject('p_1');app.delay(kind);const pending=app.w.navigationTest[kind==='versions'?'openVersions':'openApplications']();await until(app.waiting);await app.w.navigationTest.openProject('p_2');app.release();await pending;
   assert.equal(app.d.querySelector('#'+kind+'-dialog').open,false,'late old-project '+kind+' cannot open');assert(!app.d.querySelector(kind==='versions'?'#version-list':'#application-list').textContent.includes('OLD_PRIVATE'));
  }
  await app.w.navigationTest.openVersions();const oldRestore=app.d.querySelector('[data-restore]');assert(oldRestore);await app.w.navigationTest.openSession('c_2');assert.equal(app.d.querySelector('#versions-dialog').open,false);app.d.querySelector('#version-list').onclick({target:oldRestore});assert(app.d.querySelector('#toast').textContent.includes('项目已变化'));assert.equal(app.calls.filter(call=>call.method!=='GET').length,0,'old restore action cannot submit into the next context');
  await app.w.navigationTest.openApplications();const oldApplication=app.d.querySelector('[data-application-revise]');await app.w.navigationTest.openProject('p_1');app.d.querySelector('#application-list').onclick({target:oldApplication});assert(app.d.querySelector('#toast').textContent.includes('项目已变化'));assert.equal(app.d.querySelector('#message').value,'');
  await app.w.navigationTest.openVersions();app.w.navigationTest.seal();assert.equal(app.d.querySelector('#versions-dialog').open,false);assert.equal(app.d.querySelector('#version-list').textContent,'');
 }finally{app.dom.window.close()}
 const empty=await boot({empty:true});try{const before=empty.calls.length;await empty.w.navigationTest.openApplications();await empty.w.navigationTest.openVersions();assert.equal(empty.calls.length,before);assert(empty.d.querySelector('#toast').textContent.includes('先选择或新建'));assert(!empty.calls.some(call=>call.route.includes('//')))}finally{empty.dom.window.close()}
 console.log(JSON.stringify({late_application_and_history_responses_ignored:true,project_and_session_navigation_close_contextual_dialogs:true,old_actions_cannot_mutate_new_context:true,auth_seal_clears_dialogs:true,empty_project_guard:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1});
