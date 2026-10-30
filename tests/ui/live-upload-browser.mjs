// Chromium + actual Atlas API/database over non-secure HTTP on IPv4-mapped loopback.
// A local proxy simulates a two-slot gateway and a lost upload response;
// no real business data or model connection is used.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {spawn,spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright-core';

const root=fileURLToPath(new URL('../../',import.meta.url));
const python=path.join(root,'.venv','bin','python');
const executable=process.env.ATLAS_BROWSER_EXECUTABLE;
if(!executable)throw Error('Set ATLAS_BROWSER_EXECUTABLE to an installed Chromium binary');
const state=fs.mkdtempSync(path.join(os.tmpdir(),'atlas-live-upload-'));
const token='fixture-owner-local-test-token-not-secret';
const env={...process.env,DATA_DIR:path.join(state,'data'),DATABASE_URL:'sqlite:///'+path.join(state,'data','metadata.sqlite'),
 APP_TOKEN:token,ATLAS_AUTH_MODE:'token',ATLAS_DESKTOP_MODE:'0',ATLAS_OPENCODE_RUNTIME:'0',
 LLM_BASE_URL:'',LLM_MODEL:'',DLT_TELEMETRY:'false',DO_NOT_TRACK:'1'};
const setup=spawnSync(python,['-c',`from agent_service import model_catalog\nmodel_catalog.provision({'default_model_id':'fixture','models':[{'id':'fixture','name':'fixture','base_url':'http://127.0.0.1:9/v1','model':'fixture','api_key':'test-only','share_data':True}]},verify=False)`],{cwd:root,env,encoding:'utf8'});
if(setup.status!==0)throw Error('Could not initialize isolated fixture: '+setup.stderr);
async function port(){const server=net.createServer();await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const value=server.address().port;await new Promise(resolve=>server.close(resolve));return value}
const backendPort=await port(),backendOrigin='http://127.0.0.1:'+backendPort;
const backend=spawn(python,['-m','uvicorn','agent_service.api:app','--host','127.0.0.1','--port',String(backendPort)],{cwd:root,env,stdio:['ignore','ignore','pipe']});
let backendError='';backend.stderr.on('data',chunk=>{backendError+=chunk.toString().slice(0,1000)});
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function jsonRequest(url,options={}){const response=await fetch(url,{...options,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json',...options.headers}});if(!response.ok)throw Error('Actual API HTTP '+response.status+' '+(await response.text()).slice(0,300));return response.json()}
let proxy,browser;
const messages=[],uploadAttempts=[];let active=0,peak=0,overloaded=0,droppedSuccess=false,gateRejected=false,uploadActive=0,uploadPeak=0,serveOldClient=false;
const priorClient=spawnSync('git',['show','7bbec0b:agent_service/static/app.js'],{cwd:root,encoding:'utf8'});
try{
 let healthy=false;for(let i=0;i<100&&!healthy;i++){if(backend.exitCode!==null)throw Error('Atlas API exited: '+backendError);try{healthy=(await fetch(backendOrigin+'/health')).ok}catch{}if(!healthy)await pause(100)}
 assert(healthy,'actual Atlas API did not start');
 const project=await jsonRequest(backendOrigin+'/api/projects',{method:'POST',body:JSON.stringify({name:'Browser upload integration',request_id:'live-browser-project'})});
 proxy=http.createServer(async(req,res)=>{
  if(serveOldClient&&req.url.startsWith('/app.js')){res.writeHead(200,{'Content-Type':'text/javascript','Cache-Control':'no-store'});res.end(priorClient.stdout);return}
  const isApi=req.url.startsWith('/api/'),isUpload=req.url.endsWith('/attachments')&&req.method==='POST';
  if(isApi&&active>=2){overloaded++;res.writeHead(503,{'Content-Type':'application/json','Retry-After':'1'});res.end(JSON.stringify({detail:'controlled proxy concurrent request limit'}));return}
  if(isApi){active++;peak=Math.max(peak,active)}
  if(isUpload){uploadActive++;uploadPeak=Math.max(uploadPeak,uploadActive)}
  try{
   const chunks=[];for await(const chunk of req)chunks.push(chunk);const body=Buffer.concat(chunks);
   if(isUpload){const match=body.toString('utf8').match(/name="request_id"\r\n\r\n([^\r]+)/);assert(match);uploadAttempts.push(match[1])}
   if(isUpload&&!serveOldClient&&!gateRejected){gateRejected=true;res.writeHead(503,{'Content-Type':'application/json','Retry-After':'1'});res.end(JSON.stringify({detail:'controlled gateway busy'}));return}
   if(req.url.endsWith('/messages')&&req.method==='POST')messages.push(JSON.parse(body.toString()));
   const headers={...req.headers,host:'127.0.0.1:'+backendPort};delete headers['content-length'];delete headers.connection;
   if(isUpload)await pause(120); // allow polling to contend with uploads
   const response=await fetch(backendOrigin+req.url,{method:req.method,headers,body:body.length?body:undefined,redirect:'manual'});
   const bytes=Buffer.from(await response.arrayBuffer());
   if(isUpload&&response.ok&&!droppedSuccess&&!serveOldClient){
    droppedSuccess=true;res.writeHead(503,{'Content-Type':'application/json','Retry-After':'0'});res.end(JSON.stringify({detail:'controlled lost upload response'}));return;
   }
   const out={};for(const [key,value] of response.headers){if(!['content-encoding','content-length','transfer-encoding','connection'].includes(key.toLowerCase()))out[key]=value}
   res.writeHead(response.status,out);res.end(bytes);
  }catch(error){if(!res.headersSent)res.writeHead(500,{'Content-Type':'application/json'});res.end(JSON.stringify({detail:String(error.message)}))}
  finally{if(isApi)active--;if(isUpload)uploadActive--}
 });
 await new Promise(resolve=>proxy.listen(0,'127.0.0.1',resolve));const proxyPort=proxy.address().port;
 browser=await chromium.launch({executablePath:executable,headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote','--no-proxy-server']});
 const context=await browser.newContext({viewport:{width:1380,height:900},serviceWorkers:'block'});
 await context.addInitScript(value=>sessionStorage.setItem('atlas_token',value),token);
 const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
 await page.goto('http://[::ffff:127.0.0.1]:'+proxyPort+'/?project='+project.id,{waitUntil:'domcontentloaded'});
 await page.waitForSelector('#shell:not([hidden])',{timeout:30000});
 const actualSecureContext=await page.evaluate(()=>isSecureContext);
 assert.equal(actualSecureContext,false,'must exercise an actual non-secure HTTP context');
 assert.equal(await page.evaluate(()=>typeof crypto.randomUUID),'undefined','browser must lack secure-context randomUUID');
 assert.equal(await page.evaluate(()=>typeof crypto.getRandomValues),'function');
 const files=[{name:'same.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n1,1.25\n')},
  {name:'same.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n2,2.50\n')},
  {name:'third.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n3,3.75\n')}];
 for(const mode of ['chat','work']){
  await page.click('[data-mode='+mode+']');const choosing=page.waitForEvent('filechooser');await page.click('#attach');
  const picker=await choosing;assert.equal(picker.isMultiple(),true);await picker.setFiles(files);
  await page.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===3);
  const expected=messages.length+1;await page.fill('#message','Read all files for '+mode);await page.click('#send');
  await page.waitForFunction(count=>document.querySelectorAll('.attachment-chip').length===0&&document.querySelectorAll('.user-row').length>=count,expected,{timeout:30000});
 }
 assert.equal(messages.length,2);for(const [index,mode] of ['chat','work'].entries()){
  assert.equal(messages[index].interaction_mode,mode);assert.equal(messages[index].source_ids.length,3);
  assert.equal(new Set(messages[index].source_ids).size,3);
 }
 assert(gateRejected&&droppedSuccess&&uploadAttempts[0]===uploadAttempts[1]&&uploadAttempts[1]===uploadAttempts[2],'gateway rejection and ambiguous upload response must retry the same id');
 assert.equal(uploadPeak,1,'UI must upload at most one attachment at a time');
 const sources=await jsonRequest(backendOrigin+'/api/sources');
 assert.equal(sources.filter(source=>source.project_id===project.id).length,6,'lost response retry must not create a duplicate source');
 const conversation=await jsonRequest(backendOrigin+'/api/projects/'+project.id+'/conversation');
 for(const message of messages){const turn=conversation.turns.find(value=>value.text===message.text);assert(turn);assert.deepEqual(turn.source_ids,message.source_ids);assert.equal(turn.attached_sources.length,3)}
 assert.deepEqual(errors,[]);
 let oldClientSaturatedGateway=false;
 if(priorClient.status===0){
  serveOldClient=true;const oldContext=await browser.newContext({serviceWorkers:'block'});
  await oldContext.addInitScript(value=>sessionStorage.setItem('atlas_token',value),token);
  const oldPage=await oldContext.newPage();
  await oldPage.goto('http://127.0.0.1:'+proxyPort+'/?project='+project.id,{waitUntil:'domcontentloaded'});
  await oldPage.waitForSelector('#shell:not([hidden])',{timeout:30000});
  const oldChooser=oldPage.waitForEvent('filechooser');await oldPage.click('#attach');await (await oldChooser).setFiles(files);
  await oldPage.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===3);
  await oldPage.fill('#message','Previous main concurrent upload');await oldPage.click('#send');
  await oldPage.waitForFunction(()=>!document.querySelector('#connection-error').hidden,{timeout:30000});
  oldClientSaturatedGateway=overloaded>0;
  assert(oldClientSaturatedGateway,'previous client must reproduce gateway rejection');
  assert((await oldPage.locator('#connection-error').textContent()).includes('部分文件上传失败'));
  assert.equal(messages.length,2,'partial old-client upload must not submit a message');
  await oldContext.close();
 }
 console.log(JSON.stringify({real_chromium:browser.version(),actual_api_and_sqlite:true,nonsecure_http:true,actual_secure_context:actualSecureContext,random_uuid_unavailable:true,
  chat_and_work_three_distinct_files:true,actual_turns_keep_all_sources:true,sequential_uploads:true,gateway_busy_retried:true,lost_response_same_request_id:true,
  no_duplicate_sources:true,simulated_gateway_slots:2,peak_api_requests:peak,previous_main_gateway_failure_reproduced:oldClientSaturatedGateway,gateway_503_count:overloaded,
  real_model_or_windows_tested:false,evidence_dir:state}));
}finally{
 if(browser)await browser.close();if(proxy)await new Promise(resolve=>proxy.close(resolve));backend.kill('SIGTERM');
 await Promise.race([new Promise(resolve=>backend.once('exit',resolve)),pause(5000)]);if(backend.exitCode===null)backend.kill('SIGKILL');
}
