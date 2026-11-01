// Transport-level regression: mocked Playwright; not a real Chromium test.
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';
import net from 'node:net';
const dir=await mkdtemp(join(tmpdir(),'atlas-post-stub-'));
await mkdir(join(dir,'node_modules','playwright-core'),{recursive:true});await writeFile(join(dir,'package.json'),'{}');
await writeFile(join(dir,'node_modules','playwright-core','index.js'),`
const {EventEmitter}=require('node:events');let routeHandler,sent=0,aborted=0,bodyRead='';const emitter=new EventEmitter();
const payload=Buffer.from('{"month":"2026-10","amount":"125.00"}');
const req={url:()=>"http://127.0.0.1:8080/submit?period=10",method:()=>"POST",headers:()=>({'content-type':'application/json'}),postDataBuffer:()=>payload,response:async()=>({status:()=>200})};
function post(){return {request:()=>req,continue:async()=>{sent++;bodyRead=payload.toString('utf8');setImmediate(()=>emitter.emit('requestfinished',req))},abort:async()=>{aborted++}}}
function get(){return {request:()=>({url:()=>"http://127.0.0.1:8080/update",method:()=>"GET"}),continue:async()=>{throw Error('unexpected GET')},abort:async()=>{aborted++}}}
function custom(){return {request:()=>({...req,headers:()=>({'content-type':'application/json','x-account':'hidden-account'})}),continue:async()=>{throw Error('custom header sent')},abort:async()=>{aborted++}}}
function secret(){return {request:()=>({...req,postDataBuffer:()=>Buffer.from('{\"password\":\"hidden\"}')}),continue:async()=>{throw Error('sensitive request sent')},abort:async()=>{aborted++}}}
const page={setDefaultTimeout(){},on(){},goto:async()=>{},screenshot:async()=>Buffer.from('89504e470d0a1a0a0000000d494844520000000100000001','hex'),
 evaluate:async()=>({url:'http://127.0.0.1:8080/index',title:'测试',text:'sent='+sent+';aborted='+aborted+';body='+bodyRead,sensitive_url:false,scroll:[0,0],controls:[{ref:1,tag:'button',label:'提交',visible:true,disabled:false,sensitive:false},{ref:2,tag:'button',label:'多次发送',visible:true,disabled:false,sensitive:false},{ref:3,tag:'button',label:'密码提交',visible:true,disabled:false,sensitive:false},{ref:4,tag:'button',label:'隐藏字段',visible:true,disabled:false,sensitive:false}]}),
 waitForEvent:(name,options)=>new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('timeout')),options.timeout);emitter.once(name,x=>{clearTimeout(timer);resolve(x)})}),
 locator:()=>({nth:n=>({click:async()=>{await routeHandler(get());if(n===2)await routeHandler(secret());else if(n===3)await routeHandler(custom());else await routeHandler(post());if(n===1)await routeHandler(post())}})}),mouse:{move:async()=>{},wheel:async()=>{}}};
const ctx={newPage:async()=>page,on(){},route:async(p,cb)=>{routeHandler=cb},routeWebSocket:async()=>{}};
exports.chromium={launch:async()=>({newContext:async()=>ctx,close:async()=>{}})};
`);
const port=await new Promise((resolve,reject)=>{const s=net.createServer();s.listen(0,'127.0.0.1',()=>{const p=s.address().port;s.close(()=>resolve(p))});s.on('error',reject)});
const token='t'.repeat(40),sid='b'.repeat(64),wid='c'.repeat(64),base='/v1/sessions/'+sid;
const server=spawn(process.execPath,['browser_runner/service.mjs'],{cwd:process.cwd(),env:{...process.env,PORT:String(port),ATLAS_BROWSER_PLAYWRIGHT_ROOT:join(dir,'package.json'),ATLAS_BROWSER_TEST_MODE:'1',ATLAS_BROWSER_TOKEN:token,ATLAS_BROWSER_ALLOWED_HOSTS:'127.0.0.1'},stdio:'ignore'});
const call=async(path,body)=>{const r=await fetch('http://127.0.0.1:'+port+path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});return [r.status,await r.json()]};
try{
 let ready=false;for(let i=0;i<60;i++){try{const r=await fetch('http://127.0.0.1:'+port+'/v1/health',{headers:{Authorization:'Bearer '+token}});if(r.ok){ready=true;break}}catch{}await new Promise(r=>setTimeout(r,50))}assert(ready);
 const [openStatus,opened]=await call(base,{workspace_id:wid,url:'http://127.0.0.1:8080/index'});assert.equal(openStatus,200);
 const [captureStatus,captured]=await call(base+'/action',{workspace_id:wid,action_id:'1'.repeat(64),agent_authorized:true,action:'capture_post',capture_confirmed:true,revision:opened.revision,ref:1});assert.equal(captureStatus,200);const p=captured.pending_post;assert(p.id&&p.digest&&p.body_sha256);assert(!JSON.stringify(captured).includes('125.00'));
 const [inspectStatus,inspected]=await call(base+'/observe',{workspace_id:wid,action_id:'2'.repeat(64)});assert.equal(inspectStatus,200);assert(inspected.text.includes('sent=0;aborted=1'));
 const [foreign]=await call(base+'/post-reveal',{workspace_id:'d'.repeat(64),post_id:p.id,digest:p.digest});assert.equal(foreign,404);
 const [revealStatus,reveal]=await call(base+'/post-reveal',{workspace_id:wid,post_id:p.id,digest:p.digest});assert.equal(revealStatus,200);const text='{"month":"2026-10","amount":"125.00"}';assert.equal(reveal.pending_post.body,text);assert.equal(p.body_sha256,createHash('sha256').update(Buffer.from(text)).digest('hex'));assert.equal(p.url,'http://127.0.0.1:8080/submit?period=10');
 const [bad]=await call(base+'/post-decision',{workspace_id:wid,post_id:p.id,digest:'0'.repeat(64),approve:true});assert.equal(bad,409);
 const decision={workspace_id:wid,post_id:p.id,digest:p.digest,approve:true};const [decisionStatus,result]=await call(base+'/post-decision',decision);assert.equal(decisionStatus,200);assert.equal(result.state,'sent');assert.equal(result.transport_code,200);
 const [repeatStatus,repeated]=await call(base+'/post-decision',decision);assert.equal(repeatStatus,200);assert.equal(repeated.state,'sent');
 const [observeStatus,after]=await call(base+'/observe',{workspace_id:wid,action_id:'3'.repeat(64)});assert.equal(observeStatus,200);assert(after.text.includes('sent=1;aborted=1;body='+text));
 // A second pending action can be rejected and repeated without sending.
 const second='e'.repeat(64),base2='/v1/sessions/'+second;
 const [,opened2]=await call(base2,{workspace_id:wid,url:'http://127.0.0.1:8080/index'});
 const [,captured2]=await call(base2+'/action',{workspace_id:wid,action_id:'4'.repeat(64),agent_authorized:true,action:'capture_post',capture_confirmed:true,revision:opened2.revision,ref:1});
 const p2=captured2.pending_post,deny={workspace_id:wid,post_id:p2.id,digest:p2.digest,approve:false};
 const [denyStatus,denied]=await call(base2+'/post-decision',deny);assert.equal(denyStatus,200);assert.equal(denied.state,'rejected');
 const [denyAgainStatus,deniedAgain]=await call(base2+'/post-decision',deny);assert.equal(denyAgainStatus,200);assert.equal(deniedAgain.state,'rejected');
 const [,afterDeny]=await call(base2+'/observe',{workspace_id:wid,action_id:'5'.repeat(64)});assert(afterDeny.text.includes('sent=1;'));
 // Resume human control so the same session can start a fresh capture.
 const [,human]=await call(base2+'/mode',{workspace_id:wid,action_id:'6'.repeat(64),mode:'human'});
 const [,agent]=await call(base2+'/mode',{workspace_id:wid,action_id:'7'.repeat(64),mode:'agent'});
 const [,multi]=await call(base2+'/action',{workspace_id:wid,action_id:'8'.repeat(64),agent_authorized:true,action:'capture_post',capture_confirmed:true,revision:agent.revision,ref:2});
 const [multiRevealStatus,multiReveal]=await call(base2+'/post-reveal',{workspace_id:wid,post_id:multi.pending_post.id,digest:multi.pending_post.digest});assert.equal(multiRevealStatus,200);assert.equal(multiReveal.pending_post.multiple_post,true);
 const [multiApproval]=await call(base2+'/post-decision',{workspace_id:wid,post_id:multi.pending_post.id,digest:multi.pending_post.digest,approve:true});assert.equal(multiApproval,409);
 const [,human2]=await call(base2+'/mode',{workspace_id:wid,action_id:'9'.repeat(64),mode:'human'});const [,agent2]=await call(base2+'/mode',{workspace_id:wid,action_id:'a'.repeat(64),mode:'agent'});
 const [sensitiveStatus]=await call(base2+'/action',{workspace_id:wid,action_id:'f'.repeat(64),agent_authorized:true,action:'capture_post',capture_confirmed:true,revision:agent2.revision,ref:3});assert.equal(sensitiveStatus,409);
 // Full FastAPI -> actual runner HTTP -> mocked Playwright, with both approvals.
 const [,human3]=await call(base2+'/mode',{workspace_id:wid,action_id:'b'.repeat(64),mode:'human'});const [,agent3]=await call(base2+'/mode',{workspace_id:wid,action_id:'c'.repeat(64),mode:'agent'});
 const [customStatus]=await call(base2+'/action',{workspace_id:wid,action_id:'d'.repeat(64),agent_authorized:true,action:'capture_post',capture_confirmed:true,revision:agent3.revision,ref:4});assert.equal(customStatus,409);
 const helper=spawn(process.env.ATLAS_TEST_PYTHON||'./.venv/bin/python',['tests/browser_post_capture_helper.py'],{cwd:process.cwd(),env:{...process.env,PYTHONPATH:'.',DATA_DIR:join(dir,'backend-data'),APP_TOKEN:'local-test-token-123456789',ATLAS_BROWSER_URL:'http://127.0.0.1:'+port,ATLAS_BROWSER_TOKEN:token,ATLAS_WEB_ENABLED:'1',ATLAS_WEB_ALLOWED_HOSTS:'127.0.0.1',API_ALLOWED_HOSTS:'127.0.0.1'}});
 const third='9'.repeat(64),base3='/v1/sessions/'+third;await call(base+'/close',{workspace_id:wid,action_id:'a'.repeat(64)});
 const [thirdStatus,thirdOpened]=await call(base3,{workspace_id:wid,url:'http://127.0.0.1:8080/index'});assert.equal(thirdStatus,200);
 helper.stdin.end(JSON.stringify({session_id:third,workspace_id:wid,origin:'http://127.0.0.1:8080',snapshot:thirdOpened,expected_url:'http://127.0.0.1:8080/submit?period=10',expected_body:text}));
 let helperOut='',helperErr='';helper.stdout.on('data',v=>helperOut+=v);helper.stderr.on('data',v=>helperErr+=v);
 const helperCode=await new Promise((resolve,reject)=>{helper.on('exit',resolve);helper.on('error',reject)});assert.equal(helperCode,0,helperErr);assert(JSON.parse(helperOut).two_stage_approval);
 console.log(JSON.stringify({mock:true,fastapi_bridge:true,preapproval_never_sent:true,exact_payload_reviewed:true,bad_digest_rejected:true,cross_workspace_rejected:true,one_send_only:true,other_get_blocked:true,rejection_idempotent:true,multiple_posts_rejected:true,sensitive_body_blocked:true,unknown_business_header_blocked:true}));
}finally{server.kill('SIGTERM');await rm(dir,{recursive:true,force:true})}
