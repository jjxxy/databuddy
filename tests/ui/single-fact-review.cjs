'use strict';
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webcrypto,createHash}=require('node:crypto');
const root=path.resolve(__dirname,'../../agent_service/static'),sha='a'.repeat(64),handoff='b'.repeat(64),previewSha='c'.repeat(64);
const raw=Buffer.from('isolated original fixture'),fileSha=createHash('sha256').update(raw).digest('hex');
const quote='2026-10 合肥收入 9007199254740993.12 元 😀',textSha=createHash('sha256').update(quote).digest('hex');
const reply=(value,status=200)=>({ok:status<400,status,json:async()=>value,blob:async()=>value});
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
async function until(fn){for(let i=0;i<150;i++){if(fn())return;await tick()}throw Error('UI did not settle');}
async function boot(opts={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'data-workbench.html'),'utf8'),{url:'https://atlas.test/data-workbench.html?project=p_1&workflow=wf_1',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];
 w.sessionStorage.setItem('atlas_token','fixture-token');Object.defineProperty(w.crypto,'subtle',{value:opts.noCrypto?undefined:webcrypto.subtle});w.setTimeout=()=>1;w.clearTimeout=()=>{};
 w.URL.createObjectURL=()=> 'blob:https://atlas.test/file';w.URL.revokeObjectURL=()=>{};w.HTMLAnchorElement.prototype.click=()=>{};
 const imageSrc=Object.getOwnPropertyDescriptor(w.HTMLImageElement.prototype,'src');Object.defineProperty(w.HTMLImageElement.prototype,'src',{get:imageSrc.get,set(value){imageSrc.set.call(this,value);setImmediate(()=>this.dispatchEvent(new w.Event(opts.imageFails?'error':'load')))}});
 let releaseConfirm;
 const workflow={workflow_id:'wf_1',name:'任务',draft_sha256:sha,candidate_run:'r_1',steps:[],inputs:{},document:{plan:{stages:[]}}};
 const fact={quote,value_as_printed:'9007199254740993.12',normalized_value:'9007199254740993.12',measure:'收入<script>bad</script>',unit:'元',period:'2026-10',dimensions:{地区:'合肥'}};
 const original={id:'src_1',revision:1,sha256:fileSha,...(opts.text?{text_sha256:textSha,text_start:3,text_end:3+Array.from(quote).length}:{page:1})};
 const preview={project_id:'p_1',run_id:'r_1',plan_sha256:sha,handoff_sha256:opts.badHandoff?'d'.repeat(64):handoff,preview_sha256:previewSha,original,fact,source:{id:'data_1',revision:1,sha256:'f'.repeat(64)},fact_review:{id:'fact_1',sha256:'e'.repeat(64),kind:opts.text?'text':'visual'},scope:'single_fact_original_value_only'};
 w.fetch=async(url,options={})=>{
  const u=new URL(url,w.location.href),method=options.method||'GET';calls.push({path:u.pathname,query:u.searchParams,method,body:options.body?JSON.parse(options.body):null});
  if(u.pathname.endsWith('/data-workflows'))return reply({items:[{id:'wf_1',name:'任务'}]});
  if(u.pathname.endsWith('/data-workflows/wf_1'))return reply(workflow);
  if(u.pathname.endsWith('/semantic-proposals'))return reply({items:[],next_offset:null});
  if(u.pathname==='/api/runs')return reply([]);
  if(u.pathname.endsWith('/data-results/r_1'))return reply({run_id:'r_1',plan_sha256:sha,manifest_sha256:handoff,total:1,rows:[{n:'9007199254740993.12'}],schema:[{name:'n',label:'金额'}],stages:[],context:{input_evidence:{facts:{source_id:'data_1',source_revision:1,content_hash:'f'.repeat(64)}},requires_source_review:true,semantic_review_reasons:['FACT_SEMANTIC_ALIGNMENT_REQUIRED']}});
  if(u.pathname.endsWith('/single-fact-review/confirm')){
   assert.deepEqual(JSON.parse(options.body),{expected_preview_sha256:previewSha,confirm_original_value:true});
   const value=opts.confirmFail?reply({detail:'来源版本已变化，请重新核对'},400):reply({receipt_id:'sfv_1',preview_sha256:previewSha,scope:'single_fact_original_value_only',download_url:'/api/projects/p_1/data-results/r_1/single-fact-review/download'});
   return opts.deferConfirm?new Promise(resolve=>{releaseConfirm=()=>resolve(value)}):value;
  }
  if(u.pathname.endsWith('/single-fact-review/download'))return reply(new Blob(['{"value":"9007199254740993.12"}'],{type:'application/json'}));
  if(u.pathname.endsWith('/single-fact-review'))return opts.previewFail?reply({detail:'该候选不满足单条原值条件'},400):reply(preview);
  if(u.pathname.endsWith('/sources/src_1/artifact')){
   assert.equal(u.searchParams.get('revision'),'1');assert.equal(u.searchParams.get('expected_sha256'),fileSha);
   return opts.forbidden?reply({detail:'无权查看'},403):reply({kind:'source',id:'a_1',sha256:opts.badMetadata?'d'.repeat(64):fileSha,source_refs:[{source_id:'src_1',revision:1}],download_url:'/api/projects/p_1/artifacts/a_1/download'});
  }
  if(u.pathname.endsWith('/artifacts/a_1/download'))return reply(new Blob([opts.badBytes?Buffer.from('changed'):raw],{type:opts.text?'text/plain':'image/png'}));
  if(u.pathname.endsWith('/data-documents/src_1')){
   assert.equal(u.searchParams.get('offset'),'3');assert.equal(u.searchParams.get('limit'),String(Array.from(quote).length));
   return reply({source_revision:1,source_file_sha256:fileSha,source_text_sha256:opts.badText?'e'.repeat(64):textSha,offset:3,end_offset:3+Array.from(quote).length,text:quote});
  }
  throw Error('Unexpected '+method+' '+u.pathname);
 };
 w.eval(fs.readFileSync(path.join(root,'data-workbench.js'),'utf8')+';window.test={ready:workbenchReady,state:wbState,render:wbRenderResult};');await w.test.ready;
 return {dom,w,d,calls,releaseConfirm:()=>releaseConfirm?.()};
}
async function load(a){a.d.querySelector('#single-fact-load').click();await until(()=>!!a.d.querySelector('[data-single-original]')||a.d.querySelector('#single-fact-status').classList.contains('error'));}
async function open(a){a.d.querySelector('[data-single-original]').click();await until(()=>!a.d.querySelector('[data-single-ack]').disabled||a.d.querySelector('.visual-original').textContent.includes('无法打开或核验'));}
function acknowledge(a){const ack=a.d.querySelector('[data-single-ack]');ack.checked=true;ack.dispatchEvent(new a.w.Event('change'));}
(async()=>{
 const a=await boot();assert.equal(a.d.querySelector('#single-fact-panel').hidden,false);await load(a);
 assert(a.d.querySelector('#single-fact-body').textContent.includes('9007199254740993.12'));assert.equal(a.d.querySelectorAll('#single-fact-body script').length,0);
 assert(a.d.querySelector('[data-single-confirm]').disabled,'original must be opened before confirmation');await open(a);assert(a.d.querySelector('[data-single-confirm]').disabled,'an explicit checkbox is required');
 acknowledge(a);a.d.querySelector('[data-single-confirm]').click();await until(()=>!!a.d.querySelector('[data-single-download]'));
 assert(a.d.querySelector('#single-fact-body').textContent.includes('不能据此汇总'));a.d.querySelector('[data-single-download]').click();await until(()=>a.d.querySelector('#single-fact-status').textContent.includes('已准备单条原值下载'));
 assert(a.calls.some(c=>c.path.endsWith('/single-fact-review/download')));
 assert(!a.calls.some(c=>c.path.endsWith('/publish')));a.dom.window.close();
 for(const opts of [{forbidden:true},{badMetadata:true},{badBytes:true},{noCrypto:true},{imageFails:true},{text:true,badText:true}]){
  const b=await boot(opts);await load(b);await open(b);assert(b.d.querySelector('[data-single-ack]').disabled);assert(b.d.querySelector('[data-single-confirm]').disabled);
  assert(!b.calls.some(c=>c.path.endsWith('/confirm')));b.dom.window.close();
 }
 const text=await boot({text:true});await load(text);await open(text);assert(!text.d.querySelector('[data-single-ack]').disabled);assert(text.d.querySelector('#single-fact-body').textContent.includes(quote));text.dom.window.close();
 for(const opts of [{badHandoff:true},{previewFail:true}]){const b=await boot(opts);await load(b);assert(!b.d.querySelector('[data-single-confirm]'));b.dom.window.close();}
 const stale=await boot({deferConfirm:true});await load(stale);await open(stale);acknowledge(stale);stale.d.querySelector('[data-single-confirm]').click();await until(()=>stale.calls.some(c=>c.path.endsWith('/confirm')));
 stale.w.history.replaceState({},'','?project=p_2');stale.releaseConfirm();await tick();await tick();assert(!stale.d.querySelector('[data-single-download]'),'a delayed confirmation cannot be attached to a different project');stale.dom.window.close();
 const replaced=await boot();await load(replaced);await open(replaced);acknowledge(replaced);replaced.w.test.state.workflow.candidate_run='r_2';replaced.w.test.state.resultId='r_2';replaced.w.test.render();
 assert(!replaced.d.querySelector('[data-single-confirm]'),'a different candidate invalidates the entire review');replaced.dom.window.close();
 const failure=await boot({confirmFail:true});await load(failure);await open(failure);acknowledge(failure);failure.d.querySelector('[data-single-confirm]').click();await until(()=>failure.d.querySelector('#single-fact-body').textContent.includes('本次请求未通过'));
 assert(!failure.d.querySelector('[data-single-download]'));failure.dom.window.close();
 process.stdout.write('single-fact-review UI: passed\n');
})().catch(error=>{console.error(error);process.exitCode=1});
