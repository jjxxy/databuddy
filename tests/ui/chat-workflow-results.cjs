const assert=require('assert'),fs=require('fs'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="result"></div>',{url:'http://localhost/',runScripts:'outside-only'}),w=dom.window;w.eval(fs.readFileSync('agent_service/static/chat-workflow-results.js','utf8'));
let state={projectId:'p1',viewEpoch:1,authSealed:false},calls=[],resolve;
const sample={run_id:'r1',workflow_id:'wf1',plan_sha256:'different',schema:[{name:'amount',label:'金额'}],rows:[{amount:'118.1500'}],total:1,offset:0,next_offset:null,context:{input_evidence:{orders:{source_id:'s1',sheet:'流水',rows:3}},requires_semantic_review:true}};
const api=url=>{calls.push(url);return new Promise(r=>resolve=r)};
const root=w.document.querySelector('#result'),panel=w.createChatWorkflowResults({container:root,api,getState:()=>state});
function select(wf,project='p1'){w.dispatchEvent(new w.CustomEvent('nexus-chat-workflow-selected',{detail:{projectId:project,workflow:wf}}))}
const tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
 select({workflow_id:'wf1',candidate_run:'r1',published_run:'r2',draft_sha256:'current'});assert(calls[0].includes('/projects/p1/data-results/r1'));resolve(sample);await tick();
 assert(root.textContent.includes('118.1500'));assert(root.textContent.includes('新结果（待核对）'));assert(root.textContent.includes('其他做法版本'));assert(root.textContent.includes('业务口径尚需复核'));assert(root.textContent.includes('页签：流水'));
 select({workflow_id:'wf1',candidate_run:'r1',draft_sha256:'current'});state={projectId:'p2',viewEpoch:2,authSealed:false};select(null,'p2');resolve(sample);await tick();assert.equal(root.textContent,'');
 state={projectId:'p1',viewEpoch:3,authSealed:false};select({workflow_id:'wf1',published_run:'r1',draft_sha256:'different'});resolve({...sample,workflow_id:'wrong'});await tick();assert(root.textContent.includes('不一致'));
 select({workflow_id:'wf1',published_run:'r1',draft_sha256:'different'});resolve({...sample,rows:[{amount:'<script>alert(1)</script>'}],context:{}});await tick();assert(root.textContent.includes('<script>'));assert.equal(root.querySelector('script'),null);assert(root.textContent.includes('已采用结果'));panel.dispose();console.log('chat result preview: version binding, source evidence, project races, and escaped values passed');
})().catch(e=>{console.error(e);process.exitCode=1});
