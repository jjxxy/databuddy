const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),{pathToFileURL}=require('node:url');
const desktop=path.resolve(__dirname,'..');
test('actual retry IPC accepts only the startup recovery document, never the workbench',async()=>{
 const handlers=new Map(),loads=[],source=fs.readFileSync(path.join(desktop,'main.cjs'),'utf8');
 const electron={app:{setName(){},requestSingleInstanceLock:()=>false,quit(){},getPath:()=>path.resolve('fixture-no-userdata')},BrowserWindow(){},Menu:{},ipcMain:{on(){},handle:(name,callback)=>handlers.set(name,callback)},session:{},dialog:{}};
 const context=vm.createContext({__dirname:desktop,URL,AbortSignal,Buffer,setTimeout,clearTimeout,process,require(name){if(name==='electron')return electron;if(name==='node:fs')return {existsSync:()=>false};if(name.startsWith('./'))return require(path.join(desktop,name));return require(name)}});
 vm.runInContext(source+';globalThis.setFixture=(window,ready)=>{mainWindow=window;connection=ready};',context);
 const frame={url:'http://127.0.0.1:8123/'},contents={mainFrame:frame,send(){}},window={webContents:contents,isDestroyed:()=>false,loadFile:async file=>loads.push(file)};
 context.setFixture(window,{origin:'http://127.0.0.1:8123',token:'fixture-owner-token'});const retry=handlers.get('atlas:retry'),event={sender:contents,senderFrame:frame};
 await assert.rejects(retry(event),/请求来源/);assert.equal(loads.length,0);
 frame.url=pathToFileURL(path.join(desktop,'start-screen.html')).href;
 await assert.rejects(retry({...event,senderFrame:{url:frame.url}}),/请求来源/);await assert.rejects(retry({...event,sender:{mainFrame:frame}}),/请求来源/);
 await retry(event);assert.deepEqual(loads,[path.join(desktop,'start-screen.html')]);
});
test('actual preload exposes recovery retry on a file page only',()=>{
 const source=fs.readFileSync(path.join(desktop,'preload.cjs'),'utf8');
 for(const protocol of ['http:','file:']){
  let bridge;const context=vm.createContext({location:{protocol,origin:protocol==='http:'?'http://127.0.0.1:8123':'null'},Object,require(){return {contextBridge:{exposeInMainWorld:(name,value)=>bridge=value},ipcRenderer:{sendSync:()=>null,invoke:()=>Promise.resolve(),on(){}}}}});
  vm.runInContext(source,context);assert.equal(typeof bridge.openLineage,'function');assert.equal(typeof bridge.retry,protocol==='file:'?'function':'undefined');
 }
});
