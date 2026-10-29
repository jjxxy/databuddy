// Actual Work progress module: safe output, project races and account cleanup.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(resolve=>setTimeout(resolve,5))}throw Error('Work UI did not settle')};
(async()=>{
  const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
  try{
    w.HTMLDialogElement.prototype.showModal=function(){this.open=true};
    w.HTMLDialogElement.prototype.close=function(){this.open=false;this.dispatchEvent(new w.Event('close'))};
    let projectId='p',pending=null,delay=false,opened=null;
    w.setTimeout=()=>1;w.clearTimeout=()=>{};
    const response={configured:true,executions:[{id:'a'.repeat(64),state:'running',created_at:'2026-10-29T01:00:00Z',stdout:'<img src=x onerror=steal()> exact=9007199254740993.00000001',output_files:[{path:'output/report.csv'}]}]};
    w.atlasBusiness={getState:()=>({projectId}),toast:()=>{},api:async route=>{
      assert.equal(route,'/projects/'+projectId+'/work-environment');
      if(delay)return new Promise(resolve=>pending=resolve);
      return response;
    }};
    w.atlasShell={openArtifacts:async kind=>{opened=kind}};
    w.eval(fs.readFileSync(path.join(root,'work-runtime.js'),'utf8'));
    d.querySelector('#work-progress').click();
    await wait(()=>d.querySelector('.work-execution'));
    assert(d.querySelector('#work-progress-dialog').open);
    assert(d.querySelector('.work-execution').textContent.includes('正在执行'));
    assert(d.querySelector('.work-execution').textContent.includes('9007199254740993.00000001'));
    assert.equal(d.querySelector('.work-execution img'),null);
    d.querySelector('[data-work-artifacts]').click();await wait(()=>opened==='work');
    assert.equal(d.querySelector('#work-progress-dialog').open,false);
    delay=true;d.querySelector('#work-progress').click();await wait(()=>pending);
    projectId='new-project';w.atlasWorkRuntime.projectChanged();
    pending(response);await new Promise(resolve=>setTimeout(resolve,10));
    assert.equal(d.querySelectorAll('.work-execution').length,0);
    assert.equal(d.querySelector('#work-progress-dialog').open,false);
    delay=false;d.querySelector('#work-progress').click();await wait(()=>d.querySelector('.work-execution'));
    w.atlasWorkRuntime.reset();
    assert.equal(d.querySelectorAll('.work-execution').length,0);
    assert.equal(d.querySelector('[data-work-notice]').textContent,'');
    console.log(JSON.stringify({work_progress:true,untrusted_logs_as_text:true,exact_values_preserved:true,stale_project_discarded:true,logout_clears_content:true,artifact_filter:true,real_browser_layout_verified:false}));
  }finally{w.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
