const fs = require('fs');
const assert = require('assert');
const {JSDOM} = require('jsdom');
const html=fs.readFileSync('agent_service/static/data-workbench.html','utf8');
const js=fs.readFileSync('agent_service/static/data-workbench.js','utf8');
const block=js.slice(js.indexOf("let wbScheduleKey="),js.indexOf("function wbRenderScheduleReview()"));
assert(block.startsWith('let wbScheduleKey=') && block.includes('wbSetSchedule'));
const dom=new JSDOM(html,{url:'https://nexus.test/data-workbench.html?project=p',runScripts:'outside-only'});
const win=dom.window;
win.confirm=()=>true;
win.eval(`
 const wbState={workflow:null,workflowId:'w1',busy:false,workflowLoading:false,activeJob:null};
 const wbEl=(s)=>document.querySelector(s),wbEnc=encodeURIComponent;
 const wbCurrentProject=()=>true,wbRoot=()=>'/api/projects/p';
 const requests=[]; let requestError=null;
 const wbNotice=()=>{};
 async function wbRequest(url,options){requests.push({url,...options});if(requestError)throw requestError;}
 async function wbLoadWorkflow(){}
 ${block}
 window.testSchedule={wbState,requests,render:wbRenderScheduleControls,set:wbSetSchedule,error:e=>requestError=e};
`);
(async()=>{
 const t=win.testSchedule,el=s=>win.document.querySelector(s);
 t.render();assert(el('#workbench-schedule-panel').hidden);
 const sha='a'.repeat(64);
 t.wbState.workflow={published_run:'run1',published_sha256:sha,timezone:'Asia/Shanghai',cron:'5 9 * * *',watch_sources:true};
 t.render();assert(!el('#workbench-schedule-panel').hidden);
 assert.strictEqual(el('#workbench-schedule-time').value,'09:05');
 assert(el('#workbench-schedule-details').textContent.includes('来源变化后也运行'));
 el('#workbench-schedule-time').value='08:42';
 await t.set(false);
 assert.strictEqual(t.requests[0].body.cron,'42 8 * * *');
 assert.strictEqual(t.requests[0].body.expected_published_sha256,sha);
 assert.strictEqual(t.requests[0].body.watch_sources,true);
 t.wbState.workflow.schedule_has_unresolved=true;t.render();
 assert(el('#workbench-schedule-save').disabled);
 assert(!el('#workbench-schedule-pause').disabled);
 await t.set(true);assert.strictEqual(t.requests[1].body.cron,null);
 assert.strictEqual(t.requests[1].body.watch_sources,false);
 t.wbState.workflow.schedule_has_unresolved=false;t.error(new Error('offline'));
 await t.set(false);assert(el('#workbench-schedule-save').disabled);
 assert(el('#workbench-schedule-message').textContent.includes('未确认设置是否生效'));
 const count=t.requests.length;await t.set(false);assert.strictEqual(t.requests.length,count);
 console.log('workbench schedule UI: passed');
})().catch(e=>{console.error(e);process.exitCode=1});
