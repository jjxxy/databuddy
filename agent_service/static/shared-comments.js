'use strict';
/* Comments are version-bound discussion, never financial approval. */
window.nexusSharedComments=(()=>{
 const panel=()=>document.getElementById('share-comments');
 let current=null,epoch=0,pending=null,writing=false;
 function reset(){epoch++;current=null;pending=null;writing=false;const p=panel();if(p){p.replaceChildren();p.hidden=true}}
 const node=(tag,text)=>{const x=document.createElement(tag);if(text!=null)x.textContent=String(text);return x};
 const valid=s=>s===current&&!!panel();
 function activate(scope){
  if(!scope||!/^share_[a-f0-9]{24}$/.test(scope.shareId||'')||!/^[a-f0-9]{64}$/.test(scope.manifest||'')){reset();return}
  const key=scope.shareId+'|'+scope.manifest;if(current?.key===key)return;
  reset();const s={...scope,key};current=s;
  const p=panel();if(!p)return;p.hidden=false;
  const heading=node('h2','结果评论'),notice=node('p','获准访问此分享的账号均可看到评论。评论只用于交流，不构成财务审批或数据准确性确认。');
  const state=node('p','正在检查评论权限…');state.setAttribute('role','status');
  const list=node('div'),navigation=node('div'),prev=node('button','上一页评论'),next=node('button','下一页评论'),form=node('form'),label=node('label','填写评论'),input=node('textarea');prev.type=next.type='button';prev.disabled=next.disabled=true;navigation.append(prev,next);input.maxLength=2000;input.required=true;input.rows=3;label.append(input);
  const submit=node('button','发表评论');submit.type='submit';const retry=node('button','再次发送同一条评论');retry.type='button';retry.hidden=true;const refresh=node('button','刷新评论');refresh.type='button';
  const discard=node('button','已核对，不再重试这条评论');discard.type='button';discard.hidden=true;
  form.append(label,submit,retry,refresh,discard);form.hidden=true;p.append(heading,notice,state,list,navigation,form);
  let pageOffset=0,hasMore=false;
  function update(){submit.disabled=writing||!!pending;input.disabled=writing||!!pending;retry.hidden=!pending;retry.disabled=writing||!!pending?.blocked;discard.hidden=!pending;discard.disabled=writing;refresh.disabled=writing;prev.disabled=writing||pageOffset<=0;next.disabled=writing||!hasMore}
  async function read(offset=pageOffset){
   const ticket=++epoch;state.textContent='正在读取评论…';
   try{const response=await fetch('/api/shared-data/'+encodeURIComponent(s.shareId)+'/comments?offset='+offset+'&limit=50',{credentials:'same-origin',cache:'no-store'});
    if(!response.ok){if(response.status===403||response.status===401||response.status===404||response.status===410){if(valid(s)&&ticket===epoch){form.hidden=true;list.replaceChildren();state.textContent='评论未启用或当前账号已无权访问。';}return}throw Error('暂时无法读取评论，请稍后重试。')}
    const data=await response.json();if(!valid(s)||ticket!==epoch)return;
    if(data?.handoff_sha256!==s.manifest||!Array.isArray(data.items)||!Number.isSafeInteger(data.total)||data.total<0||data.items.length>50||data.offset!==offset||typeof data.has_more!=='boolean'||offset+data.items.length>data.total)throw Error('评论对应的结果版本不一致，未显示评论。');
    list.replaceChildren();if(!data.items.length)list.append(node('p','暂无评论。'));
    for(const item of data.items){if(typeof item?.text!=='string'||typeof item?.author!=='string'||item.handoff_sha256!==s.manifest)throw Error('评论记录不完整，未显示评论。');const wrap=node('article'),by=node('p',item.author+' · '+String(item.created_at||'')),text=node('p',item.text);text.style.whiteSpace='pre-wrap';wrap.append(by,text);list.append(wrap)}
    form.hidden=false;pageOffset=offset;hasMore=data.has_more;update();state.textContent=pending?'提交状态尚未确认，请核对列表或显式重试同一条评论。':'当前展示 '+(data.total?offset+1:0)+'–'+(offset+data.items.length)+' 条，共 '+data.total+' 条评论。';
   }catch(error){if(valid(s)&&ticket===epoch){list.replaceChildren();state.textContent=error.message||'暂时无法读取评论，请稍后重试。'}}
  }
  async function send(){
   if(writing||!pending||!valid(s))return;writing=true;++epoch;update();const attempt=pending;state.textContent='正在提交评论…';
   try{const response=await fetch('/api/shared-data/'+encodeURIComponent(s.shareId)+'/comments',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:attempt.text,request_id:attempt.id,expected_handoff_sha256:s.manifest})});
    if(!valid(s)||pending!==attempt)return;
    if(!response.ok){if([401,403,404,410].includes(response.status)){form.hidden=true;list.replaceChildren();pending=null;input.value='';state.textContent='评论未启用、访问权限已失效或服务的同源安全配置不匹配，请联系分享者或管理员核对。';return}if(response.status===409){attempt.blocked=true;state.textContent='评论版本或请求编号冲突，请核对版本和评论记录；不要重复提交。';return}if([400,422].includes(response.status)){pending=null;state.textContent='评论内容未通过校验，请调整后再提交。';return}throw Error('无法确认提交结果')}
    const value=await response.json();if(!valid(s)||pending!==attempt)return;
    if(!value||value.handoff_sha256!==s.manifest||value.text!==attempt.text||typeof value.id!=='string')throw Error('无法确认提交结果');
    pending=null;input.value='';await read();
   }catch(_){if(valid(s)&&pending===attempt)state.textContent='未确认评论是否提交成功。请核对记录，或手动重试同一条评论；不会自动发送。'}
   finally{if(valid(s)){writing=false;update()}}
  }
  form.onsubmit=event=>{event.preventDefault();if(writing||pending||!valid(s))return;const text=input.value.trim();if(!text||text.length>2000){state.textContent='请输入 1 至 2000 字的评论。';return}
   try{if(typeof window.nexusSecureUUID!=='function')throw Error();pending={id:window.nexusSecureUUID(),text}}catch(_){state.textContent='当前浏览器无法生成安全的请求编号，请使用 HTTPS 或更新浏览器。';return}update();void send()};
  retry.onclick=()=>{if(!writing&&pending)void send()};refresh.onclick=()=>{if(!writing)void read()};prev.onclick=()=>{if(!writing&&pageOffset>0)void read(Math.max(0,pageOffset-50))};next.onclick=()=>{if(!writing&&hasMore)void read(pageOffset+50)};discard.onclick=()=>{if(!writing&&pending){pending=null;input.value='';state.textContent='已停止重试；请以评论记录为准。';update()}};
  void read();
 }
 return {activate,reset,hasPending:()=>!!pending};
})();
