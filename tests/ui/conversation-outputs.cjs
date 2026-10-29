// Actual app and preview shell; controlled API, no OS/layout claim.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),wait=async check=>{for(let i=0;i<200;i++){if(check())return;await new Promise(r=>setTimeout(r,5))}throw Error('Conversation output did not settle')};
const reply=value=>({ok:true,status:200,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
(async()=>{
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.fixture/',runScripts:'outside-only'}),w=dom.window,d=w.document,requests=[],downloads=[];let release,slow=false;
 try{
  w.sessionStorage.setItem('atlas_token','fixture-owner');w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.HTMLElement.prototype.scrollIntoView=()=>{};w.URL.createObjectURL=()=> 'blob:fixture';w.URL.revokeObjectURL=()=>{};w.HTMLAnchorElement.prototype.click=function(){downloads.push(this.download)};
  const items=['docx','xlsx','png'].map((format,i)=>{const id='wa_'+String(i+1).repeat(44);return {id,kind:format==='png'?'media':'office',name:'真实成果<img>.'+format,format,sha256:'a'.repeat(64),preview_url:'/api/projects/p/artifacts/'+id+'/preview',download_url:'/api/projects/p/artifacts/'+id+'/download'}});
  const reportId='wa_'+'4'.repeat(44);items.push({id:reportId,kind:'report',name:'经营报告',format:'md',preview_url:'/api/projects/p/artifacts/'+reportId+'/preview',download_url:'/api/projects/p/artifacts/'+reportId+'/download',downloads:[{format:'docx',url:'/api/projects/p/artifacts/'+reportId+'/download?format=docx'},{format:'md',url:'/api/projects/p/artifacts/'+reportId+'/download?format=md'}]});
  const outside={...items[0],id:'wa_'+'a'.repeat(44),preview_url:'https://elsewhere.test/preview',download_url:'https://elsewhere.test/download'};
  w.fetch=async(url,options={})=>{
   const route=new URL(url,w.location.href).pathname;requests.push(route);
   assert.equal(options.headers.Authorization,'Bearer fixture-owner');
   if(route==='/api/models')return reply({models:[]});
   if(route==='/api/projects')return reply([{id:'p',name:'项目一'},{id:'p2',name:'项目二'}]);
   if(route.endsWith('/conversation'))return reply({project:{id:route.includes('/p2/')?'p2':'p',name:'项目'},session_id:'c',sessions:[{id:'c',name:'对话'}],sources:[],applications:[],mode:'rules',turns:route.includes('/p2/')?[]:[{id:'t',text:'生成文件',state:'done',answer:'成果已保存',interaction_mode:'work',created_at:'2026-10-29',outputs:[...items,outside]}]});
   if(route.endsWith('/preview')){const item=items.find(value=>value.preview_url===route);if(slow)await new Promise(resolve=>release=resolve);return reply({artifact:item,render:'text',text:'此为真实文件的预览内容'})}
   if(route.endsWith('/download'))return {ok:true,status:200,blob:async()=>new w.Blob(['file bytes'])};
   throw Error('Unexpected route '+route);
  };
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));w.eval(fs.readFileSync(path.join(root,'workspace-shell.js'),'utf8'));
  await wait(()=>d.querySelectorAll('#messages .conversation-artifact').length===4);
  assert(d.querySelector('[data-filename="经营报告.md"]').dataset.download.endsWith('?format=md'),'selected download bytes match the displayed extension');
  assert.equal(d.querySelectorAll('.conversation-artifact img').length,0);assert(d.querySelector('.conversation-artifact').textContent.includes('<img>'));
  assert.equal(requests.filter(url=>url.endsWith('/artifacts')).length,0,'inline delivery does not require opening the catalog');
  d.querySelector('[data-turn-artifact]').click();await wait(()=>d.querySelector('#artifact-preview-body').textContent.includes('真实文件'));
  assert.equal(d.querySelector('#workspace-artifacts-panel').hidden,false);d.querySelector('#workspace-artifacts-close').click();
  d.querySelector('.conversation-artifact [data-download]').click();await wait(()=>downloads.length===1);assert.equal(downloads[0],items[0].name,'keep actual filename/extension');
  slow=true;d.querySelector('[data-turn-artifact]').click();await wait(()=>!!release);await w.atlasBusiness.openProject('p2');release();await new Promise(r=>setTimeout(r,10));
  assert.equal(d.querySelector('#artifact-preview-body').textContent,'','late old-project file must not be shown');assert.equal(d.querySelectorAll('.conversation-artifact').length,0);
  console.log(JSON.stringify({all_generated_files_in_conversation:true,view_opens_exact_version:true,download_retains_filename:true,no_catalog_hunt:true,filenames_escaped:true,foreign_links_rejected:true,late_preview_discarded_after_navigation:true}));
 }finally{dom.window.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
