const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const script=fs.readFileSync(path.resolve(__dirname,'../../agent_service/static/workspace-login.js'),'utf8');
const tick=()=>new Promise(r=>setImmediate(r));
async function boot(enabled,logoutStatus=200){const dom=new JSDOM('<form id="login-form"><button type="submit">进入</button></form><button id="logout"></button>',{url:'https://nexus.test/',runScripts:'outside-only'}),w=dom.window,calls=[];
 w.nexusGatewaySessionGeneration=async()=>'gen-a';w.nexusGatewayNotifyChange=()=>{};
 w.fetch=async(url,options={})=>{calls.push({url,options});if(url==='/auth/status')return enabled===null?{ok:false,status:404}:{ok:true,status:200,json:async()=>({enabled})};return {ok:logoutStatus===200,status:logoutStatus}};
 w.eval(script);await tick();return {dom,w,calls}}
(async()=>{const a=await boot(true);try{assert(a.w.document.querySelector('#login-form').textContent.includes('使用企业账号登录'));assert.deepEqual(a.calls.map(x=>x.url),['/auth/status']);assert(await a.w.nexusGatewayLogout());assert.equal(a.calls[1].url,'/auth/logout');assert.equal(a.calls[1].options.method,'POST');assert.equal(a.calls[1].options.credentials,'same-origin');assert.equal(a.calls[1].options.headers['X-Nexus-Session'],'gen-a')}finally{a.dom.window.close()}
 const b=await boot(null);try{assert(!b.w.document.querySelector('#login-form').textContent.includes('使用企业账号登录'));assert(await b.w.nexusGatewayLogout());assert.equal(b.calls.length,1)}finally{b.dom.window.close()}
 const c=await boot(true,503);try{assert.equal(await c.w.nexusGatewayLogout(),false)}finally{c.dom.window.close()}
 console.log(JSON.stringify({optional_enterprise_login:true,same_origin_logout:true,failed_logout_not_claimed:true,no_token_in_js:true}));})().catch(e=>{console.error(e);process.exitCode=1});
