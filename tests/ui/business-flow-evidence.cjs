const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(r=>setImmediate(r));
async function until(fn){for(let i=0;i<200;i++){await tick();if(fn())return}throw Error('page did not settle')}
const digest='a'.repeat(64),commit='b'.repeat(40),detailHash='c'.repeat(64),finalHash='d'.repeat(64);
function reply(value,status=200){return {ok:status>=200&&status<300,status,json:async()=>structuredClone(value)}}
function page(stage,offset=0,overrides={}){
 const total=stage==='fx_empty'?0:51,rows=Array.from({length:Math.min(50,Math.max(0,total-offset))},(_,i)=>({id:'row-'+(offset+i+1),amount:offset+i===0?'9007199254740993.123456789012':'<img src=x>'}));
 return {run_id:'r_one',node_id:'convert',stage_id:stage,columns:[{name:'id',label:'编号'},{name:'amount',label:'金额',unit:'CNY'}],rows,total,offset,next_offset:offset+rows.length<total?offset+rows.length:null,output_sha256:detailHash,final_output_sha256:finalHash,financial_verified:false,...overrides};
}
async function boot(options={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'business-flows.html'),'utf8'),{url:'https://nexus.test/business-flows.html?project=p_one',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];w.sessionStorage.setItem('nexus_token','x');
 w.fetch=async(url)=>{const u=new URL(url,w.location.href),p=u.pathname;calls.push(u.pathname+u.search);
  if(p.endsWith('/business-flows'))return reply({items:[{id:'bf_one',name:'本次流程'},{id:'bf_two',name:'另一流程'}]});
  if(p.endsWith('/business-flows/bf_one'))return reply({candidate_digest:digest,candidate_commit:commit,candidate_run:'r_one',candidate_document:{name:'汇率核对',inputs:[],nodes:[{id:'convert',kind:'table_workflow',title:'集团数据',config:{plan:{stages:[{id:'fx',kind:'fx_convert',description:'月度汇率'},{id:'fx_empty',kind:'fx_convert',description:'空明细'}]}}}]}});
  if(p.endsWith('/business-flows/bf_two'))return reply({candidate_digest:digest,candidate_commit:commit,candidate_run:'r_two',candidate_document:{name:'另一流程',inputs:[],nodes:[]}});
  if(p.endsWith('/runs/r_two'))return reply({id:'r_two',plan_digest:digest,commit,status:'succeeded',steps:[],artifacts:[]});
  if(p.endsWith('/runs/r_one'))return reply({id:'r_one',plan_digest:digest,commit,status:'succeeded',steps:[{node_id:'convert',title:'集团数据',status:'succeeded',table_available:true,evidence_stages:[{stage_id:'fx',row_count:51},{stage_id:'fx_empty',row_count:0}]}],artifacts:[]});
  if(p.endsWith('/tables/convert'))return reply({run_id:'r_one',node_id:'convert',columns:[{name:'v',label:'汇总'}],rows:[{v:'最终结果'}],total:1,offset:0,next_offset:null});
  if(p.endsWith('/evidence/fx')||p.endsWith('/evidence/fx_empty')){
   const stage=p.endsWith('/fx_empty')?'fx_empty':'fx',offset=Number(u.searchParams.get('offset'));
   if(options.evidence)return options.evidence({stage,offset,url:u});
   return reply(page(stage,offset));
  }
  throw Error('unexpected '+p);
 };
 w.eval(fs.readFileSync(path.join(root,'business-flow-view.js'),'utf8'));w.eval(fs.readFileSync(path.join(root,'business-flows.js'),'utf8'));
 await until(()=>[...d.querySelectorAll('#flow-output-list button')].some(b=>b.textContent.startsWith('查看逐笔换算明细')));
 return {dom,w,d,calls};
}
function button(d,prefix){return [...d.querySelectorAll('#flow-output-list button')].find(x=>x.textContent.startsWith(prefix))}
(async()=>{
 let env=await boot();try{
  const {d}=env;const buttons=[...d.querySelectorAll('#flow-output-list button')].filter(b=>b.textContent.startsWith('查看逐笔换算明细'));
  assert.equal(buttons.length,2);buttons[0].click();await until(()=>d.querySelector('#flow-preview table'));
  const preview=d.querySelector('#flow-preview');assert(preview.textContent.includes('共 51 行'));assert(preview.textContent.includes('不能直接视为某一结果行的逐笔归因'));assert(preview.textContent.includes('后续筛选、计算或汇总可能改变最终结果'));
  assert(preview.textContent.includes('9007199254740993.123456789012'));assert(preview.textContent.includes('<img src=x>'));assert.equal(preview.querySelector('img'),null);
  assert(preview.textContent.includes(detailHash));assert(preview.textContent.includes(finalHash));
  [...preview.querySelectorAll('button')].find(b=>b.textContent==='下一页').click();await until(()=>preview.textContent.includes('row-51'));
  assert(preview.textContent.includes('当前展示 51–51 行'));[...preview.querySelectorAll('button')].find(b=>b.textContent==='上一页').click();await until(()=>preview.textContent.includes('row-1'));
  buttons[1].click();await until(()=>preview.textContent.includes('共 0 行'));assert.equal(preview.querySelectorAll('table tr').length,1);
 }finally{env.dom.window.close()}
 env=await boot({evidence:async()=>reply({detail:'无权读取本次明细'},403)});try{button(env.d,'查看逐笔换算明细').click();await until(()=>env.d.querySelector('#flow-preview').textContent.includes('明细未能读取'));assert(env.d.querySelector('#flow-notice').textContent.includes('无权读取'));assert(env.d.querySelector('#flow-preview').textContent.includes('重试'));}finally{env.dom.window.close()}
 env=await boot({evidence:async({stage,offset})=>reply(page(stage,offset,offset?{final_output_sha256:'f'.repeat(64)}:{}))});try{
  button(env.d,'查看逐笔换算明细').click();await until(()=>env.d.querySelector('#flow-preview table'));const preview=env.d.querySelector('#flow-preview');[...preview.querySelectorAll('button')].find(b=>b.textContent==='下一页').click();await until(()=>env.d.querySelector('#flow-notice').textContent.includes('版本或分页'));assert(preview.textContent.includes('row-1'));assert(!preview.textContent.includes('row-51'));
 }finally{env.dom.window.close()}
 let release;const delayed=new Promise(resolve=>{release=resolve});env=await boot({evidence:()=>delayed});try{
  button(env.d,'查看逐笔换算明细').click();button(env.d,'查看数据：').click();await until(()=>env.d.querySelector('#flow-preview').textContent.includes('最终结果'));release(reply(page('fx')));await tick();await tick();assert(env.d.querySelector('#flow-preview').textContent.includes('最终结果'));assert(!env.d.querySelector('#flow-preview').textContent.includes('row-1'));
 }finally{env.dom.window.close()}
 let resolveOther;const pending=new Promise(resolve=>{resolveOther=resolve});env=await boot({evidence:()=>pending});try{
  button(env.d,'查看逐笔换算明细').click();[...env.d.querySelectorAll('#flow-list button')].find(b=>b.textContent==='另一流程').click();await until(()=>env.d.querySelector('#flow-definition').textContent.includes('另一流程'));resolveOther(reply(page('fx')));await tick();await tick();assert(!env.d.querySelector('#flow-preview').textContent.includes('row-1'));assert.equal(env.d.querySelector('#flow-preview').hidden,true);
 }finally{env.dom.window.close()}
 console.log(JSON.stringify({run_bound:true,exact_values:true,complete_page_checks:true,empty_and_error:true,version_changes_rejected:true,safe_text:true,preview_and_flow_race_protected:true,financial_approval:false}));
})().catch(e=>{console.error(e);process.exitCode=1});
