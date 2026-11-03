// Exercise the actual composer across session/project navigation.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async f=>{for(let i=0;i<500;i++){if(f())return;await new Promise(r=>setTimeout(r,5))}throw Error('UI did not settle')};
async function scenario(withLifecycle){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window,d=w.document;
 try{
  w.sessionStorage.setItem('nexus_token','fixture');w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.HTMLElement.prototype.scrollIntoView=()=>{};
  let uploaded=0;
  if(withLifecycle)w.createNexusLifecycle=b=>({register:async()=>null,start:async()=>{},persist:async()=>{},schedule(){},restore:async()=>{b.setAttachments([])},invalidate(){},logout:async()=>{}});
  w.fetch=async(url,o={})=>{let u=new URL(url,w.location.href),p=u.pathname,value;
   if(p==='/api/models')value={models:[{id:'model',name:'模型'}],default_model_id:'model'};
   else if(p==='/api/projects')value=[{id:'p1',name:'项目一'},{id:'p2',name:'项目二'}];
   else if(p.endsWith('/conversation')){const pid=p.includes('/p2/')?'p2':'p1';value={project:{id:pid,name:pid},session_id:u.searchParams.get('session_id')||'c1',sessions:[{id:'c1',name:'一'},{id:'c2',name:'二'}],sources:[],turns:[],applications:[],mode:'llm'}}
   else if(p.endsWith('/attachments')){uploaded++;value={id:'s_'+uploaded,name:o.body.get('file').name}}
   else if(p.endsWith('/messages'))value={id:'turn'};
   else throw Error('unexpected '+url);
   return {ok:true,status:200,headers:{get:()=>null},json:async()=>structuredClone(value)};
  };
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.attachmentTest={attach,send,state:()=>({projectId,sessionId,attached,pending})};');
  await wait(()=>w.attachmentTest.state().sessionId==='c1');
  const a=new w.File(['1'],'相同.csv'),b=new w.File(['2'],'相同.csv');w.attachmentTest.attach([a,b]);
  assert.equal(d.querySelectorAll('.attachment-chip').length,2);assert.equal(uploaded,0,'navigation never uploads');
  let leaving=new w.Event('beforeunload',{cancelable:true});w.dispatchEvent(leaving);assert(leaving.defaultPrevented,'refresh warns of files still only in memory');
  await w.nexusBusiness.openSession('c2');assert.equal(w.attachmentTest.state().attached.length,0);
  w.attachmentTest.attach([new w.File(['3'],'另一对话.csv')]);
  await w.nexusBusiness.openProject('p2');assert.equal(w.attachmentTest.state().attached.length,0);
  await w.nexusBusiness.openProject('p1');assert.equal(w.attachmentTest.state().sessionId,'c1');assert.equal(w.attachmentTest.state().attached.length,2);assert.strictEqual(w.attachmentTest.state().attached[0].file,a);assert.strictEqual(w.attachmentTest.state().attached[1].file,b);
  await w.nexusBusiness.openSession('c2');assert.equal(w.attachmentTest.state().attached[0].file.name,'另一对话.csv');
  await w.nexusBusiness.openSession('c1');d.querySelector('#message').value='分析两个文件';await w.attachmentTest.send();assert.equal(uploaded,2);assert.equal(w.attachmentTest.state().attached.length,0);
  leaving=new w.Event('beforeunload',{cancelable:true});w.dispatchEvent(leaving);assert(leaving.defaultPrevented,'other conversation still has an unsent file');
  await w.nexusBusiness.openSession('c2');d.querySelector('[data-remove-attachment="0"]').click();leaving=new w.Event('beforeunload',{cancelable:true});w.dispatchEvent(leaving);assert(!leaving.defaultPrevented,'removed/sent files do not block navigation');
  w.attachmentTest.attach([a]);w.confirm=()=>false;d.querySelector('#logout').click();assert.equal(w.attachmentTest.state().attached.length,1,'cancel logout retains files');
 }finally{dom.window.close()}
}
(async()=>{await scenario(false);await scenario(true);console.log(JSON.stringify({session_project_drafts_preserved:true,no_auto_upload:true,reload_warning:true,no_cross_session_files:true,logout_retains_on_cancel:true}))})().catch(e=>{console.error(e);process.exitCode=1});
