'use strict';
function show(value) {
  document.getElementById('title').textContent = value.failed ? '工作空间暂时无法启动' : '正在打开你的工作空间';
  document.getElementById('message').textContent = value.message || '正在启动本机服务。';
  document.getElementById('retry').hidden = !value.failed;
  document.getElementById('retry').disabled = false;
  document.getElementById('choose-folder').hidden = !value.failed;
  document.getElementById('log').textContent = value.logPath ? '诊断日志：' + value.logPath : '';
}
window.atlasDesktop.onStatus(show);
window.atlasDesktop.status().then(show).catch(() => show({failed:true,message:'桌面服务连接中断，请关闭后重新打开。'}));
document.getElementById('retry').onclick = async () => {
  document.getElementById('retry').disabled = true;
  try { await window.atlasDesktop.retry(); }
  catch { show({failed:true,message:'重新启动失败，请关闭后再打开，并查看诊断日志。'}); }
};
document.getElementById('choose-folder').onclick = async () => {
  try { await window.atlasDesktop.chooseWorkspace(); }
  catch { show({failed:true,message:'工作空间未切换，请选择普通本地文件夹后重试。'}); }
};
