const {JSDOM}=require('jsdom');const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window;w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;
w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.component={workActionCards,renderThread,operationTrace};');
const html=w.component.workActionCards([{id:'wa_1',state:'pending',digest:'a'.repeat(64),tool:'create_ticket',arguments:{title:'<img src=x onerror=alert(1)>'}}]);const temp=w.document.createElement('div');temp.innerHTML=html;assert.equal(temp.querySelector('img'),null);assert.equal(temp.querySelectorAll('[data-work-decision]').length,2);assert.equal(temp.querySelector('[data-approve="true"]').dataset.digest,'a'.repeat(64));
const unknown=w.component.workActionCards([{id:'wa_2',state:'unknown',tool:'send_message',arguments:{}}]);temp.innerHTML=unknown;assert.equal(temp.querySelector('[data-work-decision]'),null);assert(temp.textContent.includes('不会自动重发'));
temp.innerHTML=w.component.operationTrace({resumed_from:'old',inherited_step_count:2,agent_trace:[
 {tool:'read_file',status:'done'},{tool:'run_data_workflow',status:'failed'},
 {tool:'run_data_workflow',status:'done',observation:{executed:false,reused_existing_run:true}},
 {tool:'read_tool_result',status:'failed',observation:{executed:false,code:'RECOVERY_NO_PROGRESS'}}]});
assert(temp.querySelector('.operation-trace>summary').textContent.includes('本轮 2 条 · 沿用 2 条'));
assert.equal(temp.querySelector('.inherited-trace').open,false);
assert.equal(temp.querySelectorAll('.inherited-trace li').length,2,'old failures and successes remain available');
assert.equal(temp.querySelectorAll('.current-trace li').length,2);
assert(temp.querySelector('.current-trace').textContent.includes('复用已有成果'));
assert(temp.querySelector('.current-trace').textContent.includes('未重复执行'));
temp.innerHTML=w.component.operationTrace({inherited_step_count:1,agent_trace:[{tool:'<img src=x onerror=alert(1)>',status:'done'}]});
assert.equal(temp.querySelector('img'),null);assert.equal(temp.querySelector('.inherited-trace'),null,'unrelated tasks do not hide steps');
temp.innerHTML=w.component.operationTrace({agent_trace:[{tool:'run_work_command',status:'done',observation:{remote_receipt:true,state:'failed'}}]});
assert(temp.textContent.includes('执行未成功'));assert(!temp.textContent.includes('工作任务 完成'));
temp.innerHTML=w.component.operationTrace({agent_trace:[{tool:'run_work_command',status:'done',observation:{remote_receipt:true,state:'unknown'}}]});
assert(temp.textContent.includes('执行结果待核对'));
temp.innerHTML=w.component.operationTrace({model_usage:{calls:123456,completion_tokens:7654321,usage_incomplete:true},context_metrics:{selected_tools:9999},agent_trace:[{tool:'read_file',status:'done'}]});
assert(temp.textContent.includes('读取项目文件'));
assert(!/模型请求|tokens|本步加载|123456|7654321|9999/.test(temp.textContent),'internal model/tool-loading counters are not product progress');
w.component.renderThread({project:{id:'p',name:'Fixture'},sources:[],turns:[{id:'old',text:'生成报告',answer:'本轮模型预算已用完，已有成果已保存；可以缩小目标后继续',state:'done',obsolete_budget_pause:true,resume_available:true,interaction_mode:'work'}]});
assert(w.document.querySelector('.historical-budget-note').textContent.includes('继续原目标'));
const historicalDetail=w.document.querySelector('.historical-budget-detail');
assert(historicalDetail && !historicalDetail.open,'old budget error must be collapsed by default');
assert(historicalDetail.textContent.includes('本轮模型预算已用完'),'the original historical receipt remains available');
assert.equal(w.document.querySelector('[data-continue]').dataset.continue,'old');
console.log(JSON.stringify({approval_is_inline:true,work_execution_status_truthful:true,exact_digest:true,arguments_escaped:true,unknown_cannot_be_retried_from_card:true,inherited_history_is_distinct_from_new_attempts:true,reused_or_blocked_operations_not_labelled_as_new_execution:true}));dom.window.close();
