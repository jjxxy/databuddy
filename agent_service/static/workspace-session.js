'use strict';
/* Bind each page to the gateway session seen at boot. No access token is stored. */
(function(){
 if(typeof window.fetch!=='function')return;
 const rawFetch=window.fetch.bind(window),pending=new Set(),generationKey='atlas_gateway_generation',broadcastKey='atlas_gateway_auth_changed';
 let generation=null,enabled=false,invalid=false;
 function clear(){for(const key of Object.keys(sessionStorage))if(key.startsWith('atlas_'))sessionStorage.removeItem(key)}
 function seal(){
  if(invalid)return;invalid=true;for(const item of pending)item.abort();pending.clear();clear();
  const draw=()=>{if(!document.body)return;const wrap=document.createElement('main'),title=document.createElement('h1'),note=document.createElement('p'),button=document.createElement('button');title.textContent='登录账号已变化';note.textContent='为保护之前的工作内容，当前页面已关闭。请重新进入工作空间。';button.textContent='重新进入';button.type='button';button.onclick=()=>location.reload();wrap.append(title,note,button);document.body.replaceChildren(wrap)};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',draw,{once:true});else draw();
 }
 function cancelled(){const e=Error('请求已取消');e.name='AbortError';return e}
 async function status(signal){
  const controller=new AbortController();let timer,stop;
  const timeout=new Promise(resolve=>{timer=setTimeout(()=>{controller.abort();resolve(null)},5000)});
  const interrupted=new Promise((_,reject)=>{stop=()=>{controller.abort();reject(cancelled())};if(signal?.aborted)stop();else signal?.addEventListener('abort',stop,{once:true})});
  const read=(async()=>{try{const r=await rawFetch('/auth/status',{credentials:'same-origin',cache:'no-store',signal:controller.signal});
   if(r.status===404)return {enabled:false};if(!r.ok)return null;return await r.json()
  }catch{return null}})();
  try{return await Promise.race([read,timeout,interrupted])}finally{clearTimeout(timer);signal?.removeEventListener('abort',stop)}
 }
 const ready=status().then(value=>{
  if(!value||value.enabled!==true)return;enabled=true;
  const next=value.authenticated===true&&typeof value.generation==='string'?value.generation:null;
  const prior=sessionStorage.getItem(generationKey);
  // Broadcast a changed server session before sealing this page so older tabs
  // cannot remain visible just because this tab itself was stale at boot.
  const announced=next||(prior?'changed:'+Date.now()+':'+Math.random():null);
  if(announced){try{if(localStorage.getItem(broadcastKey)!==announced)localStorage.setItem(broadcastKey,announced)}catch{}}
  if(prior&&prior!==next){seal();return}
  generation=next;
  if(next)sessionStorage.setItem(generationKey,next);
 });
 async function check(){if(!enabled||invalid)return;const value=await status();if(value&&value.enabled===true){const next=value.authenticated===true?value.generation:null;if(next!==generation)seal()}}
 window.addEventListener('storage',event=>{
  if(event.key!==broadcastKey||invalid)return;
  // Never wait for a network round-trip after another tab announces a change.
  if(enabled&&event.newValue!==generation)seal();else void check();
 });
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)void check()});
 window.addEventListener('focus',()=>void check());
 window.atlasGatewayNotifyChange=()=>{try{localStorage.setItem(broadcastKey,'changed:'+Date.now()+':'+Math.random())}catch{}};
 window.atlasGatewaySessionGeneration=async()=>{await ready;if(invalid)throw Error('登录账号已变化');return generation};
 async function waitReady(signal){
  if(!signal)return ready;if(signal.aborted)throw cancelled();
  let stop;const cancelledPromise=new Promise((_,reject)=>{stop=()=>reject(cancelled());signal.addEventListener('abort',stop,{once:true})});
  try{return await Promise.race([ready,cancelledPromise])}finally{signal.removeEventListener('abort',stop)}
 }
 function discard(response){try{const operation=response?.body?.cancel();operation?.catch(()=>{})}catch{}}
 window.fetch=async function(input,options={}){
  let url;try{url=new URL(typeof input==='string'||input instanceof URL?input:input.url,location.href)}catch{return rawFetch(input,options)}
  if(url.origin!==location.origin||!url.pathname.startsWith('/api/'))return rawFetch(input,options);
  const original=options.signal||(input instanceof Request?input.signal:null);
  await waitReady(original);if(invalid)throw Error('登录账号已变化，请重新进入');
  if(!enabled||!generation)return rawFetch(input,options);
  const headers=new Headers(input instanceof Request?input.headers:undefined);
  new Headers(options.headers||{}).forEach((value,key)=>headers.set(key,value));headers.set('X-Atlas-Session',generation);
  const controller=new AbortController();pending.add(controller);
  const stop=()=>controller.abort();if(original){if(original.aborted)stop();else original.addEventListener('abort',stop,{once:true})}
  let response;
  try{response=await rawFetch(input,{...options,headers,signal:controller.signal});if(invalid)throw Error('登录账号已变化，请重新进入');
   // Do not hand a response back to a page that has since switched accounts,
   // even if a cross-tab storage notification was delayed or never delivered.
   const latest=await status(controller.signal);
   if(!latest)throw Error('无法确认登录状态，请刷新并核对操作结果');
   if(latest.enabled!==true||latest.authenticated!==true||latest.generation!==generation){seal();throw Error('登录账号已变化，请重新进入')}
   if(invalid)throw Error('登录账号已变化，请重新进入');return response;
  }catch(error){controller.abort();discard(response);throw error}
  finally{pending.delete(controller);original?.removeEventListener('abort',stop)}
 };
})();
