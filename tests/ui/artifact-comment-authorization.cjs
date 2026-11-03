'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
const delay=()=>new Promise(r=>setTimeout(r,15));
const sha='b'.repeat(64),viewer='c'.repeat(64),id='as_'+('a'.repeat(40));
const ok=data=>({ok:true,json:async()=>data});
const page=()=>ok({name:'机密报告',sha256:sha,viewer_hash:viewer,render:'text',text:'重要内容',offset:0,next_offset:null,allow_download:false,allow_comments:true,notice:'review'});
const comments=(v=viewer)=>ok({items:[],total:0,offset:0,has_more:false,content_sha256:sha,viewer_hash:v});
function setup(fetch){const html=fs.readFileSync('agent_service/static/shared-artifact.html','utf8');const w=new JSDOM(html,{url:'https://fixture.test/shared-artifact.html?id='+id,runScripts:'outside-only',pretendToBeVisual:true}).window;w.fetch=fetch;w.nexusSecureUUID=()=> 'comment_request_12345';w.eval(fs.readFileSync('agent_service/static/shared-artifact-comments.js','utf8'));w.eval(fs.readFileSync('agent_service/static/shared-artifact.js','utf8'));return w}
(async()=>{
 let mode='ok';const w=setup(async(url,opts={})=>{if(!String(url).includes('/comments'))return page();if(opts.method==='POST')return {ok:false,status:403};return mode==='denied'?{ok:false,status:403}:mode==='identity'?comments('f'.repeat(64)):comments()});await delay();
 let panel=w.document.querySelector('#artifact-comments'),content=w.document.querySelector('#content');assert.equal(content.hidden,false);
 const form=panel.querySelector('form');panel.querySelector('textarea').value='意见';form.dispatchEvent(new w.Event('submit',{cancelable:true}));await delay();assert.equal(content.hidden,false,'a 403 on POST alone can be CSRF, read must revalidate');
 mode='denied';[...panel.querySelectorAll('button')].find(x=>x.textContent==='刷新评论').click();await delay();assert.equal(content.hidden,true);assert.equal(w.document.querySelector('#preview').textContent,'');assert.equal(w.document.querySelector('#title').textContent,'查看分享成果');
 let count=0,resolveOld;const x=setup(async url=>{if(!String(url).includes('/comments'))return page();count++;if(count===1)return await new Promise(resolve=>resolveOld=resolve);return comments()});await delay();x.dispatchEvent(new x.Event('blur'));x.dispatchEvent(new x.Event('focus'));await delay();assert.equal(x.document.querySelector('#content').hidden,false);resolveOld({ok:false,status:403});await delay();assert.equal(x.document.querySelector('#content').hidden,false,'late old denial must not blank fresh session');
 const y=setup(async()=>ok({name:'机密报告',sha256:sha,render:'text',text:'不可显示',offset:0,next_offset:null,allow_download:false,allow_comments:false,notice:'review'}));await delay();assert.equal(y.document.querySelector('#content').hidden,true,'missing viewer identity must not display a report');
 mode='identity';w.dispatchEvent(new w.Event('focus'));await delay();assert.equal(content.hidden,true,'a different viewer must close all old content');
 console.log('comment permission failures close the entire pinned report, distinguish CSRF, and ignore stale denials');
})().catch(error=>{console.error(error);process.exit(1)});
