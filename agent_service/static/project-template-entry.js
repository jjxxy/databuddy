'use strict';
(function(){
 const button=document.getElementById('project-template-open');if(!button)return;
 button.addEventListener('click',()=>{
  const bridge=window.nexusBusiness,state=bridge?.getState?.();
  if(!state?.projectId){bridge?.toast?.('请先打开一个项目');return}
  if(state.sending||state.pending){bridge?.toast?.('请先确认当前消息是否发送成功');return}
  location.assign('/project-templates.html?'+new URLSearchParams({project:state.projectId}));
 });
})();
