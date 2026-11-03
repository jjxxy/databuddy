// DOM/API contract test; does not claim real browser layout or PowerPoint rendering.
const {JSDOM}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const dir=path.resolve(__dirname,'../../agent_service/static');
const html=fs.readFileSync(path.join(dir,'project-templates.html'),'utf8');
const script=fs.readFileSync(path.join(dir,'project-templates.js'),'utf8');
const dom=new JSDOM(html,{url:'https://example.test/project-templates.html?project=p_test',runScripts:'outside-only'});
const w=dom.window,doc=w.document,sha='a'.repeat(64),calls=[];w.confirm=()=>true;w.nexusSecureUUID=()=> 'secure-request-id';
let failRender=false;
function response(value){return {ok:true,status:200,json:async()=>value}}
w.fetch=async(url,options={})=>{
 calls.push({url,method:options.method,body:options.body});
 if(url.endsWith('/personal-ppt-template-sources'))return response({items:[{id:'s_1',name:'季度<img src=x>',revision:1,sha256:sha}]});
 if(url.endsWith('/personal-ppt-templates'))return response({items:[{id:'pt_1',title:'模板<script>bad</script>',source_revision:1,version:1,available:true},{id:'pt_stale',title:'失效',source_revision:1,version:1,available:false}]});
 if(url.endsWith('/personal-ppt-templates/pt_1'))return response({template:{id:'pt_1',title:'模板<script>bad</script>',source_revision:1,version:1},structure:{slides:[{page:1,placeholders:[{name:'标题<img src=x>',shape_id:5,text:'旧值',text_truncated:false}],text_shapes:[]}]}});
 if(url.endsWith('/personal-ppt-templates/pt_1/render')){if(failRender)throw Error('lost response');return response({artifact_id:'of_'+'a'.repeat(40)})}
 throw Error('unexpected request '+url);
};
function wait(){return new Promise(resolve=>setTimeout(resolve,20))}
(async()=>{w.eval(script);await wait();
 assert.equal(doc.querySelectorAll('.template-card').length,2);
 assert.equal(doc.querySelectorAll('script[src="x"], img').length,0);
 const cards=doc.querySelectorAll('.template-card');assert.equal(cards[1].querySelector('button').disabled,true);
 cards[0].querySelector('button').click();await wait();
 const check=doc.querySelector('.position input[type=checkbox]'),text=doc.querySelector('.position textarea');assert.equal(text.disabled,true);
 check.click();text.value='收入 118.15 元';text.dispatchEvent(new w.Event('input'));doc.querySelector('#render').click();await wait();
 const r=calls.find(x=>x.url.endsWith('/render'));const body=JSON.parse(r.body);
 assert.deepEqual(body.replacements,[{page:1,shape_id:5,text:'收入 118.15 元'}]);assert.equal(body.version,1);
 assert.match(doc.querySelector('#result').textContent,/已生成/);
 failRender=true;doc.querySelector('#render').click();await wait();assert.equal(doc.querySelector('#render').disabled,true);
 assert.match(doc.querySelector('#status').textContent,/未确认生成结果/);assert.equal(text.value,'收入 118.15 元');
 console.log(JSON.stringify({project_bound:true,untrusted_labels_are_text:true,stale_template_disabled:true,explicit_fields_only:true,ambiguous_result_blocks_replay:true,draft_preserved:true,real_browser_verified:false}));
})().catch(e=>{console.error(e);process.exitCode=1});
