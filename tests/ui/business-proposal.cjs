// Real conversation card and confirmation interaction; mock service is not visual browser QA.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<120;i++){await tick();if(check())return}throw Error('business proposal did not settle')}
const reply=value=>({ok:true,status:200,headers:{get:()=>null},json:async()=>JSON.parse(JSON.stringify(value))});
async function boot(state='pending',networkFail=false){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window,d=w.document,calls=[];let release;
 w.sessionStorage.setItem('nexus_token','fixture');w.setInterval=()=>0;w.matchMedia=()=>({matches:false,addEventListener(){}});w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 const proposal={id:'bp_'+ 'a'.repeat(40),digest:'b'.repeat(64),state,title:'收入<img>',sources:[{name:'收入表',role:'主来源',design_revision:1}],steps:[{title:'核对金额',details:['检查口径']}],warnings:['币种待核对']};
 w.fetch=async(url,options={})=>{const u=new URL(url,w.location.href);calls.push({path:u.pathname,method:options.method||'GET',body:options.body});
  if(u.pathname==='/api/models')return reply({models:[],configured:false});
  if(u.pathname==='/api/projects')return reply([{id:'p',name:'项目'},{id:'p2',name:'项目二'}]);
  if(u.pathname.endsWith('/conversation'))return reply({project:{id:u.pathname.includes('/p2/')?'p2':'p',name:'项目'},session_id:'c',sessions:[{id:'c',name:'对话'}],sources:[],applications:[],turns:u.pathname.includes('/p2/')?[]:[{id:'t',text:'核对',state:'done',created_at:'2026-11-01T00:00:00Z',answer:'请确认方案',business_proposal:proposal}],mode:'rules',has_more:false});
  if(u.pathname.endsWith('/decision')){if(networkFail)throw Error('network lost');await new Promise(resolve=>release=resolve);proposal.state='approved';return reply(proposal)}
  throw Error('unexpected path '+u.pathname);
 };
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8')+';window.proposalTest={openProject,seal:sealAuthentication};');
 await until(()=>d.querySelector('.business-proposal-card'));
 return {dom,w,d,calls,proposal,release:()=>release?.(),ready:()=>!!release};
}
(async()=>{
 const a=await boot();try{const card=a.d.querySelector('.business-proposal-card');assert(card.textContent.includes('业务方案待确认'));assert(card.textContent.includes('正式发布仍需另行确认'));assert(card.textContent.includes('核对金额'));assert(card.textContent.includes('币种待核对'));assert.equal(card.querySelector('img'),null);
  const confirm=card.querySelector('[data-approve="true"]'),reject=card.querySelector('[data-approve="false"]');confirm.click();confirm.click();reject.click();await until(a.ready);
  const posts=a.calls.filter(x=>x.method==='POST');assert.equal(posts.length,1,'double confirmation is suppressed');assert(posts[0].path.endsWith('/business-proposals/'+a.proposal.id+'/decision'));assert.deepEqual(JSON.parse(posts[0].body),{digest:a.proposal.digest,approve:true,session_id:'c'});
  a.release();await until(()=>a.d.querySelector('.business-proposal-card').textContent.includes('方案已确认'));assert(!a.d.querySelector('[data-proposal-decision]'));assert(!a.calls.some(c=>c.path.endsWith('/messages')),'approval does not create duplicate conversation message');
 }finally{a.dom.window.close()}
 const b=await boot('rejected');try{assert(b.d.querySelector('.business-proposal-card').textContent.includes('方案已拒绝'));assert(!b.d.querySelector('[data-proposal-decision]'))}finally{b.dom.window.close()}
 const c=await boot();try{c.d.querySelector('[data-proposal-decision]').click();await until(c.ready);await c.w.proposalTest.openProject('p2');c.release();await tick();await tick();assert(!c.d.querySelector('.business-proposal-card'));assert(!c.d.querySelector('#toast').textContent.includes('方案已确认'),'late old response cannot change the new project');}finally{c.dom.window.close()}
 const sealed=await boot();try{sealed.d.querySelector('[data-proposal-decision]').click();await until(sealed.ready);sealed.w.proposalTest.seal();sealed.release();await tick();assert(sealed.d.querySelector('#shell').hidden);assert(!sealed.d.querySelector('#toast').textContent.includes('方案已确认'))}finally{sealed.dom.window.close()}
 const uncertain=await boot('pending',true);try{uncertain.d.querySelector('[data-proposal-decision]').click();await until(()=>uncertain.d.querySelector('#toast').textContent.includes('没有确认操作结果'));assert.equal(uncertain.calls.filter(x=>x.method==='POST').length,1);assert(uncertain.d.querySelector('.business-proposal-card').textContent.includes('业务方案待确认'));}finally{uncertain.dom.window.close()}
 console.log(JSON.stringify({unknown_result_never_retried:true,late_project_and_auth_responses_guarded:true,proposal_steps_and_scope:true,request_digest_and_session_bound:true,double_click_guard:true,escaped_untrusted_labels:true,approval_separate_from_publication:true,no_extra_message:true,real_browser_layout_verified:false}));
})().catch(e=>{console.error(e);process.exitCode=1});
