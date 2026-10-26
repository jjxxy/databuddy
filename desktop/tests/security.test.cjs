'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {loopbackOrigin,sameOrigin,sameFile,validPipelineId,parseReady}=require('../security.cjs');
test('readiness accepts only a credential-free HTTP loopback origin',()=>{
 assert.equal(loopbackOrigin('http://127.0.0.1:8123/'),'http://127.0.0.1:8123');
 for(const value of ['https://127.0.0.1:8123/','http://localhost:8123/','http://127.0.0.1.evil.test:8123/','http://u:p@127.0.0.1:8123/','http://127.0.0.1:8123/api','http://127.0.0.1:8123/?token=x','file:///x']) assert.equal(loopbackOrigin(value),null,value);
 assert.equal(parseReady({event:'ready',url:'http://127.0.0.1:8123/',token:'workspace-test-token-123'}).origin,'http://127.0.0.1:8123');
 assert.equal(parseReady({event:'ready',url:'http://127.0.0.1:8123/',token:'short'}),null);
 assert.equal(parseReady({event:'ready',url:'http://127.0.0.1:8123/',token:'workspace-test\r\nheader'}),null);
});
test('token scope is exact origin, not host suffix or a different port',()=>{
 assert.equal(sameOrigin('http://127.0.0.1:8123/api/projects','http://127.0.0.1:8123'),true);
 for(const url of ['http://127.0.0.1:8124/api','http://127.0.0.1.evil.test:8123/api','https://127.0.0.1:8123/api'])assert.equal(sameOrigin(url,'http://127.0.0.1:8123'),false);
});
test('file IPC target and pipeline identifiers cannot select arbitrary files or API paths',()=>{
 const local=path.resolve('ui/dist/index.html');assert.equal(sameFile(pathToFileURL(local).href,local),true);
 assert.equal(sameFile(pathToFileURL(local).href+'?other',local),false);
 assert.equal(sameFile(pathToFileURL(path.resolve('secrets.json')).href,local),false);
 assert.equal(validPipelineId('p_123-a'),true);
 for(const value of ['../model','a/b','',{},'x'.repeat(101)])assert.equal(validPipelineId(value),false);
});
test('lineage graph uses returned provenance and never invents a published result',async()=>{
 const {lineageGraph}=await import('../../ui/src/graph.mjs');
 const candidate=lineageGraph({id:'p1',name:'核对任务'},{source_ids:['s1','s1'],candidate_commit:'abc'});
 assert.equal(candidate.nodes.length,2);assert.equal(candidate.edges.length,1);
 assert.equal(candidate.nodes.some(n=>n.id.startsWith('result:')),false);
 const published=lineageGraph({id:'p1',name:'核对任务'},{source_ids:['s1'],active_commit:'frozen',run_id:'r1'});
 assert.equal(published.nodes.length,3);assert.equal(published.edges.length,2);assert.match(published.nodes[1].data.label,/frozen/);
});
test('folder selection IPC rejects foreign frames, subframes and other windows',()=>{
 const {trustedMainFrame}=require('../security.cjs');
 const frame={url:'http://127.0.0.1:8123/'},contents={mainFrame:frame},event={sender:contents,senderFrame:frame};
 const startup=path.resolve('desktop/start-screen.html');
 assert.equal(trustedMainFrame(event,contents,'http://127.0.0.1:8123',startup),true);
 assert.equal(trustedMainFrame({...event,senderFrame:{url:frame.url}},contents,'http://127.0.0.1:8123',startup),false);
 assert.equal(trustedMainFrame({...event,sender:{mainFrame:frame}},contents,'http://127.0.0.1:8123',startup),false);
 frame.url='https://evil.test/';assert.equal(trustedMainFrame(event,contents,'http://127.0.0.1:8123',startup,true),false);
 frame.url=pathToFileURL(startup).href;assert.equal(trustedMainFrame(event,contents,null,startup,true),true);
 assert.equal(trustedMainFrame(event,contents,null,startup,false),false);
});
test('workspace picker and restart share an exclusive transition without losing recovery',async()=>{
 const {createTransitionGate}=require('../security.cjs'),gate=createTransitionGate(),calls=[];let release;
 const picking=gate.run(async()=>{calls.push('picker');await new Promise(resolve=>release=resolve);calls.push('restart-selected');});
 await assert.rejects(gate.run(()=>{calls.push('competing-restart');}),/正在处理/);
 assert.deepEqual(calls,['picker']);release();await picking;
 await assert.rejects(gate.run(()=>{throw Error('failed-start');}),/failed-start/);
 await gate.run(()=>calls.push('retry-after-failure'));
 assert.deepEqual(calls,['picker','restart-selected','retry-after-failure']);
});
