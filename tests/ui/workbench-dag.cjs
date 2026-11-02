const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><div id="graph"></div>',{runScripts:'outside-only'});
dom.window.eval(fs.readFileSync(path.join(__dirname,'../../agent_service/static/workbench-dag.js'),'utf8'));
const render=dom.window.renderBusinessWorkflowGraph,root=dom.window.document.querySelector('#graph');
const workflow={document:{plan:{inputs:{orders:{meaning:'订单原件'},customers:{meaning:'客户资料'}},stages:[
  {id:'clean',kind:'filter',input:'orders',description:'筛选已发货订单'},
  {id:'join',kind:'lookup',input:'clean',right:'customers',description:'匹配客户资料'},
  {id:'sum',kind:'aggregate',input:'join',description:'按地区汇总'}],output:'sum'}}};
render(root,workflow);
assert.equal(root.querySelectorAll('rect').length,6);
assert.equal(root.querySelectorAll('path[marker-end]').length,5);
assert.match(root.textContent,/订单原件/);assert.match(root.textContent,/匹配资料/);
assert.match(root.textContent,/尚需核对|仍需核对/);
assert.match(root.textContent,/当前做法尚未采用/);
render(root,{...workflow,published_run:'old',published_sha256:'old',draft_sha256:'new',latest_run:{status:'succeeded',matches_current_draft:false}});
assert.match(root.textContent,/当前做法与已采用版本不同/);assert.match(root.textContent,/最近运行使用的是其他版本/);
render(root,{...workflow,published_run:'old',latest_run:{status:'failed'}});
assert.match(root.textContent,/无法确认当前做法与已采用版本是否一致/);assert.match(root.textContent,/关系待核对/);
const detailed={document:{bindings:{orders:{source_id:'s1'}},plan:{inputs:{orders:{meaning:'订单',columns:[{name:'c_status',label:'订单状态'},{name:'c_amount',label:'金额'},{name:'c_region',label:'地区'}]}},stages:[
  {id:'filter1',kind:'filter',input:'orders',conditions:[{column:'c_status',operator:'eq',value:'已完成'}]},
  {id:'total',kind:'aggregate',input:'filter1',by:['c_region'],aggregations:[{function:'sum',column:'c_amount',output:{name:'net',label:'收入合计'}}]},
  {id:'query',kind:'sql',inputs:['total'],sql:'select secret from source'}],output:'query'}}};
render(root,detailed);
const nodes=[...root.querySelectorAll('g[role="button"]')];assert.equal(nodes.length,5);
nodes[1].dispatchEvent(new dom.window.MouseEvent('click',{bubbles:true}));assert.match(root.querySelector('.workflow-graph-detail').textContent,/订单状态 等于 已完成/);
nodes[2].dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));assert.match(root.querySelector('.workflow-graph-detail').textContent,/地区/);assert.match(root.querySelector('.workflow-graph-detail').textContent,/求和 金额/);
nodes[3].dispatchEvent(new dom.window.MouseEvent('click',{bubbles:true}));assert.match(root.querySelector('.workflow-graph-detail').textContent,/自定义查询/);assert.doesNotMatch(root.querySelector('.workflow-graph-detail').textContent,/select secret/);
render(root,{document:{plan:{inputs:{x:{meaning:'<img src=x onerror=alert(1)>'}},stages:[{id:'s',kind:'filter',input:'missing',description:'核对'}],output:'s'}}});
assert.equal(root.querySelectorAll('img').length,0);assert.match(root.textContent,/依赖未找到/);
render(root,null);assert.match(root.textContent,/没有可展示/);
console.log('business DAG reflects saved dependencies, warns on missing links and treats source labels as text');
