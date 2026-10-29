// Real Chromium picker flags and production app/component; API responses are controlled fixtures.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {chromium} from 'playwright-core';
const root=fileURLToPath(new URL('../../agent_service/static/',import.meta.url));
if(!process.env.ATLAS_BROWSER_EXECUTABLE)throw Error('Set ATLAS_BROWSER_EXECUTABLE to an installed Chromium binary.');
const browser=await chromium.launch({executablePath:process.env.ATLAS_BROWSER_EXECUTABLE,headless:true,args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote']});
const sources=[],uploads=[],pipelines=[],errors=[],composerUploads=[],messages=[];let failSecond=true,processed;
const fields=n=>[{name:'field_'+n,label:'字段'+n,numeric:true}];
const files=[{name:'same-name.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n1,12.34\n')},{name:'same-name.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n2,56.78\n')},{name:'third.csv',mimeType:'text/csv',buffer:Buffer.from('id,amount\n3,90.12\n')}];
try{
 const context=await browser.newContext({viewport:{width:1380,height:960},serviceWorkers:'block'});
 await context.addInitScript(()=>sessionStorage.setItem('atlas_token','controlled-picker-fixture'));
 const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname;
  if(url.origin!=='https://atlas.fixture')return route.abort();
  if(p.startsWith('/api/')){
   let value={};
   if(p==='/api/models')value={models:[{id:'fixture',name:'Fixture model'}],default_model_id:'fixture'};
   else if(p==='/api/projects')value=[{id:'p',name:'Picker check'}];
   else if(p.endsWith('/conversation'))value={project:{id:'p',name:'Picker check'},session_id:'c',sessions:[{id:'c',name:'Review'}],turns:[],sources:[],applications:[],mode:'rules'};
   else if(p==='/api/local-workspace')value={enabled:true,label:'Controlled test workspace'};
   else if(p==='/api/desktop/runtime')value={notice:'本地处理'};
   else if(p==='/api/projects/p/attachments'){
    const body=request.postDataBuffer().toString('utf8'),n=body.includes('3,90.12')?3:body.includes('2,56.78')?2:1;
    const local=body.includes('name="local_processing"\r\n\r\ntrue');if(!local){const value={id:'composer_'+messages.length+'_'+n,name:files[n-1].name};composerUploads.push(value);return route.fulfill({status:200,headers:{'X-Atlas-Runtime-Revision':'2026.10.29.1'},contentType:'application/json',body:JSON.stringify(value)})}const rid=body.match(/name="request_id"\r\n\r\n([^\r]+)/)?.[1];assert(rid);uploads.push({n,rid});
    if(n===2&&failSecond)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'暂时无法导入第二份'})});
    value={id:'s_'+n,name:files[n-1].name};if(!sources.some(s=>s.id===value.id))sources.push({...value,status:'ready',profile:{fields:fields(n),sample_rows:1}});
   }
   else if(p==='/api/projects/p/messages'){messages.push(request.postDataJSON());value={id:'turn_'+messages.length}}
   else if(p==='/api/sources')value=sources;
   else if(p==='/api/pipelines'&&request.method()==='POST'){processed=request.postDataJSON();value={id:'pipe',project_id:'p'};pipelines.push({...value,candidate_run:'run'});}
   else if(p==='/api/pipelines')value=pipelines;
   else if(p==='/api/pipelines/pipe/preview')value={run_id:'run',manifest:{fields:fields(3),records:1,summary:[]},rows:[{field_3:'90.12'}]};
   return route.fulfill({status:200,headers:{'X-Atlas-Runtime-Revision':'2026.10.29.1'},contentType:'application/json',body:JSON.stringify(value)});
  }
  const file=path.join(root,p==='/'?'index.html':p.slice(1));
  if(!file.startsWith(root)||!fs.existsSync(file))return route.fulfill({status:404,body:'Not found'});
  const contentType=file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':file.endsWith('.html')?'text/html':'application/octet-stream';
  return route.fulfill({status:200,contentType,body:fs.readFileSync(file)});
 });
 await page.goto('https://atlas.fixture/?project=p');await page.waitForSelector('#shell:not([hidden])');
 for(const mode of ['chat','work']){
  await page.click('[data-mode='+mode+']');
  const chooser=page.waitForEvent('filechooser');await page.click('#attach');const picker=await chooser;assert.equal(picker.isMultiple(),true);await picker.setFiles(files.slice(0,2));
  await page.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===2);
  const appendChooser=page.waitForEvent('filechooser');await page.click('#add-more-files');const appendPicker=await appendChooser;assert.equal(appendPicker.isMultiple(),true);await appendPicker.setFiles(files.slice(2));
  await page.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===3);assert.equal(uploads.length,0);
  assert((await page.locator('#attachment-count').textContent()).includes('3 / 20'));
  await page.fill('#message','Read all selected files in '+mode);const count=messages.length;await page.click('#send');
  await page.waitForFunction(()=>document.querySelectorAll('.attachment-chip').length===0);
  assert.equal(messages.length,count+1);assert.equal(messages.at(-1).interaction_mode,mode);assert.equal(messages.at(-1).source_ids.length,3);
  assert.deepEqual(messages.at(-1).source_ids,[1,2,3].map(n=>'composer_'+count+'_'+n));
 }
 assert.equal(composerUploads.length,6);assert((await page.locator('#runtime-version').textContent()).includes('2026.10.29.1'));
 await page.click('#open-local-work');await page.waitForSelector('#atlas-local-workspace-content:not([hidden])');
 await page.waitForFunction(()=>!document.querySelector('#atlas-local-file').disabled);
 const localChooser=page.waitForEvent('filechooser');await page.click('#atlas-local-file');const localPicker=await localChooser;assert.equal(localPicker.isMultiple(),true);await localPicker.setFiles(files);
 assert((await page.locator('#atlas-local-selection-count').textContent()).includes('已选3个'));assert.equal(uploads.length,0);
 await page.click('#atlas-local-upload-button');await page.waitForFunction(()=>document.querySelector('#atlas-local-status').textContent.includes('已导入2个文件，1个未完成'));
 assert.deepEqual(uploads.map(row=>row.n),[1,2,3]);assert.equal(await page.locator('#atlas-local-source-select option').count(),2);
 failSecond=false;await page.click('#atlas-local-upload-button');await page.waitForFunction(()=>document.querySelector('#atlas-local-status').textContent.includes('3个文件已保存在本机'));
 assert.deepEqual(uploads.map(row=>row.n),[1,2,3,2]);assert.equal(uploads[1].rid,uploads[3].rid);assert.equal(await page.locator('#atlas-local-source-select option').count(),3);
 await page.selectOption('#atlas-local-source-select','s_3');assert.equal(await page.locator('#atlas-local-amount option[value="field_3"]').count(),1);
 await page.click('#atlas-local-process-button');await page.waitForSelector('#atlas-local-result:not([hidden])');assert.equal(processed.source_id,'s_3');
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.locator('#composer').evaluate(node=>node.scrollWidth>node.clientWidth+1),false,'mobile composer does not overflow');
 assert.equal(await page.locator('#atlas-local-workspace-panel').evaluate(node=>node.scrollWidth>node.clientWidth+1),false,'mobile panel does not overflow horizontally');
 const output=process.env.ATLAS_BROWSER_EVIDENCE_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'atlas-picker-'));fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,'multiple-file-picker.png')});
 assert.deepEqual(errors,[]);console.log(JSON.stringify({real_chromium:browser.version(),api_fixture:true,chat_and_work_picker_multiple:true,both_modes_post_all_three_files:true,append_preserves_first_batch:true,local_picker_multiple:true,three_files_from_one_selection:true,partial_failure_keeps_and_continues_files:true,retry_only_failed_with_same_id:true,processing_uses_third_file:true,mobile_no_horizontal_overflow:true,no_live_model_or_windows_shell_test:true,evidence_directory:output}));
}finally{await browser.close()}
