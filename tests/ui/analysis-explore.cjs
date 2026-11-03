const {JSDOM}=require('jsdom');
const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const dom=new JSDOM('<section id="data-explore-panel" hidden></section>',{runScripts:'outside-only',url:'https://example.test'}),w=dom.window;
 w.eval(fs.readFileSync('agent_service/static/analysis-explore.js','utf8'));
 const sha='a'.repeat(64),schema=[{name:'office',label:'<img>',data_type:'text'},{name:'month',label:'月份',data_type:'text'},
  {name:'amount',label:'金额',data_type:'decimal',aggregation:'additive',value_format:'native',unit:'CNY'}];
 let calls=[],resolve,current=true;const request=(path,options)=>{calls.push({path,body:options.body});return new Promise(r=>resolve=r)};
 const result={run_id:'run_1',manifest_sha256:sha,context:{output_semantics:{columns:schema}}};
 const ctx={project:'p_1',workflow:{published_run:'run_1'},result,resultId:'run_1',request,isCurrent:()=>current};
 w.nexusExplore.sync(ctx);const root=w.document.querySelector('#data-explore-panel');assert(!root.hidden);assert.equal(root.querySelectorAll('img').length,0);
 let button=[...root.querySelectorAll('button')].find(x=>x.textContent==='开始分析');button.click();assert.equal(calls[0].body.expected_handoff_sha256,sha);
 resolve({run_id:'run_1',handoff_sha256:sha,complete:true,total_rows:2,selected_rows:2,metric:'count',measure:null,period:null,period_column:null,group_by:['office'],filters:[],notice:'需核对',groups:[{values:['<script>'],count:2}]});await new Promise(r=>setTimeout(r,0));
 assert.equal(root.querySelectorAll('script').length,0);assert(root.textContent.includes('<script>'));
 [...root.querySelectorAll('button')].find(x=>x.textContent==='查看此范围').click();assert(root.textContent.includes('下钻范围'));
 button.click();assert.equal(calls[1].body.group_by[0],'month');assert.equal(calls[1].body.filters[0].value,'<script>');
 current=false;resolve({run_id:'run_1',handoff_sha256:sha,complete:true,total_rows:1,selected_rows:1,metric:'count',measure:null,period:null,period_column:null,group_by:['month'],filters:[{column:'office',value:'<script>'}],notice:'stale response',groups:[]});await new Promise(r=>setTimeout(r,0));assert(!root.textContent.includes('stale response'));
 w.nexusExplore.sync({...ctx,resultId:'run_other'});assert(root.hidden);assert.equal(root.textContent,'');
 console.log('analysis UI pins the adopted result, supports safe drilldown, and discards stale responses');
})().catch(e=>{console.error(e);process.exit(1)});
