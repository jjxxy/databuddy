const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{
 const dom=new JSDOM('<main id="main"><textarea></textarea></main><aside id="workspace-artifacts-panel"><section id="workflow-panel"><div id="host"></div></section></aside>',{runScripts:'outside-only',url:'https://nexus.test/'}),w=dom.window,d=w.document;
 const state={projectId:'p1',viewEpoch:1,authSealed:false};let options,syncs=0;
 w.nexusShell={syncLayout(){syncs++}};w.renderWorkflowOverview=(_container,value)=>{options=value};w.renderBusinessWorkflowGraph=target=>{target.textContent='流程图'};
 w.eval(fs.readFileSync(path.join(__dirname,'../../agent_service/static/chat-workflows.js'),'utf8'));
 const id='wf_'+'a'.repeat(40),panel=w.createChatWorkflowPanel({container:d.querySelector('#host'),getState:()=>state,api:async url=>url.endsWith('/data-workflows')?{items:[{id,name:'业务流程'}]}:url.endsWith('/business-flows')?{items:[]}:{workflow_id:id,document:{plan:{inputs:{},stages:[]}}}});
 await panel.refresh(true);assert(options);options.onDetail();const button=[...d.querySelectorAll('button')].find(x=>x.textContent==='全屏查看');assert(button);button.click();assert(d.body.classList.contains('workflow-expanded'));assert.equal(button.getAttribute('aria-pressed'),'true');assert.equal(d.querySelector('textarea').disabled,false);
 w.dispatchEvent(new w.Event('nexus-exit-workflow-expanded'));assert(!d.body.classList.contains('workflow-expanded'));assert.equal(button.textContent,'全屏查看');assert.equal(d.activeElement,button);
 button.click();panel.clear();assert(!d.body.classList.contains('workflow-expanded'));assert(syncs>=4);dom.window.close();console.log('Expanded workflow toggles, preserves chat, restores focus and clears on project reset');
})().catch(e=>{console.error(e);process.exitCode=1});
