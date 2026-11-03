const {JSDOM}=require('jsdom');
const fs=require('fs');const assert=require('assert/strict');
(async()=>{
 const dom=new JSDOM('<section id="data-factor-panel" hidden></section>',{runScripts:'outside-only',url:'https://fixture.test'}),w=dom.window;
 w.eval(fs.readFileSync('agent_service/static/factor-analysis.js','utf8'));
 const field=(name,aggregation,unit)=>({name,label:name,data_type:'decimal',value_format:'native',aggregation,unit,period:'2026-09'});
 const schema=[field('q0','additive','件'),field('q1','additive','件'),field('p0','non_additive','CNY/件'),field('p1','non_additive','CNY/件')];
 const sha='a'.repeat(64),result={run_id:'run',manifest_sha256:sha,context:{output_semantics:{columns:schema}}};
 let active=true,resolve,body;const request=(path,options)=>{assert(path.includes('/factor-analysis'));body=options.body;return new Promise(r=>resolve=r)};
 w.nexusFactorAnalysis.sync({project:'p',workflow:{published_run:'run'},result,resultId:'run',request,isCurrent:()=>active});
 const panel=w.document.querySelector('#data-factor-panel');assert(!panel.hidden);
 for(const [label,value] of [['基准数量','q0'],['本期数量','q1'],['基准单价','p0'],['本期单价','p1'],['基准月份','2026-08'],['本期月份','2026-09'],['金额单位','CNY']])panel.querySelector(`[aria-label="${label}"]`).value=value;
 panel.querySelector('button').click();assert.equal(body.expected_handoff_sha256,sha);assert.equal(body.quantity_before,'q0');
 resolve({run_id:'run',handoff_sha256:sha,groups:[{values:['<script>bad</script>'],count:1,before:'10',after:'12',quantity_effect:'2',price_effect:'0',delta:'2'}],total_rows:1,unit:'CNY',notice:'数学归因'});
 await new Promise(r=>setTimeout(r,0));assert(panel.textContent.includes('数学归因'));assert(panel.textContent.includes('<script>bad</script>'));assert.equal(panel.querySelectorAll('script').length,0);
 panel.querySelector('button').click();active=false;resolve({run_id:'run',handoff_sha256:sha,groups:[],total_rows:99,unit:'CNY',notice:'stale data'});
 await new Promise(r=>setTimeout(r,0));assert(!panel.textContent.includes('stale data'));
 console.log('factor analysis pins adopted result, renders safe text and rejects stale responses');
})().catch(e=>{console.error(e);process.exit(1)});
