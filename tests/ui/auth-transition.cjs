// Real app/lifecycle jsdom regression; does not claim a native browser run.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async f=>{const end=Date.now()+4000;while(!f()){if(Date.now()>end)throw Error('auth transition timed out');await new Promise(r=>setTimeout(r,5))}};
const reply=(value,status=200)=>({ok:status<400,status,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
function page(account){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 let delay=false,release,reloads=0;
 w.matchMedia=()=>({matches:false,addEventListener(){}});w.sessionStorage.setItem('atlas_token',account);
 w.fetch=async(url,options={})=>{const u=new URL(url,'https://atlas.test'),route=u.pathname,body=options.body?JSON.parse(options.body):null;calls.push({route,body,auth:options.headers?.Authorization,method:options.method});
  if(route==='/api/expire')return reply({},401);
  if(route==='/api/browser/register')return reply({desktop:false});
  if(route==='/api/models')return reply({models:[{id:account+'-model',name:account+' model'}],default_model_id:account+'-model'});
  if(route==='/api/projects')return reply([{id:'same-project',name:account+' project'}]);
  if(route.endsWith('/conversation')){if(delay)await new Promise(r=>release=r);return reply({project:{id:'same-project',name:account+' project'},session_id:'same-session',sessions:[{id:'same-session',name:account+' session'}],sources:[],applications:[],turns:[],mode:'llm',has_more:false})}
  if(route.endsWith('/view'))return reply(options.method==='PUT'?{revision:1,draft_saved:true}:{revision:0,at_bottom:true});
  if(route==='/api/activity')return reply({events:[],unread:[],unread_count:0,cursor:0,active:[],active_count:0});
  if(route.endsWith('/subscription'))return reply({ok:true});
  throw Error('unexpected route '+route);
 };
 w.eval(fs.readFileSync(path.join(root,'lifecycle.js'),'utf8')+';window.createAtlasLifecycle=createAtlasLifecycle;');
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.authTest={life,api,refresh,state:()=>({token,authSealed,projectId,sessionId,modelCatalog})};window.setAuthReload=fn=>{reloadAuthDocument=fn};');
 // Override the real navigation function only in this jsdom fixture.
 w.setAuthReload(()=>{reloads++});
 return {dom,w,d,calls,delay:()=>{delay=true},release:()=>release(),waiting:()=>!!release,reloads:()=>reloads};
}
(async()=>{
 const alice=page('alice');let bob;
 try{
  await wait(()=>alice.w.authTest.state().sessionId==='same-session');await alice.w.authTest.life.restore();
  alice.d.querySelector('#message').value='ALICE PRIVATE UNSAVED DRAFT';
  alice.w.sessionStorage.setItem('atlas_draft_same-project_same-session','ALICE LOCAL SECRET');
  alice.delay();const stale=alice.w.authTest.api('/projects/same-project/conversation').then(()=>false,e=>e.authTransition);
  await wait(alice.waiting);await assert.rejects(alice.w.authTest.api('/expire'));
  assert.equal(alice.reloads(),1);assert.equal(alice.w.sessionStorage.getItem('atlas_token'),null);assert.equal(alice.d.querySelector('#shell').hidden,true);
  alice.d.querySelector('#access-token').value='bob';await alice.d.querySelector('#login-form').onsubmit({preventDefault(){}});
  assert.equal(alice.reloads(),2);assert.equal(alice.w.authTest.state().token,'alice','old controller credential is immutable');
  assert.equal(alice.w.sessionStorage.getItem('atlas_token'),'bob');assert.equal(alice.w.sessionStorage.getItem('atlas_draft_same-project_same-session'),null);
  alice.w.dispatchEvent(new alice.w.Event('pagehide'));await alice.w.authTest.life.persist();
  alice.release();assert.equal(await stale,true,'late Alice response must not be accepted');
  assert(alice.calls.every(c=>c.auth!=='Bearer bob'),'old page must never use Bob credential');
  bob=page(alice.w.sessionStorage.getItem('atlas_token'));await wait(()=>bob.w.authTest.state().sessionId==='same-session');await bob.w.authTest.life.restore();
  assert.equal(bob.d.querySelector('#message').value,'');assert.equal(bob.d.querySelector('#model-select').value,'bob-model');
  assert(bob.calls.some(c=>c.route==='/api/models'));assert(bob.calls.every(c=>!JSON.stringify(c.body).includes('ALICE')));
  await standaloneTransitions();
  console.log(JSON.stringify({standalone_pages:true,same_resource_ids:true,no_old_draft_under_new_token:true,late_old_response_rejected:true,credential_change_reloads:true,model_catalog_reloaded:true,real_browser_verified:false}));
 }finally{alice.dom.window.close();bob?.dom.window.close()}
})().catch(e=>{console.error(e);process.exitCode=1});

async function standaloneTransitions(){
 for(const kind of ['data-app','data-workbench']){
  function make(account){
   const dom=new JSDOM(fs.readFileSync(path.join(root,kind+'.html'),'utf8'),{url:'https://atlas.test/'+kind+'.html?project=same-project&app=same-app',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];let release,reloads=0;
   w.sessionStorage.setItem('atlas_token',account);w.setInterval=()=>0;
   w.fetch=async(url,options={})=>{const route=new URL(url,w.location.href).pathname;calls.push({route,auth:options.headers?.Authorization,body:options.body});if(route==='/api/delayed')await new Promise(r=>release=r);return reply(route==='/api/expire'?{}:route.endsWith('/same-app')?{name:account+' application',run_id:null}:{items:[],owner:account},route==='/api/expire'?401:200)};
   const extra=kind==='data-app'?';window.pageTest={request,token:()=>token,setReload:fn=>{reloadAuthDocument=fn}};':';window.pageTest={request:wbRequest,ready:workbenchReady,token:()=>wbState.token,setReload:fn=>{wbReloadAuthDocument=fn}};';
   w.eval(fs.readFileSync(path.join(root,kind+'.js'),'utf8')+extra);w.pageTest.setReload(()=>reloads++);
   return {dom,w,d,calls,release:()=>release(),waiting:()=>!!release,reloads:()=>reloads};
  }
  const old=make('alice');let next;
  try{
   await old.w.pageTest.ready;await new Promise(r=>setTimeout(r,0));
   const pending=old.w.pageTest.request('/api/delayed').then(()=>false,e=>e.authTransition);await wait(old.waiting);
   await assert.rejects(old.w.pageTest.request('/api/expire'));assert.equal(old.reloads(),1);assert.equal(old.w.sessionStorage.getItem('atlas_token'),null);
   const tokenId=kind==='data-app'?'#token':'#workbench-token',formId=kind==='data-app'?'#login-form':'#workbench-login-form';
   old.w.sessionStorage.setItem('atlas_draft_same-project_same-session','PRIVATE ALICE');old.d.querySelector(tokenId).value='bob';old.d.querySelector(formId).dispatchEvent(new old.w.Event('submit',{cancelable:true}));
   assert.equal(old.reloads(),2);assert.equal(old.w.pageTest.token(),'alice');assert.equal(old.w.sessionStorage.getItem('atlas_token'),'bob');assert.equal(old.w.sessionStorage.getItem('atlas_draft_same-project_same-session'),null);
   old.release();assert.equal(await pending,true);await assert.rejects(old.w.pageTest.request('/api/projects/same-project/messages',{method:'POST',body:{text:'PRIVATE ALICE'}}));assert(old.calls.every(c=>c.auth==='Bearer alice'));
   next=make('bob');await next.w.pageTest.ready;await new Promise(r=>setTimeout(r,0));const result=await next.w.pageTest.request('/api/projects/same-project');assert.equal(result.owner,'bob');assert(next.calls.every(c=>c.auth==='Bearer bob'&&!String(c.body).includes('ALICE')));
  }finally{old.dom.window.close();next?.dom.window.close()}
 }
}
