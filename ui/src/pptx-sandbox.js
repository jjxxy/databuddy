import JSZip from 'jszip';
import {loadPresentation,getSlides} from '@office-kit/pptx';
import {renderSlideToSvg} from '@office-kit/pptx-preview';
const MAX_INPUT=25*1024*1024,MAX_ENTRY=24*1024*1024,MAX_TOTAL=96*1024*1024,MAX_FILES=2048;
let used=false;
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
 if(!z.file('ppt/presentation.xml')||!z.file('[Content_Types].xml'))throw Error('type');
}

const send=type=>parent.postMessage({nexusPptx:type},'*');
window.addEventListener('message',async event=>{
 if(event.source!==parent||used||event.data?.nexusPptx!=='render')return;
 used=true;
 try{
  await boundedZip(event.data.bytes);
  const presentation=await loadPresentation(event.data.bytes);
  const slides=getSlides(presentation);
  if(!slides.length||slides.length>120)throw Error('pages');
  let page=0,blobUrl=null;
  const image=document.getElementById('slide'),status=document.getElementById('status');
  const previous=document.getElementById('previous'),next=document.getElementById('next');
  function show(index){
   const svg=renderSlideToSvg(presentation,slides[index],{textLayout:'svg'});
   if(typeof svg!=='string'||svg.length>12*1024*1024)throw Error('slide');
   if(blobUrl)URL.revokeObjectURL(blobUrl);
   blobUrl=URL.createObjectURL(new Blob([svg],{type:'image/svg+xml'}));
   image.src=blobUrl;image.alt='第 '+(index+1)+' 页幻灯片';page=index;
   previous.disabled=index===0;next.disabled=index===slides.length-1;
   status.textContent='第 '+(index+1)+' / '+slides.length+' 页'+(svg.includes('data-pptx-fallback')?' · 本页部分内容暂无法还原，请下载原件核对':'');
  }
  previous.onclick=()=>{try{show(page-1)}catch{status.textContent='本页无法显示，请下载原件核对'}};
  next.onclick=()=>{try{show(page+1)}catch{status.textContent='本页无法显示，请下载原件核对'}};
  window.addEventListener('unload',()=>{if(blobUrl)URL.revokeObjectURL(blobUrl)});
  show(0);send('ready');
 }catch(_){send('error')}
});
send('loaded');
