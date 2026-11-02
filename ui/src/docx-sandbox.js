import JSZip from 'jszip';
import { renderAsync } from 'docx-preview';
const MAX_INPUT=25*1024*1024, MAX_ENTRY=24*1024*1024, MAX_TOTAL=96*1024*1024, MAX_FILES=2048;
let used=false;
const blobUrls=new Set(),createBlobURL=URL.createObjectURL.bind(URL),revokeBlobURL=URL.revokeObjectURL.bind(URL);
URL.createObjectURL=blob=>{const value=createBlobURL(blob);blobUrls.add(value);return value};
window.addEventListener('unload',()=>{for(const value of blobUrls)revokeBlobURL(value);blobUrls.clear()});
function safeNode(v){
 if(typeof v==='string')return document.createTextNode(v);
 if(v instanceof Node)return v;
 const {ns,tagName,className,style,children,...props}=v;
 const tag=String(tagName||'').toLowerCase();
 if(tag==='#fragment'){const node=document.createDocumentFragment();for(const c of children||[])node.append(safeNode(c));return node}
 if(tag==='#comment')return document.createComment('');
 const forbidden=['script','iframe','object','embed','form','base','meta','link','a'];
 const name=forbidden.includes(tag)?'span':tagName;
 const node=ns&&!forbidden.includes(tag)?document.createElementNS(ns,name):document.createElement(name);
 if(className)node.setAttribute('class',className);
 if(style){if(typeof style==='string')node.setAttribute('style',style);else Object.assign(node.style,style)}
 for(const [key,value] of Object.entries(props)){if(value===undefined||/^(on|href|src|srcdoc|action|formaction|target|innerHTML|outerHTML|xlink:href)/i.test(key))continue;try{node[key]=value}catch{}}
 for(const c of children||[])node.append(safeNode(c));return node;
}
function inspectDirectory(bytes){
 const a=new Uint8Array(bytes),v=new DataView(bytes);if(a.length<22)throw Error('zip');let end=-1;
 for(let p=a.length-22;p>=Math.max(0,a.length-65557);p--){if(v.getUint32(p,true)===0x06054b50&&p+22+v.getUint16(p+20,true)===a.length){end=p;break}}
 if(end<0||v.getUint16(end+4,true)!==0||v.getUint16(end+6,true)!==0)throw Error('zip');
 const count=v.getUint16(end+10,true),size=v.getUint32(end+12,true),start=v.getUint32(end+16,true);
 if(count===0xffff||count>MAX_FILES||v.getUint16(end+8,true)!==count||start+size!==end)throw Error('directory');
 let pos=start,declared=0;const names=new Set();
 for(let i=0;i<count;i++){
  if(pos+46>end||v.getUint32(pos,true)!==0x02014b50)throw Error('directory');
  const compressed=v.getUint32(pos+20,true),expanded=v.getUint32(pos+24,true),n=v.getUint16(pos+28,true),x=v.getUint16(pos+30,true),c=v.getUint16(pos+32,true);
  if(!n||pos+46+n+x+c>end||compressed===0xffffffff||expanded===0xffffffff||expanded>MAX_ENTRY)throw Error('directory');
  const name=Array.from(a.subarray(pos+46,pos+46+n),b=>String.fromCharCode(b)).join('');if(names.has(name))throw Error('duplicate');names.add(name);
  declared+=expanded;if(declared>MAX_TOTAL)throw Error('expanded');pos+=46+n+x+c;
 }
 if(pos!==end)throw Error('directory');
}
async function boundedZip(bytes){
 if(!(bytes instanceof ArrayBuffer)||bytes.byteLength<4||bytes.byteLength>MAX_INPUT)throw Error('size');
 inspectDirectory(bytes);const z=await JSZip.loadAsync(bytes,{checkCRC32:false,createFolders:false});
 const entries=Object.values(z.files);if(entries.length>MAX_FILES)throw Error('entries');
 let total=0;
 for(const entry of entries){if(entry.dir)continue;let size=0;await new Promise((resolve,reject)=>{
  const stream=entry.internalStream('uint8array');let done=false;
  stream.on('data',chunk=>{size+=chunk.length;total+=chunk.length;if(size>MAX_ENTRY||total>MAX_TOTAL){done=true;stream.pause();reject(Error('expanded'))}});
  stream.on('error',error=>{if(!done){done=true;reject(error)}});stream.on('end',()=>{if(!done){done=true;resolve()}});stream.resume();
 })}
 if(!z.file('word/document.xml')||!z.file('[Content_Types].xml'))throw Error('type');
}
const send=(type)=>parent.postMessage({nexusDocx:type},'*');
window.addEventListener('message',async event=>{
 if(event.source!==parent||used||event.data?.nexusDocx!=='render')return;used=true;
 try{await boundedZip(event.data.bytes);await renderAsync(event.data.bytes,document.getElementById('body'),document.getElementById('styles'),{renderAltChunks:false,ignoreFonts:true,useBase64URL:false,h:safeNode});send('ready')}
 catch(_){send('error')}
});
for(const type of ['click','submit','dragstart'])document.addEventListener(type,e=>{if(e.target.closest?.('a,form'))e.preventDefault()},true);
send('loaded');
