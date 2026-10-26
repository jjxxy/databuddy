// Exact uploaded-source resolution uses the existing safe, paginated preview.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async f=>{const end=Date.now()+4000;while(!f()){if(Date.now()>end)throw Error('Source preview did not settle');await new Promise(r=>setTimeout(r,5))}};
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};

async function run(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document;
 const requests=[],downloads=[],toasts=[],delays=new Map(),blobs=[];let projectId='p',sending=false,pending=null;
 try{
  w.matchMedia=()=>({matches:false,addEventListener(){}});w.HTMLElement.prototype.scrollIntoView=function(){};
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
  w.URL.createObjectURL=blob=>{blobs.push(blob);return 'blob:source-'+blobs.length};w.URL.revokeObjectURL=()=>{};
  w.HTMLAnchorElement.prototype.click=function(){downloads.push({url:this.href,name:this.download})};
  const esc=value=>{const span=d.createElement('span');span.textContent=value??'';return span.innerHTML.replace(/"/g,'&quot;')};
  const descriptor=(id,format='txt',pid='p')=>({id:'wa_'+id,kind:'source',name:id==='s_excel'?'多页签底稿.xlsx':id+'.'+format,
    format,version:'frozen-revision',source_refs:[{source_id:id,revision:2}],
    download_url:'/api/projects/'+pid+'/artifacts/wa_'+id+'/download',preview_url:'/api/projects/'+pid+'/artifacts/wa_'+id+'/preview',
    downloads:[{format,url:'/api/projects/'+pid+'/artifacts/wa_'+id+'/download'}]});
  const result=(id,query)=>id==='wa_s_excel'?{render:'table',columns:[{name:'A',label:'A'},{name:'B',label:'B'}],
    rows:query.get('sheet')==='预算'?[{A:'收入预算',B:'=SUM(B2:B5)'}]:query.get('offset')==='50'?[{A:'后续行',B:'-0.000000000001'}]:[{A:'原始底稿',B:'9007199254740993.00000001'}],
    total:51,next_offset:query.get('sheet')==='预算'||query.get('offset')==='50'?null:50,
    sheet_names:['明细','预算'],current_sheet:query.get('sheet')||'明细',fidelity_note:'原始网格；合并值保留锚点，公式未重算'}:
    {render:'text',text:id==='wa_s_text'?'<script>window.MALICIOUS=true</script>\n财务口径：保留负数。':id+' 原文',total:50,next_offset:null,fidelity_note:'原始文件文本摘录'};
  w.atlasBusiness={getState:()=>({projectId,sending,pending}),projectItems:()=>[{id:'p',name:'财务项目'},{id:'p2',name:'新项目'}],sessionItems:()=>[],
    esc,toast:message=>toasts.push(message),closeSidebar(){},setDraft(){throw Error('Opening an input must never replace the conversation draft')},
    async api(raw){
     const url=new URL('/api'+raw,'https://atlas.test');requests.push({method:'GET',path:url.pathname,query:url.search});
     if(delays.has(raw))return delays.get(raw).promise;
     if(url.pathname.endsWith('/artifacts'))return {items:[],total:0,next_offset:null};
     const parts=url.pathname.split('/');
     if(parts[4]==='sources'){
      const id=parts[5];if(id==='s_missing')throw Error('资料原件不存在，请重新上传');
      if(id==='s_unsafe')return {...descriptor(id),preview_url:'/api/projects/other/artifacts/x/preview'};
      return descriptor(id,id==='s_excel'?'xlsx':'txt',parts[3]);
     }
     if(parts[4]==='artifacts'&&parts[6]==='preview')return result(parts[5],url.searchParams);
     throw Error('Unexpected source preview route '+raw);
    },
    async authFetch(url){requests.push({method:'GET',path:url,authenticated:true});return {ok:true,blob:async()=>new w.Blob(['unchanged original bytes'])}}
  };
  w.eval(fs.readFileSync(path.join(root,'workspace-shell.js'),'utf8'));
  const shell=w.atlasShell,body=()=>d.querySelector('#artifact-preview-body'),title=()=>d.querySelector('#artifact-preview-title').textContent;

  sending=true;pending={request_id:'already-submitted'};
  await shell.openSource('s_excel',2);
  assert.equal(d.querySelector('#workspace-artifacts-panel').hidden,false);
  assert.equal(d.querySelector('#artifact-filter').value,'source');assert.equal(title(),'多页签底稿.xlsx');
  assert.equal(body().querySelector('td:nth-child(2)').textContent,'9007199254740993.00000001');
  assert(requests.some(r=>r.path==='/api/projects/p/sources/s_excel/artifact'&&r.query==='?revision=2'));
  assert(!requests.some(r=>r.path.endsWith('/artifacts')),'exact source opening must not scan a catalog');
  assert(d.querySelector('#artifact-preview-meta').textContent.includes('原件只读预览，不修改源文件'));
  assert(d.querySelector('#artifact-preview-meta').textContent.includes('公式未重算'));
  assert.deepEqual([...d.querySelectorAll('#artifact-preview-actions button')].map(b=>b.textContent),['下载原文件']);
  d.querySelector('#artifact-next-page').click();await wait(()=>body().textContent.includes('后续行'));
  assert(body().textContent.includes('-0.000000000001'));
  d.querySelector('#artifact-previous-page').click();await wait(()=>body().textContent.includes('原始底稿'));
  const sheet=body().querySelector('select');sheet.value='预算';sheet.dispatchEvent(new w.Event('change'));
  await wait(()=>body().textContent.includes('=SUM(B2:B5)'));
  assert(requests.some(r=>r.query.includes('sheet=%E9%A2%84%E7%AE%97')));
  d.querySelector('#artifact-preview-actions button').click();await wait(()=>downloads.length===1);
  assert.equal(downloads[0].name,'多页签底稿.xlsx');assert(requests.some(r=>r.authenticated&&r.path.endsWith('/wa_s_excel/download')));

  await shell.openSource('s_text');
  assert.equal(title(),'s_text.txt');assert.equal(body().querySelector('select'),null,'sheet selection resets between source files');
  assert.equal(body().querySelector('script'),null);assert.equal(w.MALICIOUS,undefined);assert(body().textContent.includes('<script>'));
  assert(requests.some(r=>r.path.endsWith('/sources/s_text/artifact')&&r.query===''));
  sending=false;pending=null;

  const slowPath='/projects/p/sources/s_slow/artifact?revision=2',slow=deferred();delays.set(slowPath,slow);
  const old=shell.openSource('s_slow',2);await wait(()=>requests.some(r=>r.path.endsWith('/sources/s_slow/artifact')));
  await shell.openSource('s_text',1);slow.resolve(descriptor('s_slow'));await old;
  assert.equal(title(),'s_text.txt');assert(!requests.some(r=>r.path.endsWith('/wa_s_slow/preview')),'late first source cannot replace the second file');

  const closePath='/projects/p/sources/s_close/artifact',close=deferred();delays.set(closePath,close);
  const closing=shell.openSource('s_close');shell.closeArtifacts();close.resolve(descriptor('s_close'));await closing;
  assert.equal(d.querySelector('#workspace-artifacts-panel').hidden,true);assert.equal(d.querySelector('#artifact-preview').hidden,true);
  assert(!requests.some(r=>r.path.endsWith('/wa_s_close/preview')));

  const previewPath='/projects/p/artifacts/wa_s_wait/preview?offset=0&limit=50&text_limit=12000',latePreview=deferred();delays.set(previewPath,latePreview);
  const waiting=shell.openSource('s_wait');await wait(()=>requests.some(r=>r.path.endsWith('/wa_s_wait/preview')));
  shell.closeArtifacts();latePreview.resolve({render:'text',text:'closed file content must stay hidden'});await waiting;
  assert.equal(d.querySelector('#workspace-artifacts-panel').hidden,true);assert(!body().textContent.includes('closed file content'));

  const changedPath='/projects/p/sources/s_changed/artifact',changed=deferred();delays.set(changedPath,changed);
  const changing=shell.openSource('s_changed');projectId='p2';shell.projectChanged();changed.resolve(descriptor('s_changed'));await changing;
  assert(!body().textContent.includes('s_changed'));assert(!requests.some(r=>r.path.endsWith('/wa_s_changed/preview')));
  projectId='p';shell.projectChanged();

  const loggedPath='/projects/p/sources/s_logout/artifact',logged=deferred();delays.set(loggedPath,logged);
  const logout=shell.openSource('s_logout');shell.reset();logged.resolve(descriptor('s_logout'));await logout;
  assert.equal(d.querySelector('#workspace-artifacts-panel').hidden,true);assert(!requests.some(r=>r.path.endsWith('/wa_s_logout/preview')));

  await assert.rejects(shell.openSource('s_missing',1),/资料原件不存在/);assert(d.querySelector('#artifact-error').textContent.includes('重新上传'));
  await assert.rejects(shell.openSource('s_unsafe'),/当前项目范围/);assert(!requests.some(r=>r.path.includes('/projects/other/')));
  const count=requests.length;await assert.rejects(shell.openSource('../other'),/资料标识无效/);await assert.rejects(shell.openSource('s_text',0),/资料版本无效/);
  assert.equal(requests.length,count);
  assert(requests.every(r=>r.method==='GET'),'preview never submits a message, uploads, or writes source data');
  assert.equal(toasts.length,0,'stale responses must not toast in the new workspace');
  console.log(JSON.stringify({exact_frozen_revision_source_open:true,existing_sheet_and_page_preview:true,precise_strings_and_formula_disclosure:true,read_only_source_actions:true,authenticated_original_download:true,safe_text:true,view_during_pending_send:true,late_source_and_preview_responses_discarded:true,close_project_and_logout_races_guarded:true,no_automatic_task_or_upload:true,real_browser_layout_verified:false}));
 }finally{dom.window.close()}
}
run().catch(error=>{console.error(error);process.exitCode=1});
