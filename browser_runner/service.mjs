import http from 'node:http';
import {createRequire} from 'node:module';
import {createHash, timingSafeEqual} from 'node:crypto';
const require = createRequire(process.env.ATLAS_BROWSER_PLAYWRIGHT_ROOT || '/opt/atlas-work/package.json');
const {chromium} = require('playwright-core');
const token = process.env.ATLAS_BROWSER_TOKEN || '';
if(token.length < 32) throw Error('A dedicated browser service token is required');
const proxy = process.env.ATLAS_BROWSER_PROXY;
const testMode = process.env.ATLAS_BROWSER_TEST_MODE === '1';
if(!proxy && !testMode) throw Error('A restricted egress proxy is mandatory');
const expectedWorkspace=process.env.ATLAS_BROWSER_WORKSPACE_ID||'';
if(!testMode&&!/^[a-f0-9]{64}$/.test(expectedWorkspace))throw Error('Bind this container to one actor/project workspace');
const hosts = new Set((process.env.ATLAS_BROWSER_ALLOWED_HOSTS || '').split(',').map(s=>s.trim().toLowerCase()).filter(Boolean));
const sessions = new Map(), lifetime = 30 * 60 * 1000, maximum = 2;
let opening=0;
const hash = s=>createHash('sha256').update(s).digest('hex');
function allowed(raw) {
  const url = new URL(raw);
  if(!['http:','https:'].includes(url.protocol) || url.username || url.password || (url.port && !['80','443'].includes(url.port) && !(testMode&&url.hostname==='127.0.0.1'))) throw Error('URL denied');
  if(!hosts.has('*') && !hosts.has(url.hostname.toLowerCase())) throw Error('Host is not approved');
  return url.href;
}
function checkId(id){if(!/^[a-f0-9]{64}$/.test(id||''))throw Error('Invalid identifier');return id}
async function state(s, image=true) {
  const dom = await s.page.evaluate(()=>{
    const sensitiveKeys=/^(?:code|access_token|id_token|refresh_token|token|password|secret|session|authorization)$/i;
    function safeLink(raw){try{const url=new URL(raw,location.href);return [...url.searchParams.keys()].some(k=>sensitiveKeys.test(k))?null:url.href}catch{return null}}
    const list=[...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"]')];
    return {url:safeLink(location.href)||location.origin+location.pathname,sensitive_url:!safeLink(location.href),title:document.title.slice(0,300),text:(document.body?.innerText||'').slice(0,12000),
      controls:list.slice(0,160).map((el,index)=>{
        const box=el.getBoundingClientRect();
        return {ref:index+1,tag:el.tagName.toLowerCase(),type:el.getAttribute('type')||'',
          label:(el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.innerText||el.getAttribute('name')||'').slice(0,180),
          href:el.tagName==='A'?safeLink(el.href)?.slice(0,2000)||null:null,
          sensitive:el.matches('input[type=password],input[autocomplete*=password],input[autocomplete=one-time-code]'),
          visible:box.width>0&&box.height>0&&box.bottom>0&&box.top<innerHeight,
          disabled:!!el.disabled,box:[box.x,box.y,box.width,box.height].map(Math.round)};
      }),scroll:[Math.round(scrollX),Math.round(scrollY)]};
  });
  if(s.mode==='agent'&&dom.sensitive_url)throw Error('Finish sign-in and leave the authentication URL before returning control');
  if(s.mode==='agent')allowed(dom.url);
  const revision=hash(JSON.stringify(dom));
  s.revision=revision;
  const result={...dom,revision,mode:s.mode,untrusted_content:true,expires_at:s.expires,
    viewport:{width:1280,height:800},image_media_type:'image/png'};
  if(image){
    const png=await s.page.screenshot({type:'png',timeout:15000});
    if(png.length>2*1024*1024)throw Error('Screenshot exceeds budget');
    result.image_base64=png.toString('base64');result.image_sha256=hash(png);
  }
  return result;
}
async function close(id){const s=sessions.get(id);if(s){sessions.delete(id);await s.browser.close().catch(()=>{})}}
async function open(id,scope,url){
  if(sessions.size+opening>=maximum)throw Error('Browser capacity reached');
  checkId(scope);allowed(url);
  opening++;let browser;
  try{
  browser=await chromium.launch({executablePath:process.env.ATLAS_BROWSER_EXECUTABLE||'/usr/bin/chromium',headless:true,
    env:{PATH:'/usr/local/bin:/usr/bin:/bin',HOME:'/tmp',TMPDIR:'/tmp',LANG:'C.UTF-8',LD_LIBRARY_PATH:process.env.LD_LIBRARY_PATH||''},
    proxy:proxy?{server:proxy,bypass:'<-loopback>'}:undefined,
    args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote','--disable-extensions','--disable-background-networking',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp','--proxy-bypass-list=<-loopback>']});
    const context=await browser.newContext({viewport:{width:1280,height:800},acceptDownloads:false,serviceWorkers:'block',permissions:[]});
    const s={browser,context,page:await context.newPage(),scope,mode:'agent',expires:Date.now()+lifetime,results:new Map(),busy:true};
    sessions.set(id,s);
    s.page.setDefaultTimeout(12000);
    context.on('page',page=>{if(page!==s.page)page.close().catch(()=>{})});
    s.page.on('dialog',dialog=>dialog.dismiss().catch(()=>{}));
    s.page.on('download',download=>download.cancel().catch(()=>{}));
    await context.route('**/*',route=>{
      try{
        allowed(route.request().url());
        if(s.mode!=='human'&&!['GET','HEAD'].includes(route.request().method()))return route.abort();
        return route.continue();
      }catch{return route.abort()}
    });
    await context.routeWebSocket('**/*',socket=>socket.close());
    await s.page.goto(url,{waitUntil:'domcontentloaded',timeout:25000});
    const result=await state(s);s.busy=false;return result;
  }catch(error){sessions.delete(id);await browser?.close().catch(()=>{});throw error}
  finally{opening--}
}
async function action(s,body){
  if(s.mode!=='human')throw Error('Take over the browser before interacting');
  const fresh=await state(s,false);
  if(body.revision!==fresh.revision)throw Error('Page changed; refresh before acting');
  const a=body.action;
  if(a==='click'||a==='fill'){
    const ref=body.ref;
    if(!Number.isInteger(ref)||ref<1||ref>fresh.controls.length)throw Error('Invalid control reference');
    const item=fresh.controls[ref-1];if(!item.visible||item.disabled)throw Error('Control is unavailable');
    const el=s.page.locator('a,button,input,textarea,select,[role="button"],[role="link"]').nth(ref-1);
    if(a==='fill'){
      if(typeof body.text!=='string'||body.text.length>4000)throw Error('Invalid input');
      await el.fill(body.text);
    }else await el.click({noWaitAfter:true});
  }else if(a==='type'){
    if(typeof body.text!=='string'||body.text.length>4000)throw Error('Invalid input');
    await s.page.keyboard.insertText(body.text);
  }else if(a==='pointer'){
    if(!Number.isInteger(body.x)||!Number.isInteger(body.y)||body.x<0||body.x>=1280||body.y<0||body.y>=800)throw Error('Invalid pointer');
    await s.page.mouse.click(body.x,body.y);
  }else if(a==='press'){
    if(!['Enter','Tab','Escape','ArrowDown','ArrowUp','Backspace'].includes(body.key))throw Error('Key denied');
    await s.page.keyboard.press(body.key);
  }else if(a==='scroll'){
    if(!Number.isInteger(body.delta)||Math.abs(body.delta)>1200)throw Error('Invalid scroll');
    await s.page.mouse.move(1200,700);await s.page.mouse.wheel(0,body.delta);
  }else throw Error('Action denied');
  // A bounded settling interval, never a wait for an arbitrary network idle.
  await new Promise(resolve=>setTimeout(resolve,150));
  return state(s);
}
async function bodyOf(req){let parts=[],length=0;for await(const b of req){length+=b.length;if(length>256*1024)throw Error('Request too large');parts.push(b)}return JSON.parse(Buffer.concat(parts).toString()||'{}')}
const server=http.createServer(async(req,res)=>{
  res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
  const supplied=Buffer.from((req.headers.authorization||'').replace(/^Bearer /,'')),expected=Buffer.from(token);
  if(supplied.length!==expected.length||!timingSafeEqual(supplied,expected)){res.writeHead(401);return res.end('{}')}
  try{
    if(req.url==='/v1/health'&&req.method==='GET'){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({ready:true,production_policy:!testMode,workspace_id:expectedWorkspace,restricted_proxy:!!proxy,active:sessions.size}))}
    const match=req.url.match(/^\/v1\/sessions\/([a-f0-9]{64})(?:\/(observe|mode|action|navigate|follow|scroll|close))?$/);
    if(!match||req.method!=='POST')throw Error('Route denied');
    const [,id,operation]=match,body=await bodyOf(req);checkId(body.workspace_id);
    if(expectedWorkspace&&body.workspace_id!==expectedWorkspace){res.writeHead(404);return res.end('{}')}
    let s=sessions.get(id);
    if(!operation){
      if(s)throw Error('Session already exists; observe instead');
      const result=await open(id,body.workspace_id,body.url);res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({session_id:id,...result}));
    }
    if(!s){
      if(operation==='close'){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({session_id:id,closed:true}))}
      res.writeHead(404);return res.end('{}');
    }
    if(s.scope!==body.workspace_id){res.writeHead(404);return res.end('{}')}
    if(Date.now()>s.expires){await close(id);res.writeHead(404);return res.end('{}')}
    if(s.busy)throw Error('Session busy');
    const actionId=checkId(body.action_id),fingerprint=hash(JSON.stringify(body));
    if(s.results.has(actionId)){
      const old=s.results.get(actionId);if(old.fingerprint!==fingerprint)throw Error('Action identifier was reused');
      res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(old.result));
    }
    if(s.results.size>=64&&operation!=='close')throw Error('Session action budget reached');
    s.busy=true;
    // An unacknowledged mutation is never replayed after a connection failure.
    s.results.set(actionId,{fingerprint,result:{session_id:id,state:'unknown',message:'Action result could not be confirmed'}});
    try{
      let result;
      if(operation==='close'){await close(id);result={closed:true}}
      else if(operation==='observe')result=await state(s);
      else if(operation==='mode'){
        if(!['agent','human'].includes(body.mode))throw Error('Invalid mode');
        const old=s.mode;s.mode=body.mode;try{result=await state(s)}catch(error){s.mode=old;throw error}
      }else if(operation==='navigate'||operation==='follow'){
        if(s.mode!=='agent')throw Error('Browser is under user control');
        let url=body.url;
        if(operation==='follow'){
          const fresh=await state(s,false);
          if(body.revision!==fresh.revision||!Number.isInteger(body.ref))throw Error('Page changed');
          const control=fresh.controls[body.ref-1];if(!control?.href||!control.visible)throw Error('Link unavailable');url=control.href;
        }
        await s.page.goto(allowed(url),{waitUntil:'domcontentloaded',timeout:25000});result=await state(s);
      }else if(operation==='scroll'){
        if(s.mode!=='agent'||!Number.isInteger(body.delta)||Math.abs(body.delta)>1200)throw Error('Scroll denied');
        await s.page.mouse.move(1200,700);await s.page.mouse.wheel(0,body.delta);await new Promise(r=>setTimeout(r,150));result=await state(s);
      }else result=await action(s,body);
      result={session_id:id,...result};s.results.set(actionId,{fingerprint,result});
      res.setHeader('Content-Type','application/json');res.end(JSON.stringify(result));
    }finally{s.busy=false}
  }catch{
    // Do not log URLs, DOM, passwords, cookies or upstream exceptions.
    res.writeHead(409,{'Content-Type':'application/json'});res.end(JSON.stringify({error:'Browser operation failed or page changed; refresh and review before continuing'}));
  }
});
setInterval(()=>{for(const [id,s] of sessions)if(Date.now()>s.expires)close(id)},15000).unref();
server.requestTimeout=35000;server.headersTimeout=10000;
server.listen(Number(process.env.PORT||8091),'0.0.0.0');
for(const signal of ['SIGTERM','SIGINT'])process.on(signal,async()=>{server.close();await Promise.all([...sessions.keys()].map(close));process.exit(0)});
