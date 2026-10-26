const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');const {indexedDB}=require('fake-indexeddb');
const handlers={},shown=[],opened=[],messages=[];let windows=[];
const worker={location:{origin:'https://atlas.test'},clients:{claim:async()=>{},matchAll:async()=>windows,openWindow:async p=>opened.push(p)},registration:{showNotification:async(title,options)=>shown.push({title,...options})},skipWaiting:async()=>{},addEventListener:(name,fn)=>handlers[name]=fn};
const context=vm.createContext({self:worker,indexedDB,URL,URLSearchParams,console});vm.runInContext(fs.readFileSync(path.join(__dirname,'../../agent_service/static/sw.js'),'utf8'),context);
async function event(name,props){const waits=[];handlers[name]({...props,waitUntil:p=>waits.push(p)});await Promise.all(waits)}
async function run(){
 const item={seq:4,created:1700000000,kind:'completed',project_id:'p_123',session_id:'c_456',turn_id:'t_789',label:'must-not-display-company-content'};
 await event('push',{data:{json:()=>item}});assert.equal(shown.length,0);
 await event('message',{data:{type:'policy',enabled:true},ports:[{postMessage:()=>messages.push('policy')} ]});assert.deepEqual(messages,['policy']);
 await event('push',{data:{json:()=>item}});await event('message',{data:{type:'notify',event:item}});assert.equal(shown.length,1);assert(!shown[0].title.includes('company'));assert(!shown[0].body.includes('company'));
 await event('notificationclick',{notification:{data:item,close(){}}});assert.equal(opened.length,1);assert(opened[0].includes('turn=t_789'));
 windows=[{url:'https://atlas.test/',focus:async()=>{},postMessage:m=>messages.push(m)}];await event('notificationclick',{notification:{data:item,close(){}}});assert.equal(messages.at(-1).type,'navigate');
 await event('message',{data:{type:'policy',enabled:false},ports:[]});await event('push',{data:{json:()=>({...item,seq:5})}});assert.equal(shown.length,1);
 await event('notificationclick',{notification:{data:{...item,project_id:'https://evil.test'},close(){}}});assert.equal(opened.length,1);
 console.log(JSON.stringify({ok:true,permission_policy:true,duplicate_push_and_page_event_suppressed:true,generic_content_only:true,click_deep_link:true,logout_suppresses:true,untrusted_navigation_rejected:true,real_os_delivery_evaluated:false}));
}run().catch(e=>{console.error(e);process.exit(1)});
