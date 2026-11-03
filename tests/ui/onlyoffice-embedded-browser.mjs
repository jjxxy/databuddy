import {chromium} from 'playwright-core';import fs from 'node:fs';import assert from 'node:assert/strict';
if(!process.env.NEXUS_BROWSER_EXECUTABLE||!process.env.ATLAS_ONLYOFFICE_FIXTURE_CONFIG)throw Error('Browser and fixture config required');
const config=JSON.parse(fs.readFileSync(process.env.ATLAS_ONLYOFFICE_FIXTURE_CONFIG,'utf8'));
const browser=await chromium.launch({executablePath:process.env.NEXUS_BROWSER_EXECUTABLE,headless:true});
try{
 const page=await browser.newPage({viewport:{width:1280,height:900}}),denied=[];
 await page.route('**/*',r=>{const url=new URL(r.request().url());if(url.hostname==='127.0.0.1'&&url.port==='18080')return r.continue();denied.push(url.hostname);return r.abort()});
 await page.setContent('<main></main>');
 await page.evaluate(value=>{window.done=new Promise(resolve=>{const frame=document.createElement('iframe');frame.sandbox='allow-scripts allow-same-origin';frame.style.cssText='width:100%;height:850px';frame.src='http://127.0.0.1:18080/web-apps/apps/api/documents/atlas-viewer.html';window.addEventListener('message',e=>{if(e.source!==frame.contentWindow||e.origin!=='http://127.0.0.1:18080')return;if(e.data?.nexusOnlyOffice==='loaded')frame.contentWindow.postMessage({nexusOnlyOffice:'open',config:value},'http://127.0.0.1:18080');else if(e.data?.nexusOnlyOffice==='ready')resolve(true);else if(e.data?.nexusOnlyOffice==='error')resolve(false)});document.querySelector('main').append(frame);setTimeout(()=>resolve(false),30000)})},config);
 assert.equal(await page.evaluate(()=>window.done),true);assert.deepEqual(denied,[]);await page.screenshot({path:process.env.TEMP+'\\atlas-onlyoffice-embedded.png'});console.log('ONLYOFFICE isolated cross-origin viewer opened synthetic PPT');
}finally{await browser.close()}
