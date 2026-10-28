'use strict';
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webcrypto,createHash}=require('node:crypto');
const root=path.resolve(__dirname,'../../agent_service/static');
const html=fs.readFileSync(path.join(root,'data-workbench.html'),'utf8');
const script=fs.readFileSync(path.join(root,'data-workbench.js'),'utf8');
const bytes=Buffer.from('synthetic-image-fixture');const sha=createHash('sha256').update(bytes).digest('hex');
const tick=()=>new Promise(resolve=>setTimeout(resolve,5));
async function until(fn){for(let i=0;i<200;i++){if(fn())return;await tick()}throw Error('Timed out waiting for UI');}
function response(data,status=200){return {status,ok:status>=200&&status<300,json:async()=>data,blob:async()=>data};}
async function boot(options={}){
 const dom=new JSDOM(html,{url:'https://atlas.test/data-workbench.html?project=p_1',runScripts:'outside-only'}),w=dom.window,calls=[];
 w.sessionStorage.setItem('atlas_token','fixture-token');Object.defineProperty(w.crypto,'subtle',{value:options.noCrypto?undefined:webcrypto.subtle});
 w.URL.createObjectURL=()=> 'blob:https://atlas.test/fixture';w.URL.revokeObjectURL=()=>{};
 w.setTimeout=()=>1;w.clearTimeout=()=>{};
 const source={id:'src_1',project_id:'p_1',kind:'file',name:options.pdf?'账单.pdf':'账单<img src=x>.png',revision:1,content_hash:sha},foreign={...source,id:'foreign',project_id:'p_2',name:'其他项目.png'};
 const quote='2026-10 合肥办事处净收入 123.45 元 <img src=x onerror=alert(1)>';
 let record=null,releaseReview,releaseDownload,releaseOcr;
 w.fetch=async(url,opts={})=>{
  const u=new URL(url,w.location.href),method=opts.method||'GET';calls.push({path:u.pathname,search:u.searchParams,method,body:opts.body?JSON.parse(opts.body):null});
  if(u.pathname==='/api/projects/p_1/data-workflows'||u.pathname==='/api/projects/p_1/semantic-proposals'||u.pathname==='/api/projects/p_1/semantic-catalogs'||u.pathname==='/api/projects/p_1/data-facts')return response({items:[],next_offset:null});
  if(u.pathname==='/api/sources')return response([source,foreign]);
  if(u.pathname==='/api/projects/p_1/sources/src_1/ocr'){
    assert.deepEqual(JSON.parse(opts.body),{expected_revision:1,expected_sha256:sha,page:1});
    const value=options.ocrFail?response({detail:'本机 OCR 模型不可用，请配置本地组件。'},400):response({source_file_sha256:options.ocrWrongHash?'b'.repeat(64):sha,source_revision:1,page:1,image_width:100,image_height:50,ocr_executed:true,financial_facts_verified:false,lines:[{text:'收入 123.4500 元 <img src=x onerror=alert(1)>',box:[[1,2],[80,2],[80,12],[1,12]],human_review_required:true}]});
    return options.deferOcr?new Promise(resolve=>{releaseOcr=()=>resolve(value)}):value;
  }
  if(u.pathname==='/api/projects/p_1/visual-facts'&&method==='GET')return response({items:record?[{id:record.id,source_id:record.source_id,page:record.page,status:record.status}]:[],next_offset:null});
  if(u.pathname==='/api/projects/p_1/visual-facts'&&method==='POST'){
    const data=JSON.parse(opts.body);record={...data,id:'visual_1',status:'pending',content_sha256:'a'.repeat(64),source_revision:1,source_file_sha256:sha,approval_blockers:options.blockers||[],output_source_id:null};return response(record);
  }
  if(u.pathname==='/api/projects/p_1/visual-facts/visual_1')return response(record);
  if(u.pathname==='/api/projects/p_1/sources/src_1/artifact'){
    if(options.forbidden)return response({detail:'denied'},403);
    assert.equal(u.searchParams.get('revision'),'1');assert.equal(u.searchParams.get('expected_sha256'),sha);
    return response({kind:'source',id:'art_1',sha256:options.badMetadata?'f'.repeat(64):sha,source_refs:[{source_id:'src_1',revision:1}],download_url:'/api/projects/p_1/artifacts/art_1/download'});
  }
  if(u.pathname==='/api/projects/p_1/artifacts/art_1/download'){
    const value=response(new Blob([options.badBytes?Buffer.from('changed'):bytes],{type:'image/png'}));
    return options.deferDownload?new Promise(resolve=>{releaseDownload=()=>resolve(value)}):value;
  }
  if(u.pathname==='/api/projects/p_1/visual-facts/visual_1/review'){
    assert.deepEqual(JSON.parse(opts.body),{expected_sha256:'a'.repeat(64),decision:'approved',confirmed_visual_review:true});
    if(options.deferReview)return new Promise(resolve=>{releaseReview=()=>resolve(response({...record,status:'approved',output_source_id:'facts_1'}))});
    if(options.reviewFails)return response({detail:'failed'},500);
    return response({...record,status:'approved',output_source_id:'facts_1'});
  }
  throw Error('Unexpected '+method+' '+u.pathname);
 };
 w.eval(script+';window.testWorkbench={ready:workbenchReady};');await w.testWorkbench.ready;
 w.document.querySelector('#workbench-tab-semantics').click();await until(()=>!w.document.querySelector('#visual-refresh').disabled);
 assert.equal(w.document.querySelectorAll('#visual-source option').length,2,'only current-project source is selectable');
 const values={'source':'src_1','page':'1','quote':quote,'value':'123.45','measure':'净收入','unit':'元','period':'2026-10','dimension-name':'办事处','dimension-value':'合肥办事处'};
 for(const [key,value] of Object.entries(values))w.document.querySelector('#visual-'+key).value=value;
 w.document.querySelector('#visual-source').dispatchEvent(new w.Event('change'));
 if(options.noCreate)return {dom,w,calls,releaseOcr:()=>releaseOcr?.()};
 w.document.querySelector('#visual-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 await until(()=>!!w.document.querySelector('[data-visual-record]'));
 const details=w.document.querySelector('[data-visual-record]');details.open=true;details.dispatchEvent(new w.Event('toggle'));
 await until(()=>!!details.querySelector('[data-visual-open]')||!!details.querySelector('.proposal-warning'));
 return {dom,w,calls,details,releaseReview:()=>releaseReview?.(),releaseDownload:()=>releaseDownload?.()};
}
(async()=>{
 const a=await boot(),d=a.details;
 await until(()=>!!d.querySelector('[data-visual-open]'));
 assert.equal(d.querySelectorAll('img').length,0,'untrusted quote is text, not markup');
 assert.equal(d.querySelector('[data-visual-approve]').disabled,true,'save is not approval');
 d.querySelector('[data-visual-open]').click();await until(()=>!d.querySelector('[data-visual-ack]').disabled);
 assert.equal(d.querySelectorAll('img').length,1,'the verified original is visible');
 const ack=d.querySelector('[data-visual-ack]');ack.checked=true;ack.dispatchEvent(new a.w.Event('change'));
 d.querySelector('[data-visual-approve]').click();await until(()=>d.textContent.includes('已保存人工核对记录'));
 assert(d.textContent.includes('业务语义对齐')&&d.textContent.includes('不能用于正式数值加工或报告'),'manual review must not be described as permission to calculate or report');
 assert(a.calls.some(c=>c.path.endsWith('/review')&&c.body.confirmed_visual_review===true));a.dom.window.close();
 for(const opts of [{forbidden:true},{badMetadata:true},{badBytes:true},{noCrypto:true}]){
  const b=await boot(opts),part=b.details;await until(()=>!!part.querySelector('[data-visual-open]'));
  part.querySelector('[data-visual-open]').click();await until(()=>part.textContent.includes('原件无法打开或核验'));
  assert(part.querySelector('[data-visual-ack]').disabled);assert(part.querySelector('[data-visual-approve]').disabled);
  assert(!b.calls.some(c=>c.path.endsWith('/review')));b.dom.window.close();
 }
 const closed=await boot({deferDownload:true}),part=closed.details;await until(()=>!!part.querySelector('[data-visual-open]'));
 part.querySelector('[data-visual-open]').click();await until(()=>closed.calls.some(c=>c.path.endsWith('/download')));
 part.open=false;part.dispatchEvent(new closed.w.Event('toggle'));closed.releaseDownload();await tick();await tick();
 assert(part.querySelector('[data-visual-ack]').disabled,'closing an in-flight preview cannot enable acknowledgement');
 assert(part.querySelector('[data-visual-approve]').disabled);closed.dom.window.close();
 const blocked=await boot({blockers:[{missing_or_unsupported:['unit']}]});await until(()=>blocked.details.textContent.includes('未完整核对'));
 assert.equal(blocked.details.querySelector('[data-visual-approve]'),null);blocked.dom.window.close();
 const stale=await boot({deferReview:true}),s=stale.details;await until(()=>!!s.querySelector('[data-visual-open]'));
 s.querySelector('[data-visual-open]').click();await until(()=>!s.querySelector('[data-visual-ack]').disabled);
 const check=s.querySelector('[data-visual-ack]');check.checked=true;check.dispatchEvent(new stale.w.Event('change'));s.querySelector('[data-visual-approve]').click();
 await until(()=>stale.calls.some(c=>c.path.endsWith('/review')));stale.w.history.replaceState({},'', '?project=p_2');stale.releaseReview();await tick();await tick();
 assert(!s.textContent.includes('已保存人工核对记录'),'response for old project must not appear in the new project');stale.dom.window.close();
 const failure=await boot({reviewFails:true}),f=failure.details;await until(()=>!!f.querySelector('[data-visual-open]'));
 f.querySelector('[data-visual-open]').click();await until(()=>!f.querySelector('[data-visual-ack]').disabled);
 const checkbox=f.querySelector('[data-visual-ack]');checkbox.checked=true;checkbox.dispatchEvent(new failure.w.Event('change'));f.querySelector('[data-visual-approve]').click();
 await until(()=>f.textContent.includes('未收到确认结果'));assert(!f.textContent.includes('已保存人工核对记录'));failure.dom.window.close();
 const ocr=await boot({noCreate:true}),od=ocr.w.document;od.querySelector('#visual-quote').value='';od.querySelector('#visual-ocr-run').click();
 await until(()=>!!od.querySelector('[data-visual-ocr-use]'));assert.equal(od.querySelector('#visual-quote').value,'','OCR never automatically modifies a candidate');
 assert.equal(od.querySelectorAll('#visual-ocr-results img').length,0,'recognized text must never become HTML');
 assert(od.querySelector('#visual-ocr-results').textContent.includes('原图第 1 处'));assert(od.querySelector('#visual-ocr-results').textContent.includes('尚未高亮'));assert(!od.querySelector('#visual-ocr-results').textContent.includes('像素'));od.querySelector('[data-visual-ocr-use]').click();
 assert(od.querySelector('#visual-quote').value.includes('123.4500'));assert(!ocr.calls.some(c=>c.path.endsWith('/visual-facts')&&c.method==='POST'),'choosing OCR text never saves or approves a fact');ocr.dom.window.close();
 const wrong=await boot({noCreate:true,ocrWrongHash:true}),wd=wrong.w.document;wd.querySelector('#visual-ocr-run').click();
 await until(()=>wd.querySelector('#visual-ocr-status').textContent.includes('识别未完成'));assert(!wd.querySelector('[data-visual-ocr-use]'));wrong.dom.window.close();
 const noModel=await boot({noCreate:true,ocrFail:true}),nd=noModel.w.document;nd.querySelector('#visual-ocr-run').click();
 await until(()=>nd.querySelector('#visual-ocr-status').textContent.includes('本机 OCR 模型不可用'));assert(!nd.querySelector('[data-visual-ocr-use]'));noModel.dom.window.close();
 const pdf=await boot({noCreate:true,pdf:true});assert(pdf.w.document.querySelector('#visual-ocr-run').disabled);assert(pdf.w.document.querySelector('#visual-ocr-status').textContent.includes('PDF'));pdf.dom.window.close();
 const badPage=await boot({noCreate:true}),pd=badPage.w.document;pd.querySelector('#visual-page').value='2';pd.querySelector('#visual-page').dispatchEvent(new badPage.w.Event('input'));
 assert(pd.querySelector('#visual-ocr-run').disabled);assert(pd.querySelector('#visual-ocr-status').textContent.includes('第 1 页'));badPage.dom.window.close();
 const changed=await boot({noCreate:true,deferOcr:true}),cd=changed.w.document;cd.querySelector('#visual-ocr-run').click();
 await until(()=>changed.calls.some(c=>c.path.endsWith('/ocr')));cd.querySelector('#visual-source').value='';cd.querySelector('#visual-source').dispatchEvent(new changed.w.Event('change'));changed.releaseOcr();await tick();await tick();
 assert(!cd.querySelector('[data-visual-ocr-use]'),'late OCR output for previous source must be discarded');changed.dom.window.close();
 process.stdout.write('visual-facts UI: passed\n');
})().catch(error=>{console.error(error);process.exitCode=1});
