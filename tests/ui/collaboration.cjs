// Real app DOM/API interactions; does not claim native browser layout coverage.
const {webcrypto,createHash}=require('node:crypto');
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async f=>{const until=Date.now()+5000;while(!f()){if(Date.now()>until)throw Error('Collaboration UI condition timed out');await new Promise(r=>setTimeout(r,5))}};
const reply=(v,status=200)=>({ok:status<400,status,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(v))});
async function run(){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,requests=[];
 try{
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};w.matchMedia=()=>({matches:false,addEventListener(){}});w.setInterval=()=>0;w.sessionStorage.setItem('atlas_token','fixture-owner');
  Object.defineProperty(w.crypto,'subtle',{value:webcrypto.subtle});const downloaded=[],createdBlobs=[];w.URL.createObjectURL=blob=>{createdBlobs.push(blob);return 'blob:remote-verified'};w.URL.revokeObjectURL=()=>{};w.HTMLAnchorElement.prototype.click=function(){downloaded.push({name:this.download,url:this.href})};const remoteBytes=Buffer.alloc(70010,71),hash=bytes=>createHash('sha256').update(bytes).digest('hex');let corruptFile=false,filePages=0,pauseFilePage=true,releaseFilePage;
  const personal={id:'remote',name:'预算 Agent <img src=x>',transport:'a2a',origin:'personal',revision:'rev1',enabled:true,can_edit:true,url:'https://remote.test/a2a',remote_project_id:'remote_p',remote_model_id:'remote_finance',data_sharing_approved:true,submit_approved:true,model_approved:true,approved_model_ids:['finance'],private_data_approved:false};
  let peers=[{id:'local_finance_reviewer',name:'财务复核',transport:'local',enabled:true,can_edit:false,description:'核对口径、合计和关联粒度'},personal];
  let tasks=[{id:'unknown',peer_id:'remote',peer_name:'外部复核',goal:'核对9月预算',state:'unknown',status_note:'retry the request'},
   {id:'work',peer_id:'local_finance_reviewer',peer_name:'财务复核',goal:'核对全部记录 <svg>',state:'working',deadline_at:'2026-10-23T10:15:00Z'},
   {id:'verified',peer_id:'remote',peer_name:'预算复核',goal:'核对金额',state:'succeeded',output_artifacts:[{id:'safe_file',name:'复核 <img>.txt',bytes:remoteBytes.length,sha256:hash(remoteBytes),media_type:'text/plain'}],received_result_ids:['received_actual'],input_read_coverage:{all_assigned_inputs_read:false,missing_sources:1,missing_results:0},evidence_count:2,result_summary:'收入 300.000000000002 <script>bad</script>',evidence:{integrity_verified:true,financial_verified:false,manifest_sha256:'a'.repeat(64),files_count:2,bytes:120,artifacts:[{name:'明细 <img>',sha256:'b'.repeat(64),url:'javascript:alert(1)'}]}},
   {id:'claim',peer_id:'remote',peer_name:'口头完成',goal:'未知依据',state:'succeeded',result_summary:'我已算好',evidence:{integrity_verified:false,financial_verified:true}}];
  let cancelled=0,submitted=0,detailRelease=null,peerRelease=null,delayPeers=false,delayDetail=false,disableCount=0,probeCount=0,failAuth=false;
  w.fetch=async(url,options={})=>{
   const u=new URL(url,'https://atlas.test'),route=u.pathname,method=options.method||'GET',body=options.body?JSON.parse(options.body):null;requests.push({route,method,body,query:u.search});if(failAuth)return reply({detail:'请重新登录'},401);
   if(route==='/api/models')return reply({models:[{id:'finance',name:'财经模型 <img>'},{id:'fast',name:'日常模型'}],default_model_id:'finance'});
   if(route==='/api/projects')return reply([{id:'p',name:'9月经营'},{id:'p2',name:'10月经营'}]);
   if(route.endsWith('/conversation')){const id=route.split('/')[3];return reply({project:{id,name:id==='p'?'9月经营':'10月经营'},session_id:'c_'+id,sessions:[{id:'c_'+id,name:'本期'}],sources:[],applications:[],turns:[],mode:'llm'})}
   if(route.endsWith('/agent-peers/probe')){probeCount++;return reply({configured:false,protocol_profile:'A2A 1.0',capabilities:{streaming:false}})}
   if(route==='/api/projects/p2/agent-peers')return reply({items:[{id:'p2_checker',name:'10月核对',transport:'local',origin:'builtin',can_edit:false}]});
   if(route==='/api/projects/p2/agent-tasks')return reply({items:[]});
   if(route==='/api/projects/p/agent-peers'&&method==='GET'){if(delayPeers)return new Promise(resolve=>peerRelease=()=>resolve(reply({items:[{id:'stale',name:'旧项目不能显示',origin:'builtin'}]})));return reply({items:peers})}
   if(route==='/api/projects/p/agent-peers'&&method==='POST'){submitted++;peers=[...peers,{...personal,id:'created',name:body.name}];return reply({id:'created'})}
   if(route==='/api/projects/p/agent-peers/remote'&&method==='PUT'){assert.equal(body.credential,undefined,'blank credential must preserve server secret');return reply({id:'remote'})}
   if(route==='/api/projects/p/agent-peers/remote'&&method==='DELETE'){disableCount++;peers=peers.map(p=>p.id==='remote'?{...p,enabled:false}:p);return reply({disabled:true,id:'remote'})}
   if(route==='/api/projects/p/agent-tasks')return reply({items:tasks,aggregate_usage:{calls:7}});
   if(route==='/api/projects/p/agent-tasks/work/cancel'){cancelled++;tasks=tasks.map(t=>t.id==='work'?{...t,state:'cancel_requested'}:t);return reply({id:'work',state:'cancel_requested'})}
   if(route==='/api/projects/p/agent-tasks/verified/artifacts/safe_file'){filePages++;const offset=Number(u.searchParams.get('offset')),page=remoteBytes.subarray(offset,offset+65536);const response=reply({id:'safe_file',task_id:'verified',bytes:remoteBytes.length,sha256:hash(remoteBytes),offset,length:page.length,next_offset:offset+page.length,eof:offset+page.length===remoteBytes.length,encoding:'base64',content_base64:page.toString('base64'),integrity_verified:true,page_sha256:corruptFile?'0'.repeat(64):hash(page)});if(pauseFilePage&&offset===65536)return new Promise(resolve=>releaseFilePage=()=>resolve(response));return response}
   if(route.startsWith('/api/projects/p/agent-tasks/')){const id=route.split('/').pop();if(delayDetail)return new Promise(resolve=>detailRelease=()=>resolve(reply({...tasks[2],result_summary:'旧项目详情不得出现'})));return reply(tasks.find(t=>t.id===id))}
   throw Error('Unexpected collaboration API '+method+' '+route);
  };
  w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.appTest={openProject,state:()=>({projectId,sessionId})};reloadAuthDocument=()=>{};');
  w.eval(fs.readFileSync(path.join(root,'agent-collaboration.js'),'utf8'));
  await wait(()=>w.appTest.state().sessionId==='c_p');
  d.querySelector('#agent-collaboration').click();await wait(()=>d.querySelector('[data-agent-peer-edit=remote]'));
  assert.equal(d.querySelector('[data-agent-peer-edit=local_finance_reviewer]'),null);
  assert.equal(d.querySelector('#collaboration-peers img'),null);assert.equal(d.querySelector('#collaboration-tasks svg'),null);
  assert(d.querySelector('#collaboration-peers').textContent.includes('私有资料发送需管理员批准'));
  assert.equal(d.querySelector('#collaboration-usage').hidden,true);assert.equal(d.querySelector('#collaboration-usage').textContent,'');
  const unknown=d.querySelector('[data-agent-task=unknown]').closest('article');
  assert(unknown.textContent.includes('这里不会自动重发'));assert.equal(unknown.querySelector('[data-agent-task-cancel]'),null);assert.equal(unknown.querySelector('[data-retry]'),null);
  assert(d.querySelector('[data-agent-task=claim]').closest('article').textContent.includes('尚无经过本方核验的完整成果'));
  d.querySelector('[data-agent-task=verified]').click();await wait(()=>d.querySelector('#collaboration-detail-result').textContent.includes('300.000000000002'));
  assert.equal(d.querySelector('#collaboration-detail script'),null);assert.equal(d.querySelector('#collaboration-detail a'),null);
  assert(d.querySelector('#collaboration-detail-evidence').textContent.includes('财务正确性：需依据来源与实际结果验收'));
  assert(d.querySelector('#collaboration-detail-evidence').textContent.includes('a'.repeat(64)));assert(d.querySelector('#collaboration-detail-evidence').textContent.includes('复核 <img>.txt'));assert(d.querySelector('#collaboration-detail-evidence').textContent.includes('来源 1 份'));assert.equal(d.querySelector('#collaboration-detail-evidence img'),null);assert.equal(d.querySelectorAll('#collaboration-detail-artifacts button').length,2);const downloadFile=()=>[...d.querySelectorAll('#collaboration-detail-artifacts button')].find(b=>b.textContent.includes('下载完整成果'));downloadFile().click();await wait(()=>releaseFilePage);await w.atlasCollaboration.load();assert(downloadFile().disabled,'progress refresh cannot enable duplicate download');pauseFilePage=false;releaseFilePage();await wait(()=>downloaded.length===1&&!downloadFile().disabled);assert.equal(filePages,2);assert.equal(createdBlobs[0].size,remoteBytes.length);assert.equal(downloaded[0].name,'复核 <img>.txt');corruptFile=true;downloadFile().click();await wait(()=>d.querySelector('#collaboration-error').textContent.includes('片段摘要校验失败'));assert.equal(downloaded.length,1,'corrupt page never becomes a downloaded file');corruptFile=false;
  d.querySelector('[data-agent-task-cancel=work]').click();await wait(()=>cancelled===1&&d.querySelector('[data-agent-task=work]').closest('article').textContent.includes('正在取消'));
  assert(d.querySelector('[data-agent-task=work]').closest('article').textContent.includes('不代表外部操作已经撤销'));
  assert.equal(d.querySelector('[data-agent-task-cancel=work]'),null);
  const input=(id,value)=>d.querySelector('#agent-peer-'+id).value=value;
  input('name','我的预算复核');input('url','https://peer.test/a2a');input('project','budget_p');input('credential','FIXTURE-REMOTE-CREDENTIAL');
  d.querySelector('#agent-peer-probe').click();await wait(()=>probeCount===1&&d.querySelector('#agent-peer-probe-result').textContent.includes('尚未提交任务'));
  assert.equal(submitted,0);assert.equal(requests.find(r=>r.route.endsWith('/agent-peers/probe')).body.model_approved,false);
  const submit=()=>d.querySelector('#agent-peer-form').onsubmit({preventDefault(){},submitter:d.querySelector('#agent-peer-save')});
  submit();await wait(()=>d.querySelector('#collaboration-error').textContent.includes('明确核对'));assert.equal(submitted,0);
  d.querySelector('#agent-peer-data-approved').checked=true;d.querySelector('#agent-peer-submit-approved').checked=true;d.querySelector('#agent-peer-model-approved').checked=true;
  assert([...d.querySelectorAll('[data-agent-grant-model]')].every(m=>!m.checked),'models are not silently approved');
  d.querySelector('[data-agent-grant-model=finance]').checked=true;
  submit();await wait(()=>submitted===1&&d.querySelector('#agent-peer-credential').value==='');
  const saved=requests.find(r=>r.route==='/api/projects/p/agent-peers'&&r.method==='POST').body;
  assert.equal(saved.credential,'FIXTURE-REMOTE-CREDENTIAL');assert.deepEqual(saved.approved_model_ids,['finance']);assert(!('private_data_approved' in saved));
  assert(!d.querySelector('#collaboration-dialog').textContent.includes('FIXTURE-REMOTE-CREDENTIAL'));
  d.querySelector('[data-agent-peer-edit=remote]').click();assert.equal(d.querySelector('#agent-peer-credential').value,'');
  input('name','修改显示名');submit();await wait(()=>requests.some(r=>r.route.endsWith('/agent-peers/remote')&&r.method==='PUT'));
  const edited=requests.find(r=>r.route.endsWith('/agent-peers/remote')&&r.method==='PUT');assert.equal(edited.query,'?expected_revision=rev1');assert(!('credential' in edited.body));
  await wait(()=>!d.querySelector('#agent-peer-id').value);d.querySelector('[data-agent-peer-disable=remote]').click();await wait(()=>disableCount===1);
  assert.equal(requests.find(r=>r.route.endsWith('/agent-peers/remote')&&r.method==='DELETE').query,'?expected_revision=rev1');
  // Late detail and list responses cannot cross the project boundary.
  delayDetail=true;d.querySelector('[data-agent-task=verified]').click();await wait(()=>detailRelease);
  delayPeers=true;const oldLoad=w.atlasCollaboration.load();await wait(()=>peerRelease);
  input('credential','STALE-SECRET');await w.appTest.openProject('p2');await wait(()=>d.querySelector('#collaboration-peers').textContent.includes('10月核对'));
  assert.equal(d.querySelector('#agent-peer-credential').value,'');detailRelease();peerRelease();await oldLoad;
  await new Promise(r=>setTimeout(r,20));assert(!d.querySelector('#collaboration-dialog').textContent.includes('旧项目'));assert(d.querySelector('#collaboration-detail').hidden);
  input('credential','CLEAR-ON-LOGOUT');d.querySelector('#logout').click();await wait(()=>!d.querySelector('#agent-peer-credential').value&&!d.querySelector('#collaboration-dialog').open);
  // Logout navigates away in a browser. jsdom leaves this sealed document alive.
  const afterLogout=requests.length;await assert.rejects(w.atlasBusiness.api('/models'),e=>e.authTransition);assert.equal(requests.length,afterLogout);assert.equal(d.querySelector('#agent-peer-credential').value,'');assert.equal(d.querySelector('#collaboration-dialog').open,false);assert.equal(d.querySelector('#login').hidden,false);
  assert(!requests.some(r=>r.route.includes('/operator/')||r.route==='/api/model'||r.route.includes('/company-search')));
  console.log(JSON.stringify({builtin_specialists:true,untrusted_results_are_not_financial_approval:true,unknown_never_blindly_retried:true,cancellation_pending_visible:true,personal_peer_explicit_grants:true,write_only_peer_credentials:true,remote_file_pages_and_complete_hash_verified:true,corrupt_remote_file_not_downloaded:true,versioned_peer_updates:true,project_race_protection:true,logout_clears_credentials:true,post_logout_api_sealed:true,real_browser_layout_verified:false}));
 }finally{dom.window.close()}
}
run().catch(error=>{console.error(error);process.exitCode=1});
