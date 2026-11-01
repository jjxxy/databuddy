import http from 'node:http';
import {createRequire} from 'node:module';
import {createHash, timingSafeEqual, randomBytes} from 'node:crypto';
import {stat, open as openFile} from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import {basename, extname} from 'node:path';
const require = createRequire(process.env.ATLAS_BROWSER_PLAYWRIGHT_ROOT || '/opt/atlas-work/package.json');
const {chromium} = require('playwright-core');
const token = process.env.ATLAS_BROWSER_TOKEN || '';
if(token.length < 32) throw Error('A dedicated browser service token is required');
const proxy = process.env.ATLAS_BROWSER_PROXY;
const testMode = process.env.ATLAS_BROWSER_TEST_MODE === '1';
const allowDownloads = process.env.ATLAS_BROWSER_ALLOW_DOWNLOADS === '1';
const maxDownloadMb=Number(process.env.ATLAS_BROWSER_MAX_DOWNLOAD_MB||25);
if(!Number.isSafeInteger(maxDownloadMb)||maxDownloadMb<1||maxDownloadMb>256)throw Error('Invalid download budget');
const maxDownloadBytes=maxDownloadMb*1024*1024,downloadChunkBytes=512*1024;
if(!proxy && !testMode) throw Error('A restricted egress proxy is mandatory');
const expectedWorkspace=process.env.ATLAS_BROWSER_WORKSPACE_ID||'';
if(!testMode&&!/^[a-f0-9]{64}$/.test(expectedWorkspace))throw Error('Bind this container to one actor/project workspace');
const hosts = new Set((process.env.ATLAS_BROWSER_ALLOWED_HOSTS || '').split(',').map(s=>s.trim().toLowerCase()).filter(Boolean));
const sessions = new Map(), lifetime = 30 * 60 * 1000, maximum = 2;
let opening=0;
const hash = s=>createHash('sha256').update(s).digest('hex');
const secretKey=/(password|passwd|passcode|one.?time.?code|otp|secret|csrf|xsrf|token|authorization|credential|api.?key|验证码|密码|口令)/i;
function checkedPost(request, pageUrl){
  const url=new URL(allowed(request.url())),current=new URL(allowed(pageUrl));
  if(url.origin!==current.origin||request.method()!=='POST')throw Error('Only same-origin POST can be reviewed');
  if([...url.searchParams.keys()].some(k=>secretKey.test(k)))throw Error('Sensitive URL requires user takeover');
  const headers=request.headers(),type=(headers['content-type']||'').trim(),encoding=(headers['content-encoding']||'').trim();
  // An undisclosed business header (e.g. X-Account, X-Amount or If-Match)
  // can change the meaning of an otherwise identical body. Reject it rather
  // than pretending the user reviewed a complete business request. Cookie
  // and Authorization remain private browser-session credentials.
  const standard=new Set(['accept','accept-encoding','accept-language','authorization','cache-control','connection',
    'content-length','content-type','cookie','host','origin','pragma','priority','referer','user-agent']);
  if(Object.keys(headers).some(k=>!standard.has(k.toLowerCase())&&!/^sec-[a-z0-9-]+$/i.test(k)))throw Error('Request headers require user takeover');
  if(encoding||! /^(application\/json|application\/x-www-form-urlencoded|text\/plain)(?:\s*;\s*charset\s*=\s*utf-8)?$/i.test(type))throw Error('Unsupported request format');
  const raw=request.postDataBuffer();if(!Buffer.isBuffer(raw)||raw.length>65536)throw Error('Request body unavailable or too large');
  let text;try{text=new TextDecoder('utf-8',{fatal:true}).decode(raw)}catch{throw Error('Unsupported request encoding')}
  if(!Buffer.from(text,'utf8').equals(raw))throw Error('Request bytes are not faithfully representable');
  if(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text))throw Error('Binary content requires user takeover');
  const keys=[];
  if(/^application\/json/i.test(type)){
    let parsed;try{parsed=JSON.parse(text)}catch{throw Error('Invalid JSON')}
    function walk(v,depth){if(depth>30)throw Error('Excessive request depth');if(v&&typeof v==='object'){for(const [k,x] of Object.entries(v)){keys.push(k);walk(x,depth+1)}}}walk(parsed,0);
  }else if(/^application\/x-www-form-urlencoded/i.test(type)){
    if(/%(?![0-9a-f]{2})/i.test(text))throw Error('Invalid form encoding');
    for(const [key] of new URLSearchParams(text))keys.push(key);
  }else if(/(?:password|passcode|验证码|密码|口令|csrf|authorization|access_token)\s*[:=]/i.test(text))throw Error('Sensitive content requires user takeover');
  if(keys.some(key=>secretKey.test(key)))throw Error('Sensitive content requires user takeover');
  const body_sha256=hash(raw),descriptor={method:'POST',url:url.href,content_type:type,body_sha256,bytes:raw.length};
  return {descriptor,digest:hash(JSON.stringify(descriptor)),body:text};
}
function postPublic(post){return {id:post.id,digest:post.digest,...post.descriptor,expires_at:post.expires}}
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
          sensitive:el.matches('input[type=password],input[autocomplete*=password],input[autocomplete=one-time-code]') || /(?:password|passcode|verification.?code|one.?time|otp|验证码|密码|口令)/i.test([el.getAttribute('aria-label'),el.getAttribute('placeholder'),el.getAttribute('name'),el.id].join(' ')),
          visible:box.width>0&&box.height>0&&box.bottom>0&&box.top<innerHeight,
          disabled:!!el.disabled,options:el.tagName==='SELECT'?[...el.options].slice(0,80).map(o=>({value:o.value.slice(0,200),label:o.text.slice(0,200),selected:o.selected})):undefined,box:[box.x,box.y,box.width,box.height].map(Math.round)};
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
async function close(id){const s=sessions.get(id);if(s){sessions.delete(id);clearTimeout(s.capture?.timer);if(s.capture?.route&&s.capture.phase==='pending')await s.capture.route.abort().catch(()=>{});await s.browser.close().catch(()=>{})}}
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
    const context=await browser.newContext({viewport:{width:1280,height:800},acceptDownloads:allowDownloads,serviceWorkers:'block',permissions:[]});
    const s={id,browser,context,page:await context.newPage(),scope,mode:'agent',expires:Date.now()+lifetime,results:new Map(),downloads:new Map(),busy:true};
    sessions.set(id,s);
    s.page.setDefaultTimeout(12000);
    context.on('page',page=>{if(page!==s.page)page.close().catch(()=>{})});
    s.page.on('dialog',dialog=>dialog.dismiss().catch(()=>{}));
    s.page.on('download',download=>{if(!s.expectedDownload)download.cancel().catch(()=>{})});
    await context.route('**/*',async route=>{
      try{
        allowed(route.request().url());
        const cap=s.capture;
        if(s.mode==='agent'&&cap){
          // Block every other request, including GET and navigation, while the
          // action is being reviewed. GET is not proof of read-only behaviour.
          if(cap.phase!=='armed'||route.request().method()!=='POST'){
            cap.blocked++;if(route.request().method()==='POST')cap.multiplePost=true;
            return await route.abort()
          }
          // Reserve before parsing so concurrent requests cannot win the race.
          cap.phase='capturing';
          try{
            const verified=checkedPost(route.request(),cap.pageUrl);
            Object.assign(cap,verified,{route,request:route.request(),phase:'pending'});
            cap.resolve(true);
            // Keep the original route paused; a separate decision operates on
            // this exact object, never on a reconstructed request.
            return;
          }catch(error){cap.phase='invalid';cap.resolve(false);return await route.abort()}
        }
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
  if(s.mode!=='human' && !(s.mode==='agent' && body.agent_authorized===true))throw Error('Browser interaction not authorized');
  if(s.mode==='agent' && !['click','select','fill','capture_post'].includes(body.action))throw Error('Action denied');
  const fresh=await state(s,false);
  if(body.revision!==fresh.revision)throw Error('Page changed; refresh before acting');
  const a=body.action;
  if(a==='click'||a==='fill'||a==='select'||a==='capture_post'){
    const ref=body.ref;
    if(!Number.isInteger(ref)||ref<1||ref>fresh.controls.length)throw Error('Invalid control reference');
    const item=fresh.controls[ref-1];if(!item.visible||item.disabled)throw Error('Control is unavailable');
    if(s.mode==='agent'&&item.sensitive)throw Error('Sensitive controls require user takeover');
    if(a==='capture_post'){
      if(s.mode!=='agent'||body.capture_confirmed!==true||s.capture)throw Error('Capture requires confirmation');
      let resolve;const ready=new Promise(r=>{resolve=r});
      const cap={id:randomBytes(32).toString('hex'),phase:'armed',pageUrl:fresh.url,blocked:0,resolve,expires:Math.min(Date.now()+300000,s.expires)};
      s.capture=cap;
      // Do not await click: a form navigation may wait on the paused route.
      const el=s.page.locator('a,button,input,textarea,select,[role="button"],[role="link"]').nth(ref-1);
      Promise.resolve(el.click({noWaitAfter:true})).catch(()=>{if(cap.phase==='armed'){cap.phase='invalid';resolve(false)}});
      const timer=setTimeout(()=>{if(cap.phase==='armed'){cap.phase='invalid';resolve(false)}},8000);
      const found=await ready;clearTimeout(timer);
      if(!found||cap.phase!=='pending')throw Error('No safely reviewable request was captured');
      cap.timer=setTimeout(()=>{if(cap.phase==='pending'){cap.phase='expired';cap.route.abort().catch(()=>{});close(s.id).catch(()=>{})}},Math.max(1,cap.expires-Date.now()));
      cap.timer.unref();
      return {pending_post:postPublic(cap),notice:'请求尚未发送；用户必须核对实际内容。其他请求已阻断。'};
    }
    if(s.mode==='agent'&&a==='fill'&&body.fill_confirmed!==true)throw Error('Text fill requires confirmation');
    if(s.mode==='agent'&&a==='click'&&!/^(?:查询|搜索|筛选|查找|展开|收起|下一页|上一页|切换|search|filter|find|expand|collapse|next|previous)(?:\s|$)/i.test((item.label||'').trim()))throw Error('Only query and navigation controls are allowed; take over for other clicks');
    const el=s.page.locator('a,button,input,textarea,select,[role="button"],[role="link"]').nth(ref-1);
    if(a==='select'){
      if(item.tag!=='select'||!Number.isInteger(body.option_index)||body.option_index<0||body.option_index>=Math.min(80,(item.options||[]).length))throw Error('Invalid option');
      await el.selectOption({index:body.option_index});
    }else if(a==='fill'){
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
function revealPost(s,body){
  const cap=s.capture;
  if(!cap||cap.phase!=='pending'||cap.expires<=Date.now()||body.post_id!==cap.id||body.digest!==cap.digest)throw Error('Pending request unavailable');
  return {pending_post:{...postPublic(cap),body:cap.body,blocked_requests:cap.blocked,multiple_post:!!cap.multiplePost}};
}
async function decidePost(s,body){
  const cap=s.capture;
  if(!cap||body.post_id!==cap.id||body.digest!==cap.digest||typeof body.approve!=='boolean')throw Error('Pending request unavailable');
  if(cap.phase!=='pending'){
    if(cap.decision===body.approve&&['rejected','sent','unknown'].includes(cap.phase))return {state:cap.phase,transport_code:cap.transportCode||null,notice:'已有执行记录；未重复发送。'};
    throw Error('Request is no longer pending');
  }
  if(cap.expires<=Date.now())throw Error('Approval expired');
  if(body.approve&&cap.multiplePost)throw Error('Multiple requests require user takeover');
  clearTimeout(cap.timer);cap.decision=body.approve;
  if(!body.approve){
    cap.phase='unknown';cap.body='';
    try{await cap.route.abort();cap.phase='rejected'}catch{}
    return {state:cap.phase,notice:cap.phase==='rejected'?'请求已取消，未发送；其他页面请求仍被阻断。':'取消结果未确认，请接管并核对业务系统。'};
  }
  // Mark unknown before continuing. A transport error must not retry the POST.
  cap.phase='unknown';cap.body='';
  const finished=s.page.waitForEvent('requestfinished',{predicate:request=>request===cap.request,timeout:10000});
  finished.catch(()=>{});
  try{
    await cap.route.continue();
    const request=await finished;const response=await request.response();
    if(response&&!cap.multiplePost){cap.transportCode=response.status();cap.phase='sent'}
  }catch{}
  return {state:cap.phase,transport_code:cap.transportCode||null,
    notice:cap.phase==='sent'?'已收到网络回执；业务是否完成仍需核对，其他页面请求仍被阻断。':'请求结果未知，请接管并核对业务系统；未自动重试。'};
}
async function download(s,body){
  if(!allowDownloads||s.mode!=='agent'||body.agent_authorized!==true)throw Error('Download denied');
  const fresh=await state(s,false);
  if(body.revision!==fresh.revision||!Number.isInteger(body.ref))throw Error('Page changed');
  const item=fresh.controls[body.ref-1];if(!item||!item.visible||item.disabled||item.sensitive)throw Error('Control unavailable');
  if(!/(下载|导出|download|export)/i.test((item.label||'')+' '+(item.href||'')))throw Error('Download control not identified');
  const el=s.page.locator('a,button,input,textarea,select,[role="button"],[role="link"]').nth(body.ref-1);
  s.expectedDownload=true;let file;
  try{
    [file]=await Promise.all([s.page.waitForEvent('download',{timeout:15000}),el.click({noWaitAfter:true})]);
    const filename=basename(file.suggestedFilename());
    if(!['.csv','.xlsx','.json','.jsonl','.pdf','.docx','.pptx','.txt','.md'].includes(extname(filename).toLowerCase()))throw Error('File type denied');
    const path=await file.path();const size=(await stat(path)).size;
    if(size<1||size>maxDownloadBytes||[...s.downloads.values()].reduce((n,item)=>n+item.size,0)+size>maxDownloadBytes)throw Error('Download size denied');
    const digest=createHash('sha256');let read=0;
    for await(const chunk of createReadStream(path)){read+=chunk.length;if(read>size)throw Error('Download size changed');digest.update(chunk)}
    if(read!==size||(await stat(path)).size!==size)throw Error('Download size changed');
    const id=checkId(body.action_id);
    s.downloads.set(id,{file,path,size,digest:digest.digest('hex')});file=null;
    const item=s.downloads.get(id);
    return {download_id:id,filename,bytes:size,content_sha256:item.digest};
  }finally{s.expectedDownload=false;if(file)await file.delete().catch(()=>{})}
}
async function downloadChunk(s,body){
  const id=checkId(body.download_id),item=s.downloads.get(id);
  if(!item)throw Error('Download unavailable');
  const {offset,length}=body;
  if(!Number.isSafeInteger(offset)||offset<0||offset>item.size||!Number.isSafeInteger(length)||length<1||length>downloadChunkBytes)throw Error('Invalid chunk range');
  if((await stat(item.path)).size!==item.size)throw Error('Download size changed');
  const count=Math.min(length,item.size-offset),buffer=Buffer.alloc(count),f=await openFile(item.path,'r');
  try{const {bytesRead}=await f.read(buffer,0,count,offset);if(bytesRead!==count)throw Error('Short download read')}
  finally{await f.close()}
  return {download_id:id,offset,bytes:count,content_base64:buffer.toString('base64')};
}
async function releaseDownload(s,body){
  const id=checkId(body.download_id),item=s.downloads.get(id);
  if(item){s.downloads.delete(id);await item.file.delete().catch(()=>{})}
  return {download_id:id,released:true};
}
async function bodyOf(req){let parts=[],length=0;for await(const b of req){length+=b.length;if(length>256*1024)throw Error('Request too large');parts.push(b)}return JSON.parse(Buffer.concat(parts).toString()||'{}')}
const server=http.createServer(async(req,res)=>{
  res.setHeader('Cache-Control','no-store');res.setHeader('X-Content-Type-Options','nosniff');
  const supplied=Buffer.from((req.headers.authorization||'').replace(/^Bearer /,'')),expected=Buffer.from(token);
  if(supplied.length!==expected.length||!timingSafeEqual(supplied,expected)){res.writeHead(401);return res.end('{}')}
  try{
    if(req.url==='/v1/health'&&req.method==='GET'){res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({ready:true,production_policy:!testMode,workspace_id:expectedWorkspace,restricted_proxy:!!proxy,active:sessions.size}))}
    const match=req.url.match(/^\/v1\/sessions\/([a-f0-9]{64})(?:\/(observe|mode|action|navigate|follow|scroll|download|download-chunk|download-release|post-reveal|post-decision|close))?$/);
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
    if(['download-chunk','download-release','post-reveal','post-decision'].includes(operation)){
      if(s.busy||s.chunkBusy)throw Error('Session busy');
      s.chunkBusy=true;
      try{
        const result=operation==='download-chunk'?await downloadChunk(s,body):operation==='download-release'?await releaseDownload(s,body):operation==='post-reveal'?revealPost(s,body):await decidePost(s,body);
        res.setHeader('Content-Type','application/json');return res.end(JSON.stringify({session_id:id,...result}));
      }finally{s.chunkBusy=false}
    }
    if(s.busy||s.chunkBusy)throw Error('Session busy');
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
        if(body.mode==='human'&&s.capture){
          clearTimeout(s.capture.timer);
          if(s.capture.phase==='pending')await s.capture.route.abort().catch(()=>{});
          s.capture=null;
        }
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
      }else if(operation==='download')result=await download(s,body);
      else result=await action(s,body);
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
