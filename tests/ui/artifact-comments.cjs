'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
const delay=()=>new Promise(r=>setTimeout(r,15));
(async()=>{
 const html=fs.readFileSync('agent_service/static/shared-artifact.html','utf8');
 const w=new JSDOM(html,{url:'https://fixture.test/shared-artifact.html?id=as_'+('a'.repeat(40)),runScripts:'outside-only'}).window;
 const sha='b'.repeat(64),viewer='c'.repeat(64);let posts=[],first=true,posted=false;
 w.nexusSecureUUID=()=> 'comment_request_12345';
 w.fetch=async(url,opts={})=>{
  if(String(url).includes('/comments')){
   if(opts.method==='POST'){let body=JSON.parse(opts.body);posts.push(body);if(first){first=false;throw Error('disconnect')}posted=true;return {ok:true,json:async()=>({id:'ac_test',content_sha256:sha,text:body.text})}}
   return {ok:true,json:async()=>({items:posted?[{id:'ac_test',author:'<b>A</b>',text:'<img src=x onerror=alert(1)>',content_sha256:sha}]:[],total:posted?1:0,offset:0,has_more:false,content_sha256:sha,viewer_hash:viewer})};
  }
  return {ok:true,json:async()=>({name:'report',sha256:sha,viewer_hash:viewer,render:'text',text:'报告',offset:0,next_offset:null,allow_download:false,allow_comments:true,notice:'review'})};
 };
 w.eval(fs.readFileSync('agent_service/static/shared-artifact-comments.js','utf8'));w.eval(fs.readFileSync('agent_service/static/shared-artifact.js','utf8'));await delay();
 const panel=w.document.querySelector('#artifact-comments');assert.equal(panel.hidden,false);const form=panel.querySelector('form');assert.equal(form.hidden,false);
 panel.querySelector('textarea').value='收入核对';form.dispatchEvent(new w.Event('submit',{cancelable:true}));await delay();assert.equal(posts.length,1);assert.equal(posts[0].expected_content_sha256,sha);assert.equal(posts[0].expected_viewer_hash,viewer);
 [...panel.querySelectorAll('button')].find(x=>x.textContent==='再次发送同一条评论').click();await delay();assert.equal(posts.length,2);assert.deepEqual(posts[0],posts[1]);assert(panel.textContent.includes('<img src=x'));assert.equal(panel.querySelector('img'),null);
 w.dispatchEvent(new w.Event('blur'));assert.equal(panel.hidden,true);assert.equal(panel.textContent,'');
 console.log('artifact comments bind file and viewer, retry the same request, escape text and clear on suspension');
})().catch(error=>{console.error(error);process.exit(1)});
