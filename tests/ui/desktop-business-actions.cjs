// Actual workbench integration. No Electron process or native layout claim.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static'),tick=()=>new Promise(resolve=>setImmediate(resolve));
async function until(check){for(let i=0;i<100;i++){await tick();if(check())return}throw Error('desktop business action did not settle')}
const reply=value=>({ok:true,status:200,headers:{get:()=>null},json:async()=>value});
async function boot(desktop){
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'http://127.0.0.1:8123/',runScripts:'outside-only'}),w=dom.window,d=w.document;let opened=0,retried=0,release;
 w.sessionStorage.setItem('atlas_token','fixture-owner');w.setInterval=()=>0;w.matchMedia=()=>({matches:false,addEventListener(){}});w.HTMLDialogElement.prototype.close=function(){this.open=false};
 if(desktop)w.atlasDesktop={openLineage:async()=>{opened++;await new Promise(resolve=>release=resolve)},retry:async()=>retried++};
 w.fetch=async url=>{const route=new URL(url,w.location.href).pathname;if(route==='/api/models')return reply({models:[],configured:false});if(route==='/api/projects')return reply([{id:'p_1',name:'测试项目'}]);if(route.endsWith('/conversation'))return reply({project:{id:'p_1',name:'测试项目'},session_id:'c_1',sessions:[{id:'c_1',name:'对话'}],sources:[],applications:[],turns:[],mode:'rules',has_more:false});throw Error('unexpected '+route)};
 w.eval(fs.readFileSync(path.join(root,'app.js'),'utf8'));w.eval(fs.readFileSync(path.join(root,'workspace-shell.js'),'utf8'));await until(()=>w.atlasBusiness.getState().sessionId==='c_1');
 return {dom,w,d,opened:()=>opened,retried:()=>retried,release:()=>release()};
}
(async()=>{
 const web=await boot(false);try{assert.equal(web.d.querySelector('#desktop-lineage').hidden,true);web.d.querySelector('#desktop-lineage').click();assert.equal(web.opened(),0);assert.equal(web.d.querySelector('#desktop-restart'),null)}finally{web.dom.window.close()}
 const app=await boot(true);try{const button=app.d.querySelector('#workspace-settings-menu #desktop-lineage');assert.equal(button.hidden,false);app.d.querySelector('#workspace-settings').click();button.click();button.click();assert.equal(app.opened(),1,'duplicate activation is guarded');assert.equal(app.d.querySelector('#workspace-settings-menu').hidden,true);app.release();await until(()=>!button.disabled);app.w.atlasShell.reset();assert.equal(button.hidden,true);button.click();assert.equal(app.opened(),1,'sealed account cannot invoke old desktop actions');assert.equal(app.retried(),0);assert.equal(app.d.querySelector('#desktop-restart'),null)}finally{app.dom.window.close()}
 console.log(JSON.stringify({lineage_in_existing_work_settings:true,plain_web_hides_unavailable_action:true,double_click_guard:true,auth_seal_blocks_old_action:true,no_business_restart_action:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1});
