const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{runScripts:'outside-only'});
try{
 const w=dom.window,root=w.document.querySelector('#root');let groups,click,selected;
 w.NexusFlowGraph={mount(_root,items,onClick){groups=items;click=onClick}};
 w.eval(fs.readFileSync(path.join(__dirname,'../../agent_service/static/workflow-overview.js'),'utf8'));
 const plan={nodes:[
  {id:'a',kind:'source_table',title:'读取资料'},
  {id:'b',kind:'table_workflow',title:'加工',depends_on:['a']},
  {id:'c',kind:'table_report_spec',title:'整理',config:{format:'presentation'},depends_on:['b']},
  {id:'d',kind:'office_document',title:'交付',inputs:{spec:{source:'node',name:'c'}},depends_on:['c']},
  {id:'e',kind:'html_report',title:'查看成果',depends_on:['c']},
  {id:'f',kind:'office_document',title:'PPT 文件',inputs:{spec:{source:'input',name:'external'}}},
 ]};
 w.renderWorkflowOverview(root,{kind:'business',plan,onGroup:ids=>selected=ids});
 const materials=root.querySelector('.workflow-overview-materials').textContent;
 assert.match(materials,/生成汇报 PPT/);assert.match(materials,/生成网页报告/);assert.match(materials,/生成办公文件/);
 assert.match(materials,/是否生成成功请查看运行结果/);
 assert(groups.some(g=>g.label.includes('生成汇报 PPT')));assert(groups.some(g=>g.label.includes('生成网页报告')));
 const group=groups.find(g=>g.nodeIds.includes('d'));click(group.id);assert.deepEqual(Array.from(selected),Array.from(group.nodeIds));
 w.renderWorkflowOverview(root,{kind:'business',plan:{nodes:[{id:'x',kind:'model',title:'生成 PPT'}]}});
 assert.equal(root.querySelector('.workflow-overview-materials'),null,'title alone cannot assert a material node');
 console.log('overview shows declared material types, preserves group selection and does not infer generation');
}finally{dom.window.close()}
