const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<div id="root"></div>',{runScripts:'outside-only'});
try {
 const w=dom.window,root=w.document.querySelector('#root');
 w.eval(fs.readFileSync(path.join(__dirname,'../../agent_service/static/business-flow-view.js'),'utf8'));
 const nodes=[
  {id:'analysis',kind:'model',title:'撰写经营分析'},
  {id:'slides',kind:'table_report_spec',title:'组织汇报内容',config:{format:'presentation'}},
  {id:'ppt',kind:'office_document',title:'交付汇报材料',depends_on:['slides'],inputs:{spec:{source:'node',name:'slides'}}},
  {id:'report',kind:'html_report',title:'交付网页',depends_on:['slides']},
  {id:'unknown',kind:'office_document',title:'PPT 文件',inputs:{spec:{source:'input',name:'external'}}}
 ];
 w.NexusBusinessFlowView.render(root,{plan:{nodes}});
 const labels=[...root.querySelectorAll('.flow-step-kind')].map(x=>x.textContent);
 assert.deepEqual(labels,['AI 处理','准备汇报 PPT 内容','生成汇报 PPT','生成网页报告','生成办公文件']);
 const ppt=root.querySelectorAll('.flow-graph g[role=button]')[2];ppt.dispatchEvent(new w.MouseEvent('click',{bubbles:true}));
 assert.match(root.querySelector('.flow-graph-detail').textContent,/生成汇报 PPT/);
 // A title that says PPT is not evidence of the type of a supplied spec.
 assert.equal(labels[4],'生成办公文件');
 console.log('business flow node labels reflect declared model, HTML and connected PPT spec');
}finally{dom.window.close()}
