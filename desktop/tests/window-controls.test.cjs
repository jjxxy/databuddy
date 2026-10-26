const test=require('node:test'),assert=require('node:assert/strict'),{EventEmitter}=require('node:events');
const {configureDesktopWindow}=require('../window-controls.cjs');
function fixture(options={}){
 const calls=[],contents=new EventEmitter();let zoom=0,fullscreen=false;
 for(const name of ['selectAll','copy','paste','cut','redo','undo'])contents[name]=()=>calls.push(name);
 contents.getZoomLevel=()=>zoom;contents.setZoomLevel=value=>{zoom=value;calls.push('zoom:'+value)};
 const window={webContents:contents,setMenu:value=>calls.push({menu:value}),setFullScreen:value=>{fullscreen=value},isFullScreen:()=>fullscreen};
 configureDesktopWindow(window,options);
 function input(key,extra={}){let prevented=false;contents.emit('before-input-event',{preventDefault(){prevented=true}},{type:'keyDown',key,control:true,...extra});return prevented}
 return {calls,input,zoom:()=>zoom,fullscreen:()=>fullscreen};
}
test('window menu removal retains normal editing keys without global capture',()=>{
 const f=fixture();assert.deepEqual(f.calls,[{menu:null}]);
 for(const key of ['a','c','v','x','z','y'])assert.equal(f.input(key),true);
 assert.equal(f.input('z',{shift:true}),true);assert.deepEqual(f.calls.slice(1),['selectAll','copy','paste','cut','undo','redo','redo']);
 for(const extra of [{type:'keyUp'},{alt:true},{meta:true},{control:false},{shift:true}])assert.equal(f.input('c',extra),false);
});
test('zoom and fullscreen shortcuts remain bounded and window-local',()=>{
 const f=fixture();for(let i=0;i<8;i++)f.input('+');assert.equal(f.zoom(),3);for(let i=0;i<8;i++)f.input('-');assert.equal(f.zoom(),-3);f.input('0');assert.equal(f.zoom(),0);f.input('=');assert.equal(f.zoom(),1);
 assert.equal(f.input('F11',{control:false}),true);assert.equal(f.fullscreen(),true);f.input('F11',{control:false});assert.equal(f.fullscreen(),false);
});
test('lineage shortcut is optional and only active in the trusted workbench',()=>{
 let trusted=false,opened=0;const f=fixture({openLineage:()=>opened++,canOpenLineage:()=>trusted});
 assert.equal(f.input('L',{shift:true}),false);trusted=true;assert.equal(f.input('L',{shift:true}),true);assert.equal(opened,1);assert.equal(f.input('L'),false);
 const secondary=fixture();assert.equal(secondary.input('L',{shift:true}),false);assert.deepEqual(secondary.calls,[{menu:null}]);
});
