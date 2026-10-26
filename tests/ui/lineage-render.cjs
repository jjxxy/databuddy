// Exercises the built React + React Flow interface in a DOM. Build via npm run ui:build first.
// Geometry is stubbed; no native/browser layout claim.
const {JSDOM}=require('jsdom'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const dir=path.resolve(__dirname,'../../ui/dist'),html=fs.readFileSync(path.join(dir,'index.html'),'utf8'),asset=html.match(/src="\.\/([^\"]+\.js)"/)[1],bundle=fs.readFileSync(path.join(dir,asset),'utf8');
async function until(check){for(let i=0;i<100;i++){await new Promise(resolve=>setTimeout(resolve,5));if(check())return}throw Error('lineage render did not settle')}
async function boot({failed=false,empty=false}={}){
 const dom=new JSDOM(html,{url:'https://atlas.test/lineage/',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,d=w.document;
 w.ResizeObserver=class{observe(){}unobserve(){}disconnect(){}};w.DOMMatrixReadOnly=class{constructor(){this.m22=1}};
 const calls=[];w.atlasLineage={list:async()=>{calls.push('list');return empty?[]:[{id:'pipe_1',name:'收入核对',published_run:'run_1'}]},get:async id=>{calls.push(id);if(failed)throw Error('fixture unavailable');return {source_ids:['source_1'],run_id:'run_1',active_commit:'frozen'}}};
 w.eval(bundle);await until(()=>!d.body.textContent.includes('正在读取本机记录'));
 return {dom,d,calls};
}
(async()=>{
 const good=await boot();try{await until(()=>good.d.querySelector('.react-flow__controls-zoomin')?.title==='放大'&&good.d.body.textContent.includes('显示正式版本及来源'));for(const [selector,title] of [['.react-flow__controls-zoomin','放大'],['.react-flow__controls-zoomout','缩小'],['.react-flow__controls-fitview','显示完整处理链']]){const el=good.d.querySelector(selector);assert.equal(el.title,title);assert.equal(el.getAttribute('aria-label'),title)}assert(!good.d.body.textContent.includes('Press delete'));assert(good.d.body.textContent.includes('显示正式版本及来源'));assert.deepEqual(good.calls,['list','pipe_1'])}finally{good.dom.window.close()}
 const failed=await boot({failed:true});try{await until(()=>failed.d.querySelector('[role=alert]'));assert(failed.d.querySelector('.toolbar').textContent.includes('状态暂时无法确认'));assert(!failed.d.querySelector('.toolbar').textContent.includes('尚未发布正式成果'))}finally{failed.dom.window.close()}
 const empty=await boot({empty:true});try{await until(()=>empty.d.querySelector('.empty'));assert(empty.d.body.textContent.includes('还没有可显示的数据处理任务'));assert.deepEqual(empty.calls,['list'])}finally{empty.dom.window.close()}
 console.log(JSON.stringify({built_react_flow_chinese_controls:true,readonly_instructions_honest:true,read_error_not_reported_as_unpublished:true,empty_state_no_fake_graph:true,real_browser_layout_verified:false}));
})().catch(error=>{console.error(error);process.exitCode=1});
