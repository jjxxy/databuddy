'use strict';
/* randomUUID requires a secure context; getRandomValues also works on HTTP LAN pages. */
(function (root) {
  function uuid() {
    const source = root.crypto;
    if (typeof source?.randomUUID === 'function') return source.randomUUID();
    if (typeof source?.getRandomValues !== 'function') {
      throw Error('浏览器无法生成安全的请求标识，请更新浏览器或使用 HTTPS 访问。');
    }
    const bytes = new Uint8Array(16);
    source.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }
  Object.defineProperty(root, 'atlasSecureUUID', {value:uuid, writable:false, configurable:false});
})(window);
