/* Production module with DOM, microphone and HTTP fixtures; no live model. */
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');
const wait=async predicate=>{for(let i=0;i<100;i++){if(predicate())return;await new Promise(r=>setTimeout(r,5))}throw Error('Interactive UI did not settle')};
(async()=>{
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{url:'https://atlas.test',runScripts:'outside-only'}),w=dom.window,d=w.document;
 try{
  w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
  w.HTMLMediaElement.prototype.pause=function(){};
  let projectId='p',mode='agent',calls=[],draft='',micResolver=null,stopped=0,recording=null,uploadResolver=null,realtimeEnabled=false,rejectHangup=false,rtcClosed=0;
  const snapshot=()=>({url:'https://public.test/login',mode,revision:'a'.repeat(64),image_base64:'fixture-png',
   controls:[{ref:1,tag:'input',sensitive:true,label:'<img src=x onerror=bad()>',visible:true}]});
  Object.defineProperty(w.navigator,'mediaDevices',{value:{getUserMedia:()=>new Promise(resolve=>micResolver=resolve)}});
  w.MediaRecorder=class{
   static isTypeSupported(){return true}
   constructor(){this.state='inactive';recording=this}
   start(){this.state='recording'}
   stop(){this.state='inactive';this.ondataavailable?.({data:new w.Blob(['audio'])});this.onstop?.()}
  };
  w.RTCPeerConnection=class{
   addTrack(){} createDataChannel(){return {close(){}}}
   async createOffer(){return {sdp:'v=0\r\ncontrolled-offer'}}
   async setLocalDescription(){} async setRemoteDescription(){}
   close(){rtcClosed++}
  };
  w.atlasBusiness={getState:()=>({projectId}),toast:()=>{},setDraft:text=>{draft=text;return true},
   authFetch:route=>{if(route.endsWith('/end')){if(rejectHangup)throw Error('authentication sealed');return Promise.resolve({ok:true})}return new Promise(resolve=>uploadResolver=resolve)},
   api:async(route,options={})=>{
    calls.push({route,options});
    if(route.endsWith('/browsers'))return {configured:true,sessions:[{id:'b'.repeat(64),created_at:'2026-10-29T01:00:00Z',state:'open',mode,reason:'<img src=x onerror=bad()>'}]};
    if(route.endsWith('/observe'))return snapshot();
    if(route.endsWith('/mode')){mode=options.body.mode;return {mode}}
    if(route.endsWith('/action'))return snapshot();
    if(route.endsWith('/media'))return {transcription:true,realtime:realtimeEnabled};
    if(route.endsWith('/voice/connect'))return {sdp:'v=0\r\ncontrolled-answer',call_id:'vc_fixture',max_seconds:300};
    throw Error('Unexpected request '+route);
   }};
  w.eval(fs.readFileSync(path.join(root,'interactive-work.js'),'utf8'));
  d.querySelector('#cloud-browser').click();await wait(()=>d.querySelector('[data-browser-image]').hasAttribute('src'));
  assert.equal(d.querySelector('[data-browser-list] img'),null);
  assert(d.querySelector('[data-browser-click]').disabled);
  d.querySelector('[data-browser-takeover]').click();await wait(()=>!d.querySelector('[data-browser-fill]').disabled);
  const input=d.querySelector('[data-browser-input]');assert.equal(input.type,'password');input.value='transient-secret';
  d.querySelector('[data-browser-fill]').click();await wait(()=>input.value==='');
  assert.equal(calls.find(x=>x.route.endsWith('/action')).options.body.text,'transient-secret');
  d.querySelector('[data-browser-return]').click();await wait(()=>d.querySelector('[data-browser-fill]').disabled);
  assert(d.querySelector('[data-browser-notice]').textContent.includes('发送“继续”'));
  d.querySelector('#voice-input').click();await wait(()=>!d.querySelector('[data-voice-record]').disabled);
  d.querySelector('[data-voice-record]').click();await wait(()=>micResolver);
  projectId='other';w.atlasInteractive.projectChanged();
  micResolver({getTracks:()=>[{stop:()=>stopped++}]});await wait(()=>stopped===1);
  assert.equal(d.querySelector('#voice-dialog').open,false,'late microphone permission cannot reopen another project');
  d.querySelector('#voice-input').click();await wait(()=>!d.querySelector('[data-voice-record]').disabled);
  micResolver=null;d.querySelector('[data-voice-record]').click();await wait(()=>micResolver);
  micResolver({getTracks:()=>[{stop:()=>stopped++}]});await wait(()=>recording?.state==='recording');
  d.querySelector('[data-voice-stop]').click();await wait(()=>uploadResolver);
  w.atlasInteractive.reset();uploadResolver({ok:true,json:async()=>({text:'stale previous project transcript'})});
  await new Promise(r=>setTimeout(r,10));assert.equal(d.querySelector('[data-voice-text]').value,'');assert.equal(draft,'');
  assert(stopped>=2);
  realtimeEnabled=true;micResolver=null;d.querySelector('#voice-input').click();await wait(()=>!d.querySelector('[data-voice-realtime]').disabled);
  d.querySelector('[data-voice-realtime]').click();await wait(()=>micResolver);
  micResolver({getTracks:()=>[{stop:()=>stopped++}]});await wait(()=>!d.querySelector('[data-voice-stop]').disabled);
  rejectHangup=true;const stoppedBefore=stopped;
  assert.doesNotThrow(()=>w.atlasInteractive.reset());assert(stopped>stoppedBefore);assert(rtcClosed>0);
  assert.equal(d.querySelector('#voice-dialog').open,false,'synchronous auth rejection cannot leave microphone active');
  console.log(JSON.stringify({human_takeover:true,controls_gated:true,password_masked_and_cleared:true,no_untrusted_html:true,
   microphone_stopped_on_project_change:true,late_permission_stream_disposed:true,stale_transcript_discarded:true,logout_auth_rejection_cannot_prevent_microphone_shutdown:true,no_auto_send:true,real_microphone_or_model_tested:false}));
 }finally{w.close()}
})().catch(error=>{console.error(error);process.exitCode=1});
