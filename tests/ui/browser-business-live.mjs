/* Actual Chromium + browser service + local business fixture; no live company
 * site, Atlas deployment, model, or gVisor. Set ATLAS_BROWSER_EXECUTABLE to a
 * locally installed Chromium. No browser is downloaded by this test. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {spawn,spawnSync} from 'node:child_process';
import {createHash,randomBytes} from 'node:crypto';
import {access,mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';

const executable=process.env.ATLAS_BROWSER_EXECUTABLE;
if(!executable)throw Error('Set ATLAS_BROWSER_EXECUTABLE to a locally installed Chromium; the test does not download browsers');
await access(executable);
const reports=Buffer.from('office,amount\n'+'Shanghai,123.45\n'.repeat(180000));
assert(reports.length>2*1024*1024);
let loginPosts=0,queryRequests=0,downloads=0,interruptedDownloads=0,submissions=0,passivePosts=0,lastOffice='';
const fixture=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');
  if(url.pathname==='/login'&&req.method==='POST'){
    let body='';for await(const part of req)body+=part;
    if(body!=='fixture-password'){res.writeHead(403);return res.end('denied')}
    loginPosts++;res.setHeader('Set-Cookie','login=fixture; HttpOnly; SameSite=Lax');return res.end('ok');
  }
  if(url.pathname==='/passive'&&req.method==='POST'){passivePosts++;return res.end('unexpected')}
  if(url.pathname==='/query'){
    if(!(req.headers.cookie||'').includes('login=fixture')){res.writeHead(401);return res.end('login required')}
    queryRequests++;lastOffice=url.searchParams.get('office');return res.end('verified query '+lastOffice);
  }
  if(url.pathname==='/report.csv'){
    if(!(req.headers.cookie||'').includes('login=fixture')){res.writeHead(401);return res.end('login required')}
    downloads++;res.writeHead(200,{'Content-Type':'text/csv','Content-Disposition':'attachment; filename="report.csv"','Content-Length':reports.length});return res.end(reports);
  }
  if(url.pathname==='/interrupted.csv'){
    interruptedDownloads++;res.writeHead(200,{'Content-Type':'text/csv','Content-Disposition':'attachment; filename=\"interrupted.csv\"','Content-Length':1024*1024});
    res.write('office,amount\nShanghai,1');res.socket.destroy();return;
  }
  if(url.pathname==='/submit'&&req.method==='POST'){
    if(!(req.headers.cookie||'').includes('login=fixture')){res.writeHead(401);return res.end('login required')}
    submissions++;return res.end('receipt-fixture-'+submissions);
  }
  res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});
  return res.end(`<!doctype html><meta charset="utf-8"><title>Business fixture</title><h1>Business fixture</h1>
    <input name="password" type="password" aria-label="Password"><button id="login">Login</button>
    <select id="office" aria-label="Office"><option value="all">All</option><option value="Shanghai">Shanghai</option></select><input id="period" aria-label="Period">
    <button id="query">查询</button><a href="/report.csv">导出数据</a><a href="/interrupted.csv">导出中断资料</a><button id="submit">提交</button><p id="result"></p>
    <script>const result=document.querySelector('#result');
    document.querySelector('#login').onclick=async()=>{const r=await fetch('/login',{method:'POST',body:document.querySelector('[name=password]').value});result.textContent=r.ok?'Logged in':'Denied'};
    document.querySelector('#query').onclick=async()=>{const r=await fetch('/query?office='+encodeURIComponent(document.querySelector('#office').value));result.textContent=await r.text()};
    document.querySelector('#submit').onclick=async()=>{try{const r=await fetch('/submit',{method:'POST',body:'fixture=1'});result.textContent=await r.text()}catch{result.textContent='Submission blocked'}};
    fetch('/passive',{method:'POST'}).catch(()=>{});
    </script>`);
});
await new Promise(r=>fixture.listen(0,'127.0.0.1',r));
const reserve=http.createServer();await new Promise(r=>reserve.listen(0,'127.0.0.1',r));const port=reserve.address().port;await new Promise(r=>reserve.close(r));
const root=fileURLToPath(new URL('../..',import.meta.url)),token=randomBytes(24).toString('hex');
const scope='b'.repeat(64),session='a'.repeat(64),other='c'.repeat(64);
const origin='http://127.0.0.1:'+fixture.address().port,api='http://127.0.0.1:'+port;
const child=spawn(process.execPath,[root+'/browser_runner/service.mjs'],{cwd:root,env:{...process.env,PORT:String(port),ATLAS_BROWSER_TOKEN:token,
  ATLAS_BROWSER_TEST_MODE:'1',ATLAS_BROWSER_PROXY:'',ATLAS_BROWSER_WORKSPACE_ID:scope,ATLAS_BROWSER_ALLOWED_HOSTS:'127.0.0.1',
  ATLAS_BROWSER_ALLOW_DOWNLOADS:'1',ATLAS_BROWSER_MAX_DOWNLOAD_MB:'6',ATLAS_BROWSER_PLAYWRIGHT_ROOT:root+'/tests/ui/package.json'},stdio:['ignore','pipe','pipe']});
let serviceOutput='';child.stdout.on('data',b=>serviceOutput+=b);child.stderr.on('data',b=>serviceOutput+=b);
async function call(id,operation,body={},expected=200){
  const payload={workspace_id:scope,action_id:randomBytes(32).toString('hex'),...body};
  const response=await fetch(api+'/v1/sessions/'+id+(operation?'/'+operation:''),{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const result=await response.json();assert.equal(response.status,expected,JSON.stringify({status:response.status,error:result.error,state:result.state}));return result;
}
const pick=(page,label)=>{const item=page.controls.find(c=>c.label===label);assert(item,'Missing control '+label);return item.ref};
try{
  let ready=false;for(let i=0;i<100;i++){try{const r=await fetch(api+'/v1/health',{headers:{Authorization:'Bearer '+token}});if(r.ok){ready=true;break}}catch{}await new Promise(r=>setTimeout(r,100))}
  assert(ready,'Browser service failed to start: '+serviceOutput.slice(0,1000));
  assert.equal((await fetch(api+'/v1/health')).status,401);
  let page=await call(session,'',{url:origin+'/'});assert(page.text.includes('Business fixture'));
  assert.equal(passivePosts,0,'Agent-mode background POST must be blocked');
  const password=page.controls.find(c=>c.type==='password');assert(password.sensitive);
  await call(session,'action',{agent_authorized:true,revision:page.revision,action:'fill',ref:password.ref,text:'fixture-password'},409);
  await call(session,'mode',{mode:'human'});page=await call(session,'observe');
  page=await call(session,'action',{revision:page.revision,action:'fill',ref:password.ref,text:''});
  page=await call(session,'action',{revision:page.revision,action:'type',text:'fixture-password'});
  page=await call(session,'action',{revision:page.revision,action:'click',ref:pick(page,'Login')});
  assert(page.text.includes('Logged in'));assert.equal(loginPosts,1);
  page=await call(session,'mode',{mode:'agent'});
  const office=pick(page,'Office'),oldRevision=page.revision;
  await call(session,'action',{revision:page.revision,action:'select',ref:office,option_index:1},409);
  page=await call(session,'action',{agent_authorized:true,revision:page.revision,action:'select',ref:office,option_index:1});
  assert(page.controls.find(c=>c.ref===office).options.some(o=>o.value==='Shanghai'&&o.selected));
  await call(session,'action',{agent_authorized:true,revision:oldRevision,action:'click',ref:pick(page,'查询')},409);
  page=await call(session,'action',{agent_authorized:true,revision:page.revision,action:'click',ref:pick(page,'查询')});
  assert(page.text.includes('verified query Shanghai'));assert.equal(queryRequests,1);assert.equal(lastOffice,'Shanghai');
  // Form input is only accepted after the application has confirmed the exact
  // value; this tests the trusted runner protocol, not the app approval UI.
  const period=pick(page,'Period');
  await call(session,'action',{agent_authorized:true,revision:page.revision,action:'fill',ref:period,text:'2026-10'},409);
  page=await call(session,'action',{agent_authorized:true,fill_confirmed:true,revision:page.revision,action:'fill',ref:period,text:'2026-10'});
  assert(!JSON.stringify(page).includes('2026-10'));
  await call(session,'action',{agent_authorized:true,fill_confirmed:true,revision:page.revision,action:'fill',ref:password.ref,text:'must-not-enter'},409);
  const downloadId=randomBytes(32).toString('hex');
  const downloadArgs={action_id:downloadId,agent_authorized:true,revision:page.revision,ref:pick(page,'导出数据')};
  const receipt=await call(session,'download',downloadArgs);assert.equal(receipt.download_id,downloadId);
  assert.equal(receipt.bytes,reports.length);assert.equal(receipt.content_sha256,createHash('sha256').update(reports).digest('hex'));assert.equal(downloads,1);
  const retry=await call(session,'download',downloadArgs);assert.deepEqual(retry,receipt);assert.equal(downloads,1);
  let received=[];for(let offset=0;offset<receipt.bytes;){
    const chunk=await call(session,'download-chunk',{download_id:downloadId,offset,length:512*1024});
    assert.equal(chunk.offset,offset);const data=Buffer.from(chunk.content_base64,'base64');assert.equal(data.length,chunk.bytes);received.push(data);offset+=data.length;
  }
  assert.deepEqual(Buffer.concat(received),reports);
  await call(session,'download-chunk',{download_id:downloadId,offset:0,length:512*1024+1},409);
  const isolated=await call(other,'',{url:origin+'/'});assert(isolated.text.includes('Business fixture'));
  await call(other,'download-chunk',{download_id:downloadId,offset:0,length:1},409);
  await call(session,'download-release',{download_id:downloadId});
  await call(session,'download-chunk',{download_id:downloadId,offset:0,length:1},409);
  // Import via the *real* Python download handler and its real HTTP client,
  // into a disposable project database. No browser or download call is mocked.
  page=await call(session,'observe');
  const dataDir=await mkdtemp(join(tmpdir(),'atlas-live-browser-'));
  try{
    const childImport=spawnSync(process.env.ATLAS_TEST_PYTHON||root+'/.venv/bin/python',
      [root+'/tests/browser_business_import_helper.py'],{
        cwd:root,encoding:'utf8',timeout:60000,maxBuffer:2*1024*1024,
        input:JSON.stringify({session_id:session,workspace_id:scope,origin,snapshot:page,
          export_ref:pick(page,'导出数据'),bytes:reports.length,sha256:receipt.content_sha256}),
        env:{...process.env,DATA_DIR:dataDir,APP_TOKEN:'synthetic-local-fixture-token-only',PYTHONPATH:root,
          ATLAS_BROWSER_URL:api,ATLAS_BROWSER_TOKEN:token,ATLAS_WEB_ENABLED:'1',
          API_ALLOWED_HOSTS:'127.0.0.1',ATLAS_NETWORK_MODE:'connected'}
      });
    assert.equal(childImport.status,0,'Python project import failed: '+String(childImport.stderr||'').slice(-2000));
    const imported=JSON.parse(childImport.stdout.trim().split('\n').at(-1));
    assert.equal(imported.stored_bytes,reports.length);assert.equal(imported.sha256,receipt.content_sha256);
    assert.equal(imported.source_status,'queued');assert(imported.retry_reused_source);
    assert.equal(downloads,2,'Python import must export once; replay must not re-export');
  }finally{await rm(dataDir,{recursive:true,force:true})}
  // A connection loss is an unknown result: replaying the same identifier
  // retrieves that state without another export click or pretending success.
  page=await call(session,'observe');
  const failedId=randomBytes(32).toString('hex');
  const failedArgs={action_id:failedId,agent_authorized:true,revision:page.revision,ref:pick(page,'导出中断资料')};
  await call(session,'download',failedArgs,409);const countAfterFailure=interruptedDownloads;
  assert(countAfterFailure>=1);
  const failedAgain=await call(session,'download',failedArgs);assert.equal(failedAgain.state,'unknown');
  assert.equal(interruptedDownloads,countAfterFailure);
  // Agent submission is denied; human takeover can submit and an identical
  // action ID must replay the receipt rather than send the POST twice.
  page=await call(session,'observe');
  let submitRef=pick(page,'提交');
  await call(session,'action',{agent_authorized:true,revision:page.revision,action:'click',ref:submitRef},409);
  assert.equal(submissions,0);
  await call(session,'action',{agent_authorized:true,submit_confirmed:true,revision:page.revision,action:'click',ref:submitRef,
                              post_target:{method:'POST',origin,path:'/submit'}},409);
  assert.equal(submissions,0);
  // The original POST remains paused while its exact target/body is reviewed.
  const capture=await call(session,'action',{agent_authorized:true,capture_confirmed:true,action:'capture_post',revision:page.revision,ref:submitRef});
  assert.equal(submissions,0);const pending=capture.pending_post;assert(pending?.id&&pending?.digest);
  const reviewed=await call(session,'post-reveal',{post_id:pending.id,digest:pending.digest});
  assert.equal(reviewed.pending_post.method,'POST');assert.equal(reviewed.pending_post.url,origin+'/submit');
  assert.equal(reviewed.pending_post.body,'fixture=1');
  const approved={post_id:pending.id,digest:pending.digest,approve:true};
  const sent=await call(session,'post-decision',approved);assert.equal(sent.state,'sent');assert.equal(sent.transport_code,200);assert.equal(submissions,1);
  const repeated=await call(session,'post-decision',approved);assert.equal(repeated.state,'sent');assert.equal(submissions,1);
  page=await call(session,'mode',{mode:'human'});
  assert(!serviceOutput.includes('fixture-password'));
  await call(session,'close');await call(session,'observe',{},404);await call(other,'close');
  console.log(JSON.stringify({real_chromium:true,synthetic_local_fixture:true,user_login_takeover:true,
    unauthorized_mutation_blocked:true,query:true,approved_fill:true,unapproved_submit_denied:true,approved_post_once:true,large_download_bytes:reports.length,download_sha256:true,
    chunks_reconstructed:true,retry_did_not_redownload:true,project_import:true,import_status_queued:true,interrupted_download_unknown:true,cross_session_download_denied:true,
    actual_company_site:false,actual_model:false,production_isolation_not_tested:true,submissions}));
}finally{
  child.kill('SIGTERM');await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve()},5000).unref()});
  await new Promise(r=>fixture.close(r));
}
