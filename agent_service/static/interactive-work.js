/* Real browser handoff and microphone transport; no credentials in local storage. */
(()=>{
 'use strict';
 const b=window.atlasBusiness,browserDialog=document.querySelector('#cloud-browser-dialog'),voiceDialog=document.querySelector('#voice-dialog');
 if(!b||!browserDialog||!voiceDialog)return;
 const $=s=>browserDialog.querySelector(s),v=s=>voiceDialog.querySelector(s);
 let generation=0,voiceEpoch=0,pid='',sid='',snapshot=null,busy=false,recorder=null,stream=null,peer=null,channel=null,voiceTimer=null,voiceCall=null;
 function node(tag,text){const el=document.createElement(tag);el.textContent=text;return el}
 function id(){return crypto.randomUUID()}
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
  generation++;stopVoice();pid='';sid='';snapshot=null;busy=false;
  $('[data-browser-image]').removeAttribute('src');$('[data-browser-controls]').replaceChildren();$('[data-browser-list]').replaceChildren();
  $('[data-browser-input]').value='';$('[data-browser-url]').textContent='';$('[data-browser-notice]').textContent='';
  v('[data-voice-text]').value='';v('[data-voice-notice]').textContent='';
  v('[data-voice-record]').disabled=true;v('[data-voice-realtime]').disabled=true;
  if(browserDialog.open)browserDialog.close();if(voiceDialog.open)voiceDialog.close();
 }
 function current(mark,project){return mark===generation&&project===b.getState().projectId&&pid===project}
 function base(){return '/projects/'+encodeURIComponent(pid)+'/browsers/'+encodeURIComponent(sid)}
 function paint(result){
  snapshot=result;
  $('[data-browser-url]').textContent=result.url||'';
  $('[data-browser-image]').src='data:image/png;base64,'+result.image_base64;
  $('[data-browser-input]').value='';
  $('[data-browser-controls]').replaceChildren();
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
  $('[data-browser-notice]').textContent=human?'你正在接管。点击页图或选择控件操作，输入会发送给网页；模型暂停读取。完成登录或表单后点击“交还 Agent”。':'Agent 可读取此页。登录、验证码及提交请先接管；页面是独立云浏览器，不是你的本机浏览器。';
 }
 async function observe(){
  if(busy||!sid)return;busy=true;const mark=generation,project=pid;
  try{const result=await b.api(base()+'/observe',{method:'POST',body:{}});if(current(mark,project)&&browserDialog.open)paint(result)}
  catch(error){if(current(mark,project))$('[data-browser-notice]').textContent=error.message}
  finally{if(current(mark,project))busy=false}
 }
 async function loadBrowsers(){
  const mark=generation,project=pid;
  const result=await b.api('/projects/'+encodeURIComponent(pid)+'/browsers');
  if(!current(mark,project))return;
  $('[data-browser-notice]').textContent=result.configured?'在对话中说明要浏览的网站，Agent 打开后会显示在这里。':'管理员尚未开启云浏览器。';
  for(const session of result.sessions||[]){
   const row=node('button',new Date(session.created_at).toLocaleString('zh-CN')+' · '+(session.state==='closed'?'已关闭':session.mode==='human'?'需要接管':'Agent 使用中'));
   row.disabled=session.state==='closed';row.onclick=()=>{sid=session.id;observe()};$('[data-browser-list]').append(row);
   if(session.reason)$('[data-browser-list]').append(node('p',session.reason));
  }
  const active=(result.sessions||[]).find(s=>s.state!=='closed');if(active){sid=active.id;await observe()}
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
 document.querySelector('#cloud-browser').onclick=()=>{
  if(!b.getState().projectId)return b.toast('请先打开项目');
  reset();pid=b.getState().projectId;browserDialog.showModal();loadBrowsers().catch(error=>b.toast(error.message));
 };
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
   channel=active.createDataChannel('atlas-voice');
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
 window.atlasInteractive={reset,projectChanged:reset};
})();
