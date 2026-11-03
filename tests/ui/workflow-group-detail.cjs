const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');

(async () => {
  const dom = new JSDOM('<div id="g"></div><div id="detail"></div>', {runScripts:'outside-only'});
  const w = dom.window, doc = w.document;
  w.eval(fs.readFileSync(path.join(__dirname,'../../agent_service/static/workflow-overview.js'),'utf8'));
  const root = doc.getElementById('g'), detail = doc.getElementById('detail');
  let nodes, select, selectedGroup, all = 0;
  w.NexusFlowGraph = {mount(_root,items,click){nodes=items;select=click}};
  const plan = {nodes:[
    {id:'source',title:'读取订单',depends_on:[]}, {id:'process',title:'计算收入',depends_on:['source']},
    {id:'report',title:'生成报告',depends_on:['process']}, {id:'ppt',title:'制作汇报',depends_on:['process']},
    {id:'last',title:'检查材料',depends_on:['report','ppt']}, {id:'more',title:'发布',depends_on:['last']}
  ]};
  w.renderWorkflowOverview(root,{kind:'business',plan,onGroup:ids=>selectedGroup=ids,onDetail:()=>all++});
  select(nodes[1].id);
  assert.deepEqual(Array.from(selectedGroup),['report','ppt']);
  root.querySelector('.workflow-overview-detail').click();
  assert.equal(all,1);

  const version={digest:'d',commit:'c'};
  const run={id:'run1',flow_id:'flow1',plan_digest:'d',commit:'c',status:'succeeded',
    steps:[{node_id:'process',status:'succeeded',table_available:true}]};
  const sample={run_id:'run1',node_id:'process',offset:0,total:1,columns:[{name:'value',label:'金额'}],rows:[{value:'118.15'}]};
  let requests=0;
  const api=async()=>{requests++;return sample};
  const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
  const render=options=>w.renderWorkflowGroupDetail(detail,{kind:'business',ids:['process'],plan,run,version,
    flowId:'flow1',projectId:'project',api,isCurrent:()=>true,...options});
  const outputButton=()=>Array.from(detail.querySelectorAll('button')).find(b=>b.textContent.includes('实际输出'));
  const selected=()=>detail.querySelector('.workflow-step-files');

  render({ids:['report','ppt','last']});
  assert.equal(selected(),null);
  assert.deepEqual(Array.from(nodes,n=>[n.id,Array.from(n.deps)]),[['report',[]],['ppt',[]],['last',['report','ppt']]]);
  assert.equal(nodes[0].y,nodes[1].y);
  assert.notEqual(nodes[0].x,nodes[1].x);
  assert(nodes[2].y>nodes[0].y);
  select('ppt');
  assert.match(selected().textContent,/制作汇报/);
  select('last');
  assert.equal(detail.querySelectorAll('.workflow-step-files').length,1);
  assert.equal(selected().querySelector('h5').textContent,'检查材料');

  render({});
  assert.deepEqual(Array.from(nodes,n=>n.id),['process']);
  assert.equal(selected(),null);
  assert.equal(requests,0);
  select('process');
  assert.match(selected().textContent,/计算收入/);
  assert.doesNotMatch(selected().textContent,/生成报告/);
  assert.match(selected().textContent,/实际输入明细/);
  assert.equal(requests,0,'inspecting a step must not fetch financial rows');
  outputButton().click();await tick();
  assert.equal(requests,1);
  assert.match(selected().textContent,/118.15/);
  for(const status of ['failed','cancelled']){
    render({run:{...run,status}});select('process');
    outputButton().click();await tick();
    assert.match(selected().textContent,/整体流程未完成/);
    assert.match(selected().textContent,/118.15/);
  }
  render({run:{...run,status:'unknown'}});select('process');
  assert.equal(outputButton(),undefined);
  render({version:{digest:'other',commit:'c'}});select('process');
  assert.equal(outputButton(),undefined);
  assert.match(selected().textContent,/没有可核验/);

  let resolvePending;
  render({ids:['process','report'],api:()=>new Promise(resolve=>{resolvePending=resolve})});
  select('process');outputButton().click();select('report');
  resolvePending({...sample,rows:[{value:'sensitive'}]});await tick();
  assert.doesNotMatch(detail.textContent,/sensitive/);
  console.log('single-step, parallel branches, scope, click-only inspector, partial verified outputs and stale result isolation passed');
  dom.window.close();
})().catch(e=>{console.error(e);process.exitCode=1});
