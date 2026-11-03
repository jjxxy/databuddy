const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../../agent_service/static');let click,location='',toast='';
const button={addEventListener:(type,fn)=>{assert.equal(type,'click');click=fn}};
const state={projectId:'p_one',pending:null,sending:false};
const context={document:{getElementById:id=>id==='project-template-open'?button:null},window:{nexusBusiness:{getState:()=>state,toast:t=>{toast=t}}},location:{assign:s=>{location=s}},URLSearchParams};
vm.runInNewContext(fs.readFileSync(path.join(root,'project-template-entry.js'),'utf8'),context);
click();assert.equal(location,'/project-templates.html?project=p_one');location='';state.pending={request_id:'one'};click();assert.equal(location,'');assert.match(toast,/发送/);
const html=fs.readFileSync(path.join(root,'index.html'),'utf8');assert(html.includes('id="project-template-open"')&&html.includes('/project-template-entry.js?v=1'));
console.log(JSON.stringify({current_project_bound:true,pending_send_guard:true,entry_present:true}));
