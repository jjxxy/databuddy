'use strict';
const { app, BrowserWindow, Menu, ipcMain, session, dialog } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const crypto = require('node:crypto');
const { configureDesktopWindow } = require('./window-controls.cjs');
const { sameOrigin, sameFile, validPipelineId, parseReady, trustedMainFrame, createTransitionGate } = require('./security.cjs');

app.setName('Atlas DataBuddy');
const root = path.resolve(__dirname, '..');
const startFile = path.join(__dirname, 'start-screen.html');
const lineageFile = path.join(root, 'ui', 'dist', 'index.html');
let mainWindow, lineageWindow, runtime, connection, starting, stopping, quitAllowed = false;
let status = {failed:false,message:'正在启动本机服务。资料与成果保存在这台电脑上。'};
let logFile;
let selectedWorkspace=null,choosingWorkspace=false;
const transitions=createTransitionGate();
function workspaceSelectionFile(){return path.join(app.getPath('userData'),'selected-workspace.json');}
function readWorkspaceSelection(){
  const file=workspaceSelectionFile();if(!fs.existsSync(file))return null;
  const value=JSON.parse(fs.readFileSync(file,'utf8'));
  if(value.workspace_path===null)return null;
  if(typeof value.workspace_path!=='string'||!path.isAbsolute(value.workspace_path))throw new Error('本地工作空间选择记录无效，请重新选择工作文件夹。');
  return value.workspace_path;
}
function saveWorkspaceSelection(value){
  const temporary=path.join(app.getPath('userData'),'selected-workspace.'+crypto.randomUUID()+'.tmp');
  fs.writeFileSync(temporary,JSON.stringify({workspace_path:value},null,2)+'\n',{flag:'wx',mode:0o600});
  try{fs.renameSync(temporary,workspaceSelectionFile());}catch(error){try{fs.unlinkSync(temporary);}catch{}throw error;}
}
function log(message) {
  if (!logFile) return;
  const safe = connection?.token ? String(message).split(connection.token).join('[redacted]') : String(message);
  fs.appendFileSync(logFile, new Date().toISOString() + ' ' + safe.slice(0,8000) + '\n');
}
function notify(message, failed = false) {
  status = {message,failed,logPath:logFile};
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('atlas:status',status);
}
function isMain(event, allowStart = false) {
  return mainWindow && !mainWindow.isDestroyed() &&
    trustedMainFrame(event,mainWindow.webContents,connection?.origin,startFile,allowStart);
}
function isLineage(event) {
  return lineageWindow && !lineageWindow.isDestroyed() && event.sender === lineageWindow.webContents &&
    event.senderFrame === event.sender.mainFrame && sameFile(event.senderFrame.url,lineageFile);
}
function secureWindow(window, localFile) {
  configureDesktopWindow(window,{openLineage:localFile===startFile?openLineage:null,canOpenLineage:()=>sameOrigin(window.webContents.getURL(),connection?.origin)});
  window.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  window.webContents.on('will-navigate',(event,url) => {
    if (!(sameOrigin(url,connection?.origin) || sameFile(url,localFile))) event.preventDefault();
  });
  window.webContents.on('will-redirect',(event,url) => {
    if (!sameOrigin(url,connection?.origin)) event.preventDefault();
  });
}
async function readApi(route) {
  if (!connection) throw new Error('本机服务尚未就绪，请返回工作台重试。');
  const response = await fetch(connection.origin + route, {
    headers:{Authorization:'Bearer ' + connection.token},redirect:'error',signal:AbortSignal.timeout(15000)
  });
  if (!response.ok) throw new Error('暂时无法读取溯源信息，请稍后刷新。');
  const length = Number(response.headers.get('content-length') || 0);
  if (length > 2*1024*1024) throw new Error('溯源信息超过显示上限，请在工作台缩小范围。');
  const chunks=[];let size=0;
  for await (const chunk of response.body) {
    size+=chunk.length;
    if(size>2*1024*1024){throw new Error('溯源信息超过显示上限，请在工作台缩小范围。');}
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
async function openLineage() {
  if (!connection) return;
  if (lineageWindow && !lineageWindow.isDestroyed()) {lineageWindow.focus();return;}
  lineageWindow = new BrowserWindow({width:1200,height:800,minWidth:760,minHeight:600,
    title:'数据来源与处理链 · Atlas DataBuddy',backgroundColor:'#ffffff',
    webPreferences:{preload:path.join(__dirname,'lineage-preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true,spellcheck:false}});
  secureWindow(lineageWindow,lineageFile);
  lineageWindow.on('closed',()=>{lineageWindow=null;});
  await lineageWindow.loadFile(lineageFile);
}
async function stopRuntime() {
  if (stopping) return stopping;
  const process = runtime;
  // A provenance view belongs to one workspace. Destroy it before a new
  // connection can satisfy an old window's read-only IPC requests.
  if(lineageWindow&&!lineageWindow.isDestroyed())lineageWindow.close();
  lineageWindow=null;
  connection = null;
  if (!process || process.exitCode !== null) return;
  stopping = new Promise(resolve => {
    const timer = setTimeout(()=>{log('Backend shutdown timeout; terminating host.');process.kill();},15000);
    const hard = setTimeout(resolve,18000);
    process.once('exit',()=>{clearTimeout(timer);clearTimeout(hard);resolve();});
    try { process.stdin.write('stop\n'); } catch { process.kill(); }
  }).finally(()=>{stopping=null;});
  return stopping;
}
async function startRuntime() {
  if (starting) return starting;
  starting = (async () => {
    await stopRuntime();
    notify('正在启动本机服务。启动过程不会下载依赖或检查在线模型。');
    await mainWindow.loadFile(startFile);
    const python = path.join(root,'runtime','python','python.exe');
    const host = path.join(root,'portable_runtime.py');
    if (!fs.existsSync(python) || !fs.existsSync(host)) {
      notify('安装包缺少运行文件。请重新解压完整的 Atlas DataBuddy 交付包；无需自行安装 Python。',true);return false;
    }
    return new Promise(resolve => {
      let ready = false, complete = false;
      const arguments_=[host,'serve','--state-dir',app.getPath('userData'),'--port','0'];
      if(selectedWorkspace)arguments_.push('--workspace-dir',selectedWorkspace);
      const process = runtime = spawn(python,arguments_,{
        cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe'],env:{...global.process.env,PYTHONUTF8:'1',PYTHONUNBUFFERED:'1'}
      });
      const timer = setTimeout(()=>{if(!ready){notify('本机服务启动时间过长。请重试，诊断记录已保存在本机。',true);void stopRuntime();finish();}},120000);
      function finish(){if(complete)return;complete=true;clearTimeout(timer);resolve(ready);}
      const lines = readline.createInterface({input:process.stdout});
      lines.on('line',line => {
        if (line.length > 65536) {log('Oversized backend output omitted.');return;}
        let value;try{value=JSON.parse(line);}catch{log('Backend diagnostic output omitted; see backend log.');return;}
        if (ready || complete) return;
        if(value?.event==='error'&&typeof value.message==='string'){
          notify(value.message.slice(0,1200),true);log('Host reported a startup error.');finish();return;
        }
        const parsed = parseReady(value);
        if (!parsed) {log('Backend output did not contain a valid readiness event.');return;}
        connection = parsed;ready=true;
        configureHeaders();
        notify('本机工作空间已就绪。');
        mainWindow.loadURL(connection.origin+'/').catch(()=>notify('工作台页面打开失败，请重试。',true));
        finish();
      });
      process.stderr.on('data',chunk=>log(chunk.toString('utf8')));
      process.on('error',error=>{log('Backend spawn error: '+error.code);notify('本机运行文件无法启动。请检查安装包是否完整，并查看诊断日志。',true);finish();});
      process.on('exit',(code,signal)=>{
        log('Backend exited code='+code+' signal='+signal);
        if (runtime===process) {runtime=null;connection=null;}
        if (!quitAllowed && !stopping) {
          if(!status.failed)notify('本机服务已停止，资料与成果仍保留。请重新启动工作空间。',true);
          if(mainWindow&&!mainWindow.isDestroyed())void mainWindow.loadFile(startFile);
        }
        finish();
      });
    });
  })().finally(()=>{starting=null;});
  return starting;
}
function configureHeaders() {
  const scope = mainWindow.webContents.session;
  scope.webRequest.onBeforeSendHeaders({urls:['http://127.0.0.1/*']},(details,callback)=>{
    const headers = {...details.requestHeaders};
    if (connection && sameOrigin(details.url,connection.origin)) headers.Authorization='Bearer '+connection.token;
    callback({requestHeaders:headers});
  });
}

ipcMain.on('atlas:bootstrap',event=>{
  event.returnValue = isMain(event) && connection ? {...connection} : null;
});
ipcMain.handle('atlas:startup-status',event=>{
  if(!isMain(event,true))throw new Error('请求来源不受支持');return status;
});
ipcMain.handle('atlas:retry',async event=>{
  if(!mainWindow||mainWindow.isDestroyed()||!trustedMainFrame(event,mainWindow.webContents,null,startFile,true))throw new Error('请求来源不受支持');await transitions.run(()=>startRuntime());return true;
});
ipcMain.handle('atlas:open-lineage',async event=>{
  if(!isMain(event))throw new Error('请求来源不受支持');await openLineage();return true;
});
ipcMain.handle('atlas:runtime-status',async event=>{
  if(!isMain(event))throw new Error('请求来源不受支持');
  const value=await readApi('/api/desktop/runtime');
  return {notice:String(value.notice||'本地能力可用；云端 AI 需要网络。')};
});
ipcMain.handle('atlas:choose-workspace',async event=>{
  if(!isMain(event,true))throw new Error('请求来源不受支持');
  return transitions.run(async()=>{
  if(choosingWorkspace||starting)throw new Error('正在处理工作空间切换，请稍候。');
  choosingWorkspace=true;
  try{
    const chosen=await dialog.showOpenDialog(mainWindow,{title:'选择本地工作文件夹',buttonLabel:'使用此文件夹',properties:['openDirectory','createDirectory']});
    if(chosen.canceled||!chosen.filePaths.length||!mainWindow||mainWindow.isDestroyed())return {cancelled:true};
    const directory=chosen.filePaths[0];
    if(!path.isAbsolute(directory)||!fs.lstatSync(directory).isDirectory()||fs.lstatSync(directory).isSymbolicLink())throw new Error('请选择普通本地文件夹。');
    const consent=await dialog.showMessageBox(mainWindow,{type:'question',title:'切换本地工作空间',message:'将打开所选文件夹的独立工作空间。',detail:'当前项目、对话和资料保留在原工作空间，不会被自动移动。请选择空闲时切换；已提交的后台工作会先停止。',buttons:['取消','切换工作空间'],defaultId:0,cancelId:0,noLink:true});
    if(consent.response!==1||!mainWindow||mainWindow.isDestroyed())return {cancelled:true};
    const previous=selectedWorkspace;
    saveWorkspaceSelection(directory);selectedWorkspace=directory;
    if(!await startRuntime()){
      selectedWorkspace=previous;saveWorkspaceSelection(previous);
      notify(status.message+' 原工作空间选择已保留；点击“重新启动”返回原工作空间，或重新选择文件夹。',true);
      throw new Error('工作空间未能启动，原选择已保留。可以重试或重新选择文件夹。');
    }
    return {cancelled:false};
  }finally{choosingWorkspace=false;}
  });
});
ipcMain.handle('atlas:lineage-list',async event=>{
  if(!isLineage(event))throw new Error('请求来源不受支持');
  const values = await readApi('/api/pipelines');
  return (Array.isArray(values)?values:[]).map(value=>({id:value.id,name:value.name,project_id:value.project_id,published_run:value.published_run,candidate_run:value.candidate_run}));
});
ipcMain.handle('atlas:lineage-get',async(event,id)=>{
  if(!isLineage(event)||!validPipelineId(id))throw new Error('请选择有效的数据任务');
  return readApi('/api/pipelines/'+encodeURIComponent(id)+'/lineage');
});

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance',()=>{if(mainWindow){if(mainWindow.isMinimized())mainWindow.restore();mainWindow.focus();}});
  app.whenReady().then(async()=>{
    Menu.setApplicationMenu(null);
    fs.mkdirSync(app.getPath('userData'),{recursive:true});
    logFile=path.join(app.getPath('userData'),'desktop.log');
    if(fs.existsSync(logFile)&&fs.statSync(logFile).size>4*1024*1024)fs.renameSync(logFile,logFile+'.previous');
    session.defaultSession.setPermissionRequestHandler((_contents,_permission,callback)=>callback(false));
    mainWindow=new BrowserWindow({width:1380,height:900,minWidth:900,minHeight:650,title:'Atlas DataBuddy',backgroundColor:'#ffffff',
      webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true,spellcheck:false}});
    secureWindow(mainWindow,startFile);
    mainWindow.on('closed',()=>{mainWindow=null;});
    try{selectedWorkspace=readWorkspaceSelection();await transitions.run(()=>startRuntime());}
    catch{notify('工作文件夹选择记录无法读取。请重新选择本地工作文件夹；原资料保持不变。',true);await mainWindow.loadFile(startFile);}
  }).catch(error=>{log('Desktop initialization error: '+error.code);app.quit();});
  app.on('before-quit',event=>{
    if(quitAllowed)return;
    event.preventDefault();
    void stopRuntime().finally(()=>{quitAllowed=true;app.quit();});
  });
  app.on('window-all-closed',()=>app.quit());
}
