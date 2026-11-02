/* Real browser handoff and microphone transport; no credentials in local storage. */
(()=>{
 'use strict';
 const b=window.nexusBusiness,browserDialog=document.querySelector('#cloud-browser-dialog'),voiceDialog=document.querySelector('#voice-dialog');
 if(!b||!browserDialog||!voiceDialog)return;
 const $=s=>browserDialog.querySelector(s),v=s=>voiceDialog.querySelector(s);
 let generation=0,voiceEpoch=0,pid='',sid='',snapshot=null,busy=false,recorder=null,stream=null,peer=null,channel=null,voiceTimer=null,voiceCall=null,browserPollTimer=null,pendingSignature='';
 function node(tag,text){const el=document.createElement(tag);el.textContent=text;return el}
 function id(){return window.nexusSecureUUID?window.nexusSecureUUID():crypto.randomUUID()}
 function hangup(call){try{Promise.resolve(b.authFetch('/api/projects/'+encodeURIComponent(call.project)+'/voice/calls/'+encodeURIComponent(call.id)+'/end',{method:'POST',keepalive:true})).catch(()=>{})}catch{}}
 function stopVoice(){
  voiceEpoch++;
  const call=voiceCall;voiceCall=null;
  clearTimeout(voiceTimer);voiceTimer=null;
  if(recorder){recorder.onstop=null;recorder.ondataavailable=null;if(recorder.state!=='inactive')recorder.stop();recorder=null}
  stream?.getTracks().forEach(track=>track.stop());stream=null;channel?.close();channel=null;peer?.close();peer=null;
  v('audio').pause();v('audio').srcObject=null;v('[data-voice-stop]').disabled=true;
  if(call)hangup(call);
 }
 function reset(){
  generation++;stopVoice();clearTimeout(browserPollTimer);browserPollTimer=null;pendingSignature='';pid='';sid='';snapshot=null;busy=false;
  $('[data-browser-image]').removeAttribute('src');$('[data-browser-controls]').replaceChildren();$('[data-browser-list]').replaceChildren();
  $('[data-browser-input]').value='';$('[data-browser-url]').textContent='';$('[data-browser-notice]').textContent='';
  v('[data-voice-text]').value='';v('[data-voice-notice]').textContent='';
  v('[data-voice-record]').disabled=true;v('[data-voice-realtime]').disabled=true;
  if(browserDialog.open)browserDialog.close();if(voiceDialog.open)voiceDialog.close();
 }
 function current(mark,project){return mark===generation&&project===b.getState().projectId&&pid===project}
 function base(){return '/projects/'+encodeURIComponent(pid)+'/browsers/'+encodeURIComponent(sid)}
 // Conversation approvals use only identifiers already carried by the current
 // turn. Exact contents are fetched only after the user chooses to review.
 // They never enter HTML strings, local storage or an implicit retry queue.
 const chatInFlight=new Set(),chatUncertain=new Set();
 async function reviewInConversation(button){
  const card=button.closest('[data-browser-chat-card]'),turn=card?.closest('[data-turn]');
  const fill=card?.dataset.browserChatCard,session=card?.dataset.browserChatSession;
  const state=b.getState(),project=state.projectId,conversation=state.sessionId,mark=generation;
  if(!card||!turn||!project||!conversation||!/^bfill_[a-f0-9]{48}$/.test(fill||'')||!/^[a-f0-9]{64}$/.test(session||''))return;
  const key=project+':'+session+':'+fill,notice=card.querySelector('[data-browser-chat-notice]'),detail=card.querySelector('[data-browser-chat-detail]');
  const valid=()=>mark===generation&&b.getState().projectId===project&&b.getState().sessionId===conversation&&card.isConnected;
  if(chatInFlight.has(key))return;
  if(chatUncertain.has(key)){notice.textContent='上次操作结果尚未确认，请打开浏览器接管核对，不要重复操作。';return}
  chatInFlight.add(key);button.disabled=true;
  const path='/projects/'+encodeURIComponent(project)+'/browsers/'+encodeURIComponent(session)+'/pending-actions';
  try{
   const list=await b.api(path);if(!valid())return;
   const item=(Array.isArray(list.items)?list.items:[]).find(x=>x?.id===fill&&['fill','capture_post','post'].includes(x.kind));
   if(!item||!/^[a-f0-9]{64}$/.test(item.digest||''))throw Error('这项待确认操作已失效，请刷新工作记录。');
   const reviewed=await b.api(path+'/'+encodeURIComponent(fill)+'/reveal',{method:'POST',body:{}});
   if(!valid())return;
   if(reviewed.id!==fill||reviewed.kind!==item.kind||reviewed.digest!==item.digest)throw Error('操作内容已变化，请刷新后重新核对。');
   if(item.kind==='fill'){
    if(typeof reviewed.text!=='string')throw Error('无法完整读取填写内容，未执行。');
    detail.replaceChildren(node('p','网站：'+String(reviewed.origin||'')+'；控件：'+String(reviewed.label||'未命名')),
                           node('p','准备填写以下内容：'),node('pre',reviewed.text));
   }else if(item.kind==='capture_post'){
    if(typeof reviewed.origin!=='string'||typeof reviewed.label!=='string'||!reviewed.origin||!reviewed.label)throw Error('无法核对点击目标，未执行。');
    detail.replaceChildren(node('p','网站：'+reviewed.origin+'；控件：'+reviewed.label),
     node('p','确认后将点击控件以捕获待发送请求；实际请求仍须再次核对并确认。页面脚本可能产生副作用，请先确认目标。'));
   }else{
    if(reviewed.method!=='POST'||typeof reviewed.url!=='string'||typeof reviewed.content_type!=='string'||typeof reviewed.body!=='string'||reviewed.body.length>1000000)throw Error('无法完整读取请求内容，请接管页面核对。');
    let url;try{url=new URL(reviewed.url)}catch{throw Error('请求目标无效，未发送。')}
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('请求目标无效，未发送。');
    detail.replaceChildren(node('p','请求目标：'+reviewed.method+' '+reviewed.url),node('p','内容类型：'+reviewed.content_type),
                           node('p','请求使用当前会话凭据；Cookie 和授权头不显示。确认后会将以下内容发送至目标系统。'));
    if(Array.isArray(reviewed.parsed_fields)){
     const fields=node('div','');fields.append(node('p','解析后的字段：'));
     for(const field of reviewed.parsed_fields){if(!field||typeof field.name!=='string'||typeof field.value!=='string')throw Error('请求字段不完整，未发送。');fields.append(node('p',field.name+'：'+field.value))}
     detail.append(fields);
    }
    detail.append(node('p','完整原文如下，请核对金额、期间和接收方：'),node('pre',reviewed.body));
    if(Number.isInteger(reviewed.blocked_requests)&&reviewed.blocked_requests>0)detail.append(node('p','另有 '+reviewed.blocked_requests+' 个页面请求已阻断，业务可能不完整；发送成功也不代表业务办理成功。'));
   }
   const approve=node('button',item.kind==='fill'?'确认填写':item.kind==='capture_post'?'确认捕获请求':'确认发送请求'),reject=node('button','拒绝');approve.type=reject.type='button';detail.append(approve,reject);
   async function decide(accepted){
    if(!valid()||chatInFlight.has(key)||chatUncertain.has(key))return;
    if(accepted&&!window.confirm(item.kind==='fill'?'确认将上面显示的内容填入所示网站和控件？':item.kind==='capture_post'?'确认点击所示控件以捕获请求？实际请求仍须再次确认。':'确认向所示目标发送上述完整内容？请先核对业务信息。'))return;
    chatInFlight.add(key);approve.disabled=reject.disabled=true;
    try{
     const result=await b.api(path+'/'+encodeURIComponent(fill)+'/decision',{method:'POST',body:{digest:item.digest,approve:accepted}});
     if(!valid())return;
     if(accepted&&item.kind==='capture_post'&&result.state==='pending'){
      detail.replaceChildren();notice.textContent='请求已捕获，尚未发送。请查看实际请求内容并再次确认。';
      const next=node('button','查看实际请求');next.type='button';next.dataset.browserChatReview='';card.insertBefore(next,detail);return;
     }
     if(result.state!==(accepted?'applied':'rejected'))throw Error('没有确认操作的最终状态。');
     detail.replaceChildren();button.remove();notice.textContent=(result.notice||(accepted?(item.kind==='fill'?'已填写。':'请求已发送，请核对业务系统的实际回执。'):'已拒绝该操作。'))+(item.kind==='post'?' 请接管浏览器核对后交还，才能继续访问页面。':' 可点击下方“继续这项工作”。');
     if(item.kind==='post'){const takeover=node('button','打开浏览器接管核对');takeover.type='button';takeover.onclick=()=>openBrowserForSession(session);detail.append(takeover)}
    }catch(error){
     // A lost response cannot distinguish a completed page action from an
     // unexecuted one. Never issue another decision automatically.
     chatUncertain.add(key);if(valid()){detail.replaceChildren();notice.textContent=error.message+' 请先接管浏览器核对实际页面，不要重复操作。'}
    }finally{chatInFlight.delete(key)}
   }
   approve.onclick=()=>decide(true);reject.onclick=()=>decide(false);button.remove();notice.textContent='';
  }catch(error){if(valid()){button.disabled=false;notice.textContent=error.message}}
  finally{chatInFlight.delete(key)}
 }
 document.querySelector('#messages')?.addEventListener('click',event=>{
  const button=event.target.closest('[data-browser-chat-review]');if(button)reviewInConversation(button);
 });
 function paint(result){
  snapshot=result;
  $('[data-browser-url]').textContent=result.url||'';
  $('[data-browser-image]').src='data:image/png;base64,'+result.image_base64;
  $('[data-browser-input]').value='';
  $('[data-browser-controls]').replaceChildren();
  pendingSignature='';
  const select=$('[data-browser-select]');select.replaceChildren();
  for(const control of result.controls||[]){
   if(!control.visible||control.disabled)continue;
   const option=node('option',control.ref+' · '+(control.sensitive?'密码或验证码':control.label||control.tag));option.value=control.ref;
   option.dataset.sensitive=control.sensitive?'true':'false';select.append(option);
  }
  select.onchange=()=>{const input=$('[data-browser-input]');input.value='';input.type=select.selectedOptions[0]?.dataset.sensitive==='true'?'password':'text'};select.onchange();
  const human=result.mode==='human';
  for(const button of browserDialog.querySelectorAll('[data-browser-action]'))button.disabled=!human;
  $('[data-browser-takeover]').disabled=human;$('[data-browser-return]').disabled=!human;
  $('[data-browser-grant]').disabled=!result.url||!!result.unknown_pending;
  $('[data-browser-resolve]').hidden=!result.unknown_pending;$('[data-browser-resolve]').disabled=!human;
  $('[data-browser-notice]').textContent=human?'你正在接管。点击页图或选择控件操作，输入会发送给网页；模型暂停读取。完成登录或表单后点击“交还 Agent”。':'Agent 可读取此页。登录、验证码及提交请先接管；页面是独立云浏览器，不是你的本机浏览器。';
 }
 async function pendingActions(mark,project,session){
  const result=await b.api(base()+'/pending-actions');
  if(!current(mark,project)||sid!==session||!browserDialog.open)return;
  const signature=JSON.stringify([session,(Array.isArray(result.items)?result.items:[]).map(x=>[x?.id,x?.kind,x?.digest])]);
  if(signature===pendingSignature)return;
  pendingSignature=signature;
  const container=$('[data-browser-controls]');container.replaceChildren();
  for(const item of result.items||[]){
   if(!/^bfill_[a-f0-9]{48}$/.test(item.id||'')||!/^[a-f0-9]{64}$/.test(item.digest||'')||!['fill','capture_post','post'].includes(item.kind))continue;
   const card=node('section',''),title=node('strong',item.kind==='fill'?'待确认页面填写':item.kind==='capture_post'?'待确认捕获请求':'待确认发送请求');
   card.append(title,node('p','网站：'+(item.origin||'')+'；控件：'+(item.label||'未命名')+'（'+item.ref+'）'));
   const detail=node('div',''),open=node('button','查看具体内容');open.type='button';card.append(detail,open);
   open.onclick=async()=>{
    open.disabled=true;
    try{
     const reviewed=await b.api(base()+'/pending-actions/'+encodeURIComponent(item.id)+'/reveal',{method:'POST',body:{}});
     if(!current(mark,project)||sid!==session||!browserDialog.open)return;
     if(reviewed.id!==item.id||reviewed.kind!==item.kind||reviewed.digest!==item.digest)throw Error('操作内容已变化，请刷新重试');
     detail.replaceChildren();
     if(item.kind==='fill'){
      if(typeof reviewed.text!=='string')throw Error('无法完整读取填写内容，未执行。');
      detail.append(node('p','准备填写以下内容：'),node('pre',reviewed.text));
     }else if(item.kind==='capture_post'){
      if(typeof reviewed.origin!=='string'||typeof reviewed.label!=='string'||!reviewed.origin||!reviewed.label)throw Error('无法核对点击目标，未执行。');
      detail.append(node('p','确认后将点击所示控件以捕获请求；实际请求仍需再次核对。页面脚本可能产生副作用。'));
     }else{
      if(reviewed.method!=='POST'||typeof reviewed.url!=='string'||typeof reviewed.content_type!=='string'||typeof reviewed.body!=='string'||reviewed.body.length>1000000)throw Error('无法完整读取请求内容，请接管页面核对。');
      let url;try{url=new URL(reviewed.url)}catch{throw Error('请求目标无效，未发送。')}
      if(!['http:','https:'].includes(url.protocol)||url.username||url.password)throw Error('请求目标无效，未发送。');
      detail.append(node('p','请求目标：'+reviewed.method+' '+reviewed.url),node('p','内容类型：'+reviewed.content_type),node('p','请求使用当前会话凭据；Cookie 和授权头不显示。确认后会将以下内容发送至目标系统。'));
      if(Array.isArray(reviewed.parsed_fields)){
       const fields=node('div','');fields.append(node('p','解析后的字段：'));
       for(const field of reviewed.parsed_fields){if(!field||typeof field.name!=='string'||typeof field.value!=='string')throw Error('请求字段不完整，未发送。');fields.append(node('p',field.name+'：'+field.value))}
       detail.append(fields);
      }
      detail.append(node('p','完整原文如下，请核对金额、期间和接收方：'),node('pre',reviewed.body));
      if(Number.isInteger(reviewed.blocked_requests)&&reviewed.blocked_requests>0)detail.append(node('p','另有 '+reviewed.blocked_requests+' 个页面请求已阻断，业务可能不完整；发送成功也不代表业务办理成功。'));
     }
     const approve=node('button','确认执行'),reject=node('button','拒绝');approve.type=reject.type='button';detail.append(approve,reject);
     const decide=async accepted=>{
      if(busy)return;
      if(accepted&&!window.confirm(item.kind==='fill'?'确认将上面显示的内容填入所示页面控件？':item.kind==='capture_post'?'确认点击控件以捕获请求？实际请求仍需再次确认。':'确认向所示目标发送上述完整内容？请先核对业务信息。'))return;
      busy=true;approve.disabled=reject.disabled=true;
      try{
       const r=await b.api(base()+'/pending-actions/'+encodeURIComponent(item.id)+'/decision',{method:'POST',body:{digest:item.digest,approve:accepted}});
       if(current(mark,project)&&sid===session){
        if(accepted&&item.kind==='capture_post'&&r.state==='pending'){
         detail.replaceChildren();open.remove();$('[data-browser-notice]').textContent='请求已捕获，尚未发送。请查看实际请求内容并再次确认。';
         await pendingActions(mark,project,session);
        }else{
         if(r.state!==(accepted?'applied':'rejected'))throw Error('没有确认操作的最终状态。');
         detail.replaceChildren();open.remove();$('[data-browser-notice]').textContent=(r.notice||(accepted?'操作已执行，请核对实际结果。':'已拒绝该操作。'))+(item.kind==='post'?' 请接管浏览器核对后交还，才能继续访问页面。':'');
        }
       }
      }catch(error){if(current(mark,project)&&sid===session){detail.replaceChildren();$('[data-browser-notice]').textContent=error.message+' 请刷新并核对实际页面，不要盲目重试。'}}
      finally{if(current(mark,project))busy=false}
     };
     approve.onclick=()=>decide(true);reject.onclick=()=>decide(false);
    }catch(error){if(current(mark,project)&&sid===session){open.disabled=false;$('[data-browser-notice]').textContent=error.message}}
   };
   container.append(card);
  }
 }
 function scheduleBrowserPoll(){
  clearTimeout(browserPollTimer);
  if(!browserDialog.open||!pid)return;
  browserPollTimer=setTimeout(async()=>{
   const mark=generation,project=pid,session=sid;
   try{if(session&&!busy&&!document.hidden)await pendingActions(mark,project,session)}
   catch(error){if(current(mark,project)&&sid===session)$('[data-browser-notice]').textContent='待确认事项暂时无法刷新，请手动重试。'}
   finally{if(current(mark,project)&&browserDialog.open)scheduleBrowserPoll()}
  },3500);
 }
 async function observe(){
  if(busy||!sid)return;busy=true;const mark=generation,project=pid,session=sid;
  try{const result=await b.api(base()+'/observe',{method:'POST',body:{}});if(current(mark,project)&&sid===session&&browserDialog.open){paint(result);await pendingActions(mark,project,session)}}
  catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message}
  finally{if(current(mark,project))busy=false}
 }
 async function loadBrowsers(preferredSession=''){
  const mark=generation,project=pid;
  const result=await b.api('/projects/'+encodeURIComponent(pid)+'/browsers');
  if(!current(mark,project))return;
  $('[data-browser-notice]').textContent=result.configured?'在对话中说明要浏览的网站，Agent 打开后会显示在这里。':'管理员尚未开启云浏览器。';
  for(const session of result.sessions||[]){
   const row=node('button',new Date(session.created_at).toLocaleString('zh-CN')+' · '+(session.state==='closed'?'已关闭':session.mode==='human'?'需要接管':'Agent 使用中'));
   row.disabled=session.state==='closed';row.onclick=()=>{sid=session.id;observe()};$('[data-browser-list]').append(row);
   if(session.reason)$('[data-browser-list]').append(node('p',session.reason));
  }
  const active=preferredSession?(result.sessions||[]).find(s=>s.id===preferredSession&&s.state!=='closed'):(result.sessions||[]).find(s=>s.state!=='closed');
  if(active){sid=active.id;await observe()}else if(preferredSession)$('[data-browser-notice]').textContent='该浏览器会话已失效，请查看工作记录。';
 }
 async function changeMode(mode){
  if(!sid||busy)return;busy=true;const mark=generation,project=pid;
  try{
   await b.api(base()+'/mode',{method:'POST',body:{mode}});
   if(!current(mark,project))return;
   if(mode==='close'){snapshot=null;$('[data-browser-image]').removeAttribute('src');$('[data-browser-notice]').textContent='会话已关闭，登录状态已清除。'}
   else{busy=false;await observe();if(mode==='agent')$('[data-browser-notice]').textContent+=' 回到对话发送“继续”，Agent 才会继续任务。'}
  }catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message}
  finally{if(current(mark,project))busy=false}
 }
 async function action(action,extra={}){
  if(busy||snapshot?.mode!=='human')return;
  busy=true;const mark=generation,project=pid;
  const payload={request_id:id(),revision:snapshot.revision,action,...extra};
  try{const result=await b.api(base()+'/action',{method:'POST',body:payload});if(current(mark,project))paint(result)}
  catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message+' 请刷新查看页面；不要重复提交。'}
  finally{$('[data-browser-input]').value='';if(current(mark,project))busy=false}
 }
 $('[data-browser-grant]').onclick=async()=>{
  if(!sid||!snapshot||busy)return;
  if(!window.confirm('允许 Agent 在当前网站点击和选择页面控件，授权最多 15 分钟。网页控件可能触发不可逆操作，系统无法仅凭按钮名称保证只读。请只为可信网站授权，密码、验证码和正式提交请自行接管。确认继续？'))return;
  busy=true;const mark=generation,project=pid;
  try{const result=await b.api(base()+'/agent-grant',{method:'POST',body:{revision:snapshot.revision,enabled:true}});if(current(mark,project))$('[data-browser-notice]').textContent='已授权 Agent 操作 '+result.origin+'。返回对话可继续任务。'}
  catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message}
  finally{if(current(mark,project))busy=false}
 };
 $('[data-browser-revoke]').onclick=async()=>{
  if(!sid||!snapshot||busy)return;busy=true;const mark=generation,project=pid;
  try{await b.api(base()+'/agent-grant',{method:'POST',body:{revision:snapshot.revision,enabled:false}});if(current(mark,project))$('[data-browser-notice]').textContent='已撤销 Agent 网站操作授权。'}
  catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message}
  finally{if(current(mark,project))busy=false}
 };
 $('[data-browser-resolve]').onclick=async()=>{
  if(!sid||!snapshot||snapshot.mode!=='human'||busy)return;
  if(!window.confirm('请先在业务系统中核对上次操作是否实际发生。确认已经核对并继续？'))return;
  busy=true;const mark=generation,project=pid;
  try{await b.api(base()+'/resolve-unknown',{method:'POST',body:{reviewed:true}});if(current(mark,project)){busy=false;await observe()}}
  catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message}
  finally{if(current(mark,project))busy=false}
 };
 function openBrowserForSession(preferredSession=''){
  if(!b.getState().projectId)return b.toast('请先打开项目');
  if(preferredSession&&!/^[a-f0-9]{64}$/.test(preferredSession))return b.toast('浏览器会话无效');
  reset();pid=b.getState().projectId;browserDialog.showModal();scheduleBrowserPoll();loadBrowsers(preferredSession).catch(error=>b.toast(error.message));
 }
 document.querySelector('#cloud-browser').onclick=()=>openBrowserForSession();
 $('[data-browser-refresh]').onclick=observe;
 $('[data-browser-takeover]').onclick=()=>changeMode('human');$('[data-browser-return]').onclick=()=>changeMode('agent');
 $('[data-browser-destroy]').onclick=()=>changeMode('close');
 $('[data-browser-click]').onclick=()=>action('click',{ref:Number($('[data-browser-select]').value)});
 $('[data-browser-fill]').onclick=()=>action('fill',{ref:Number($('[data-browser-select]').value),text:$('[data-browser-input]').value});
 $('[data-browser-type]').onclick=()=>action('type',{text:$('[data-browser-input]').value});
 $('[data-browser-enter]').onclick=()=>action('press',{key:'Enter'});
 $('[data-browser-tab]').onclick=()=>action('press',{key:'Tab'});
 $('[data-browser-up]').onclick=()=>action('scroll',{delta:-600});$('[data-browser-down]').onclick=()=>action('scroll',{delta:600});
 $('[data-browser-image]').onclick=event=>{
  if(snapshot?.mode!=='human')return;
  const box=event.currentTarget.getBoundingClientRect();
  action('pointer',{x:Math.min(1279,Math.max(0,Math.floor((event.clientX-box.left)*1280/box.width))),y:Math.min(799,Math.max(0,Math.floor((event.clientY-box.top)*800/box.height)))});
 };
 async function microphone(mark,project,epoch){
  if(!navigator.mediaDevices?.getUserMedia)throw Error('当前浏览器不支持录音；请使用HTTPS并允许麦克风');
  const value=await navigator.mediaDevices.getUserMedia({audio:true});
  if(!current(mark,project)||!voiceDialog.open||voiceEpoch!==epoch){value.getTracks().forEach(t=>t.stop());return null}
  stream=value;return value;
 }
 async function record(){
  stopVoice();const mark=generation,project=pid,epoch=voiceEpoch;
  try{
   const source=await microphone(mark,project,epoch);if(!source)return;
   const mime=['audio/webm','audio/mp4'].find(x=>MediaRecorder.isTypeSupported(x));if(!mime)throw Error('浏览器没有支持的录音格式');
   const chunks=[];let bytes=0,tooLarge=false;
   recorder=new MediaRecorder(source,{mimeType:mime});
   const activeRecorder=recorder;
   recorder.ondataavailable=e=>{bytes+=e.data.size;if(bytes>12*1024*1024){tooLarge=true;if(activeRecorder.state!=='inactive')activeRecorder.stop()}else chunks.push(e.data)};
   recorder.onstop=async()=>{
    if(voiceEpoch!==epoch)return;
    stream?.getTracks().forEach(t=>t.stop());stream=null;clearTimeout(voiceTimer);v('[data-voice-stop]').disabled=true;
    if(!current(mark,project)||!voiceDialog.open)return;
    if(tooLarge){v('[data-voice-notice]').textContent='录音超过12MiB，请缩短后重新录制。';return}
    v('[data-voice-notice]').textContent='正在转写；完成后请核对文字再发送工作。';
    const form=new FormData();form.append('audio',new Blob(chunks,{type:mime}),'recording.'+(mime.includes('mp4')?'mp4':'webm'));form.append('request_id',id());
    try{
     const response=await b.authFetch('/api/projects/'+encodeURIComponent(project)+'/voice/transcribe',{method:'POST',body:form});
     if(!response.ok)throw Error('转写没有确认完成，请查看服务配置；没有自动重发录音。');
     const result=await response.json();if(current(mark,project)&&voiceDialog.open&&voiceEpoch===epoch){v('[data-voice-text]').value=result.text;v('[data-voice-notice]').textContent='转写完成。请核对文字，加入输入框后手动发送。'}
    }catch(error){if(current(mark,project)&&voiceEpoch===epoch)v('[data-voice-notice]').textContent=error.message}
   };
   recorder.start(1000);v('[data-voice-notice]').textContent='正在录音，最长60秒。录音会发送给管理员配置的转写服务。';v('[data-voice-stop]').disabled=false;
   voiceTimer=setTimeout(()=>{if(recorder?.state==='recording')recorder.stop()},60000);
  }catch(error){if(current(mark,project)&&voiceEpoch===epoch){stopVoice();v('[data-voice-notice]').textContent=error.message}}
 }
 async function realtime(){
  stopVoice();const mark=generation,project=pid,epoch=voiceEpoch;
  try{
   const source=await microphone(mark,project,epoch);if(!source)return;
   peer=new RTCPeerConnection();const active=peer;
   for(const track of source.getTracks())active.addTrack(track,source);
   active.ontrack=e=>{if(current(mark,project)){v('audio').srcObject=e.streams[0];v('audio').play().catch(()=>{})}};
   channel=active.createDataChannel('nexus-voice');
   channel.onmessage=e=>{
    if(!current(mark,project))return;
    try{
     const event=JSON.parse(e.data);
     if(event.type==='conversation.item.input_audio_transcription.completed')v('[data-voice-text]').value=(v('[data-voice-text]').value+'\n'+event.transcript).trim().slice(-12000);
     if(event.type==='error')v('[data-voice-notice]').textContent='语音服务返回错误，请结束后检查服务。';
    }catch{}
   };
   const offer=await active.createOffer();await active.setLocalDescription(offer);
   const result=await b.api('/projects/'+encodeURIComponent(project)+'/voice/connect',{method:'POST',body:{request_id:id(),sdp:offer.sdp}});
   if(!current(mark,project)||peer!==active){active.close();if(result.call_id)hangup({project,id:result.call_id});return}
   voiceCall={project,id:result.call_id};
   await active.setRemoteDescription({type:'answer',sdp:result.sdp});
   v('[data-voice-notice]').textContent='实时语音已连接，回答由AI生成。麦克风直接连接语音服务；执行项目工作请核对转写文字后发送。5分钟后本页自动断开。';
   v('[data-voice-stop]').disabled=false;voiceTimer=setTimeout(stopVoice,Math.min(result.max_seconds||300,300)*1000);
  }catch(error){if(current(mark,project)&&voiceEpoch===epoch){stopVoice();v('[data-voice-notice]').textContent=error.message}}
 }
 document.querySelector('#voice-input').onclick=async()=>{
  if(!b.getState().projectId)return b.toast('请先打开项目');
  reset();pid=b.getState().projectId;voiceDialog.showModal();const mark=generation,project=pid;
  try{const caps=await b.api('/projects/'+encodeURIComponent(pid)+'/media');if(!current(mark,project))return;
   v('[data-voice-record]').disabled=!caps.transcription;v('[data-voice-realtime]').disabled=!caps.realtime;
   v('[data-voice-notice]').textContent=caps.notice||(caps.transcription||caps.realtime?'选择录音转文字或实时语音。只有点击开始后才使用麦克风。':'管理员尚未配置语音服务。');
  }catch(error){if(current(mark,project))v('[data-voice-notice]').textContent=error.message}
 };
 v('[data-voice-record]').onclick=record;v('[data-voice-realtime]').onclick=realtime;
 v('[data-voice-stop]').onclick=()=>{if(recorder?.state==='recording')recorder.stop();else{stopVoice();v('[data-voice-notice]').textContent='实时语音已结束。'}};
 v('[data-voice-use]').onclick=()=>{const text=v('[data-voice-text]').value;if(text&&b.setDraft(text))reset()};
 for(const dialog of [browserDialog,voiceDialog]){
  dialog.querySelector('[data-interactive-close]').onclick=reset;
  dialog.addEventListener('cancel',event=>{event.preventDefault();reset()});
 }
 document.addEventListener('visibilitychange',()=>{if(document.hidden)stopVoice()});
 window.addEventListener('pagehide',stopVoice);
 window.nexusInteractive={reset,projectChanged:reset};
})();
