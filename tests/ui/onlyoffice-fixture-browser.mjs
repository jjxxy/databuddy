// Manual integration harness: uses a separately started, synthetic fixture bridge.
import {chromium} from 'playwright-core';
import fs from 'node:fs';
import assert from 'node:assert/strict';
if(!process.env.NEXUS_BROWSER_EXECUTABLE||!process.env.ATLAS_ONLYOFFICE_FIXTURE_CONFIG)throw Error('Browser and fixture config required');
const config=JSON.parse(fs.readFileSync(process.env.ATLAS_ONLYOFFICE_FIXTURE_CONFIG,'utf8'));
const browser=await chromium.launch({executablePath:process.env.NEXUS_BROWSER_EXECUTABLE,headless:true});
try{
 const page=await browser.newPage({viewport:{width:1280,height:900}}),denied=[];
 await page.route('**/*',r=>{const url=new URL(r.request().url());if(url.hostname==='127.0.0.1'&&url.port==='18080')return r.continue();denied.push(url.hostname);return r.abort()});
 await page.setContent('<!doctype html><html><body style="margin:0"><div id="doc" style="height:900px"></div></body></html>');
 await page.addScriptTag({url:'http://127.0.0.1:18080/web-apps/apps/api/documents/api.js'});
 await page.evaluate(cfg=>{window.ready=false;window.errors=[];cfg.events={onDocumentReady:()=>{window.ready=true},onError:e=>window.errors.push(e?.data?.errorCode||'unknown')};new window.DocsAPI.DocEditor('doc',cfg)},config);
 await page.waitForFunction(()=>window.ready||window.errors.length,{timeout:60000});
 assert.equal(await page.evaluate(()=>window.ready),true,'OnlyOffice did not open fixture');
 assert.deepEqual(denied,[]);await page.screenshot({path:process.env.TEMP+'\\atlas-onlyoffice-browser.png'});
 console.log('ONLYOFFICE real browser displayed the signed read-only synthetic PPT');
}finally{await browser.close()}
