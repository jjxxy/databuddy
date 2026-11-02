'use strict';
/* Display HTML without running its scripts or navigating to remote resources. */
window.nexusSafeHtmlDocument=function(html){
 const parsed=new DOMParser().parseFromString(String(html),'text/html');
 for(const node of parsed.querySelectorAll('script,iframe,frame,object,embed,base,link,meta[http-equiv],form'))node.remove();
 for(const node of parsed.querySelectorAll('*'))for(const attr of [...node.attributes]){
  const key=attr.name.toLowerCase();
  if(key.startsWith('on')||['href','xlink:href','srcset','action','formaction','ping','srcdoc'].includes(key))node.removeAttribute(attr.name);
  else if(['src','poster','background'].includes(key)&&!/^data:image\/(?:png|jpeg|gif|webp);base64,/i.test(attr.value))node.removeAttribute(attr.name);
 }
 const csp="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'; object-src 'none'";
 const meta=parsed.createElement('meta');meta.setAttribute('http-equiv','Content-Security-Policy');meta.setAttribute('content',csp);parsed.head.prepend(meta);
 return '<!doctype html>'+parsed.documentElement.outerHTML;
};
