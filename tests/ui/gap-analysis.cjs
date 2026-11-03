'use strict';
const {JSDOM}=require('jsdom');
const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const dom=new JSDOM('<section id="data-gap-panel" hidden></section>',{runScripts:'outside-only',url:'https://example.test'}),w=dom.window;
 w.eval(fs.readFileSync('agent_service/static/gap-analysis.js','utf8'));
 const sha='a'.repeat(64),schema=[{name:'office',label:'<script>',data_type:'text'},
  {name:'base',label:'基准',data_type:'decimal',aggregation:'additive',value_format:'native',unit:'CNY',period:'2026-09'},
  {name:'actual',label:'实际',data_type:'decimal',aggregation:'additive',value_format:'native',unit:'CNY',period:'2026-09'}];
 let calls=[],resolve,current=true;const request=(path,options)=>{calls.push({path,body:options.body});return new Promise(r=>resolve=r)};
 const result={run_id:'run_1',manifest_sha256:sha,plan_sha256:'b'.repeat(64),context:{output_semantics:{columns:schema}}};
 const ctx={project:'p_1',workflow:{published_run:'run_1'},result,resultId:'run_1',request,isCurrent:()=>current};
 w.nexusGapAnalysis.sync(ctx);const root=w.document.querySelector('#data-gap-panel');assert(!root.hidden);assert.equal(root.querySelectorAll('script').length,0);
 for(const [name,value] of [['基准字段','base'],['实际或本期字段','actual'],['按字段分组','office'],['基准月份','2026-09'],['实际月份','2026-09']])root.querySelector(`[aria-label="${name}"]`).value=value;
 const button=[...root.querySelectorAll('button')].find(x=>x.textContent==='计算差异');button.click();assert.equal(calls[0].body.expected_handoff_sha256,sha);
 const body=calls[0].body;resolve({run_id:'run_1',handoff_sha256:sha,plan_sha256:'b'.repeat(64),baseline_column:'base',current_column:'actual',baseline_period:'2026-09',current_period:'2026-09',ratio_decimals:2,rounding:'half_even',group_by:['office'],filters:[],input_rows:1,total_rows:1,unit:'CNY',notice:'不是因果',chart_notice:'近似比例',groups:[{values:['<img>'],count:1,baseline:'9007199254740993.123456789012',current:'9007199254740993.123456789013',delta:'0.000000000001',relative_percent:'0.00',chart_basis_points:1,chart_direction:'increase'}]});
 await new Promise(r=>setTimeout(r,0));assert(root.textContent.includes('数据依据：结果'));assert(root.textContent.includes('9007199254740993.123456789012'));assert(root.textContent.includes('0.000000000001'));assert.equal(root.querySelectorAll('img').length,0);assert.equal(root.querySelector('.gap-chart-bar').style.width,'0.01%');
 root.querySelector('[aria-label="筛选字段"]').value='office';root.querySelector('[aria-label="筛选值"]').value='East';button.click();assert.deepEqual(JSON.parse(JSON.stringify(calls[1].body.filters)),[{column:'office',value:'East'}]);root.querySelector('[aria-label="筛选值"]').dispatchEvent(new w.Event('input'));resolve({notice:'stale filter'});await new Promise(r=>setTimeout(r,0));assert(!root.textContent.includes('stale filter'));
 button.click();root.querySelector('[aria-label="基准月份"]').dispatchEvent(new w.Event('input'));resolve({notice:'stale month'});await new Promise(r=>setTimeout(r,0));assert(!root.textContent.includes('stale month'));button.click();root.querySelector('[aria-label="变化率小数位"]').dispatchEvent(new w.Event('input'));resolve({notice:'stale decimals'});await new Promise(r=>setTimeout(r,0));assert(!root.textContent.includes('stale decimals'));root.querySelector('[aria-label="筛选值"]').value='   ';const callCount=calls.length;button.click();assert.equal(calls.length,callCount);assert(root.textContent.includes('空值或空白'));root.querySelector('[aria-label="筛选值"]').value='East';button.click();current=false;resolve({notice:'stale'});await new Promise(r=>setTimeout(r,0));assert(!root.textContent.includes('stale'));
 w.nexusGapAnalysis.sync({...ctx,resultId:'other'});assert(root.hidden);assert.equal(root.textContent,'');
 console.log('GAP UI preserves exact text, uses bounded integer bars, and rejects stale scope');
})().catch(e=>{console.error(e);process.exit(1)});
