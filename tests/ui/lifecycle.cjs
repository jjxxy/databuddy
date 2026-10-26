const {JSDOM}=require('jsdom');const fs=require('node:fs');const path=require('node:path');const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
async function scenario(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document;
 let requests=[],permissionCalls=0,restoredSources=[],view={source_ids:['s_one','s_two'],revision:7,draft:'回来继续的草稿',mode:'chat',at_bottom:true,pending:{text:'上次未确认的消息',request_id:'same-id-after-reload',session_id:'c_one',interaction_mode:'work'}},state={projectId:'p_one',sessionId:'c_one',mode:'work',pending:null,sourceIds:['s_one','s_two']},navigation=[],conflicted=false,accepted=false;
 w.Notification={permission:'default',requestPermission:async()=>{permissionCalls++;return 'denied'}};w.matchMedia=()=>({matches:false,addEventListener(){}});w.eval(fs.readFileSync(path.join(root,'lifecycle.js'),'utf8')+';window.createAtlasLifecycle=createAtlasLifecycle;');
 const bridge={api:async(p,o={})=>{requests.push([p,o]);if(p==='/browser/register')return {desktop:false,push_enabled:false,last_location:{project_id:'p_one',session_id:'c_one'}};if(p.includes('message-status'))return {accepted};if(p.includes('/view')&&o.method==='PUT'){if(conflicted){const e=Error('另一标签页更新');e.status=409;throw e}assert.equal(o.body.revision,view.revision);view={...o.body,revision:view.revision+1};return {revision:view.revision,draft_saved:true,pending_saved:!!view.pending}}if(p.includes('/view'))return {...view};if(p.startsWith('/activity'))return {events:[],unread:[],unread_count:0,cursor:1,active:[],active_count:0};if(p.includes('/subscription')||p.endsWith('/read'))return {ok:true};if(p.includes('/preferences'))return {desktop:o.body.desktop};throw Error('unexpected '+p)},toast:()=>{},state:()=>({...state}),projects:()=>[{id:'p_one',name:'公司项目'}],atLatest:()=>true,jumpLatest:()=>{},setMode:v=>state.mode=v,setPending:v=>state.pending=v,setAttachments:ids=>restoredSources=ids,refresh:async()=>{},navigate:async(p,c)=>navigation.push([p,c]),includeTurn:async()=>{}};
 const life=w.createAtlasLifecycle(bridge);assert.deepEqual(JSON.parse(JSON.stringify(await life.register())),{project_id:'p_one',session_id:'c_one'});await life.restore();assert.equal(d.querySelector('#message').value,'上次未确认的消息');assert.equal(state.pending.request_id,'same-id-after-reload');assert.equal(state.mode,'work');assert.deepEqual([...restoredSources],['s_one','s_two']);await life.persist();assert.deepEqual([...view.source_ids],['s_one','s_two']);await life.start();assert.equal(permissionCalls,0);assert(!requests.some(([p])=>p.endsWith('/messages')),'must not automatically resend a pending message');
 d.querySelector('#desktop-notify').checked=true;await d.querySelector('#desktop-notify').onchange();assert.equal(permissionCalls,1);assert.equal(d.querySelector('#desktop-notify').checked,false);assert.equal(w.localStorage.length,1);assert(!w.localStorage.getItem('atlas_token'));
 await life.navigate({project_id:'p_one',session_id:'c_two',seq:3});assert.deepEqual(navigation,[['p_one','c_two']]);assert(requests.some(([p,o])=>p.endsWith('/read')&&o.body.sequences[0]===3));
 // Simulated server conflict cannot discard local text or silently overwrite it.
 d.querySelector('#message').value='当前标签页不能丢的输入';conflicted=true;await assert.rejects(life.persist());assert.equal(d.querySelector('#message').value,'当前标签页不能丢的输入');assert.equal(d.querySelector('#draft-conflict').hidden,false);await assert.rejects(life.persist());conflicted=false;await d.querySelector('#draft-keep-local').onclick();assert.equal(view.draft,'当前标签页不能丢的输入');
 accepted=true;await life.restore();assert.equal(restoredSources.length,0,'accepted recovered message clears its attachments');await life.logout();dom.window.close();return {resume:true,pending_request_id_preserved:true,no_automatic_send:true,no_permission_prompt_on_load:true,permission_denial_handled:true,notification_navigation:true,draft_conflict_preserves_input:true};
}
scenario().then(v=>console.log(JSON.stringify(v))).catch(e=>{console.error(e);process.exit(1)});

