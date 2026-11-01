'use strict';
const $=s=>document.querySelector(s),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const params=new URLSearchParams(location.search);let project=params.get('project'),app=params.get('app'),session=params.get('session')||'',preview=params.get('preview')==='1',pinnedRun=params.get('run')||'',token=sessionStorage.getItem('atlas_token')||'',config=null,data=null,offset=0,limit=25,search='',sort='',descending=false,serial=0,busy=false,pendingAction=null;
let authClosed=false;const authControllers=new Set();
let businessPlan=null,businessPlanRun='',businessPlanState='';
function reloadAuthDocument(){location.reload()}
function authError(){return Object.assign(Error('登录状态已变化，请在新页面继续'),{authTransition:true})}
function changeAuthentication(nextToken=''){authClosed=true;serial++;for(const c of authControllers)c.abort();authControllers.clear();$('#shell').hidden=true;$('#login').hidden=false;for(const key of Object.keys(sessionStorage))if(key.startsWith('atlas_'))sessionStorage.removeItem(key);if(nextToken)sessionStorage.setItem('atlas_token',nextToken);reloadAuthDocument()}
async function authFetch(url,options={}){if(authClosed)throw authError();const c=new AbortController();authControllers.add(c);try{const r=await fetch(url,{...options,headers:{...options.headers,Authorization:'Bearer '+token},signal:c.signal});if(authClosed)throw authError();if(r.status===401){changeAuthentication();throw authError()}return r}finally{authControllers.delete(c)}}
const base=()=>'/api/projects/'+encodeURIComponent(project)+'/applications/'+encodeURIComponent(app);
function note(text,error=false){$('#notice').hidden=!text;$('#notice').textContent=text;$('#notice').classList.toggle('error',error)}
function amount(v){if(v==null)return '—';return String(v).replace(/(\.\d*?)0+$/,'$1').replace(/\.$/,'')}
function date(v){return v?new Date(v).toLocaleString('zh-CN',{hour12:false,timeZone:config?.timezone||'Asia/Shanghai'}):'尚未生成'}
async function request(url,options={}){const headers={};if(options.body){headers['Content-Type']='application/json';options={...options,body:JSON.stringify(options.body)}}const r=await authFetch(url,{...options,headers});if(!r.ok){let e;try{e=await r.json()}catch{e={detail:'服务暂时不可用'}}throw Error(typeof e.detail==='string'?e.detail:'请求未完成')}const value=await r.json();if(authClosed)throw authError();return value}
function bulkBlocked(){return typeof data?.bulk_blocked_reason==='string'&&!!data.bulk_blocked_reason.trim()}
function semanticRisk(){return typeof data?.semantic_warning==='string'&&!!data.semantic_warning.trim()}
function planValue(value){if(value==null)return '未记录';if(typeof value==='string')return value;try{return JSON.stringify(value,null,2)}catch{return '无法展示'}}
function stablePlanHTML(element,html){if(element.atlasHTML===html)return;element.innerHTML=html;element.atlasHTML=html}
function clearPlanHTML(element){element.replaceChildren();element.atlasHTML=undefined}
function renderRunEvidence(){
 const box=$('#run-evidence'),content=$('#run-evidence-content'),evidence=config?.run_evidence;
 if(!data?.run_id){box.hidden=true;clearPlanHTML(content);return}
 if(!evidence||evidence.run_id!==data.run_id||evidence.commit!==data.commit){box.hidden=false;stablePlanHTML(content,'<p class="business-plan-message">当前结果的运行依据未记录或无法核对，不能据此判断是否可以复现。</p>');return}
 box.hidden=false;
 let html=`<p class="business-plan-version">运行编号 ${esc(evidence.run_id)} · 代码版本 ${esc(evidence.commit)}</p>`;
 if(Array.isArray(evidence.inputs)&&evidence.inputs.length){
  html+='<section class="business-plan-section"><h3>本次读取的资料</h3><ul>'+evidence.inputs.map(input=>`<li>${esc(input.name||input.source_id||'资料')}${input.revision!=null?' · 版本 '+esc(input.revision):''}${input.records!=null?' · '+esc(input.records)+' 条':''}${input.collected_at?' · 采集时间 '+esc(input.collected_at):''}${input.input_hash?'<br>输入摘要：'+esc(input.input_hash):''}</li>`).join('')+'</ul></section>';
 }
 if(evidence.quality&&typeof evidence.quality==='object'){
  const quality=evidence.quality,counts=['passed','failed','other'].every(k=>Number.isInteger(quality[k])&&quality[k]>=0),recorded=quality.available===true&&counts&&(quality.passed+quality.failed+quality.other)>0;
  html+=`<section class="business-plan-section"><h3>已记录的检查</h3><p>${recorded?'通过 '+esc(quality.passed)+' 项 · 失败 '+esc(quality.failed)+' 项 · 其他 '+esc(quality.other)+' 项':'没有可展示的检查结果，不能据此判断已通过。'}</p><p>检查记录不等于财经口径已审核。</p></section>`;
 }
 if(evidence.replay?.available===false)html+=`<p class="business-plan-message">历史复现：${esc(evidence.replay.reason||'当前尚未验证能够复现。')}</p>`;
 if(evidence.input_hash||evidence.output_hash||evidence.dependencies||evidence.runtime){const detail={};for(const key of ['input_hash','output_hash','runtime','dependencies'])if(evidence[key]!=null)detail[key]=evidence[key];html+=`<section class="business-plan-section"><details class="business-plan-detail"><summary>查看数据摘要和运行配置</summary><pre>${esc(planValue(detail))}</pre></details><p>这些记录用于追溯，不代表运行环境已完整冻结或结果已审核。</p></section>`}
 stablePlanHTML(content,html);
}
function renderBusinessPlan(){
 const box=$('#business-plan'),message=$('#business-plan-message'),content=$('#business-plan-content'),status=$('#business-plan-status');
 if(!data?.run_id){box.hidden=true;clearPlanHTML(content);return}
 box.hidden=false;
 if(businessPlanRun!==data.run_id||businessPlanState==='loading'){
  status.textContent='读取中';message.textContent='正在读取与当前结果绑定的处理方案。';clearPlanHTML(content);return;
 }
 if(businessPlanState==='unavailable'){
  status.textContent='未获取';message.textContent='当前结果的业务方案暂不可用，请查看工作记录或联系管理员。';clearPlanHTML(content);return;
 }
 const selected=businessPlan?.selected;
 if(!selected||selected.run_id!==data.run_id||selected.commit!==data.commit){
  status.textContent='未获取';message.textContent='未能核对方案与当前结果的版本，暂不展示方案。';clearPlanHTML(content);return;
 }
 const role=selected.role,plan=selected.plan;
 status.textContent=({candidate:'候选方案',published:'正式方案',history:'历史方案',draft:'草稿方案'})[role]||'已保存方案';
 if(!plan||typeof plan!=='object'){
  message.textContent='此版本没有可读取的业务方案，不能据此判断处理口径。';clearPlanHTML(content);return;
 }
 message.textContent=selected.source_status==='derived_from_frozen_plan'||plan.origin==='derived_from_frozen_plan'?'此方案从该版本保存的处理规格整理，历史上未单独保存业务说明；请核对口径。':'方案与当前结果使用的代码版本绑定；是否满足业务口径仍需核对。';
 const steps=Array.isArray(plan.steps)?plan.steps.filter(x=>x&&typeof x==='object'):[],sources=Array.isArray(plan.sources)?plan.sources.filter(x=>x&&typeof x==='object'):[],warnings=[...(Array.isArray(plan.warnings)?plan.warnings:[]),...(Array.isArray(businessPlan.warnings)?businessPlan.warnings:[])].filter(x=>typeof x==='string'&&x.trim());
 let html=`<p class="business-plan-version">本次结果 ${esc(selected.run_id)} · 代码版本 ${esc(selected.commit)}</p>`;
 const request=typeof plan.user_request==='string'&&plan.user_request.trim()?plan.user_request:'未记录原始需求';
 html+=`<section class="business-plan-section"><h3>本次变更需求</h3><p>${esc(request)}</p><p>此处只展示该版本记录的本次请求，不代表此前全部对话。</p></section>`;
 if(typeof plan.continuation_request==='string'&&plan.continuation_request.trim())html+=`<section class="business-plan-section"><h3>继续任务时补充</h3><p>${esc(plan.continuation_request)}</p></section>`;
 if(typeof plan.summary==='string'&&plan.summary.trim())html+=`<section class="business-plan-section"><h3>方案说明</h3><p>${esc(plan.summary)}</p></section>`;
 if(sources.length)html+=`<section class="business-plan-section"><h3>方案使用的资料</h3><ul>${sources.map(s=>`<li>${esc(s.name||s.id||'未命名资料')}${s.role?' · '+esc(s.role):''}${s.design_revision!=null?' · 设计时版本 '+esc(s.design_revision):''}</li>`).join('')}</ul><p>设计时版本用于说明方案来源，本次运行的数据版本以运行依据为准。</p></section>`;
 if(steps.length)html+=`<section class="business-plan-section"><h3>处理步骤</h3><ol>${steps.map(step=>`<li><strong>${esc(step.title||'处理步骤')}</strong>${Array.isArray(step.details)&&step.details.length?'<ul>'+step.details.map(detail=>'<li>'+esc(detail)+'</li>').join('')+'</ul>':''}</li>`).join('')}</ol></section>`;
 if(warnings.length)html+=`<section class="business-plan-section"><h3>需要注意</h3><ul>${warnings.map(w=>`<li class="business-plan-warning">${esc(w)}</li>`).join('')}</ul></section>`;
 const changes=role==='candidate'&&Array.isArray(businessPlan.changes)?businessPlan.changes:[];
 if(changes.length)html+=`<section class="business-plan-section"><h3>与正式方案的变化</h3>${changes.map(change=>`<div class="business-plan-change"><strong>${esc(change.label||change.field||'处理规则')}</strong><span>原来：${esc(planValue(change.before))}</span><span>现在：${esc(planValue(change.after))}</span></div>`).join('')}</section>`;
 else if(role==='candidate')html+='<section class="business-plan-section"><h3>与正式方案的变化</h3><p>没有可展示的规则差异；这不代表数据和结果一致，仍需核对候选结果。</p></section>';
 if(plan.rules&&typeof plan.rules==='object')html+=`<section class="business-plan-section"><details class="business-plan-detail"><summary>查看规则详情</summary><pre>${esc(planValue(plan.rules))}</pre></details></section>`;
 stablePlanHTML(content,html);
}
async function loadBusinessPlan(id,runId){
 try{const value=await request(base()+'/business-plan?'+new URLSearchParams({preview:String(preview),run_id:runId}));if(id!==serial||data?.run_id!==runId||authClosed)return;businessPlan=value;businessPlanRun=runId;businessPlanState='ready';renderBusinessPlan()}
 catch(e){if(id!==serial||data?.run_id!==runId||authClosed)return;businessPlan=null;businessPlanRun=runId;businessPlanState='unavailable';renderBusinessPlan()}
}
function render(){if(!data)return;const blocked=bulkBlocked(),historicalRisk=semanticRisk();const warning=$('#semantic-warning');warning.hidden=!blocked&&!historicalRisk;warning.textContent=blocked?'这份候选的事实数值尚未完成指标、单位、期间和可加性的业务语义对齐，仅供查看，不能用于正式财务计算、报告、导出或采用。':'这份历史结果的事实业务口径或来源依据尚未核对，未核对的汇总与图表已隐藏；历史明细和下载仍可查看，不代表财务口径已获确认。';$('#title').textContent=data.application.title;document.title=data.application.title+' · Atlas';$('#description').textContent=config.config?.description||config.description||'';$('#record-count').textContent=data.total;$('#amount-metric').hidden=blocked||historicalRisk||data.metric.total==null;$('#amount-label').textContent=data.metric.label||'金额合计';$('#amount-total').textContent=blocked||historicalRisk?'—':amount(data.metric.total)+(data.metric.currency?' '+data.metric.currency:'');$('#amount-note').textContent=blocked||historicalRisk?'业务语义或来源依据待核对':data.metric.status==='confirmed'?'使用已确认的业务口径':'该代码版本的指标口径待确认';$('#schedule').textContent=config.schedule?'已开启自动更新':config.pending_schedule?'确认后启用':'未设置';$('#schedule-note').textContent=config.schedule?'下一次 '+date(config.next_due)+' · '+config.timezone:config.pending_schedule?'已记下更新计划，采用应用后自动启用':'可以在对话中说“每天上午9点更新”';$('#freshness').textContent='数据采集时间 '+date(data.data_as_of)+(config.freshness?.stale?' · 已超过设定时效':'')+(config.status==='updating'?' · 后台正在更新':config.last_run_status==='failed'?' · 上次更新失败，当前保留之前可用的数据':'');$('#preview').hidden=!data.preview;$('#publish').disabled=busy||!config.can_publish||blocked||historicalRisk;$('#preview span').textContent=blocked?'事实来源的指标、单位、期间和可加性尚未完成业务语义对齐，当前仅供查看，不能采用。':config.preview_type==='history'?'这是历史数据与应用代码的预览。如需恢复使用，请在项目工作历史中回退。':'这是候选预览，尚未替换正式应用。确认后可持续重跑和定时更新。';$('#rerun').disabled=busy||config.status==='updating';$('#export').disabled=blocked;$('#export').hidden=blocked;$('#code').disabled=false;
$('#table-head').innerHTML='<tr>'+data.fields.map(f=>`<th scope="col" data-sort="${esc(f.name)}">${esc(f.label)}${sort===f.name?` <span>${descending?'↓':'↑'}</span>`:''}</th>`).join('')+'</tr>';$('#table-body').innerHTML=data.rows.map(row=>'<tr>'+data.fields.map(f=>`<td title="${esc(row[f.name])}">${esc(row[f.name])}</td>`).join('')+'</tr>').join('');$('#empty').hidden=!!data.rows.length;$('#page-info').textContent=data.total?`${offset+1}–${Math.min(offset+limit,data.total)} / 共 ${data.total} 条`:'共 0 条';$('#prev').disabled=offset===0;$('#next').disabled=offset+limit>=data.total;
const summary=blocked||historicalRisk?[]:Array.isArray(data.summary)?data.summary:[];$('#chart-panel').hidden=blocked||historicalRisk||data.application.layout==='table'||data.application.chart!=='bar'||!summary.length;const max=Math.max(0,...summary.map(x=>Math.abs(Number(x.amount_total))));$('#chart').innerHTML=summary.map(x=>{const val=Number(x.amount_total),width=max&&Number.isFinite(val)?Math.min(100,Math.abs(val)/max*100):0;return `<div class="bar-row"><label title="${esc(x.category)}">${esc(x.category??'未填写')}</label><div class="bar-track"><div class="bar-fill ${val<0?'negative':''}" style="width:${width}%"></div></div><b>${esc(amount(x.amount_total))}</b></div>`}).join('');$('#negative-note').hidden=!summary.some(x=>Number(x.amount_total)<0);$('#version').textContent='数据版本 '+data.run_id.slice(0,10)+' · 代码 '+data.commit.slice(0,10)+(data.preview?' · 候选预览':' · 已发布');
navigation(!blocked&&!historicalRisk,blocked||historicalRisk);renderBusinessPlan();renderRunEvidence();}
function navigation(ready=false,blocked=false){
 const context='/?project='+encodeURIComponent(project)+'&application='+encodeURIComponent(app)+(session?'&session='+encodeURIComponent(session):'');
 $('#back').href=context;const revise=$('#revise');revise.href=context+'&intent=revise';revise.removeAttribute('aria-disabled');revise.removeAttribute('tabindex');revise.removeAttribute('title');
 const ask=$('#ask');ask.textContent=ready?'问问数据 ↗':blocked?'问问数据（待业务语义对齐）':'问问数据（结果生成后可用）';ask.setAttribute('aria-disabled',String(!ready));
 if(ready){ask.href=context+'&intent=ask';ask.removeAttribute('tabindex');ask.removeAttribute('title')}
 else{ask.removeAttribute('href');ask.setAttribute('tabindex','-1');ask.title=blocked?'事实业务口径尚未对齐，暂不能依据该候选回答。':'应用结果尚未生成，请先返回项目对话查看进度或调整做法。'}
}
async function refresh(initial=false){const id=++serial;try{const next=await request(base()+'?'+new URLSearchParams({preview:String(preview),...(pinnedRun?{run_id:pinnedRun}:{})}));if(id!==serial)return;config=next;if(!config.run_id){navigation(false);$('#title').textContent=config.name;note(config.status==='failed'?'生成尚未完成，之前的结果会保留。请回项目对话查看原因。':'正在生成应用，请稍候或回项目查看进度。');return}if(initial){limit=config.config.page_size;sort=config.config.default_sort;descending=config.config.descending;}const q=new URLSearchParams({preview:String(preview),offset:String(offset),limit:String(limit),search,sort,descending:String(descending),run_id:pinnedRun||config.run_id});const nextData=await request(base()+'/data?'+q);if(id!==serial)return;data=nextData;if(offset&&offset>=data.total){offset=Math.max(0,Math.floor(Math.max(0,data.total-1)/limit)*limit);await refresh();return}if(businessPlanRun!==data.run_id){businessPlan=null;businessPlanState='loading';businessPlanRun=data.run_id}$('#shell').hidden=false;$('#login').hidden=true;render();void loadBusinessPlan(id,data.run_id)}catch(e){note(e.message,true)}}
async function download(url,name){const r=await authFetch(url);if(!r.ok)throw Error('下载未完成，请检查登录状态和结果版本');const blob=await r.blob();if(authClosed)throw authError();const urlData=URL.createObjectURL(blob),a=document.createElement('a');a.href=urlData;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(urlData),1000)}
async function run(text){if(busy||text==='确认发布'&&(bulkBlocked()||semanticRisk()))return;busy=true;render();const body={text,application_id:app,session_id:session||null,interaction_mode:'work',request_id:pendingAction?.text===text?pendingAction.request_id:window.atlasSecureUUID?window.atlasSecureUUID():crypto.randomUUID()};if(text==='确认发布')body.candidate_run=data.run_id;pendingAction=body;
try{note(text==='确认发布'?'正在采用已确认的结果…':'已提交重跑，正在重新采集并按原口径加工…');const t=await request('/api/projects/'+encodeURIComponent(project)+'/messages',{method:'POST',body});pendingAction=null;const until=Date.now()+12*60*1000;while(Date.now()<until){await new Promise(resolve=>setTimeout(resolve,1500));const c=await request('/api/projects/'+encodeURIComponent(project)+'/conversation'+(session?'?session_id='+encodeURIComponent(session):''));const turn=c.turns.find(x=>x.id===t.id);if(turn&&['done','failed'].includes(turn.state)){note(turn.answer,turn.state==='failed');if(text==='确认发布'){preview=false;pinnedRun='';}await refresh();return}if(turn)note(turn.progress||'正在后台处理…')}note('工作仍在后台继续，可以返回项目对话查看进度。')}catch(e){note(e.message+'。再次提交会复用消息编号，避免重复执行。',true)}finally{busy=false;render()}}
$('#rerun').onclick=()=>run('重跑这个应用');$('#publish').onclick=()=>run('确认发布');$('#search-form').onsubmit=e=>{e.preventDefault();search=$('#search').value.trim();offset=0;refresh()};$('#table-head').onclick=e=>{const th=e.target.closest('[data-sort]');if(!th)return;descending=sort===th.dataset.sort?!descending:false;sort=th.dataset.sort;offset=0;refresh()};$('#prev').onclick=()=>{offset=Math.max(0,offset-limit);refresh()};$('#next').onclick=()=>{offset+=limit;refresh()};$('#export').onclick=()=>{if(bulkBlocked())return;return download(base()+'/export?'+new URLSearchParams({preview:String(preview),search,run_id:data?.run_id||''}),config?.name+'.csv').catch(e=>note(e.message,true))};$('#code').onclick=()=>download(base()+'/code.zip?'+new URLSearchParams({preview:String(preview),run_id:data?.run_id||''}),'数据应用代码.zip').catch(e=>note(e.message,true));$('#login-form').onsubmit=async e=>{e.preventDefault();const nextToken=$('#token').value.trim();if(!nextToken)return;$('#token').value='';changeAuthentication(nextToken)};
(async()=>{if(!project||!app){try{const r=await fetch('./application.json');const c=await r.json();project=c.project_id;app=c.application_id}catch{}}if(!project||!app){$('#shell').hidden=false;note('缺少应用标识，请从项目对话打开应用。',true);return}navigation(false);if(token){$('#shell').hidden=false;await refresh(true)}else $('#login').hidden=false})();setInterval(()=>{if(token&&!busy&&!document.hidden&&project&&app)refresh()},10000);
