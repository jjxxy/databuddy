const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
(async()=>{
 const dom=new JSDOM('<!doctype html><div id="graph"></div>',{runScripts:'outside-only',url:'http://localhost/data-workbench.html?project=p1'}),w=dom.window;
 w.eval(fs.readFileSync(path.join(__dirname,'../../agent_service/static/workbench-dag.js'),'utf8'));
 let resolve;w.wbCurrentProject=()=>true;w.wbRequest=()=>new Promise(r=>resolve=r);
 const root=w.document.querySelector('#graph'),flow={document:{bindings:{a:{source_id:'s1'},b:{source_id:'s2'}},plan:{inputs:{a:{meaning:'收入'},b:{meaning:'退款'}},stages:[{id:'join',kind:'lookup',input:'a',right:'b'}],output:'join'}}};
 w.renderBusinessWorkflowGraph(root,flow);
 resolve([{id:'s1',project_id:'p1',name:'收入底稿.xlsx'},{id:'s2',project_id:'other',name:'不应显示.xlsx'}]);await Promise.resolve();await Promise.resolve();
 assert.match(root.textContent,/收入底稿\.xlsx/);assert.doesNotMatch(root.textContent,/不应显示/);
 const buttons=[...root.querySelectorAll('g[role=button]')];buttons[0].dispatchEvent(new w.MouseEvent('click',{bubbles:true}));assert.match(root.querySelector('.workflow-graph-detail').textContent,/当前计划绑定的资料：收入底稿\.xlsx/);assert.match(root.querySelector('.workflow-graph-detail').textContent,/运行时来源版本以运行记录为准/);
 buttons[1].dispatchEvent(new w.MouseEvent('click',{bubbles:true}));assert.match(root.querySelector('.workflow-graph-detail').textContent,/名称尚未核实/);
 w.renderBusinessWorkflowGraph(root,flow);const oldResolve=resolve;w.renderBusinessWorkflowGraph(root,{document:{bindings:{},plan:{inputs:{n:{meaning:'新任务'}},stages:[],output:'n'}}});oldResolve([{id:'s1',project_id:'p1',name:'过期数据.xlsx'}]);await Promise.resolve();await Promise.resolve();assert.doesNotMatch(root.textContent,/过期数据/);
 dom.window.close();console.log('source names are project-scoped, asynchronous and stale-safe');
})().catch(e=>{console.error(e);process.exit(1)});
