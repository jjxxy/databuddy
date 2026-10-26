'use strict';
const path = require('node:path');
const { fileURLToPath } = require('node:url');

function loopbackOrigin(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port ||
        url.username || url.password || url.pathname !== '/' || url.search || url.hash) return null;
    const port = Number(url.port);
    return Number.isInteger(port) && port >= 1 && port <= 65535 ? url.origin : null;
  } catch { return null; }
}
function sameOrigin(value, origin) {
  try { return !!origin && new URL(value).origin === origin; } catch { return false; }
}
function sameFile(value, filename) {
  try {
    const url = new URL(value);
    return url.protocol === 'file:' && !url.search && !url.hash &&
      path.resolve(fileURLToPath(url)) === path.resolve(filename);
  } catch { return false; }
}
function validPipelineId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);
}
function parseReady(value) {
  if (!value || value.event !== 'ready' || !loopbackOrigin(value.url) ||
      typeof value.token !== 'string' || value.token.length < 16 || value.token.length > 200 ||
      /[\r\n]/.test(value.token)) return null;
  return { origin: loopbackOrigin(value.url), token: value.token };
}
function trustedMainFrame(event, contents, origin, startFile, allowStart=false) {
  return !!contents && event.sender===contents && event.senderFrame===contents.mainFrame &&
    (sameOrigin(event.senderFrame.url,origin)||allowStart&&sameFile(event.senderFrame.url,startFile));
}
function createTransitionGate() {
  let busy=false;
  return {async run(operation) {
    if(busy)throw new Error('正在处理工作空间切换，请稍候。');
    busy=true;
    try{return await operation();}finally{busy=false;}
  }};
}
module.exports = { loopbackOrigin, sameOrigin, sameFile, validPipelineId, parseReady, trustedMainFrame, createTransitionGate };
