const {JSDOM}=require('jsdom');const fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const d=new JSDOM('<section id="organization-templates"><p id="organization-template-status"></p><div id="organization-template-list"></div></section>',{url:'https://fixture.test/project-templates.html?project=p1',runScripts:'outside-only'}),w=d.window;
 let requests=[],failure=false;const item={id:'ot_'+'b'.repeat(40),title:'<img src=x>',version:1,sha256:'a'.repeat(64),source_revision:2};w.nexusSecureUUID=()=> 'same_request_id_123456';
 w.fetch=async(path,opts={})=>{requests.push({path,opts});if(!opts.method||opts.method==='GET')return {ok:true,json:async()=>({items:[item]})};if(failure)throw Error('network');return {ok:true,json:async()=>({organization_template_id:item.id,template:{source_sha256:item.sha256}})}};
 w.eval(fs.readFileSync('agent_service/static/organization-templates.js','utf8'));await new Promise(r=>setTimeout(r,0));
 assert.equal(w.document.querySelectorAll('img').length,0);let button=w.document.querySelector('#organization-template-list button');button.click();await new Promise(r=>setTimeout(r,0));
 assert.equal(requests[1].path,'/api/projects/p1/organization-ppt-templates/'+item.id+'/copy');assert.equal(JSON.parse(requests[1].opts.body).request_id,'same_request_id_123456');assert(w.document.body.textContent.includes('已复制固定版本'));
 failure=true;button.click();await new Promise(r=>setTimeout(r,0));assert(w.document.body.textContent.includes('未确认复制'));
 failure=false;w.document.querySelector('#organization-templates > button').click();await new Promise(r=>setTimeout(r,0));assert.equal(JSON.parse(requests[2].opts.body).request_id,JSON.parse(requests[3].opts.body).request_id);assert(w.document.body.textContent.includes('已复制固定版本'));
 const disabled=new JSDOM('<section id="organization-templates"><p id="organization-template-status"></p><div id="organization-template-list"></div></section>',{url:'https://fixture.test/project-templates.html?project=p1',runScripts:'outside-only'}),x=disabled.window;
 x.fetch=async()=>({ok:false,status:404,json:async()=>({detail:'Not Found'})});x.eval(fs.readFileSync('agent_service/static/organization-templates.js','utf8'));await new Promise(r=>setTimeout(r,0));
 assert(x.document.body.textContent.includes('当前工作空间未开放实例模板库。'));assert.equal(x.document.querySelector('#organization-template-list button'),null);
 console.log('instance templates safely display and copy pinned versions with idempotent retry');
})().catch(e=>{console.error(e);process.exit(1)});
