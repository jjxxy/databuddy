// Transport-level runner regression with a mocked Playwright driver; this is
// not evidence that Chromium or a real business system has been tested.
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import net from 'node:net';
import {createHash} from 'node:crypto';
const dir=await mkdtemp(join(tmpdir(),'nexus-browser-stub-'));
await mkdir(join(dir,'node_modules','playwright-core'),{recursive:true});
await writeFile(join(dir,'package.json'),'{}');
const payload=Buffer.alloc(3*1024*1024+17,65);await writeFile(join(dir,'export.csv'),payload);
await writeFile(join(dir,'node_modules','playwright-core','index.js'),`
const fs=require('node:fs/promises');let routeHandler,sent=0,aborted=0,filled=0;const file={suggestedFilename:()=>"export.csv",path:async()=>process.env.FAKE_DOWNLOAD_PATH,delete:async()=>fs.unlink(process.env.FAKE_DOWNLOAD_PATH)};
function fakeRoute(){return {request:()=>({url:()=>"http://127.0.0.1:8080/submit",method:()=>"POST"}),continue:async()=>{sent++},abort:async()=>{aborted++}}}
const page={setDefaultTimeout(){},on(){},goto:async()=>{},screenshot:async()=>Buffer.from('png'),
 evaluate:async()=>({url:'http://127.0.0.1:8080/index',title:'测试',text:'sent='+sent+';aborted='+aborted+';filled='+filled,sensitive_url:false,scroll:[0,0],controls:[{ref:1,tag:'button',label:'查询',visible:true,disabled:false,sensitive:false},{ref:2,tag:'input',label:'期间',visible:true,disabled:false,sensitive:false},{ref:3,tag:'button',label:'导出',visible:true,disabled:false,sensitive:false}]}),
 waitForEvent:async()=>file,locator:()=>({nth:n=>({click:async()=>{if(n===0)await routeHandler(fakeRoute())},fill:async()=>{filled++}})}),mouse:{move:async()=>{},wheel:async()=>{}}};
const ctx={newPage:async()=>page,on(){},route:async(p,cb)=>{routeHandler=cb},routeWebSocket:async()=>{}};
exports.chromium={launch:async()=>({newContext:async()=>ctx,close:async()=>{}})};
`);
const port=await new Promise((resolve,reject)=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p))});s.on('error',reject)});
const token='t'.repeat(40),sid='b'.repeat(64),wid='c'.repeat(64);
const server=spawn(process.execPath,['browser_runner/service.mjs'],{cwd:process.cwd(),env:{...process.env,PORT:String(port),NEXUS_BROWSER_PLAYWRIGHT_ROOT:join(dir,'package.json'),NEXUS_BROWSER_TEST_MODE:'1',NEXUS_BROWSER_TOKEN:token,NEXUS_BROWSER_ALLOWED_HOSTS:'127.0.0.1',NEXUS_BROWSER_ALLOW_DOWNLOADS:'1',FAKE_DOWNLOAD_PATH:join(dir,'export.csv')},stdio:'ignore'});
const call=async(path,body)=>{const r=await fetch('http://127.0.0.1:'+port+path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});return [r.status,await r.json()]};
try {
  let ready=false;for(let i=0;i<60;i++){try{const r=await fetch('http://127.0.0.1:'+port+'/v1/health',{headers:{Authorization:'Bearer '+token}});if(r.ok){ready=true;break}}catch{}await new Promise(r=>setTimeout(r,50))}assert(ready);
  const base='/v1/sessions/'+sid;
  let [status,opened]=await call(base,{workspace_id:wid,url:'http://127.0.0.1:8080/index'});assert.equal(status,200);
  const fill={workspace_id:wid,action_id:'1'.repeat(64),agent_authorized:true,action:'fill',ref:2,revision:opened.revision,text:'2026-10'};
  const [bad]=await call(base+'/action',fill);assert.equal(bad,409);
  const [unknownStatus,unknown]=await call(base+'/action',fill);assert.equal(unknownStatus,200);assert.equal(unknown.state,'unknown');
  const confirmed={...fill,action_id:'2'.repeat(64),fill_confirmed:true};
  const [ok,result]=await call(base+'/action',confirmed);assert.equal(ok,200);assert.equal(result.text,'sent=0;aborted=0;filled=1');
  const [replayStatus,replayed]=await call(base+'/action',confirmed);assert.equal(replayStatus,200);assert.deepEqual(replayed,result);
  const click={workspace_id:wid,action_id:'3'.repeat(64),agent_authorized:true,action:'click',ref:1,revision:result.revision};
  const [clicked,after]=await call(base+'/action',click);assert.equal(clicked,200);assert.equal(after.text,'sent=0;aborted=1;filled=1');
  const dlid='4'.repeat(64),[downloadStatus,meta]=await call(base+'/download',{workspace_id:wid,action_id:dlid,agent_authorized:true,revision:after.revision,ref:3});
  assert.equal(downloadStatus,200);assert.equal(meta.bytes,payload.length);assert.equal(meta.download_id,dlid);
  assert.equal(meta.content_sha256,createHash('sha256').update(payload).digest('hex'));
  const [foreign]=await call(base+'/download-chunk',{workspace_id:'d'.repeat(64),download_id:dlid,offset:0,length:1});assert.equal(foreign,404);
  let bytes=0;const hasher=createHash('sha256');while(bytes<meta.bytes){
    const amount=Math.min(512*1024,meta.bytes-bytes),[chunkStatus,chunk]=await call(base+'/download-chunk',{workspace_id:wid,download_id:dlid,offset:bytes,length:amount});
    assert.equal(chunkStatus,200);assert.equal(chunk.offset,bytes);const raw=Buffer.from(chunk.content_base64,'base64');assert.equal(raw.length,amount);hasher.update(raw);bytes+=amount;
  }
  assert.equal(hasher.digest('hex'),meta.content_sha256);
  const [released]=await call(base+'/download-release',{workspace_id:wid,download_id:dlid});assert.equal(released,200);
  const [releasedAgain]=await call(base+'/download-release',{workspace_id:wid,download_id:dlid});assert.equal(releasedAgain,200);
  const [missing]=await call(base+'/download-chunk',{workspace_id:wid,download_id:dlid,offset:0,length:1});assert.equal(missing,409);
  console.log(JSON.stringify({stub:true,unconfirmed_fill_denied:true,confirmed_fill_once:true,agent_post_denied:true,unknown_replay_safe:true,download_3m_chunks:true,workspace_isolated:true,release_idempotent:true}));
}finally{server.kill('SIGTERM');await rm(dir,{recursive:true,force:true})}
