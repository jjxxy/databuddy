import {promises as fs,createReadStream} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import crypto from 'node:crypto';
import {shouldCopyPayload,verifyRuntimeInventory} from './payload-policy.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const output=path.resolve(process.env.ATLAS_DESKTOP_OUTPUT||path.join(root,'dist','Atlas-DataBuddy-win32-x64'));
const outputRelative=path.relative(path.join(root,'dist'),output);
if(!outputRelative||outputRelative.startsWith('..')||path.isAbsolute(outputRelative))throw new Error('Output must be a new directory below this project dist directory.');
const appDir=path.join(output,'resources','app');
const exists=async value=>{try{await fs.access(value);return true;}catch{return false;}};
if(process.platform!=='win32'||process.arch!=='x64')throw new Error('Build this Windows x64 artifact on Windows x64; no cross-platform runtime substitution.');
for(const relative of ['node_modules/electron/dist/electron.exe','ui/dist/index.html','portable_runtime.py','runtime/python/python.exe','runtime/git/cmd/git.exe','vendor/metadata/runtime-manifest.json'])
 if(!await exists(path.join(root,relative)))throw new Error('Missing bundled input: '+relative+'. Prepare the portable runtime and UI before packaging.');
if(await exists(output))throw new Error('Output already exists. Preserve it for comparison and choose a new output directory with ATLAS_DESKTOP_OUTPUT.');
// An allowlist keeps live credentials, user data, test fixtures and caches out.
await fs.mkdir(output,{recursive:true});
await fs.cp(path.join(root,'node_modules/electron/dist'),output,{recursive:true});
await fs.rename(path.join(output,'electron.exe'),path.join(output,'Atlas DataBuddy.exe'));
await fs.rm(path.join(output,'resources','default_app.asar'),{force:true});
await fs.mkdir(appDir,{recursive:true});
await fs.writeFile(path.join(appDir,'package.json'),JSON.stringify({name:'atlas-databuddy-desktop',productName:'Atlas DataBuddy',version:'0.20.0',main:'desktop/main.cjs'},null,2)+'\n');
for(const relative of ['desktop','agent_service','runtime']){
 await fs.cp(path.join(root,relative),path.join(appDir,relative),{recursive:true,filter:source=>{
  return shouldCopyPayload(relative,path.relative(path.join(root,relative),source));
 }});
}
await fs.cp(path.join(root,'ui/dist'),path.join(appDir,'ui/dist'),{recursive:true});
await fs.copyFile(path.join(root,'portable_runtime.py'),path.join(appDir,'portable_runtime.py'));
await fs.cp(path.join(root,'vendor','metadata'),path.join(appDir,'vendor','metadata'),{recursive:true});
await fs.cp(path.join(root,'acceptance','offline'),path.join(appDir,'acceptance','offline'),{recursive:true,filter:source=>!source.includes('__pycache__')});
for(const relative of ['llm.example.json','requirements-windows.lock','requirements-ocr-windows.lock','pyproject.toml'])if(await exists(path.join(root,relative)))await fs.copyFile(path.join(root,relative),path.join(appDir,relative));
await fs.writeFile(path.join(output,'使用说明.txt'),'Atlas DataBuddy v0.20.0\r\n\r\n1. 保持整个文件夹完整，双击 Atlas DataBuddy.exe。\r\n2. 在工作模式选择“本地电脑”，选取业务文件夹；在当前项目的“工作资料”内导入、画像、加工，核对候选后确认发布并导出。\r\n3. 原始文件不会被覆盖。项目数据保存在已选文件夹的 .atlas，代码与导出分别位于 Atlas工作区、Atlas成果。切换文件夹会打开该文件夹自己的项目，不自动搬移数据。\r\n4. 云端 AI 需要有效模型配置与网络；断网时本地工作台和确定性数据功能仍可使用。“云端”工作位置在后续阶段开放。\r\n\r\n无需安装 Python、Node、Git、Docker、WSL 或编译工具。运行时不会下载依赖。\r\n应用设置和诊断日志保存在当前 Windows 用户的应用数据目录，不在程序目录中。\r\n完整业务与离线验收状态以随交付的验收记录为准。\r\n','utf8');
const files=[];
async function walk(folder){for(const entry of await fs.readdir(folder,{withFileTypes:true})){const file=path.join(folder,entry.name);if(entry.isDirectory())await walk(file);else if(entry.isFile())files.push(file);else throw new Error('Artifact must not contain symlinks: '+file);}}
await walk(output);
const hashes=[],inventory=new Map();
for(const file of files.sort()){
 const hash=crypto.createHash('sha256');let bytes=0;
 for await(const chunk of createReadStream(file)){hash.update(chunk);bytes+=chunk.length;}
 const relative=path.relative(output,file).split(path.sep).join('/'),sha256=hash.digest('hex');
 inventory.set(relative,{bytes,sha256});
 hashes.push(sha256+'  '+relative);
}
// Compare the copied dependency bytes with the independently prepared vendor
// inventory, reusing the output hashing pass rather than reading all files again.
const manifest=JSON.parse(await fs.readFile(path.join(appDir,'vendor/metadata/runtime-manifest.json'),'utf8'));
const runtimeVerified=verifyRuntimeInventory(manifest,inventory);
await fs.writeFile(path.join(output,'FILES.sha256'),hashes.join('\n')+'\n');
console.log(JSON.stringify({ok:true,output,files:files.length,runtimeVerified,entry:path.join(output,'Atlas DataBuddy.exe'),credentialsIncluded:false}));