async function appIntegration(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,views=new Map(),posts=[];try{
 w.matchMedia=()=>({matches:false,addEventListener(){}});w.sessionStorage.setItem('atlas_token','local-test-token-only');
 const pending={text:'上次尚未接收的工作',request_id:'original-request-id',source_id:'s_existing',session_id:'c_second',interaction_mode:'work',candidate_run:null,application_id:null,clear_application:false,resume_turn_id:null};
 views.set('c_second',{revision:3,draft:pending.text,mode:'work',pending,at_bottom:true});
 w.fetch=async(url,options={})=>{const p=new URL(url,'https://atlas.test'),route=p.pathname,body=options.body?JSON.parse(options.body):null;let result;
 if(route==='/api/browser/register')result={desktop:false,last_location:{project_id:'p_one',session_id:'c_second'}};
 else if(route==='/api/models')result={models:[{id:'default',name:'业务模型'}],default_model_id:'default',configured:true};
 else if(route==='/api/projects')result=[{id:'p_one',name:'项目一'}];
 else if(route.endsWith('/conversation'))result={project:{id:'p_one',name:'项目一'},session_id:p.searchParams.get('session_id')||'c_first',sessions:[{id:'c_first',name:'默认对话'},{id:'c_second',name:'继续工作'}],sources:[],applications:[],turns:[],attention:[],mode:'llm',has_more:false};
 else if(route.endsWith('/view')&&options.method==='PUT'){const old=views.get(body.session_id)||{revision:0};assert.equal(body.revision,old.revision);views.set(body.session_id,{...body,revision:old.revision+1});result={revision:old.revision+1,draft_saved:true,pending_saved:!!body.pending};}
 else if(route.endsWith('/view'))result=views.get(p.searchParams.get('session_id'))||{revision:0,draft:'',pending:null,at_bottom:true};
 else if(route.endsWith('/message-status'))result={accepted:false};
 else if(route==='/api/activity')result={events:[],unread:[],unread_count:0,cursor:10,active:[],active_count:0};
 else if(route.endsWith('/messages')){posts.push(body);result={id:'t_work'}}
 else if(route.endsWith('/subscription'))result={ok:true};else throw Error('unexpected API '+route);
 return {ok:true,status:200,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(result))};
 };
 w.eval(fs.readFileSync(path.join(root,'lifecycle.js'),'utf8')+';window.createAtlasLifecycle=createAtlasLifecycle;');w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.appTest={send,state:()=>({projectId,sessionId,pending,navigating})};');
 const until=Date.now()+3000;while(Date.now()<until&&(w.appTest.state().sessionId!=='c_second'||w.appTest.state().navigating))await new Promise(r=>setTimeout(r,10));
 assert.equal(w.appTest.state().sessionId,'c_second');assert.equal(w.document.querySelector('#message').value,pending.text);assert.equal(posts.length,0);await w.appTest.send();assert.equal(posts.length,1);assert.equal(posts[0].request_id,'original-request-id');assert.equal(posts[0].source_id,'s_existing');assert.equal(w.document.querySelector('#message').value,'');assert.equal(views.get('c_second').pending,null);console.log(JSON.stringify({actual_app_boot_restore:true,explicit_resend_reuses_request_and_source:true,accepted_message_clears_persisted_draft:true}));
 }finally{dom.window.close()}
}
appIntegration().catch(e=>{console.error(e);process.exit(1)});
