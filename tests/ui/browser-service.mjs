/* Actual Chromium and actual loopback HTTP fixture. No live model/site or gVisor. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {fileURLToPath} from 'node:url';
if(!process.env.ATLAS_BROWSER_EXECUTABLE)throw Error('Set ATLAS_BROWSER_EXECUTABLE; no browser is downloaded');
let loginPosts=0;
const fixture=http.createServer(async(req,res)=>{
 res.setHeader('Content-Type','text/html');
 if(req.url==='/login'&&req.method==='POST'){
  loginPosts++;let body='';for await(const part of req)body+=part;
  assert(body.includes('fixture-password'));res.setHeader('Set-Cookie','login=fixture; HttpOnly; SameSite=Lax');return res.end('ok');
 }
 if(req.url==='/next')return res.end('<h1>'+((req.headers.cookie||'').includes('login=fixture')?'Logged in fixture':'Anonymous fixture')+'</h1>');
 res.end(`<!doctype html><title>Actual browser fixture</title><h1>Login fixture</h1><label>Name<input name=name aria-label=Name></label><label>Password<input type=password name=password aria-label=Password></label><button id=login>Login</button><p id=result></p><a href=/next>Next page</a><script>document.querySelector('#login').onclick=async()=>{await fetch('/login',{method:'POST',body:document.querySelector('[name=password]').value});document.querySelector('#result').textContent='Login completed'};fetch('/passive',{method:'POST'});</script>`);
});
await new Promise(r=>fixture.listen(0,'127.0.0.1',r));
const reserve=http.createServer();await new Promise(r=>reserve.listen(0,'127.0.0.1',r));const port=reserve.address().port;await new Promise(r=>reserve.close(r));
const root=fileURLToPath(new URL('../..',import.meta.url)),token=randomBytes(24).toString('hex'),scope='b'.repeat(64),session='a'.repeat(64),second='c'.repeat(64);
const child=spawn(process.execPath,[root+'/browser_runner/service.mjs'],{cwd:root,env:{...process.env,PORT:String(port),ATLAS_BROWSER_TOKEN:token,
 ATLAS_BROWSER_TEST_MODE:'1',ATLAS_BROWSER_PROXY:'',ATLAS_BROWSER_WORKSPACE_ID:scope,ATLAS_BROWSER_ALLOWED_HOSTS:'127.0.0.1',ATLAS_BROWSER_PLAYWRIGHT_ROOT:root+'/tests/ui/package.json'},stdio:['ignore','pipe','pipe']});
let serviceOutput='';child.stdout.on('data',b=>serviceOutput+=b);child.stderr.on('data',b=>serviceOutput+=b);
const api='http://127.0.0.1:'+port;
async function call(id,operation,body={},expected=200){
 const value={workspace_id:scope,action_id:randomBytes(32).toString('hex'),...body};
 const response=await fetch(api+'/v1/sessions/'+id+(operation?'/'+operation:''),{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(value)});
 const result=await response.json();assert.equal(response.status,expected,JSON.stringify({error:result.error,state:result.state}));return result;
}
try{
 let ready=false;for(let i=0;i<100;i++){try{const r=await fetch(api+'/v1/health',{headers:{Authorization:'Bearer '+token}});if(r.ok){const h=await r.json();assert.equal(h.production_policy,false);ready=true;break}}catch{}await new Promise(r=>setTimeout(r,100))}
 assert(ready,'Service failed to start');
 assert.equal((await fetch(api+'/v1/health')).status,401);
 let page=await call(session,'',{url:'http://127.0.0.1:'+fixture.address().port+'/'});
 assert(page.text.includes('Login fixture'));assert.equal(Buffer.from(page.image_base64,'base64').subarray(0,8).toString('hex'),'89504e470d0a1a0a');
 const password=page.controls.find(c=>c.type==='password');assert(password.sensitive);
 await call(session,'action',{revision:page.revision,action:'fill',ref:password.ref,text:'fixture-password'},409);
 await call(session,'mode',{mode:'human'});
 page=await call(session,'observe');
 page=await call(session,'action',{revision:page.revision,action:'fill',ref:password.ref,text:''});
 page=await call(session,'action',{revision:page.revision,action:'type',text:'fixture-password'});
 assert(!JSON.stringify(page).includes('fixture-password'));
 const login=page.controls.find(c=>c.label==='Login'),actionId=randomBytes(32).toString('hex');
 const submit={action_id:actionId,revision:page.revision,action:'click',ref:login.ref};
 page=await call(session,'action',submit);assert(page.text.includes('Login completed'));assert.equal(loginPosts,1);
 const repeated=await call(session,'action',submit);assert.equal(repeated.image_sha256,page.image_sha256);assert.equal(loginPosts,1);
 await call(session,'action',{...submit,ref:password.ref},409);assert.equal(loginPosts,1);
 page=await call(session,'mode',{mode:'agent'});const next=page.controls.find(c=>c.href?.endsWith('/next'));
 page=await call(session,'follow',{ref:next.ref,revision:page.revision});assert(page.text.includes('Logged in fixture'));
 await call(session,'observe',{workspace_id:'d'.repeat(64)},404);
 await call('f'.repeat(64),'',{workspace_id:'d'.repeat(64),url:'http://127.0.0.1:'+fixture.address().port+'/'},404);
 const isolated=await call(second,'',{url:'http://127.0.0.1:'+fixture.address().port+'/next'});assert(isolated.text.includes('Anonymous fixture'));
 await call('e'.repeat(64),'',{url:'http://127.0.0.1:'+fixture.address().port+'/'},409);
 await call(second,'navigate',{url:'file:///etc/passwd'},409);
 await call(session,'close');await call(session,'observe',{},404);await call(second,'close');
 assert(!serviceOutput.includes('fixture-password'));
 console.log(JSON.stringify({real_chromium:true,actual_http_fixture:true,login_post_count:loginPosts,duplicate_action_not_replayed:true,
   cookies_preserved_within_session:true,cookies_isolated_between_sessions:true,agent_mutation_blocked:true,closed_session_inaccessible:true,
   screenshot_real_png:true,secret_input_not_in_observation:true,no_live_model:true,no_gvisor_claim:true}));
}finally{
 child.kill('SIGTERM');await new Promise(resolve=>{if(child.exitCode!==null)return resolve();child.once('exit',resolve);setTimeout(()=>{child.kill('SIGKILL');resolve()},5000).unref()});
 await new Promise(r=>fixture.close(r));
}
