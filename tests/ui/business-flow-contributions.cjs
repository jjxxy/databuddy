const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(r=>setImmediate(r));
async function until(fn){for(let i=0;i<200;i++){await tick();if(fn())return}throw Error('page did not settle')}
const digest='a'.repeat(64),commit='b'.repeat(40),input='c'.repeat(64),output='d'.repeat(64);
function reply(v,status=200){return {ok:status>=200&&status<300,status,json:async()=>structuredClone(v)}}
function detail(rowIndex,offset=0,extra={}){
 const total=51,rows=Array.from({length:Math.min(50,total-offset)},(_,i)=>({id:'明细-'+(offset+i+1),amount:offset+i===0?'9007199254740993.123456789012':'<img src=x>'}));
 return {run_id:'r_one',node_id:'agg',row_index:rowIndex,columns:[{name:'id',label:'编号'},{name:'amount',label:'金额',unit:'CNY'}],rows,total,offset,next_offset:offset+rows.length<total?offset+rows.length:null,group:{branch:'<b>东部</b>'},aggregations:[{function:'sum',column:'amount',output:{name:'total'}}],input_sha256:input,output_sha256:output,financial_verified:false,...extra};
}
async function boot(options={}){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'business-flows.html'),'utf8'),{url:'https://atlas.test/business-flows.html?project=p_one',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];w.sessionStorage.setItem('atlas_token','x');
 w.fetch=async(url)=>{const u=new URL(url,w.location.href),p=u.pathname;calls.push(p+u.search);
  if(p.endsWith('/business-flows'))return reply({items:[{id:'bf_one',name:'本次流程'},{id:'bf_two',name:'另一流程'}]});
  if(p.endsWith('/business-flows/bf_one'))return reply({candidate_digest:digest,candidate_commit:commit,candidate_run:'r_one',candidate_document:{name:'汇总',inputs:[],nodes:[{id:'agg',kind:'table_workflow',title:'集团数据',config:{plan:{stages:[{id:'total',kind:'aggregate'}]}}}]}});
  if(p.endsWith('/business-flows/bf_two'))return reply({candidate_digest:digest,candidate_commit:commit,candidate_run:'r_two',candidate_document:{name:'另一流程',inputs:[],nodes:[]}});
  if(p.endsWith('/runs/r_two'))return reply({id:'r_two',plan_digest:digest,commit,status:'succeeded',steps:[],artifacts:[]});
  if(p.endsWith('/runs/r_one'))return reply({id:'r_one',plan_digest:digest,commit,status:'succeeded',steps:[{node_id:'agg',title:'集团数据',status:'succeeded',table_available:true,contribution_available:options.available!==false}],artifacts:[]});
  if(p.endsWith('/tables/agg')){const offset=Number(u.searchParams.get('offset')),total=51,rows=Array.from({length:Math.min(50,total-offset)},(_,i)=>({v:'汇总-'+(offset+i+1)}));return reply({run_id:'r_one',node_id:'agg',columns:[{name:'v',label:'汇总'}],rows,total,offset,next_offset:offset+rows.length<total?offset+rows.length:null})}
  if(p.endsWith('/contributions')){const rowIndex=Number(u.searchParams.get('row_index')),offset=Number(u.searchParams.get('offset'));return options.contributions?options.contributions({rowIndex,offset}):reply(detail(rowIndex,offset))}
  throw Error('unexpected '+p);
 };
 w.eval(fs.readFileSync(path.join(root,'business-flow-view.js'),'utf8'));w.eval(fs.readFileSync(path.join(root,'business-flows.js'),'utf8'));
 await until(()=>[...d.querySelectorAll('#flow-output-list button')].some(b=>b.textContent.startsWith('查看数据：')));
 return {dom,w,d,calls};
}
const getButton=(d,text)=>[...d.querySelectorAll('#flow-preview button')].find(b=>b.textContent===text);
async function openTable(env){[...env.d.querySelectorAll('#flow-output-list button')].find(b=>b.textContent.startsWith('查看数据：')).click();await until(()=>env.d.querySelector('#flow-preview table'))}
(async()=>{
 let env=await boot();try{
  await openTable(env);const {d}=env,p=d.querySelector('#flow-preview');assert.equal([...p.querySelectorAll('button')].filter(b=>b.textContent==='查看汇总输入明细').length,50);
  getButton(d,'下一页').click();await until(()=>p.textContent.includes('汇总-51'));assert.equal([...p.querySelectorAll('button')].filter(b=>b.textContent==='查看汇总输入明细').length,1);
  getButton(d,'查看汇总输入明细').click();await until(()=>p.textContent.includes('明细-1'));assert(env.calls.some(x=>x.includes('row_index=50&offset=0')));
  assert(p.textContent.includes('结果第 51 行'));assert(p.textContent.includes('同组输入不代表每笔都参与每个指标'));assert(p.textContent.includes('9007199254740993.123456789012'));assert(p.textContent.includes('<img src=x>'));assert(p.textContent.includes('<b>东部</b>'));assert.equal(p.querySelector('img'),null);assert.equal(p.querySelector('b'),null);assert(p.textContent.includes(input)&&p.textContent.includes(output));
  getButton(d,'下一页').click();await until(()=>p.textContent.includes('明细-51'));getButton(d,'上一页').click();await until(()=>p.textContent.includes('明细-1'));
  getButton(d,'返回数据结果').click();await until(()=>p.textContent.includes('汇总-51'));
 }finally{env.dom.window.close()}
 env=await boot({available:false});try{await openTable(env);assert(!getButton(env.d,'查看汇总输入明细'))}finally{env.dom.window.close()}
 env=await boot({contributions:async({rowIndex,offset})=>reply(detail(rowIndex,offset,offset?{input_sha256:'f'.repeat(64)}:{}))});try{
  await openTable(env);getButton(env.d,'查看汇总输入明细').click();await until(()=>env.d.querySelector('#flow-preview').textContent.includes('明细-1'));getButton(env.d,'下一页').click();await until(()=>env.d.querySelector('#flow-notice').textContent.includes('版本或分页'));assert(!env.d.querySelector('#flow-preview').textContent.includes('明细-51'));
 }finally{env.dom.window.close()}
 env=await boot({contributions:async()=>reply({detail:'资料已失效'},400)});try{await openTable(env);getButton(env.d,'查看汇总输入明细').click();await until(()=>env.d.querySelector('#flow-preview').textContent.includes('未能读取'));assert(env.d.querySelector('#flow-notice').textContent.includes('资料已失效'));assert(getButton(env.d,'返回数据结果'))}finally{env.dom.window.close()}
 let resolve;const pending=new Promise(r=>{resolve=r});env=await boot({contributions:()=>pending});try{
  await openTable(env);getButton(env.d,'查看汇总输入明细').click();[...env.d.querySelectorAll('#flow-list button')].find(b=>b.textContent==='另一流程').click();await until(()=>env.d.querySelector('#flow-definition').textContent.includes('另一流程'));resolve(reply(detail(0)));await tick();await tick();assert(!env.d.querySelector('#flow-preview').textContent.includes('明细-1'));assert.equal(env.d.querySelector('#flow-preview').hidden,true);
 }finally{env.dom.window.close()}
 console.log(JSON.stringify({absolute_row_bound:true,pagination_integrity:true,safe_text:true,workflow_race_protected:true,actual_financial_approval:false}));
})().catch(e=>{console.error(e);process.exitCode=1});
