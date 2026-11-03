'use strict';
const {JSDOM}=require('jsdom'),fs=require('fs'),assert=require('assert/strict');
(async()=>{
 const html=fs.readFileSync('agent_service/static/office-edit.html','utf8'),dom=new JSDOM(html,{url:'https://fixture.test/office-edit.html?project=p_1&artifact=wa_'+('a'.repeat(44)),runScripts:'outside-only'}),w=dom.window;
 let posted,confirmText,allow=false;w.nexusSecureUUID=()=> 'request_edit_structure_1';w.confirm=msg=>{confirmText=msg;return allow};w.fetch=async(url,opts={})=>opts.method==='POST'?(posted=JSON.parse(opts.body),{ok:true,json:async()=>({artifact_id:'of_'+('d'.repeat(40)),sha256:'e'.repeat(64),work_commit:'f'.repeat(40)})}):{ok:true,json:async()=>({artifact_id:'wa_'+('a'.repeat(44)),artifact_sha256:'b'.repeat(64),spec_sha256:'c'.repeat(64),kind:'presentation',notice:'测试',sections:[{index:0,heading:'机密业绩',body:'数据',bullets:[],contains_table:true,contains_chart:true},{index:1,heading:'结论',body:'旧结论',bullets:[]}]})};
 w.eval(fs.readFileSync('agent_service/static/office-edit.js','utf8'));await new Promise(r=>setTimeout(r,0));const $=s=>w.document.querySelector(s);
 $('#remove-section').click();assert(confirmText.includes('表格和图表'));assert.equal($('#section').options.length,2);
 allow=true;$('#remove-section').click();assert.equal($('#section').options.length,1);$('#restore-section').click();assert.equal($('#section').options.length,2);assert.equal($('#section').value,'0');
 $('#add-section').click();assert.equal($('#section').value,'2');$('#value').value='新增说明';$('#value').dispatchEvent(new w.Event('input'));$('#add-section').click();assert.equal($('#section').value,'3');$('#remove-section').click();assert.equal($('#section').options.length,3);
 $('#section').value='0';$('#section').dispatchEvent(new w.Event('change'));$('#remove-section').click();$('#save').click();await new Promise(r=>setTimeout(r,0));assert.deepEqual(posted.section_order,[1,2]);assert.deepEqual(posted.new_sections,[{heading:'新增说明',body:''}]);assert.equal(posted.confirm_removed,true);assert.deepEqual(posted.changes,[]);assert($('#result').textContent.includes('新文件已生成'));
 console.log('office structure editing confirms removals, restores drafts and normalizes new sections');
})().catch(e=>{console.error(e);process.exit(1)});
