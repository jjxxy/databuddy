import {chromium} from 'playwright-core';
import fs from 'node:fs';
import assert from 'node:assert/strict';
if(!process.env.NEXUS_BROWSER_EXECUTABLE)throw Error('Browser executable required');
const browser=await chromium.launch({executablePath:process.env.NEXUS_BROWSER_EXECUTABLE,headless:true}),external=[];
try{
 const page=await browser.newPage();
 await page.route('**/*',route=>{if(route.request().url()==='https://nexus.test/')return route.fulfill({contentType:'text/html',body:'<!doctype html><main></main>'});external.push(route.request().url());return route.abort()});
 await page.goto('https://nexus.test/');await page.addScriptTag({content:fs.readFileSync('agent_service/static/workspace-html-view.js','utf8')});
 await page.evaluate(()=>{const html='<html><head><style>@import url(https://outside.invalid/css);h1{color:rgb(1, 2, 3)}</style><meta http-equiv="refresh" content="0;url=https://outside.invalid/refresh"></head><body><h1 onclick="parent.evil=1">月结报告</h1><img src="https://outside.invalid/img"><img src="https://nexus.test/probe"><a href="https://outside.invalid/link">详情</a><script>parent.evil=1</script></body></html>';const frame=document.createElement('iframe');frame.setAttribute('sandbox','');frame.srcdoc=window.nexusSafeHtmlDocument(html);document.querySelector('main').append(frame)});
 const frame=page.frameLocator('iframe');await frame.getByText('月结报告').waitFor();assert.equal(await frame.locator('h1').evaluate(x=>getComputedStyle(x).color),'rgb(1, 2, 3)');await frame.getByText('详情').click();
 await page.waitForTimeout(300);assert.equal(await page.evaluate(()=>window.evil),undefined);assert.deepEqual(external,[]);console.log('HTML browser: styles preserved, scripts, external resources and navigation disabled');
}finally{await browser.close()}
