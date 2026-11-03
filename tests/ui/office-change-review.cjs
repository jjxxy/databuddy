'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const html=fs.readFileSync('agent_service/static/office-edit.html','utf8');
 const dom=new JSDOM(html,{url:'https://fixture.test/office-edit.html?project=p_1&artifact=wa_'+('a'.repeat(44)),runScripts:'outside-only'}),w=dom.window;
 w.sessionStorage.setItem('nexus_token','old');w.confirm=()=>true;w.fetch=async()=>({ok:true,json:async()=>({artifact_id:'wa_'+('a'.repeat(44)),artifact_sha256:'b'.repeat(64),spec_sha256:'c'.repeat(64),kind:'presentation',notice:'test',sections:[{index:0,heading:'<img src=x onerror=alert(1)>',body:'旧金额：1.00',bullets:['待核对'],contains_table:true,contains_chart:true},{index:1,heading:'复核',body:'旧文字',bullets:[]}]})});
 w.eval(fs.readFileSync('agent_service/static/office-change-review.js','utf8'));w.eval(fs.readFileSync('agent_service/static/office-edit.js','utf8'));
 await new Promise(r=>setTimeout(r,0));const $=s=>w.document.querySelector(s),review=$('#change-review-content');assert(review.textContent.includes('当前没有暂存修改'));
 $('#field').value='body';$('#field').dispatchEvent(new w.Event('change'));$('#value').value='新金额：1.01 <script>evil()</script>';$('#value').dispatchEvent(new w.Event('input'));
 assert(review.textContent.includes('旧金额：1.00'));assert(review.textContent.includes('新金额：1.01 <script>evil()</script>'));assert.equal(review.querySelectorAll('script,img').length,0);
 $('#section').value='1';$('#section').dispatchEvent(new w.Event('change'));$('#add-section').click();$('#value').value='新章节';$('#value').dispatchEvent(new w.Event('input'));$('#field').value='body';$('#field').dispatchEvent(new w.Event('change'));$('#value').value='新增说明';$('#value').dispatchEvent(new w.Event('input'));assert(review.textContent.includes('新增说明'));
 $('#section').value='0';$('#section').dispatchEvent(new w.Event('change'));$('#remove-section').click();assert(review.textContent.includes('含表格和图表'));assert(!review.textContent.includes('新金额：1.01'));
 $('#restore-section').click();assert(review.textContent.includes('新金额：1.01'));assert(!review.textContent.includes('将从新文件移除'));
 w.sessionStorage.setItem('nexus_token','new');w.dispatchEvent(new w.StorageEvent('storage',{key:'nexus_token',newValue:'new'}));assert($('#editor').hidden);assert(!w.document.body.textContent.includes('旧金额'));assert(!w.document.body.textContent.includes('新增说明'));
 console.log('office edit review shows exact changes, order and removal impact safely and clears on account change');
})().catch(e=>{console.error(e);process.exit(1)});
