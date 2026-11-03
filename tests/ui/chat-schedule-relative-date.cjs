const {JSDOM}=require('jsdom'),assert=require('node:assert/strict'),fs=require('node:fs');
const dom=new JSDOM('<main></main>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window,tick=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 w.eval(fs.readFileSync('agent_service/static/chat-business-schedule.js','utf8'));
 const sha='a'.repeat(64),fid='bf_'+'b'.repeat(40),flow={id:fid,published_digest:sha,published_run:'r',timezone:'Asia/Shanghai',cron:null,published_document:{inputs:['date'],input_specs:{date:{type:'date',label:'统计日期'}},nodes:[]}};let saved;
 const api=async(path,opt)=>{if(opt){saved=opt.body;return {}}return {flow_id:fid,published_digest:sha,parameter_values:saved.parameter_values,source_bindings:{}}};
 const panel=w.createChatBusinessSchedule({container:w.document.querySelector('main'),api,getState:()=>({projectId:'p',viewEpoch:1,authSealed:false})});
 const select=()=>w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail:{projectId:'p',flow}}));select();
 const mode=w.document.querySelector('select[aria-label="日期方式 统计日期"]');mode.value='previous_month_end';mode.dispatchEvent(new w.Event('change'));
 assert.equal(w.document.querySelector('input[aria-label="统计日期"]').hidden,true);
 w.document.querySelector('input[type=time]').value='08:00';[...w.document.querySelectorAll('button')].find(x=>x.textContent==='保存每日定时').click();await tick();
 assert.equal(saved.parameter_values.date.relative_date,'previous_month_end');
 flow.cron='0 8 * * *';select();await tick();assert.equal(w.document.querySelector('select[aria-label="日期方式 统计日期"]').value,'previous_month_end');assert.equal(w.document.querySelector('input[aria-label="统计日期"]').hidden,true);
 panel.dispose();console.log('scheduled relative dates: save and restore');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>dom.window.close());
