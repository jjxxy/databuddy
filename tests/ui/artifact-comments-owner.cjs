'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
const delay=()=>new Promise(r=>setTimeout(r,15));
(async()=>{
 const html=fs.readFileSync('agent_service/static/artifact-shares.html','utf8');
 const w=new JSDOM(html,{url:'https://fixture.test/artifact-shares.html?project=p_test',runScripts:'outside-only'}).window;
 const sha='d'.repeat(64),share={id:'as_'+('c'.repeat(40)),name:'报告',state:'revoked',expires_at:'2000-01-01T00:00:00Z',content_sha256:sha,allow_comments:true};
 const ok=data=>({ok:true,json:async()=>data});w.fetch=async(url)=>{
  url=String(url);if(url==='/health')return ok({status:'ok'});if(url.includes('/artifacts?'))return ok({items:[],next_offset:null});
  if(url.includes('/comments?'))return ok({items:[{author:'<b>用户</b>',text:'<img src=x onerror=alert(1)>',created_at:'today',content_sha256:sha}],offset:0,total:1,has_more:false,content_sha256:sha});
  if(url.endsWith('/artifact-shares'))return ok({items:[share]});throw Error('unexpected '+url)
 };
 w.eval(fs.readFileSync('agent_service/static/artifact-shares.js','utf8'));await delay();const list=w.document.querySelector('#shares');
 [...list.querySelectorAll('button')].find(x=>x.textContent==='查看评论').click();await delay();assert(list.textContent.includes('<img src=x'));assert.equal(list.querySelector('img'),null);assert(list.textContent.includes('不构成财务审批'));
 w.dispatchEvent(new w.Event('blur'));assert.equal(list.children.length,0);
 console.log('publisher views fixed-version retained comments safely');
})().catch(error=>{console.error(error);process.exit(1)});
