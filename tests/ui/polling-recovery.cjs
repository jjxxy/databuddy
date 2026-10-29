const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const drain=async()=>{for(let i=0;i<25;i++)await Promise.resolve()};
function setup(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test',runScripts:'outside-only'}),w=dom.window;
 let now=1000000,next=0;const timers=new Map();
 w.Date.now=()=>now;w.setTimeout=(fn,delay=0)=>{const id=++next;timers.set(id,{fn,at:now+Number(delay)});return id};w.clearTimeout=id=>timers.delete(id);
 w.matchMedia=()=>({matches:false,addEventListener(){}});
 return {dom,w,timers,advance:async amount=>{now+=amount;for(const [id,t] of [...timers])if(t.at<=now){timers.delete(id);t.fn()}await drain()}};
}
const feed=()=>({events:[],unread:[],unread_count:0,cursor:3,active:[],active_count:0,services:{worker:true,agent:true}});
async function lifecycleRecovery(){
 const {dom,w,advance}=setup(),d=w.document;
 try{
  let requests=[],mode='rate',registrationRate=false,permissionCalls=0,refreshes=0,pendingFeed=null;
  const pending={request_id:'keep-original-request',text:'不要自动重发'};
  w.Notification={permission:'default',requestPermission:()=>{permissionCalls++;return Promise.resolve('denied')}};
  w.eval(fs.readFileSync(path.join(root,'lifecycle.js'),'utf8')+';window.createAtlasLifecycle=createAtlasLifecycle;');
  const life=w.createAtlasLifecycle({state:()=>({projectId:'p',sessionId:'c',sourceIds:['s_original'],pending}),projects:()=>[],atLatest:()=>false,
   refresh:async()=>{refreshes++},toast:()=>{},api:async(route,options={})=>{
    requests.push({route,options});
    if(route==='/browser/register'){
     if(registrationRate)throw Object.assign(Error('rate'),{status:429,retryAfter:60000});
     return {desktop:false,quiet:false};
    }
    if(route.startsWith('/activity')){
     if(mode==='rate')throw Object.assign(Error('rate'),{status:429,retryAfter:60000});
     if(mode==='missing')throw Object.assign(Error('missing'),{status:404,code:'browser_device_missing'});
     if(mode==='other404')throw Object.assign(Error('other resource missing'),{status:404});
     if(mode==='pending')return new Promise(resolve=>pendingFeed=resolve);
     return feed();
    }
    throw Error('Unexpected request '+route);
   }});
  await life.register();d.querySelector('#message').value='本页必须保留的草稿';
  await life.poll(true);const blocked=requests.length;
  await life.poll(true);w.dispatchEvent(new w.Event('focus'));w.dispatchEvent(new w.Event('pageshow'));w.dispatchEvent(new w.Event('online'));await drain();
  assert.equal(requests.length,blocked,'force/focus/online must honor Retry-After');
  await advance(59999);assert.equal(requests.length,blocked);
  mode='ok';await advance(1);await life.poll(true);assert.equal(refreshes,1);
  mode='missing';await life.poll(true);
  mode='ok';registrationRate=true;await life.poll(true);
  const repairing=requests.length;await life.poll(true);assert.equal(requests.length,repairing);
  registrationRate=false;await advance(60500);await life.poll(true);await drain();
  const registrations=requests.filter(r=>r.route==='/browser/register');assert(registrations.length>=3);
  assert.equal(new Set(registrations.map(r=>r.options.body.device_id)).size,1);
  assert.equal(d.querySelector('#message').value,'本页必须保留的草稿');assert.equal(permissionCalls,0);
  assert.equal(pending.request_id,'keep-original-request');assert(!requests.some(r=>r.route.endsWith('/messages')));
  const count=registrations.length;mode='other404';await life.poll(true);await life.poll(true);
  assert.equal(requests.filter(r=>r.route==='/browser/register').length,count,'unrelated 404 must not reset the device');
  mode='pending';const polling=life.poll(true);await drain();assert(pendingFeed);
  const before=refreshes;life.invalidate();pendingFeed(feed());await polling;
  assert.equal(refreshes,before,'late activity must not refresh after logout');
  console.log(JSON.stringify({retry_after_honored_on_force_focus_online:true,same_device_reregistered:true,registration_429_retried:true,
   drafts_and_pending_ids_preserved:true,no_auto_send_or_permission_prompt:true,unrelated_404_not_repaired:true,late_poll_discarded_after_logout:true}));
 }finally{dom.window.close()}
}
async function bootRecovery(stopBeforeRetry=false){
 const {dom,w,advance}=setup(),d=w.document;
 try{
  const ids=[],requests=[];w.sessionStorage.setItem('atlas_token','fixture-token');w.localStorage.setItem('preserve-project-preference','keep');
  w.fetch=async(url,options={})=>{
   const route=new URL(url,w.location.href).pathname;requests.push(route);
   let status=200,value={};
   if(route==='/api/browser/register'){
    ids.push(JSON.parse(options.body).device_id);
    if(ids.length===1){status=429;value={detail:'rate limited'}}else value={desktop:false,quiet:false};
   }else if(route==='/api/models')value={models:[],configured:false};
   else if(route==='/api/projects')value=[];
   else if(route==='/api/activity')value=feed();
   else if(route==='/api/missing-device-probe'){status=404;value={detail:'设备记录缺失',code:'browser_device_missing'}}
   else throw Error('Unexpected boot request '+route);
   return {status,ok:status===200,headers:{get:name=>status===429&&name==='Retry-After'?'60':null},json:async()=>value};
  };
  w.eval(fs.readFileSync(path.join(root,'lifecycle.js'),'utf8')+';window.createAtlasLifecycle=createAtlasLifecycle;');
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.pollRecoveryTest={sealAuthentication,api};');
  await drain();assert.equal(ids.length,1);assert(d.querySelector('#login-error').textContent.includes('无需清空数据'));
  await assert.rejects(w.pollRecoveryTest.api('/missing-device-probe'),e=>e.status===404&&e.code==='browser_device_missing');
  if(stopBeforeRetry)w.pollRecoveryTest.sealAuthentication();
  await advance(60000);await drain();
  assert.equal(ids.length,stopBeforeRetry?1:2);
  if(!stopBeforeRetry){assert.equal(ids[0],ids[1]);assert.equal(d.querySelector('#shell').hidden,false)}
  assert.equal(w.localStorage.getItem('preserve-project-preference'),'keep');
  assert(!requests.some(p=>p.endsWith('/messages')));
  console.log(JSON.stringify({startup_registration_429_auto_retry:!stopBeforeRetry,logout_cancels_startup_retry:stopBeforeRetry,no_data_reset:true}));
 }finally{dom.window.close()}
}
(async()=>{await lifecycleRecovery();await bootRecovery();await bootRecovery(true)})().catch(error=>{console.error(error);process.exitCode=1});
