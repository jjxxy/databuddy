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
 resolve({run_id:'run_1',handoff_sha256:sha,complete:true,input_complete:true,groups_total:1,grouped_rows:2,group_offset:0,group_limit:500,next_group_offset:null,total_rows:2,selected_rows:2,metric:'count',measure:null,period:null,period_column:null,group_by:['office'],filters:[],notice:'需核对',groups:[{values:['<script>'],count:2}]});await new Promise(r=>setTimeout(r,0));
 assert.equal(root.querySelectorAll('script').length,0);assert(root.textContent.includes('<script>'));
 [...root.querySelectorAll('button')].find(x=>x.textContent==='查看此范围').click();assert(root.textContent.includes('下钻范围'));
 button.click();assert.equal(calls[1].body.group_by[0],'month');assert.equal(calls[1].body.filters[0].value,'<script>');
 current=false;resolve({run_id:'run_1',handoff_sha256:sha,complete:true,total_rows:1,selected_rows:1,metric:'count',measure:null,period:null,period_column:null,group_by:['month'],filters:[{column:'office',value:'<script>'}],notice:'stale response',groups:[]});await new Promise(r=>setTimeout(r,0));assert(!root.textContent.includes('stale response'));
 current=true;w.nexusExplore.sync({...ctx,resultId:'run_other'});assert(root.hidden);assert.equal(root.textContent,'');
 w.nexusExplore.sync(ctx);button=[...root.querySelectorAll('button')].find(x=>x.textContent==='开始分析');button.click();
 const first=Array.from({length:500},(_,i)=>({values:['第'+i],count:1}));
 resolve({run_id:'run_1',handoff_sha256:sha,complete:false,input_complete:true,groups_total:501,grouped_rows:501,group_offset:0,group_limit:500,next_group_offset:500,total_rows:501,selected_rows:501,metric:'count',measure:null,period:null,period_column:null,group_by:['office'],filters:[],notice:'按页展示',groups:first});
 await new Promise(r=>setTimeout(r,0));assert(root.textContent.includes('分组共 501 项'));const next=[...root.querySelectorAll('button')].find(x=>x.textContent==='下一页分组');assert.equal(next.hidden,false);next.click();assert.equal(calls.at(-1).body.group_offset,500);
 resolve({run_id:'run_1',handoff_sha256:sha,complete:false,input_complete:true,groups_total:501,grouped_rows:501,group_offset:500,group_limit:500,next_group_offset:null,total_rows:501,selected_rows:501,metric:'count',measure:null,period:null,period_column:null,group_by:['office'],filters:[],notice:'按页展示',groups:[{values:['末项'],count:1}]});
 await new Promise(r=>setTimeout(r,0));assert(root.textContent.includes('末项'));assert(root.textContent.includes('当前显示 501–501 项'));assert.equal(next.hidden,true);
 button.click();resolve({run_id:'run_1',handoff_sha256:sha,complete:true,input_complete:true,groups_total:1,grouped_rows:2,group_offset:0,group_limit:500,next_group_offset:null,total_rows:2,selected_rows:2,metric:'count',measure:null,period:null,period_column:null,group_by:['office'],filters:[],notice:'不应显示',groups:[{values:['one'],count:1}]});
 await new Promise(r=>setTimeout(r,0));assert(root.textContent.includes('结果记录不完整'));assert(!root.textContent.includes('不应显示'));
 console.log('analysis UI pins the adopted result, supports safe drilldown, and discards stale responses');
})().catch(e=>{console.error(e);process.exit(1)});
