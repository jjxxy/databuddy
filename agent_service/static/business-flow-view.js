'use strict';
/* Render only actual saved definitions and run receipts. Never interpret input
   descriptions or filenames as HTML, and never infer completion from a plan. */
(function(root){
 const KIND_LABELS={source:'获取资料',source_table:'读取表格资料',collect:'获取资料',parse:'解析资料',transform:'数据处理',data_workflow:'数据处理',table_workflow:'数据处理',model:'模型处理',skill:'使用工作方法',template:'使用模板',office:'生成办公文件',office_document:'生成办公文件',html:'生成网页报告',html_report:'生成网页报告',table_report_spec:'准备报告内容',output:'交付成果'};
 const STATE_LABELS={pending:'等待执行',queued:'等待执行',running:'正在执行',succeeded:'执行完成',completed:'执行完成',failed:'未完成',unknown:'结果待核对',cancelled:'已停止',skipped:'已跳过',blocked:'等待处理'};
 function node(document,tag,value,cls){const e=document.createElement(tag);if(value!==undefined&&value!==null)e.textContent=String(value);if(cls)e.className=cls;return e}
 function text(value,fallback){return typeof value==='string'&&value.trim()?value.trim():fallback}
 function render(container,options){
  const document=container.ownerDocument, plan=options?.plan, run=options?.run;
  container.replaceChildren();
  if(!plan||!Array.isArray(plan.nodes)){container.append(node(document,'p','尚无可展示的流程方案。','flow-empty'));return}
  const header=node(document,'div',null,'flow-view-header');
  header.append(node(document,'h2',text(plan.name,'业务流程')));
  const version=options?.version||{};
  const labels={candidate:'候选方案',published:'正式方案',history:'历史方案',draft:'草稿'};
  header.append(node(document,'span',labels[version.role]||'版本待确认','flow-version'));
  container.append(header);
  const notices=node(document,'div',null,'flow-notices');
  if(typeof version.commit==='string'&&version.commit)notices.append(node(document,'p','代码版本：'+version.commit));
  if(typeof version.digest==='string'&&version.digest)notices.append(node(document,'p','方案版本：'+version.digest));
  const matching=!!run&&!!(version.digest||version.commit)&&(!version.digest||run.plan_digest===version.digest)&&(!version.commit||run.commit===version.commit);
  if(run){
    notices.append(node(document,'p',matching?'运行状态：'+(STATE_LABELS[run.status]||'状态未知'):'所选运行与当前方案版本不一致，未展示步骤状态。'));
    if(matching&&typeof run.message==='string'&&run.message)notices.append(node(document,'p',run.message));
  }else notices.append(node(document,'p','当前展示的是保存的方案，尚未选择对应运行记录。'));
  notices.append(node(document,'p','模型及工作方法的输出可能变化；执行完成不等于业务结果已经核验。'));
  container.append(notices);
  const receipts=Array.isArray(run?.receipts)?run.receipts:Array.isArray(run?.steps)?run.steps:[];
  const sameRun=matching;
  const byId=new Map();if(sameRun)for(const receipt of receipts){const id=receipt?.node_id||receipt?.id;if(typeof id==='string'&&!byId.has(id))byId.set(id,receipt)}
  const ids=new Map();for(const item of plan.nodes){if(item&&typeof item.id==='string')ids.set(item.id,text(item.title,text(item.name,item.id)))}
  const list=node(document,'ol',null,'flow-step-list');
  for(const item of plan.nodes){
   if(!item||typeof item.id!=='string')continue;
   const receipt=byId.get(item.id), li=node(document,'li',null,'flow-step');
   const top=node(document,'div',null,'flow-step-heading');
   top.append(node(document,'strong',text(item.title,text(item.name,KIND_LABELS[item.kind]||'工作步骤'))));
   top.append(node(document,'span',receipt?(STATE_LABELS[receipt.status]||'状态未知'):sameRun?'本次未记录':'尚未选择运行','flow-step-state'));
   li.append(top);
   if(item.kind)li.append(node(document,'p',KIND_LABELS[item.kind]||'其他工作步骤','flow-step-kind'));
   if(typeof item.description==='string'&&item.description.trim())li.append(node(document,'p',item.description,'flow-step-description'));
   const dependencies=Array.isArray(item.depends_on)?item.depends_on.filter(x=>typeof x==='string'):[];
   if(dependencies.length)li.append(node(document,'p','依赖：'+dependencies.map(id=>ids.get(id)||id).join('、'),'flow-step-dependencies'));
   if(receipt){
    const details=node(document,'details',null,'flow-step-receipt');details.append(node(document,'summary','查看本步骤记录'));
    for(const [key,label] of [['started_at','开始时间'],['finished_at','结束时间'],['input_sha256','输入摘要'],['output_sha256','输出摘要']]){
     if(typeof receipt[key]==='string'&&receipt[key])details.append(node(document,'p',label+'：'+receipt[key]));
    }
    if(receipt.deterministic===false)details.append(node(document,'p','此步骤的输出可能因模型或外部服务变化。'));
    if(receipt.status==='unknown')details.append(node(document,'p','尚不能确认是否执行成功，请先核对结果；系统不会自动重复外部操作。'));
    if(typeof receipt.message==='string'&&receipt.message)details.append(node(document,'p',receipt.message));
    li.append(details);
   }
   list.append(li);
  }
  if(list.children.length)container.append(list);else container.append(node(document,'p','当前方案没有可展示的步骤。','flow-empty'));
 }
 root.AtlasBusinessFlowView={render};
 if(typeof module!=='undefined'&&module.exports)module.exports={render};
})(typeof window!=='undefined'?window:globalThis);
