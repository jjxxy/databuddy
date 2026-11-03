'use strict';
const fs=require('node:fs');
const path=require('node:path');

// Select a whole existing state directory; never merge or rename user data.
function chooseStateDirectory(appData,current){
 const legacy=path.join(appData,'Atlas DataBuddy');
 const exists=p=>{try{const s=fs.lstatSync(p);if(s.isSymbolicLink()||!s.isDirectory())throw Error('工作空间状态目录不是安全的普通文件夹，未修改数据。');return true}catch(e){if(e.code==='ENOENT')return false;throw e}};
 if(path.resolve(current)===path.resolve(legacy))return current;
 const oldExists=exists(legacy),newExists=exists(current);
 if(oldExists&&newExists)throw Error('同时存在 Atlas DataBuddy 和 Nexus WorkStudio 状态目录。请先备份并明确选择工作空间，未修改数据。');
 return oldExists?legacy:current;
}
module.exports={chooseStateDirectory};
