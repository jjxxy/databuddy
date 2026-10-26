// Source cards in actual rendered messages and the composer open a read-only view.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const until=async fn=>{const end=Date.now()+4000;while(!fn()){if(Date.now()>end)throw Error('source button UI timeout');await new Promise(r=>setTimeout(r,5))}};
const response=value=>({ok:true,status:200,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
(async()=>{
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,requests=[],opened=[];
 try{
  w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;w.sessionStorage.setItem('atlas_token','fixture-owner');
  w.fetch=async(url,options={})=>{
   const route=new URL(url,w.location.href).pathname;requests.push({route,method:options.method||'GET'});
   if(route==='/api/models')return response({models:[{id:'business',name:'业务模型'}],default_model_id:'business'});
   if(route==='/api/projects')return response([{id:'p',name:'原件查看'}]);
   if(route.endsWith('/conversation'))return response({project:{id:'p',name:'原件查看'},session_id:'c',sessions:[{id:'c',name:'月结'}],
    sources:[{id:'s_workbook',name:'最新替换.xlsx',revision:2},{id:'s_text',name:'业务口径.txt',revision:1}],applications:[],mode:'llm',
    turns:[{id:'t_original',text:'分析这些资料',answer:'处理中',state:'agent',created_at:'2026-10-24',source_ids:['s_workbook','s_text'],
      attached_sources:[{id:'s_workbook',name:'上传底稿 <img src=x>.xlsx',revision:1},{id:'s_text',name:'业务口径.txt',revision:1}]}]});
   throw Error('unexpected request '+route);
  };
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.sourceButtonsTest={state:()=>({attached,pending,sending}),attach};');
  let settle;
  w.atlasShell={syncLayout(){},projectChanged(){},reset(){},openSource:async(id,revision)=>{opened.push({id,revision});await new Promise(resolve=>settle=resolve)}};
  await until(()=>d.querySelector('#messages [data-open-source=s_workbook]')).catch(error=>{error.message+='; login='+d.querySelector('#login-error').textContent+'; connection='+d.querySelector('#connection-error').textContent+'; routes='+JSON.stringify(requests);throw error});
  const file=d.querySelector('#messages [data-open-source=s_workbook]');
  assert.equal(file.tagName,'BUTTON');assert.equal(file.type,'button');assert.equal(file.dataset.sourceRevision,'1');
  assert(file.textContent.includes('上传底稿 <img src=x>.xlsx'));assert.equal(file.querySelector('img'),null);
  assert.equal(file.getAttribute('aria-label'),'查看资料 上传底稿 <img src=x>.xlsx');
  d.querySelector('#message').value='尚未发送的业务要求';
  file.querySelector('.file-glyph').click();await until(()=>opened.length===1);
  assert.deepEqual(opened[0],{id:'s_workbook',revision:1});assert(file.disabled);
  file.click();assert.equal(opened.length,1,'double click does not duplicate preview request');
  settle();await until(()=>!file.disabled);
  assert.equal(d.querySelector('#message').value,'尚未发送的业务要求');
  assert(!requests.some(x=>x.route.endsWith('/messages')||x.route.endsWith('/attachments')),'viewing never submits a task or uploads');
  d.querySelector('#messages [data-open-source=s_text]').click();await until(()=>opened.length===2);
  assert.deepEqual(opened[1],{id:'s_text',revision:1});settle();
  w.atlasBusiness.attachSources([{id:'s_workbook',name:'最新替换.xlsx',revision:2}]);
  const queued=d.querySelector('#attachment-tray [data-open-source=s_workbook]');assert(queued);
  queued.click();await until(()=>opened.length===3);assert.deepEqual(opened[2],{id:'s_workbook',revision:2});
  settle();await until(()=>!queued.disabled);
  assert.equal(w.sourceButtonsTest.state().attached.length,1,'viewing does not remove attachments');
  d.querySelector('[data-remove-attachment]').click();assert.equal(w.sourceButtonsTest.state().attached.length,0,'remove action stays separate');
  w.sourceButtonsTest.attach([new w.File(['waiting'],'尚未上传.txt')]);
  assert.equal(d.querySelector('#attachment-tray [data-open-source]'),null,'an unuploaded file must not claim a server snapshot');
  w.atlasShell={openSource:async()=>{throw Error('原件暂不可用，请重新选择资料')}};
  file.click();await until(()=>!file.disabled);assert(d.querySelector('#toast').textContent.includes('原件暂不可用'));
  console.log(JSON.stringify({actual_message_card_click:true,frozen_revision_not_latest:true,uploaded_composer_view:true,double_click_guard:true,draft_and_attachments_preserved:true,no_task_or_upload:true,safe_filename_rendering:true,recoverable_error:true}));
 }finally{w.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
