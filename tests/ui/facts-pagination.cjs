'use strict';
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const html=fs.readFileSync(path.join(root,'data-workbench.html'),'utf8');
const script=fs.readFileSync(path.join(root,'data-workbench.js'),'utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function wait(condition){for(let n=0;n<100;n++){if(condition())return;await tick();}throw Error('Facts UI condition timed out');}
const reply=(value,status=200)=>({ok:status>=200&&status<300,status,headers:{get:()=>null},json:async()=>value});

async function boot({project='p_1',account='alice',deferSecond=false,deferJson=false,failSecond=false}={}){
 const dom=new JSDOM(html,{url:'https://atlas.test/data-workbench.html?project='+project,runScripts:'outside-only'});
 const w=dom.window,d=w.document,calls=[];let release=null,reloads=0,failed=false;
 w.sessionStorage.setItem('atlas_token',account);
 const records=Array.from({length:25},(_,index)=>({id:'facts_'+(index+1),name:account+' '+project+' 原件 '+(index+1)+(index===24?' <img src=x>':''),status:'proposed',content_sha256:'a'.repeat(64)}));
 w.fetch=async(url,options={})=>{
  const u=new URL(url,w.location.href),offset=Number(u.searchParams.get('offset')||0);
  calls.push({path:u.pathname,offset,limit:u.searchParams.get('limit'),method:options.method||'GET',auth:options.headers?.Authorization});
  assert(u.pathname.startsWith('/api/projects/'+project+'/'),'request must stay in its document project');
  if(u.pathname.endsWith('/data-facts')){
   assert.equal(u.searchParams.get('limit'),'20');
   // One overlap models a list changing between pages; duplicate rows are not new work.
   const body={items:offset?records.slice(19):records.slice(0,20),total:25,next_offset:offset?null:20};
   if(offset&&failSecond&&!failed){failed=true;return reply({detail:'synthetic temporary failure'},500);}
   if(offset&&(deferSecond||deferJson)){
    if(deferJson)return {ok:true,status:200,json:()=>new Promise(resolve=>{release=()=>resolve(body);})};
    return new Promise(resolve=>{release=()=>resolve(reply(body));});
   }
   return reply(body);
  }
  const id=u.pathname.match(/\/data-facts\/(facts_\d+)$/)?.[1];
  if(id)return reply({...records.find(record=>record.id===id),facts:[{
   value_as_printed:'118.15元',normalized_value:'118.15',measure:'净收入',unit:'CNY',period:'2026-10',
   quote:'原文净收入为118.15元。',start:0,end:13}],approval_blockers:[]});
  if(u.pathname.endsWith('/data-workflows')||u.pathname.endsWith('/semantic-proposals')||u.pathname.endsWith('/semantic-catalogs'))return reply({items:[],total:0,next_offset:null});
  throw Error('Unexpected request '+u.pathname);
 };
 w.eval(script+';window.factTest={ready:workbenchReady,load:wbLoadFacts,state:wbState,changeAuth:wbChangeAuthentication,setReload:fn=>{wbReloadAuthDocument=fn}};');
 w.factTest.setReload(()=>reloads++);
 await w.factTest.ready;
 d.querySelector('#workbench-tab-semantics').click();
 await wait(()=>w.factTest.state.factsLoaded);
 return {dom,w,d,calls,release:()=>{assert(release,'deferred page must exist');release();},waiting:()=>!!release,reloads:()=>reloads};
}

(async()=>{
 const app=await boot({deferSecond:true});
 try{
  const {w,d,calls}=app,more=d.querySelector('#workbench-more-facts');
  assert.equal(d.querySelectorAll('[data-facts]').length,20);
  assert.equal(calls.filter(call=>call.path.endsWith('/data-facts')).length,1,'opening the tab must not fetch all pages');
  assert.equal(more.hidden,false);assert.equal(more.textContent,'加载更多来源数值');
  more.click();more.click();const duplicate=w.factTest.load(true);
  await wait(app.waiting);
  assert(more.disabled&&d.querySelector('#workbench-refresh-facts').disabled);
  assert.equal(calls.filter(call=>call.path.endsWith('/data-facts')&&call.offset===20).length,1,'double clicks must issue one page request');
  app.release();await duplicate;await wait(()=>!w.factTest.state.factsLoading);
  assert.equal(d.querySelectorAll('[data-facts]').length,25);
  assert.equal(new Set([...d.querySelectorAll('[data-facts]')].map(node=>node.dataset.facts)).size,25);
  assert.equal(more.hidden,true);assert.equal(d.querySelector('#workbench-facts img'),null);
  const last=d.querySelector('[data-facts="facts_25"]');last.open=true;last.dispatchEvent(new w.Event('toggle'));
  await wait(()=>last.textContent.includes('原文净收入为118.15元。'));
  assert(last.querySelector('[data-facts-decision="approved"]'),'an older proposal is actually openable for human review');
  assert(calls.every(call=>call.method==='GET'),'loading pages must never approve or run work');
 }finally{app.dom.window.close();}

 const retry=await boot({failSecond:true});
 try{
  const more=retry.d.querySelector('#workbench-more-facts');more.click();
  await wait(()=>!retry.w.factTest.state.factsLoading);
  assert.equal(retry.d.querySelectorAll('[data-facts]').length,20);
  assert(!more.hidden&&!more.disabled,'a failed page must remain retryable');
  assert(retry.d.querySelector('#workbench-notice').textContent.includes('暂时没能完成操作'));
  more.click();await wait(()=>retry.d.querySelectorAll('[data-facts]').length===25);
  assert.equal(retry.calls.filter(call=>call.path.endsWith('/data-facts')&&call.offset===20).length,2);
 }finally{retry.dom.window.close();}

 const oldProject=await boot({deferSecond:true});let newProject;
 try{
  const pending=oldProject.w.factTest.load(true);await wait(oldProject.waiting);
  oldProject.w.history.replaceState(null,'','/data-workbench.html?project=p_2');
  newProject=await boot({project:'p_2'});
  oldProject.release();await pending;
  assert.equal(oldProject.d.querySelectorAll('[data-facts]').length,20,'old project page must ignore a late response after navigation');
  const before=oldProject.calls.length;await oldProject.w.factTest.load(true);assert.equal(oldProject.calls.length,before);
  assert(newProject.d.querySelector('#workbench-facts').textContent.includes('p_2'));
  assert(!newProject.d.querySelector('#workbench-facts').textContent.includes('p_1'));
 }finally{oldProject.dom.window.close();newProject?.dom.window.close();}

 const oldOwner=await boot({deferJson:true});let newOwner;
 try{
  const pending=oldOwner.w.factTest.load(true).then(()=>false,error=>error.authTransition);await wait(oldOwner.waiting);
  oldOwner.w.factTest.changeAuth('bob');assert.equal(oldOwner.reloads(),1);
  newOwner=await boot({account:'bob'});
  oldOwner.release();assert.equal(await pending,true,'even a late JSON body must respect sealed authentication');
  assert.equal(oldOwner.d.querySelectorAll('[data-facts]').length,20);
  const before=oldOwner.calls.length;await oldOwner.w.factTest.load(true);assert.equal(oldOwner.calls.length,before);
  assert(oldOwner.calls.every(call=>call.auth==='Bearer alice'));
  assert(newOwner.calls.every(call=>call.auth==='Bearer bob'));
  assert(!newOwner.d.querySelector('#workbench-facts').textContent.includes('alice'));
 }finally{oldOwner.dom.window.close();newOwner?.dom.window.close();}
 console.log(JSON.stringify({older_than_first_20_openable:true,explicit_page_loading_only:true,overlap_deduplicated:true,
  double_click_single_request:true,failed_page_retry_preserves_rows:true,project_navigation_discards_stale_page:true,
  account_switch_discards_late_json:true,no_automatic_review_or_run:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
