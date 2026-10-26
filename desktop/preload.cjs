'use strict';
const { contextBridge, ipcRenderer } = require('electron');
// Only the top-level local workbench can receive this owner-workspace token.
// It remains origin-scoped, never enters a URL and is not an operator credential.
const bootstrap = ipcRenderer.sendSync('atlas:bootstrap');
if (bootstrap && typeof bootstrap.token === 'string' && location.origin === bootstrap.origin) {
  sessionStorage.setItem('atlas_token', bootstrap.token);
  window.addEventListener('DOMContentLoaded', async () => {
    if (!['/', '/index.html'].includes(location.pathname)) return;
    const main = document.getElementById('main');
    if (!main) return;
    const banner = document.createElement('aside');
    banner.setAttribute('role','status');
    banner.setAttribute('aria-live','polite');
    banner.style.cssText='flex:0 0 auto;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:10px 24px;border-bottom:1px solid #ddd;background:#fff;font:12px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;color:#444';
    const text=document.createElement('span');
    text.textContent='本地能力可用；云端 AI 需要网络和有效模型配置。';
    banner.append(text);main.prepend(banner);
    try { const state=await ipcRenderer.invoke('atlas:runtime-status');if(state?.notice)text.textContent=state.notice; }
    catch { text.textContent='本机状态暂时无法读取；请关闭后重新打开以恢复连接。'; }
  },{once:true});
}
contextBridge.exposeInMainWorld('atlasDesktop', Object.freeze({
  status: () => ipcRenderer.invoke('atlas:startup-status'),
  ...(location.protocol==='file:'?{retry: () => ipcRenderer.invoke('atlas:retry')}:{ }),
  openLineage: () => ipcRenderer.invoke('atlas:open-lineage'),
  chooseWorkspace: () => ipcRenderer.invoke('atlas:choose-workspace'),
  onStatus: callback => {
    if (typeof callback !== 'function') return;
    ipcRenderer.on('atlas:status', (_event, value) => callback(value));
  }
}));
