import {chromium} from 'playwright-core';
import fs from 'node:fs';import path from 'node:path';import assert from 'node:assert/strict';
const root=path.resolve('agent_service/static'),file=name=>fs.readFileSync(path.join(root,name),'utf8');
const exe=process.env.NEXUS_BROWSER_EXECUTABLE;if(!exe)throw Error('Set NEXUS_BROWSER_EXECUTABLE');
const browser=await chromium.launch({executablePath:exe,headless:true});const errors=[],external=[];
try{
 const page=await browser.newPage({viewport:{width:1100,height:900}});page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',route=>{const u=route.request().url();if(u==='https://nexus.test/?project=p1')return route.fulfill({contentType:'text/html',body:'<!doctype html><html><head></head><body><main id="one" hidden></main><main id="two"></main><main id="three" style="width:524px"></main></body></html>'});external.push(u);return route.abort()});
 await page.goto('https://nexus.test/?project=p1');
 await page.addStyleTag({content:file('react-dag/web-dag.css')});await page.addStyleTag({content:'#one{width:524px;max-width:100%}'});await page.addScriptTag({content:file('react-dag/web-dag.js')});
 await page.addScriptTag({content:file('workbench-dag.js')});await page.addScriptTag({content:file('business-flow-view.js')});
 await page.addStyleTag({content:file('workflow-overview.css')});await page.addScriptTag({content:file('workflow-overview.js')});
 await page.evaluate(()=>{
  window.wbCurrentProject=()=>true;window.wbRequest=()=>new Promise(resolve=>{window.finishSource=resolve});
  const workflow={document:{plan:{inputs:{orders:{meaning:'订单资料'}},stages:[{id:'clean',kind:'filter',input:'orders',conditions:[{column:'amount',operator:'gt',value:'0'}]},{id:'sum',kind:'aggregate',input:'clean',by:[],aggregations:[]}],output:'sum'},bindings:{orders:{source_id:'s_1'}}},draft_sha256:'a'.repeat(64),published_sha256:'b'.repeat(64),published_run:'r1'};
  window.renderBusinessWorkflowGraph(document.querySelector('#one'),workflow);
  window.renderWorkflowOverview(document.querySelector('#three'),{kind:'data',workflow});
  window.NexusBusinessFlowView.render(document.querySelector('#two'),{plan:{name:'经营汇报',nodes:[{id:'x',title:'读取订单',description:'检查订单来源',depends_on:[]},{id:'y',title:'汇总收入',description:'计算已核对收入',depends_on:['x']}]},version:{role:'candidate',digest:'a'.repeat(64)},run:null});
 });
 await page.waitForTimeout(200);await page.evaluate(()=>{document.querySelector('#one').hidden=false});await page.waitForSelector('#one .react-flow__node');await page.waitForSelector('#two .react-flow__edge',{state:'attached'});
 await page.waitForFunction(()=>{const host=document.querySelector('#three nexus-flow-graph')?.getBoundingClientRect(),nodes=[...document.querySelectorAll('#three .react-flow__node')];return !!host&&nodes.length===4&&nodes.every(node=>{const r=node.getBoundingClientRect();return r.width>=180&&r.left>=host.left-1&&r.right<=host.right+1&&r.top>=host.top-1&&r.bottom<=host.bottom+1})},null,{timeout:3000});
 await page.locator('#three nexus-flow-graph').screenshot({path:path.join(process.env.TEMP||'.','nexus-overview-vertical.png')});
 assert.equal(await page.locator('#one .react-flow__node').count(),4);assert.equal(await page.locator('#one .react-flow__edge').count(),3);
 await page.waitForFunction(()=>{const graph=document.querySelector('#one nexus-flow-graph')?.getBoundingClientRect(),nodes=[...document.querySelectorAll('#one .react-flow__node')];return !!graph&&nodes.length===4&&nodes.every(node=>{const r=node.getBoundingClientRect();return r.left>=graph.left-1&&r.right<=graph.right+1&&r.top>=graph.top-1&&r.bottom<=graph.bottom+1})},null,{timeout:3000});
 assert.equal(await page.locator('#two .react-flow__node').count(),2);assert.equal(await page.locator('#two .react-flow__edge').count(),1);
 await page.locator('#one nexus-flow-graph').screenshot({path:path.join(process.env.TEMP||'.','nexus-react-flow-visible.png')});await page.locator('#one .react-flow__node').filter({hasText:'筛选记录'}).click();assert.match(await page.locator('#one .workflow-graph-detail').innerText(),/筛选条件/);
 assert.equal(await page.locator('#one .react-flow__node').filter({hasText:'筛选记录'}).locator('button').getAttribute('aria-pressed'),'true');
 await page.locator('#two .react-flow__node').filter({hasText:'汇总收入'}).click();assert.match(await page.locator('#two .flow-graph-detail').innerText(),/计算已核对收入/);
 await page.locator('#two .react-flow__node').filter({hasText:'读取订单'}).locator('button').focus();await page.keyboard.press('Enter');await page.waitForFunction(()=>document.querySelector('#two .flow-graph-detail')?.textContent.includes('检查订单来源'),null,{timeout:3000});
 assert.equal(await page.locator('#two .react-flow__node').filter({hasText:'读取订单'}).locator('button').getAttribute('aria-pressed'),'true');
 await page.locator('#one .react-flow__node').filter({hasText:'订单资料'}).click();await page.evaluate(()=>window.finishSource([{id:'s_1',name:'已核对订单',project_id:'p1'}]));await page.waitForFunction(()=>document.querySelector('#one .workflow-graph-detail')?.textContent.includes('已核对订单'));assert.match(await page.locator('#one').innerText(),/与已采用版本不同/);assert.match(await page.locator('#two').innerText(),/尚未选择对应运行/);
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(200);const small=await page.locator('#one nexus-flow-graph').boundingBox();assert(small.width<=390&&small.height>=240);assert(await page.locator('#one .react-flow__controls-zoomin').isVisible());await page.locator('#one .react-flow__controls-zoomin').click();
 await page.evaluate(()=>{
  const host=document.querySelector('#three'),node=(id,title,depends_on)=>({id,title,depends_on});
  const plan={nodes:[node('load','读取资料',[]),node('sum','汇总收入',['load']),node('publish','发布结果',['sum'])]};
  const assets={showNode(section,id){const b=document.createElement('button');b.textContent=id+'.json';b.className='file-target';const box=document.createElement('div');box.className='workflow-file-links';box.append(b);section.append(box)}};
  window.renderWorkflowGroupDetail(host,{kind:'business',ids:['load','sum'],plan,assets});
 });
 await page.waitForSelector('#three .workflow-group-graph .react-flow__edge',{state:'attached'});
 assert.equal(await page.locator('#three .workflow-group-graph .react-flow__node').count(),2);
 await page.locator('#three .workflow-group-graph .react-flow__node').filter({hasText:'汇总收入'}).locator('button').click();
 assert.equal(await page.evaluate(()=>document.activeElement.textContent),'sum.json');
 assert.equal(await page.locator('#three .file-target').count(),2);
 assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
 console.log(JSON.stringify({react_flow_both_diagrams:true,browser_nodes_and_edges:true,node_business_explanations:true,version_state_preserved:true,no_network_requests:true,source_update_refreshes_selected_detail:true,mobile_controls_available:true,hidden_tab_reveals_graph:true}));
}finally{await browser.close()}
