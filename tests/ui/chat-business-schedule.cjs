const {JSDOM}=require('jsdom'),assert=require('node:assert/strict'),fs=require('node:fs');
const dom=new JSDOM('<main></main>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window,tick=()=>new Promise(r=>setTimeout(r,10));
(async()=>{
 w.eval(fs.readFileSync('agent_service/static/chat-business-schedule.js','utf8'));
 const sha='a'.repeat(64),fid='bf_'+'b'.repeat(40),doc={inputs:['amount'],input_specs:{amount:{type:'decimal',label:'金额下限'}},nodes:[]};let project='p1',epoch=1,calls=[],hold;
 const flow={id:fid,published_digest:sha,published_document:doc,published_run:'r1',timezone:'Asia/Shanghai',cron:'5 8 * * *',schedule_timezone_confirmed:false};
 const api=async(path,opt)=>{calls.push({path,opt});if(opt)return {};return await new Promise(resolve=>{hold=resolve})};
 const panel=w.createChatBusinessSchedule({container:w.document.querySelector('main'),api,getState:()=>({projectId:project,viewEpoch:epoch,authSealed:false})});
 const select=()=>w.dispatchEvent(new w.CustomEvent('nexus-chat-business-flow-selected',{detail:{projectId:project,flow}}));select();
 assert.equal(w.document.querySelector('input[type=time]').disabled,true);assert.equal(w.document.querySelector('input[aria-label="金额下限"]').disabled,true);assert.match(w.document.querySelector('main').textContent,/Asia\/Shanghai/);assert.match(w.document.querySelector('main').textContent,/历史定时未固定时区/);
 hold({flow_id:fid,published_digest:sha,parameter_values:{amount:'1.230000000001'},source_bindings:{}});await tick();
 assert.equal(w.document.querySelector('input[type=time]').value,'08:05');assert.equal(w.document.querySelector('input[aria-label="金额下限"]').value,'1.230000000001');
 [...w.document.querySelectorAll('button')].find(x=>x.textContent==='保存每日定时').click();await tick();
 assert.equal(calls.find(x=>x.opt)?.opt.body.parameter_values.amount,'1.230000000001');
 select();const stale=hold;project='p2';epoch++;select();stale({flow_id:fid,published_digest:sha,parameter_values:{amount:'999'},source_bindings:{}});await tick();
 assert.notEqual(w.document.querySelector('input[aria-label="金额下限"]')?.value,'999');flow.timezone='';flow.cron=null;select();assert.equal([...w.document.querySelectorAll('button')].find(x=>x.textContent==='保存每日定时').disabled,true);assert.match(w.document.querySelector('main').textContent,/未获取服务端时区/);panel.dispose();console.log('business schedule: exact decimal, saved values and stale-project protection');
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>dom.window.close());
