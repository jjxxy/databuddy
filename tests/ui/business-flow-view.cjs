const {JSDOM}=require('jsdom'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const root=path.resolve(__dirname,'../../agent_service/static');
const dom=new JSDOM('<!doctype html><main id="view"></main>',{url:'https://nexus.test/',runScripts:'outside-only'});
try {
 const w=dom.window,d=w.document,container=d.querySelector('#view');
 w.eval(fs.readFileSync(path.join(root,'business-flow-view.js'),'utf8'));
 const plan={name:'收入分析 <img src=x>',nodes:[
  {id:'income',name:'读取资料',kind:'source',depends_on:[],description:'读取收入明细'},
  {id:'analysis',name:'计算净收入',kind:'transform',depends_on:['income'],description:'收入减退款'},
  {id:'report',name:'生成报告',kind:'office',depends_on:['analysis']},
 ]};
 const version={role:'candidate',digest:'plan-a',commit:'commit-a'};
 const run={status:'running',plan_digest:'plan-a',commit:'commit-a',receipts:[
  {node_id:'income',status:'succeeded',output_sha256:'sha-1'},
  {node_id:'analysis',status:'running',started_at:'2026-01-01',deterministic:false},
 ]};
 w.NexusBusinessFlowView.render(container,{plan,version,run});
 assert.equal(container.querySelectorAll('.flow-step').length,3);
 assert.match(container.textContent,/候选方案/);assert.match(container.textContent,/读取资料/);
 assert.match(container.textContent,/执行完成/);assert.match(container.textContent,/正在执行/);
 assert.match(container.textContent,/本次未记录/);assert.match(container.textContent,/依赖：读取资料/);
 assert.match(container.textContent,/此步骤的输出可能因模型或外部服务变化/);
 assert.equal(container.querySelectorAll('img').length,0,'untrusted title must remain text');
 assert.match(container.textContent,/模型及工作方法的输出可能变化/);
 w.NexusBusinessFlowView.render(container,{plan,version,run:{...run,commit:'other'}});
 assert.match(container.textContent,/版本不一致/);
 assert(![...container.querySelectorAll('.flow-step-state')].some(x=>x.textContent==='执行完成'),'wrong version cannot show its receipts');
 w.NexusBusinessFlowView.render(container,{plan,version,run:{...run,plan_digest:undefined}});
 assert.match(container.textContent,/版本不一致/,'missing version binding must fail closed');
 w.NexusBusinessFlowView.render(container,{plan,version,run:{...run,status:'unknown',receipts:[{node_id:'income',status:'unknown'}]}});
 assert.match(container.textContent,/结果待核对/);assert.match(container.textContent,/不会自动重复外部操作/);
 w.NexusBusinessFlowView.render(container,{plan,version});
 assert.match(container.textContent,/尚未选择运行/);
 w.NexusBusinessFlowView.render(container,{plan:null});assert.match(container.textContent,/尚无可展示/);
 console.log(JSON.stringify({version_bound_receipts:true,complete_plan_vs_run_distinct:true,dependencies_readable:true,unknown_state_not_invented:true,untrusted_text_escaped:true}));
}finally{dom.window.close()}
