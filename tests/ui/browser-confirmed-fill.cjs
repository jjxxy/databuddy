/* The user reviews exact proposed text, never runs markup, and chooses once. */
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async f=>{for(let i=0;i<200;i++){if(f())return;await new Promise(r=>setTimeout(r,5))}throw Error('UI did not settle')};
async function run(decision,fail=false){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://nexus.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 try{
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.HTMLMediaElement.prototype.pause=function(){};
  let project='p',calls=[],confirmCount=0;const session='b'.repeat(64),digest='a'.repeat(64),id='bfill_'+'c'.repeat(48),value='<img src=x onerror=alert(1)>金额 500';
  const item={id,digest,kind:'fill',origin:'https://erp.example',label:'金额',ref:1};
  w.confirm=()=>{confirmCount++;return true};
  w.nexusBusiness={getState:()=>({projectId:project}),toast:()=>{},setDraft:()=>true,authFetch:async()=>({ok:true}),api:async(route,opt={})=>{
   calls.push({route,opt});
   if(route.endsWith('/browsers'))return {configured:true,sessions:[{id:session,state:'open',mode:'agent',created_at:'2026-10-31T00:00:00Z'}]};
   if(route.endsWith('/observe'))return {url:'https://erp.example/report',mode:'agent',revision:digest,image_base64:'fixture',controls:[]};
   if(route.endsWith('/pending-actions'))return {items:[item]};
   if(route.endsWith('/reveal'))return {...item,text:value};
   if(route.endsWith('/decision')){if(fail)throw Error('结果未知');return {state:opt.body.approve?'applied':'rejected'}};
   throw Error('unexpected '+route);
  }};
  w.eval(fs.readFileSync(path.join(root,'interactive-work.js'),'utf8'));
  d.querySelector('#cloud-browser').click();await wait(()=>d.querySelector('[data-browser-controls] button'));
  assert.equal(calls.some(c=>c.route.endsWith('/decision')),false);
  d.querySelector('[data-browser-controls] button').click();await wait(()=>d.querySelector('[data-browser-controls] pre'));
  assert.equal(d.querySelector('[data-browser-controls] pre').textContent,value);
  assert.equal(d.querySelector('[data-browser-controls] img'),null);
  const buttons=[...d.querySelectorAll('[data-browser-controls] button')];
  buttons.find(b=>b.textContent===decision).click();
  await wait(()=>calls.some(c=>c.route.endsWith('/decision')));
  const requests=calls.filter(c=>c.route.endsWith('/decision'));
  assert.equal(requests.length,1);assert.equal(requests[0].opt.body.approve,decision==='确认执行');
  assert.equal(requests[0].opt.body.digest,digest);
  if(fail){await wait(()=>d.querySelector('[data-browser-notice]').textContent.includes('不要盲目重试'));assert.equal(calls.filter(c=>c.route.endsWith('/decision')).length,1)}
  project='other';w.nexusInteractive.projectChanged();
  assert.equal(d.querySelector('[data-browser-controls]').textContent,'');assert.equal(d.querySelector('#cloud-browser-dialog').open,false);
  assert.equal(confirmCount,decision==='确认执行'?1:0);
 }finally{w.close()}
}
(async()=>{await run('确认执行');await run('拒绝');await run('确认执行',true);console.log(JSON.stringify({review_exact_text:true,untrusted_text_not_executed:true,explicit_approve_or_reject:true,project_change_clears:true,failure_not_replayed:true,real_browser_layout_verified:false}))})().catch(e=>{console.error(e);process.exitCode=1});
