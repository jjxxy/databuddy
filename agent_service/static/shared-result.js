'use strict';
/* The reverse proxy must provide the caller's verified OIDC token to the API.
 * A share ID is not an access credential. No tokens are stored by this page. */
(()=>{
 const id=new URLSearchParams(location.search).get('id')||'',el=s=>document.querySelector(s);
 const status=el('#share-status'),content=el('#share-content'),head=el('#share-head'),body=el('#share-body');
 const previous=el('#share-previous'),next=el('#share-next'),download=el('#share-download');
 let offset=0,stamp=0,scope=null,busy=false;const limit=50;
 function clear(message){content.hidden=true;head.replaceChildren();body.replaceChildren();status.textContent=message}
 const cell=(tag,value)=>{const n=document.createElement(tag);n.textContent=value==null?'—':typeof value==='object'?JSON.stringify(value):String(value);return n};
 async function load(start){
  if(busy)return;busy=true;const ticket=++stamp;clear('正在核对访问权限和结果版本…');
  try{
   const response=await fetch('/api/shared-data/'+encodeURIComponent(id)+'?offset='+start+'&limit='+limit,{credentials:'same-origin',cache:'no-store'});
   if(!response.ok){if(response.status===401)throw Error('需要企业账号身份。请通过提供此链接的企业身份入口登录，或联系管理员检查身份代理。');if(response.status===403||response.status===404||response.status===410)throw Error('分享不可用或当前账号无权查看。请联系分享者核对权限、有效期和版本。');throw Error('暂时无法读取分享结果，请稍后重试。')}
   const data=await response.json();if(ticket!==stamp)return;
   if(!data||!Array.isArray(data.schema)||!Array.isArray(data.rows)||!Number.isSafeInteger(data.total)||data.total<0||!Number.isSafeInteger(data.offset)||data.offset!==start||typeof data.run_id!=='string'||!/^[a-f0-9]{64}$/.test(data.plan_sha256||'')||!/^[a-f0-9]{64}$/.test(data.manifest_sha256||'')||data.rows.length>limit||start+data.rows.length>data.total)throw Error('结果记录不完整，未展示数据。');
   if(scope&&(scope.run!==data.run_id||scope.plan!==data.plan_sha256||scope.manifest!==data.manifest_sha256||scope.total!==data.total))throw Error('结果版本已变化，未展示混合数据。请重新打开链接。');
   scope={run:data.run_id,plan:data.plan_sha256,manifest:data.manifest_sha256,total:data.total};offset=start;
   const row=document.createElement('tr');for(const col of data.schema){const meaning=col.semantic||col,details=[meaning.unit,meaning.period].filter(v=>typeof v==='string'&&v);const label=(col.label||col.name)+(details.length?'（'+details.join(' · ')+'）':'');row.append(cell('th',label))}head.append(row);
   for(const item of data.rows){const tr=document.createElement('tr');for(const col of data.schema)tr.append(cell('td',item[col.name]));body.append(tr)}
   el('#share-version').textContent='结果版本 '+data.run_id+' · 方案 '+data.plan_sha256.slice(0,12)+'；请结合来源和口径核对。';
   el('#share-page').textContent=data.total?'第 '+(start+1)+'–'+(start+data.rows.length)+' 条，共 '+data.total+' 条':'共 0 条';
   previous.disabled=start<=0;next.disabled=data.next_offset==null;next.dataset.offset=data.next_offset==null?'':String(data.next_offset);content.hidden=false;status.textContent='仅显示当前页；已展示内容不能因撤销而从接收者设备自动收回。';
  }catch(error){if(ticket===stamp)clear(error.message||'暂时无法读取分享结果。')}
  finally{if(ticket===stamp)busy=false}
 }
 previous.onclick=()=>load(Math.max(0,offset-limit));next.onclick=()=>{const value=Number(next.dataset.offset);if(Number.isSafeInteger(value)&&value>=0)load(value)};
 download.onclick=()=>{if(busy)return;const a=document.createElement('a');a.href='/api/shared-data/'+encodeURIComponent(id)+'/download';a.download='共享数据结果.csv';a.target='_blank';a.rel='noopener noreferrer';a.click();status.textContent='已发起下载请求；服务端会再次校验权限。CSV 的空值与空字符串无法区分，类公式文本会加单引号；高精度数字和长编号须按文本导入。'};
 if(location.protocol!=='https:'&&!['localhost','127.0.0.1','[::1]'].includes(location.hostname)){clear('请使用 HTTPS 安全地址查看分享。');return}
 if(!/^share_[a-f0-9]{24}$/.test(id)){clear('分享链接格式无效。');return}void load(0);
})();
