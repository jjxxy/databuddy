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
  await page.click('[data-mode='+mode+']');
  for(const [variant,batches] of [['multi',[files]],['separate',files.map(file=>[file])]]){
   let selected=0;
   for(const batch of batches){
    // Both the first choice and each later choice use the primary + button.
    const choosing=page.waitForEvent('filechooser');await page.click('#attach');
    const picker=await choosing;assert.equal(picker.isMultiple(),true);await picker.setFiles(batch);
    selected+=batch.length;
    await page.waitForFunction(count=>document.querySelectorAll('.attachment-chip').length===count,selected);
    // File dialogs cause focus changes. Exercise the production lifecycle poll
    // against the real API before opening the next picker.
    const refreshed=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/projects/'+project.id+'/conversation'&&response.ok(),{timeout:15000});
    await page.evaluate(async()=>{window.dispatchEvent(new Event('focus'));await new Promise(resolve=>setTimeout(resolve,100));await life.poll(true)});
    await refreshed;await page.waitForFunction(()=>!polling);
    assert.equal(await page.locator('.attachment-chip').count(),selected);
   }
   const expected=messages.length+1;await page.fill('#message','Read all files for '+mode+' '+variant);await page.click('#send');
   await page.waitForFunction(count=>document.querySelectorAll('.attachment-chip').length===0&&document.querySelectorAll('.user-row').length>=count,expected,{timeout:30000});
  }
 }
 // Directory checkbox fallback uses a real temporary directory. Playwright
 // provides the folder to Chromium; the OS selection window is not automated.
 const folder=fs.mkdtempSync(path.join(os.tmpdir(),'atlas-directory-input-'));
 try{
  fs.mkdirSync(path.join(folder,'nested'));
  fs.writeFileSync(path.join(folder,'one.csv'),'id,amount\n1,10\n');
  fs.writeFileSync(path.join(folder,'nested','two.txt'),'notes');
  fs.writeFileSync(path.join(folder,'nested','one.csv'),'id,amount\n2,20\n');
  fs.writeFileSync(path.join(folder,'three.json'),'{"name":"third"}');
  fs.writeFileSync(path.join(folder,'unselected.csv'),'id,amount\n4,999\n');
  fs.writeFileSync(path.join(folder,'unsupported.exe'),'unused');
  fs.writeFileSync(path.join(folder,'empty.txt'),'');
  for(const mode of ['chat','work']){
   await page.click('[data-mode='+mode+']');
   // An existing attachment must survive and be combined with the folder choices.
   if(mode==='chat'){
    const choosing=page.waitForEvent('filechooser');await page.click('#attach');
    await (await choosing).setFiles({name:'existing.txt',mimeType:'text/plain',buffer:Buffer.from('existing')});
    await page.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===1);
   }
   const attemptsBefore=uploadAttempts.length;await page.click('#folder-attach');
   assert((await page.locator('#folder-attach-dialog').innerText()).includes('子文件夹'));
   const choosing=page.waitForEvent('filechooser');await page.click('#folder-attach-choose');
   const picker=await choosing;assert(picker.isMultiple());await picker.setFiles(folder);
   const rows=page.locator('#folder-attach-list label');assert.equal(await rows.count(),7);
   assert((await rows.filter({hasText:'unsupported.exe'}).locator('input').isDisabled()));
   assert((await rows.filter({hasText:'empty.txt'}).locator('input').isDisabled()));
   // Same-named files from two different directories are distinct choices.
   const rootOne=rows.filter({hasText:'one.csv'}).filter({hasNotText:'nested/one.csv'});
   await rootOne.locator('input').check();await rows.filter({hasText:'nested/one.csv'}).locator('input').check();
   if(mode==='work')await rows.filter({hasText:'two.txt'}).locator('input').check();
   assert.equal(uploadAttempts.length,attemptsBefore,'opening and checking never uploads');
   assert((await page.locator('#folder-attach-status').textContent()).includes('已勾选 '+(mode==='chat'?2:3)));
   await page.click('#folder-attach-confirm');await page.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===3);
   const expected=messages.length+1;await page.fill('#message','Folder selected files for '+mode);await page.click('#send');
   await page.waitForFunction(count=>document.querySelectorAll('.attachment-chip').length===0&&document.querySelectorAll('.user-row').length>=count,expected,{timeout:30000});
  }
  // Closing cancels the in-page selection and drops retained File references.
  await page.click('#folder-attach');let choosing=page.waitForEvent('filechooser');await page.click('#folder-attach-choose');await (await choosing).setFiles(folder);
  await page.locator('#folder-attach-list label').filter({hasText:'three.json'}).locator('input').check();
  await page.click('#folder-attach-close');assert.equal(await page.locator('#folder-attach-list label').count(),0);
  assert.equal(await page.locator('.attachment-chip').count(),0);
  // Results from a picker opened in a different project must never be attached.
  await page.click('#folder-attach');choosing=page.waitForEvent('filechooser');await page.click('#folder-attach-choose');await (await choosing).setFiles(folder);
  await page.locator('#folder-attach-list label').filter({hasText:'three.json'}).locator('input').check();
  const other=await jsonRequest(backendOrigin+'/api/projects',{method:'POST',body:JSON.stringify({name:'Other project',request_id:'other-'+Date.now()})});
  await page.evaluate(()=>{window.testOldFolderScope={...folderScope}});
  await page.evaluate(id=>window.atlasBusiness.openProject(id),other.id);
  await page.evaluate(()=>{const data=new DataTransfer();data.items.add(new File(['old'],'old.csv'));folderPending=window.testOldFolderScope;folderInput.files=data.files;folderInput.dispatchEvent(new Event('change'))});
  assert.equal(await page.locator('.attachment-chip').count(),0);assert.equal(await page.locator('#folder-attach-list label').count(),0);
  await page.evaluate(id=>window.atlasBusiness.openProject(id),project.id);
  await page.waitForFunction(id=>projectId===id&&!!sessionId&&!navigating,project.id);
  // Bounded listing and hard 20-file limit, including XSS-safe file names.
  await page.click('#folder-attach');
  await page.evaluate(()=>{const data=new DataTransfer();data.items.add(new File(['x'],'<img src=x>.csv'));for(let i=1;i<5001;i++)data.items.add(new File(['x'],'f'+i+'.csv'));folderPending={...folderScope};folderInput.files=data.files;folderInput.dispatchEvent(new Event('change'))});
  assert.equal(await page.locator('#folder-attach-list label').count(),5000);
  assert.equal(await page.locator('#folder-attach-list img').count(),0);
  assert((await page.locator('#folder-attach-status').textContent()).includes('共有 5001'));
  for(let i=0;i<21;i++)await page.locator('#folder-attach-list input').nth(i).check();
  assert(await page.locator('#folder-attach-confirm').isDisabled());
  assert((await page.locator('#folder-attach-status').textContent()).includes('共有 5001'));
  await page.click('#folder-attach-close');
  // A long filename must wrap in the populated checklist on a small viewport.
  fs.writeFileSync(path.join(folder,'long-'+('a'.repeat(180))+'.csv'),'id,amount\n1,1\n');
  await page.setViewportSize({width:390,height:844});await page.click('#folder-attach');
  choosing=page.waitForEvent('filechooser');await page.click('#folder-attach-choose');await (await choosing).setFiles(folder);
  assert.equal(await page.locator('#folder-attach-dialog').evaluate(node=>node.scrollWidth>node.clientWidth+1),false);
  assert.equal(await page.locator('#folder-attach-list').evaluate(node=>node.scrollWidth>node.clientWidth+1),false);
  await page.click('#folder-attach-close');await page.setViewportSize({width:1380,height:900});
 }finally{fs.rmSync(folder,{recursive:true,force:true})}
 assert.equal(messages.length,6);for(const [index,mode] of ['chat','chat','work','work','chat','work'].entries()){
  assert.equal(messages[index].interaction_mode,mode);assert.equal(messages[index].source_ids.length,3);
  assert.equal(new Set(messages[index].source_ids).size,3);
 }
 assert(gateRejected&&droppedSuccess&&uploadAttempts[0]===uploadAttempts[1]&&uploadAttempts[1]===uploadAttempts[2],'gateway rejection and ambiguous upload response must retry the same id');
 assert.equal(uploadPeak,1,'UI must upload at most one attachment at a time');
 const sources=await jsonRequest(backendOrigin+'/api/sources');
 assert.equal(sources.filter(source=>source.project_id===project.id).length,18,'lost response retry must not create a duplicate source or upload unselected files');
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
  // Showing the shell precedes the async project/session restore. During that
  // navigation the old picker silently ignores clicks, so wait for the actual
  // project to be ready rather than racing its first conversation request.
  await oldPage.waitForFunction(id=>projectId===id&&!!sessionId&&!navigating&&!sending&&!pending,project.id,{timeout:30000});
  const oldChooser=oldPage.waitForEvent('filechooser');await oldPage.click('#attach');await (await oldChooser).setFiles(files);
  await oldPage.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===3);
  await oldPage.fill('#message','Previous main concurrent upload');await oldPage.click('#send');
  await oldPage.waitForFunction(()=>!document.querySelector('#connection-error').hidden,{timeout:30000});
  oldClientSaturatedGateway=overloaded>0;
  assert(oldClientSaturatedGateway,'previous client must reproduce gateway rejection');
  assert((await oldPage.locator('#connection-error').textContent()).includes('部分文件上传失败'));
  assert.equal(messages.length,6,'partial old-client upload must not submit a message');
  await oldContext.close();
 }
 console.log(JSON.stringify({real_chromium:browser.version(),actual_api_and_sqlite:true,nonsecure_http:true,actual_secure_context:actualSecureContext,random_uuid_unavailable:true,
  chat_and_work_three_distinct_files:true,both_modes_primary_picker_repeated_single_choices:true,focus_and_real_poll_preserve_selections:true,actual_folder_checkbox_three_files:true,unselected_folder_file_not_uploaded:true,actual_turns_keep_all_sources:true,sequential_uploads:true,gateway_busy_retried:true,lost_response_same_request_id:true,
  no_duplicate_sources:true,simulated_gateway_slots:2,peak_api_requests:peak,previous_main_gateway_failure_reproduced:oldClientSaturatedGateway,gateway_503_count:overloaded,
  real_model_or_windows_tested:false,evidence_dir:state}));
}finally{
 if(browser)await browser.close();if(proxy)await new Promise(resolve=>proxy.close(resolve));backend.kill('SIGTERM');
 await Promise.race([new Promise(resolve=>backend.once('exit',resolve)),pause(5000)]);if(backend.exitCode===null)backend.kill('SIGKILL');
}
