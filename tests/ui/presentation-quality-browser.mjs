import {chromium} from 'playwright-core';
import fs from 'node:fs';import path from 'node:path';import assert from 'node:assert/strict';
const root=path.resolve('agent_service/static'),exe=process.env.NEXUS_BROWSER_EXECUTABLE;
if(!exe||!process.argv[2]||!process.argv[3])throw Error('Provide browser, PPTX file and screenshot directory');
const bytes=[...fs.readFileSync(process.argv[2])],dir=path.resolve(process.argv[3]),external=[];fs.mkdirSync(dir,{recursive:true});
const browser=await chromium.launch({executablePath:exe,headless:true});
try{
 const page=await browser.newPage({viewport:{width:1400,height:1000}});await page.route('**/*',route=>{const u=new URL(route.request().url());if(u.origin!=='https://nexus.test'){external.push(u.href);return route.abort()}
  const files={'/pptx-frame.html':['pptx-frame.html','text/html'],'/pptx-bundle/pptx-sandbox.js':['pptx-bundle/pptx-sandbox.js','application/javascript']};
  if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:'<!doctype html><main id="target"></main>'});
  const spec=files[u.pathname];if(!spec)return route.abort();const headers=u.pathname==='/pptx-frame.html'?{'content-security-policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src data: blob:; font-src data: blob:; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"}:{};
  return route.fulfill({contentType:spec[1],body:fs.readFileSync(path.join(root,spec[0])),headers});
 });
 await page.goto('https://nexus.test/');await page.addScriptTag({content:fs.readFileSync(path.join(root,'workspace-pptx-preview.js'),'utf8')});
 await page.evaluate(input=>{window.handle=window.nexusPptxPreview({container:document.querySelector('#target'),blob:new Blob([new Uint8Array(input)]),isCurrent:()=>true})},bytes);
 assert.equal(await page.evaluate(()=>window.handle.ready),true);await page.locator('iframe').evaluate(x=>{x.style.height='1000px'});const frame=page.frameLocator('iframe');
 let count=0;for(;;){await frame.locator('#slide').waitFor();assert.equal(await frame.locator('#slide').evaluate(img=>img.complete&&img.naturalWidth>0),true);count++;await frame.locator('#slide').screenshot({path:path.join(dir,`slide-${String(count).padStart(2,'0')}.png`)});if(await frame.locator('#next').isDisabled())break;await frame.locator('#next').click();await page.waitForTimeout(100);if(count>=120)throw Error('page limit')}
 assert.deepEqual(external,[]);console.log(`Rendered ${count} PPTX pages for visual review`);
}finally{await browser.close()}
