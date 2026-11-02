import {chromium} from 'playwright-core';import fs from 'node:fs';import assert from 'node:assert/strict';
if(!process.env.NEXUS_BROWSER_EXECUTABLE)throw Error('Browser executable required');
const browser=await chromium.launch({executablePath:process.env.NEXUS_BROWSER_EXECUTABLE,headless:true});
try{
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 await page.setContent('<div class="shell"><aside id="sidebar">项目</aside><main id="main"><header class="topbar">对话</header><div class="thread-panel"><div class="thread-scroll">消息</div></div><div class="composer-wrap"><textarea id="composer" aria-label="对话输入"></textarea></div></main><aside id="workspace-artifacts-panel" class="workspace-artifacts-panel"><div class="artifact-panel-heading">业务流程</div><section id="workflow-panel"><div class="chat-workflows-host" id="host"></div></section></aside></div>');
 await page.addStyleTag({content:fs.readFileSync('agent_service/static/style.css','utf8')});await page.addScriptTag({content:fs.readFileSync('agent_service/static/chat-workflows.js','utf8')});
 await page.evaluate(async()=>{const id='wf_'+'a'.repeat(40);window.nexusShell={syncLayout(){document.querySelector('#main').inert=false}};window.renderWorkflowOverview=(_c,o)=>{window.openDetail=o.onDetail};window.renderBusinessWorkflowGraph=c=>{const g=document.createElement('div');g.textContent='详细流程';g.style.cssText='height:320px;background:#ddd';g.id='graph-test';c.append(g)};const p=window.createChatWorkflowPanel({container:document.querySelector('#host'),getState:()=>({projectId:'p1',viewEpoch:1,authSealed:false}),api:async u=>u.endsWith('/data-workflows')?{items:[{id,name:'流程'}]}:u.endsWith('/business-flows')?{items:[]}:{workflow_id:id,document:{plan:{inputs:{},stages:[]}}}});await p.refresh(true);window.openDetail()});
 const button=page.getByRole('button',{name:'全屏查看'});await button.click();assert(await page.locator('body').evaluate(e=>e.classList.contains('workflow-expanded')));
 for(const viewport of [{width:1280,height:900},{width:390,height:844}]){
  await page.setViewportSize(viewport);await page.locator('#composer').fill('继续修改流程');const chat=await page.locator('#composer').boundingBox(),graph=await page.locator('#graph-test').boundingBox(),panel=await page.locator('#workspace-artifacts-panel').boundingBox();
  assert(chat&&graph&&panel&&chat.x>=0&&chat.y>=0&&chat.x+chat.width<=viewport.width+1&&chat.y+chat.height<=viewport.height+1,'chat remains visible');
  assert(graph.x<viewport.width&&graph.y<viewport.height&&panel.width>0,'workflow remains visible');
  if(viewport.width<700)assert(chat.y>=panel.y+panel.height-1,'mobile chat does not overlap the workflow');else assert(chat.x+chat.width<=panel.x+1,'desktop chat does not overlap the workflow');
 }
 await page.getByRole('button',{name:'退出全屏'}).click();assert(!(await page.locator('body').evaluate(e=>e.classList.contains('workflow-expanded'))));console.log('Chrome: expanded workflow and interactive chat visible without overlap at desktop and mobile widths');
}finally{await browser.close()}
