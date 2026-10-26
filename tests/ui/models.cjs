// Real app DOM/API contract regression. Does not replace a browser layout test.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const waitFor=async predicate=>{const end=Date.now()+3000;while(!predicate()){if(Date.now()>end)throw Error('app did not reach expected state');await new Promise(r=>setTimeout(r,5))}};
async function run(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,requests=[],posts=[];
 try{
  w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;w.sessionStorage.setItem('atlas_token','test-session-only');
  let catalog={models:[{id:'fast',name:'日常分析'},{id:'careful',name:'复杂核算 <img src=x>'}],default_model_id:'fast',configured:true},releaseFailure,rejectModel=false;
  w.fetch=async(url,options={})=>{const p=new URL(url,'https://atlas.test/'),route=p.pathname;requests.push(route);let result;
   if(route==='/api/models')result=catalog;
   else if(route==='/api/projects')result=[{id:'p',name:'经营月报'}];
   else if(route.endsWith('/conversation'))result={project:{id:'p',name:'经营月报'},session_id:p.searchParams.get('session_id')||'c1',selected_model_id:'careful',sessions:[{id:'c1',name:'本月核算'},{id:'c2',name:'专项分析'}],sources:[],applications:[],turns:[],mode:'llm',has_more:false};
   else if(route.endsWith('/message-status'))result={accepted:false};
   else if(route.endsWith('/messages')){const body=JSON.parse(options.body);posts.push(body);if(rejectModel)return {ok:false,status:400,headers:{get:()=>null},json:async()=>({detail:'所选模型当前不可用，请从提供的模型列表中重新选择'})};if(posts.length===1){await new Promise(r=>releaseFailure=r);return {ok:false,status:503,headers:{get:()=>null},json:async()=>({detail:'临时连接中断'})}}result={id:'t1'}}
   else throw Error('unexpected API '+route);
   return {ok:true,status:200,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(result))};
  };
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.appTest={send,openSession,loadModels,state:()=>({projectId,sessionId,sending,pending,selectedModelId})};');
  await waitFor(()=>w.appTest.state().sessionId==='c1');
  const picker=d.querySelector('#model-select');assert.equal(picker.value,'careful');assert.equal(picker.options.length,2);assert.equal(picker.querySelector('img'),null);
  for(const id of ['model-dialog','company-dialog','work-dialog','model-engine','model-base','model-key','company-connect'])assert.equal(d.getElementById(id),null,id+' must not be exposed to business users');
  assert.equal(d.querySelectorAll('input[type=password]').length,3,'workspace sign-in and explicit personal MCP/Agent connections accept secrets; model keys remain absent');
  picker.value='fast';picker.onchange();assert.equal(w.appTest.state().selectedModelId,'fast');
  d.querySelector('#message').value='按办事处汇总收入并核对合计';const first=w.appTest.send();await waitFor(()=>posts.length===1);assert.equal(picker.disabled,true);assert.equal(posts[0].model_id,'fast');
  // Even a programmatic change cannot mutate a message already being sent.
  picker.value='careful';picker.onchange();assert.equal(w.appTest.state().selectedModelId,'fast');releaseFailure();await first;
  assert.equal(picker.disabled,true,'uncertain delivery must preserve the original model on retry');assert.equal(w.appTest.state().pending.model_id,'fast');
  await w.appTest.send();assert.equal(posts.length,2);assert.equal(posts[1].model_id,'fast');assert.equal(posts[1].request_id,posts[0].request_id);assert.equal(picker.disabled,false);
  await w.appTest.openSession('c2');assert.equal(picker.value,'careful','new conversation uses its server-selected model');await w.appTest.openSession('c1');assert.equal(picker.value,'fast','unsent/next-message preference survives conversation navigation');
  catalog={models:[{id:'careful',name:'复杂核算'}],default_model_id:'careful',configured:true};rejectModel=true;d.querySelector('#message').value='继续分析';await w.appTest.send();assert.equal(posts.length,3);assert.equal(w.appTest.state().pending,null,'definitively rejected and unaccepted message unlocks model selection');assert.equal(d.querySelector('#message').value,'继续分析');assert.equal(w.appTest.state().selectedModelId,'fast','revocation must not silently change the chosen model');assert.equal(picker.disabled,false);assert.equal(picker.selectedOptions[0].textContent,'原模型已停用，请重新选择');d.querySelector('#message').value='继续分析';await w.appTest.send();assert.equal(posts.length,3,'revoked model needs an explicit new choice');
  catalog={models:[],default_model_id:null,configured:false};await w.appTest.loadModels();assert.equal(picker.disabled,true);d.querySelector('#message').value='继续分析';await w.appTest.send();assert.equal(posts.length,3,'missing model cannot silently use a fallback');
  assert(!requests.some(p=>p==='/api/model'||p.includes('/company-search')),'business boot/send never reads configuration');
  console.log(JSON.stringify({named_model_selection:true,no_business_configuration:true,model_frozen_on_send:true,uncertain_retry_reuses_model_and_request:true,conversation_choice_preserved:true,no_model_blocks_send:true,rejected_unaccepted_message_unlocks_choice:true}));
 }finally{dom.window.close()}
}
run().catch(e=>{console.error(e);process.exitCode=1});
