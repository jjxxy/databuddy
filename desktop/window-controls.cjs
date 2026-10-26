'use strict';
// Window-local keyboard behavior stays available without a native menu bar.
function configureDesktopWindow(window,{openLineage,canOpenLineage=()=>false}={}) {
  window.setMenu(null);
  window.webContents.on('before-input-event',(event,input)=>{
    if(input.type!=='keyDown'||input.alt||input.meta)return;
    const contents=window.webContents,key=String(input.key||'').toLowerCase();
    if(input.control){
      const command={a:'selectAll',c:'copy',v:'paste',x:'cut',y:'redo',z:input.shift?'redo':'undo'}[key];
      if(command&&(!input.shift||key==='z')){event.preventDefault();contents[command]();return;}
      if(['0','+','=','-'].includes(key)){
        event.preventDefault();
        contents.setZoomLevel(key==='0'?0:Math.max(-3,Math.min(3,contents.getZoomLevel()+ (key==='-'?-1:1))));return;
      }
      if(key==='l'&&input.shift&&openLineage&&canOpenLineage()){event.preventDefault();void openLineage();}
    }else if(key==='f11'&&!input.shift){event.preventDefault();window.setFullScreen(!window.isFullScreen());}
  });
}
module.exports={configureDesktopWindow};
