'use strict';
/* Optional gateway-owned OIDC login; no token is exposed to the page. */
(function(){
 const form=document.querySelector('#login-form');
 if(!form||typeof fetch!=='function')return;
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
 const ready=fetch('/auth/status',{credentials:'same-origin',cache:'no-store',signal:controller.signal})
  .then(r=>r.status===404?false:r.ok?r.json().then(v=>v?.enabled===true):null).catch(()=>null).finally(()=>clearTimeout(timer));
 window.nexusGatewayLogout=async()=>{
  const enabled=await ready;if(enabled===false)return true;if(enabled!==true)return false;
  try{if(typeof window.nexusGatewaySessionGeneration!=='function')return false;const generation=await window.nexusGatewaySessionGeneration();
   const r=await fetch('/auth/logout',{method:'POST',credentials:'same-origin',cache:'no-store',headers:generation?{'X-Nexus-Session':generation}:{}});if(r.ok)window.nexusGatewayNotifyChange?.();return r.ok}catch{return false}
 };
 ready.then(enabled=>{
  if(enabled!==true)return;
  // Gateway SSO requires a live cookie session. Do not offer a stale manual
  // bearer token as an alternative within the same browser session.
  for(const element of form.querySelectorAll('label[for="access-token"],#access-token,button[type="submit"]'))element.hidden=true;
  const button=document.createElement('button');button.type='button';button.className='primary';button.textContent='使用企业账号登录';
  button.addEventListener('click',()=>{for(const key of Object.keys(sessionStorage))if(key.startsWith('nexus_'))sessionStorage.removeItem(key);location.assign('/auth/login')});
  const submit=form.querySelector('button[type=submit]');form.insertBefore(button,submit);
 });
})();
