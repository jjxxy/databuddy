'use strict';
/* Project-scoped review UI. All source labels/values are data, never markup. */
const wbEl = selector => document.querySelector(selector);
const wbParams = new URLSearchParams(location.search);
const wbProject = wbParams.get('project') || '';
const wbState = {
  token: sessionStorage.getItem('atlas_token') || '', workflowId: wbParams.get('workflow') || '',
  workflows: [], workflow: null, result: null, resultId: '', offset: 0, limit: 50,
  editorBase: '', editorDirty: false, editorConflict: false, busy: false,
  loading: 0, workflowLoading: false, resultLoading: 0, pollTimer: null, activeJob: '', latestJob: null,
  proposals: new Map(), proposalOffset: null, proposalTotal: 0, factsLoaded: false,
  facts: new Map(), factsOffset: null, factsLoading: false, factsGeneration: 0,
  sourceReviewKey:'',sourceReviewAcknowledged:'',
  catalogs: new Map(), catalogsLoaded: false, catalogsLoading: false,
  catalogOffset: null, catalogTotal: 0, catalogActions: new Set(),
};
let wbAuthClosed=false;const wbAuthRequests=new Set();
const wbVisual={loaded:false,loading:false,generation:0,offset:null,sources:[],blobUrls:new Set(),ocrGeneration:0,ocrBusy:false};
const wbSingle={key:'',generation:0,blobUrl:null,preview:null};
function wbReloadAuthDocument(){location.reload()}
function wbAuthError(){return Object.assign(Error('登录状态已变化，请在新页面继续'),{authTransition:true})}
function wbChangeAuthentication(nextToken=''){wbAuthClosed=true;clearTimeout(wbState.pollTimer);for(const c of wbAuthRequests)c.abort();wbAuthRequests.clear();wbLogin();for(const key of Object.keys(sessionStorage))if(key.startsWith('atlas_'))sessionStorage.removeItem(key);if(nextToken)sessionStorage.setItem('atlas_token',nextToken);wbReloadAuthDocument()}
const wbEnc = encodeURIComponent;
const wbRoot = () => '/api/projects/' + wbEnc(wbProject);
const wbWorkflowUrl = () => wbRoot() + '/data-workflows/' + wbEnc(wbState.workflowId);
function wbNode(tag, value = '', className = '') {
  const node = document.createElement(tag);node.textContent = value == null ? '' : String(value);
  if (className) node.className = className;return node;
}
function wbValue(value) {
  if (value == null) return '—';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
function wbNotice(text, error = false) {
  const target = wbEl('#workbench-notice');target.textContent = text || '';
  target.hidden = !text;target.classList.toggle('error', error);
}
function wbLogin(text = '') {
  clearTimeout(wbState.pollTimer);wbEl('#workbench-shell').hidden = true;
  wbEl('#workbench-login').hidden = false;wbEl('#workbench-login-error').textContent = text;
}
async function wbRequest(url, options = {}) {
  if(wbAuthClosed)throw wbAuthError();
  const headers = {Authorization: 'Bearer ' + wbState.token};
  const args = {...options, headers};
  if (Object.prototype.hasOwnProperty.call(options, 'body')) {
    headers['Content-Type'] = 'application/json';args.body = JSON.stringify(options.body);
  }
  let response;
  const controller=new AbortController();wbAuthRequests.add(controller);
  try {response = await fetch(url, {...args,signal:controller.signal});}
  catch (_) {if(wbAuthClosed)throw wbAuthError();throw new Error('暂时连不上工作空间。请检查网络后刷新状态。');}
  finally {wbAuthRequests.delete(controller);}
  if(wbAuthClosed)throw wbAuthError();
  if(response.status===401){wbChangeAuthentication();throw wbAuthError();}
  if (!response.ok) {
    let detail = '请求未完成，请稍后重试。';
    try {
      const value = await response.json();
      if (typeof value.detail === 'string') detail = value.detail;
      else if (Array.isArray(value.detail)) detail = value.detail.map(e => (e.loc || []).join('.') + '：' + (e.msg || '请检查此项')).join('\n');
    } catch (_) {}
    if (response.status === 401) wbLogin('访问口令已失效，请重新输入。');
    const explanations = {401:'访问口令已失效，请重新输入。',403:'当前账号无法进行这项操作。请使用有权限的账号打开。',
      404:'这项任务或结果暂时找不到。请从项目对话重新打开。',413:'这次提交的内容太大。请回到对话调整资料范围后再试。',
      422:'任务设置有一项需要调整。请回到对话，让 Agent 检查后再保存。',429:'当前工作较多，请稍后刷新状态再试。'};
    const message = explanations[response.status] || (response.status >= 500 ? '工作空间暂时没能完成操作。请稍后刷新；若仍有问题，可以回到对话让 Agent 检查。' :
      /[\u3400-\u9fff]/.test(detail) ? detail : '这次操作没有完成。请刷新状态后再试，或回到对话让 Agent 检查。');
    const error = new Error(message);error.status = response.status;error.detail = detail;throw error;
  }
  const value=options.binary ? response : await response.json();if(wbAuthClosed)throw wbAuthError();return value;
}
function wbDefinition(value, entries) {
  const dl = wbNode('dl', '', 'definition');
  for (const [label, key] of entries) {
    const item = typeof key === 'function' ? key(value) : value?.[key];
    if (item == null || item === '' || Array.isArray(item) && !item.length) continue;
    dl.append(wbNode('dt', label), wbNode('dd', wbValue(item)));
  }
  return dl;
}
function wbRunId(value) { return typeof value === 'string' ? value : value?.id || value?.run_id || ''; }
function wbPlanSteps() {return wbState.workflow?.steps || wbState.workflow?.document?.plan?.stages || wbState.workflow?.document?.plan?.steps || [];}
function wbStageName(step) {
  const names = {filter:'筛选相关记录',select:'保留需要的字段',drop:'删除不相关字段',reclassify:'按业务规则分类',
    arithmetic:'计算业务数值',aggregate:'按业务范围汇总',concat:'拼接全部记录',concatenate:'拼接全部记录',union_rows:'拼接全部记录',
    lookup:'按关联规则匹配记录',sql:'按已保存的查询规则处理'};
  return step.label || step.name || names[step.kind || step.operation] || '处理业务数据';
}
function wbDateTime(value) {
  if (!value) return '';
  const date = new Date(value);return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN',{hour12:false});
}
function wbFailure(job) {
  if (!job || !['failed','cancelled'].includes(job.status)) return null;
  const failure = job.workflow_failure || job.output?.workflow_failure || {};
  const code = failure.code || job.error_code || 'TASK_FAILED';
  const messages = {
    INPUT_MISMATCH:['任务需要的来源没有全部对应上。','回到对话检查每份来源绑定，再运行任务。'],
    SOURCE_HASH_MISSING:['旧来源缺少可核对的原件摘要。','重新上传原件以建立带摘要的来源修订，再运行任务。'],
    SOURCE_CONTENT_CHANGED:['登记后的来源原件内容发生了变化。','重新读取并登记新的来源修订，核对变化后再运行；不要继续使用旧修订标签。'],
    INVALID_TABLE:['来源没有提供完整的表格记录。','检查来源读取范围，让 Agent 重新读取完整表格后再运行。'],
    INVALID_DECIMAL:['有数值无法按声明的格式准确读取。','检查金额或比率的原始写法与格式，修正后再运行。'],
    DECIMAL_PRECISION:['有数值超出了当前处理规则的精度范围。','让 Agent 核对数值与精度要求，保留原值后调整处理方案。'],
    TYPE_MISMATCH:['部分内容与字段声明的类型不一致。','检查该字段应是金额、日期还是文字，再统一口径。'],
    NULL_VALUE:['本次计算需要的字段含有缺失值。','确认缺失值的业务含义并补全或制定处理规则；不要直接当作零。'],
    GRAIN_NULL:['用于识别业务记录的维度有缺失。','补全识别记录所需的维度，或重新确认任务的业务粒度。'],
    GRAIN_DUPLICATE:['用于识别业务记录的维度出现了重复。','先核对重复记录，再决定去重、汇总或调整业务粒度。'],
    GRAIN_LOST:['这一步会丢失识别业务记录所需的维度。','保留这些维度，或明确需要汇总到哪个业务粒度。'],
    SCHEMA_MISMATCH:['来源或处理后的字段与保存的做法不一致。','检查是否换了表头、页签或字段，让 Agent 对齐后再运行。'],
    SCHEMA_CHANGED:['来源的字段发生了变化。','让 Agent 重新查看来源并核对字段，再更新任务做法。'],
    OUTPUT_SCHEMA_CHANGED:['结果字段的类型发生了变化。','先核对新的字段类型与业务含义，再运行新版本。'],
    MISSING_COLUMN:['当前做法引用的字段缺失或重复。','核对表头与字段名称，让 Agent 修正引用。'],
    FORMULA_CACHE_MISSING:['原表的公式没有可使用的计算结果。','让 Agent 查看公式证据；提供在 Excel 中重新计算并保存的原件，或明确的原始值。'],
    SUBTOTAL_ROWS_PRESENT:['同一数据区域包含明细和汇总候选行，直接汇总可能重复计数。','让 Agent 核对行粒度，明确读取范围或保留、排除汇总行的规则。'],
    HIDDEN_ROWS_PRESENT:['原表含隐藏行，尚未明确是否参与计算。','让 Agent 展示这些行的影响，再选择包含还是排除。'],
    LOCALIZED_CURRENCY_REQUIRED:['金额符号与已声明币种不一致，或币种尚未明确。','核对原文币种；不同币种需明确汇率，不能直接合并。'],
    LOCALIZED_VALUE_INVALID:['金额或日期文本不符合已声明的转换规则。','让 Agent 定位原始文本，修正格式规则或单列异常；不要把失败值补为零。'],
    LOCALIZED_PERCENT_SIGN_REQUIRED:['百分比文本的表示不明确。','确认原值是百分比还是比率，再让 Agent 设置显式转换。'],
    LOCALIZED_MISSING_VALUE:['数值缺失，当前规则不允许自动忽略。','明确缺失值应保留为空、补充来源还是作为异常列出，不能默认当作零。'],
    LOCALIZED_PRECISION:['转换结果超出精确计算支持的位数。','核对单位倍率和原始值；需要舍入时先明确业务精度。'],
    DUPLICATE_COLUMN:['这一步会产生重名字段。','给不同含义的字段使用不同名称后再运行。'],
    UNIT_MISMATCH:['参与计算的数值单位不一致。','先确认并保存换算规则，再进行计算。'],
    VALUE_FORMAT_MISMATCH:['比率、百分数或其他数值写法没有对齐。','确认百分数代表小数还是百分点，保存明确的转换规则。'],
    SEMANTIC_MISMATCH:['参与处理的字段在含义、单位或期间上不兼容。','回到对话核对业务口径，明确转换或汇总规则后再运行。'],
    NON_ADDITIVE:['这项指标尚未确认可以直接相加。','核对余额、比率等指标的汇总口径，再选择合适的处理方法。'],
    DIVIDE_BY_ZERO:['有一项计算的除数为零。','确认零值的业务含义，并明确这类记录如何处理。'],
    UNMAPPED_VALUE:['有来源值找不到对应的分类或映射。','先核对并补全未匹配值的处理规则，再运行任务。'],
    LOOKUP_CARDINALITY:['一条记录对应了多个目标，无法唯一关联。','先核对映射关系；如需拆分或分配，明确业务规则。'],
    ROW_BUDGET:['完整数据超过了这项任务的记录或单元格处理上限。','让 Agent 检查范围并调整处理方案，保持需要的数据完整。'],
    BYTE_BUDGET:['完整数据超过了这项任务的数据量上限。','让 Agent 检查来源范围与中间结果，调整方案后再运行。'],
    TIMEOUT:['本次任务没有在限定时间内完成。','让 Agent 检查数据量和处理步骤，调整方案后再运行。'],
    UNSAFE_SQL:['保存的查询规则不符合当前任务支持的范围。','让 Agent 调整查询规则并核对结果口径后再运行。'],
    NONDETERMINISTIC_SQL:['查询中的顺序没有明确，无法稳定复用结果。','让 Agent 补全明确的排序规则后再运行。'],
    SQL_FAILED:['保存的查询规则没有通过检查或执行。','让 Agent 检查字段和查询规则，再生成可核对的新结果。'],
    DATA_CONTRACT_FAILED:['结果没有通过保存的数值或完整性核对。','检查汇总数、记录数和原始数据，查明差异后再运行。'],
    DATA_CONTRACT_EXACTNESS:['保存的数值检查无法保证精确，已停止本次结果。','请让 Agent 从原始底稿重新读取，保留原始金额文本，并用整数或明确的小数字符串核对范围与对账规则后再运行。'],
  };
  const [genericReason,next] = job.status === 'cancelled' ? ['这次运行已取消。','需要继续时，可以重新运行这项任务。'] :
    messages[code] || ['这次任务没有完成。','回到对话，让 Agent 根据下方运行详情检查来源与任务做法。'];
  const detail=failure.message || (typeof job.error === 'string' ? job.error : '');
  // This specific server code is emitted only for controlled, redacted Chinese messages.
  const reason=job.status!=='cancelled'&&code==='DATA_CONTRACT_EXACTNESS'&&typeof detail==='string'&&detail.length<=300&&/[\u3400-\u9fff]/.test(detail)&&!/[\u0000-\u001f]/.test(detail)?detail:genericReason;
  return {code,reason,next,stage_id:failure.stage_id || null,message:detail};
}
function wbRenderTask() {
  const workflow = wbState.workflow, result = wbState.result, job = wbState.latestJob;
  const steps = wbPlanSteps(), inputs = workflow?.inputs || {}, entries = Array.isArray(inputs) ? inputs.map((v,i) => [v.name || '来源 '+(i+1),v]) : Object.entries(inputs);
  const failure = wbFailure(job), candidate = !!result && result.run_id===wbState.resultId && wbState.resultId === wbRunId(workflow?.candidate_run);
  const changed = candidate && result.plan_sha256 !== workflow?.draft_sha256;
  const taskState = wbState.workflowLoading && !workflow ? '正在读取任务' : !workflow ? '尚未选择任务' : wbState.activeJob ? ({queued:'等待执行',retrying:'等待重试'}[job?.status] || '正在运行') :
    wbState.editorConflict ? '做法需要合并' : wbState.editorDirty ? '做法待保存' : failure ? (job.status === 'cancelled' ? '已取消' : '需要检查') :
    workflow.refresh_error ? '需要检查来源' : result&&wbFactSemanticBlocked()?'需业务语义对齐':candidate ? '新结果待确认' : result ? '可以继续使用' : '可以开始';
  wbEl('#workbench-task-state').textContent = taskState;
  const time = wbDateTime(job?.finished_at || job?.started_at || job?.created_at);
  const runStatus = {queued:'等待执行',running:'正在运行',retrying:'等待重试',failed:'未完成',cancelled:'已取消',validated:'结果待确认',succeeded:'已完成'}[job?.status];
  wbEl('#workbench-latest-run').textContent = job ? '最近运行：' + (time ? time + ' · ' : '') + (runStatus || '状态待更新') : workflow ? '做法已保存，可重复使用' : '从左侧选择已保存的任务';
  wbEl('#workbench-source-count').textContent = workflow ? entries.length + ' 份' : '—';
  wbEl('#workbench-description').textContent = workflow ? '查看这项任务的来源、运行进展和保留结果；有变化时继续让 Agent 调整。' : '把常做的核对、汇总与报表保存成任务，需要时继续运行。';
  const next = !workflow ? ['从对话开始一项业务工作','告诉 Agent 你的目标，并提供相关资料；保存为任务后会出现在这里。'] :
    wbState.activeJob ? ['任务正在处理','可以稍后刷新状态。完成后会保留新结果，供你继续核对与使用。'] :
    wbState.editorConflict ? ['先合并其他会话的修改','本页内容已保留。请先复制要保留的修改，再重新载入最新做法后合并。'] :
    wbState.editorDirty ? ['先保存本页的做法修改','保存后再运行；如果不需要这些修改，可以在“任务做法”中重新载入。'] :
    failure ? ['先检查这次未完成的地方',failure.next] :
    workflow.refresh_error ? ['先检查来源的更新情况','回到对话检查来源是否可用，再继续重复执行。已有结果仍可查看。'] :
    result&&wbFactSemanticBlocked()?['先完成业务语义对齐','逐条事实的指标、单位、期间和可加性尚不能确认；当前结果仅供查看，不能采用或生成报告。'] :
    changed ? ['做法已经调整，请重新运行','当前新结果来自之前的做法。运行最新任务后，再核对并采用新结果。'] :
    candidate ? ['核对这次新结果','查看完整明细与来源，确认后点击“采用此结果”，方便以后继续使用。'] :
    result ? ['继续使用这份结果','可以查看和下载完整结果；需要读取最新来源时，点击“运行任务”。'] :
    ['运行这项任务','来源与做法已保存。点击“运行任务”，完成后就能查看和核对结果。'];
  wbEl('#workbench-next-title').textContent = next[0];wbEl('#workbench-next-text').textContent = next[1];
  wbEl('#workbench-open-result').disabled = !result || wbState.workflowLoading;
  wbEl('#workbench-open-result').textContent = candidate ? '核对新结果' : '查看保留结果';
  wbEl('#workbench-open-rules').disabled = !workflow || wbState.workflowLoading;
  wbEl('#workbench-task-summary').textContent = workflow ? '读取 ' + entries.length + ' 份已绑定来源，按 ' + steps.length + ' 个保存的步骤生成结果。' : '选择任务后查看已保存的做法。';
  const summary = wbEl('#workbench-task-steps');summary.replaceChildren();
  for (const step of steps) summary.append(wbNode('li',wbStageName(step)));
  const sources = wbEl('#workbench-planned-sources');sources.replaceChildren();
  for (const [name,input] of entries) {
    const binding = input.binding || input, options = binding.options || {};
    const item = wbNode('div','','source-item');item.append(wbNode('strong',name));
    item.append(wbDefinition(binding,[['数据来源',v => v.source_id || v.result_id || '尚未绑定']]));
    item.append(wbDefinition(options,[['页签','sheet'],['读取区域','cell_range'],['起始数据行','data_start_row'],['结束数据行','data_end_row']]));sources.append(item);
  }
  if (!sources.children.length) sources.append(wbNode('p',workflow ? '这项任务尚未提供来源说明。可以回到对话检查。' : '选择任务后显示已绑定的来源。','muted'));
  wbEl('#workbench-failure').hidden = !failure;
  if (failure) {
    wbEl('#workbench-failure-title').textContent = job.status === 'cancelled' ? '这次运行已取消' : '这次任务没有完成';
    wbEl('#workbench-failure-reason').textContent = failure.reason;
    wbEl('#workbench-failure-next').textContent = '下一步：' + failure.next;
    const frozenSteps = job.workflow_document?.plan?.stages;
    const failureSteps = Array.isArray(frozenSteps) ? frozenSteps : job.commit && job.commit === workflow?.draft_sha256 ? steps : [];
    const index = failureSteps.findIndex(step => step.id === failure.stage_id);
    wbEl('#workbench-failure-stage').textContent = failure.stage_id?.startsWith('input:') ? '停在来源读取与核对阶段。' : index >= 0 ? '停在当次任务的第 ' + (index+1) + ' 步：' + wbStageName(failureSteps[index]) : '本页无法确认当次运行的具体步骤，可展开运行详情查看记录。';
    wbEl('#workbench-failure-details').textContent = JSON.stringify({run_id:job.id,stage_id:failure.stage_id,code:failure.code,message:failure.message},null,2);
  }
}
function wbSetBusy(value) { wbState.busy = value;wbRenderButtons(); }
function wbCurrentProject(){return !wbAuthClosed && new URLSearchParams(location.search).get('project')===wbProject;}
function wbFactSemanticBlocked(){const reasons=wbState.result?.context?.semantic_review_reasons;return Array.isArray(reasons)&&reasons.includes('FACT_SEMANTIC_ALIGNMENT_REQUIRED');}
function wbUnalignedFactCandidate(){return wbFactSemanticBlocked()&&wbState.result?.run_id===wbState.resultId&&wbRunId(wbState.workflow?.candidate_run)===wbState.resultId;}
function wbSingleKey(){const r=wbState.result;return wbCurrentProject()&&wbUnalignedFactCandidate()&&/^[a-f0-9]{64}$/.test(r?.manifest_sha256||'')&&/^[a-f0-9]{64}$/.test(r?.plan_sha256||'')?JSON.stringify([wbProject,wbState.workflowId,wbState.resultId,r.plan_sha256,r.manifest_sha256]):'';}
function wbSingleRelease(){if(wbSingle.blobUrl){URL.revokeObjectURL(wbSingle.blobUrl);wbSingle.blobUrl=null;}}
function wbSyncSingleFact(){
  const key=wbSingleKey();if(key!==wbSingle.key){wbSingle.key=key;wbSingle.generation++;wbSingle.preview=null;wbSingleRelease();wbEl('#single-fact-body').replaceChildren();wbEl('#single-fact-status').hidden=true;wbEl('#single-fact-load').disabled=false;}
  wbEl('#single-fact-panel').hidden=!key;
}
function wbCandidateReviewKey(){
  const result=wbState.result,candidate=wbRunId(wbState.workflow?.candidate_run);
  if(!wbCurrentProject()||!result||result.run_id!==wbState.resultId||candidate!==result.run_id||
     result.context?.requires_source_review!==true||wbFactSemanticBlocked()||result.plan_sha256!==wbState.workflow?.draft_sha256||
     !/^[a-f0-9]{64}$/.test(result.manifest_sha256||''))return '';
  return JSON.stringify([wbProject,wbState.workflowId,result.run_id,result.plan_sha256,result.manifest_sha256]);
}
function wbSyncSourceReview(){
  const result=wbState.result,candidate=!!result&&result.run_id===wbState.resultId&&wbState.resultId===wbRunId(wbState.workflow?.candidate_run);
  const blocked=candidate&&wbFactSemanticBlocked(),required=candidate&&result.context?.requires_source_review===true&&!blocked,key=required?wbCandidateReviewKey():'';
  if(key!==wbState.sourceReviewKey){wbState.sourceReviewKey=key;wbState.sourceReviewAcknowledged='';wbEl('#workbench-source-review-ack').checked=false;}
  wbEl('#workbench-source-review').hidden=!required;wbEl('#workbench-source-review-error').hidden=!required||!!key;
  wbEl('#workbench-fact-semantic-warning').hidden=!blocked;
  wbEl('#workbench-candidate-title').textContent=blocked?'结果需业务语义对齐':'新结果待你确认';
  wbEl('#workbench-candidate-description').textContent=blocked?'这份结果可以查看，暂不能采用。':'核对数值与来源后，可以将它作为以后继续使用的结果。';
  wbEl('#workbench-source-review-ack').disabled=!key||wbState.busy;
}
function wbRenderButtons() {
  const hasWorkflow = !!wbState.workflow && !wbState.workflowLoading;
  for (const button of document.querySelectorAll('[data-workflow]')) button.disabled = wbState.busy || wbState.workflowLoading;
  wbEl('#workbench-run').disabled = !hasWorkflow || wbState.busy || wbState.editorDirty || !!wbState.activeJob;
  wbEl('#workbench-save').disabled = !hasWorkflow || !wbState.editorDirty || wbState.editorConflict || wbState.busy;
  wbEl('#workbench-reset').disabled = !hasWorkflow || wbState.busy;
  wbEl('#workbench-refresh').disabled = wbState.busy || wbState.workflowLoading;
  const candidate = wbRunId(wbState.workflow?.candidate_run);
  const sourceReviewRequired=wbState.result?.context?.requires_source_review===true,key=sourceReviewRequired?wbCandidateReviewKey():'';
  wbEl('#workbench-publish').disabled = !hasWorkflow || wbState.busy || wbState.editorDirty || !wbState.result ||
    !wbCurrentProject() || !candidate || candidate !== wbState.resultId || wbState.result.run_id!==wbState.resultId ||
    wbState.result.plan_sha256 !== wbState.workflow?.draft_sha256 || wbFactSemanticBlocked() ||
    sourceReviewRequired&&(!key||wbState.sourceReviewAcknowledged!==key||!wbEl('#workbench-source-review-ack').checked);
  wbEl('#workbench-source-review-ack').disabled=!key||wbState.busy;
  for (const format of ['xlsx', 'csv', 'handoff']) {
    const button=wbEl('#workbench-download-' + format);button.hidden=wbUnalignedFactCandidate();
    button.disabled = !hasWorkflow || !wbState.result || wbState.busy || wbUnalignedFactCandidate();
  }
}
function wbRenderNav() {
  const target = wbEl('#workbench-workflows');target.replaceChildren();
  for (const workflow of wbState.workflows) {
    const id = workflow.workflow_id || workflow.id;
    const button = wbNode('button');button.append(wbNode('span',workflow.name || '业务任务'));button.type = 'button';
    const status = workflow.refresh_error ? '需要检查' : workflow.candidate_run ? '新结果待确认' : workflow.published_run ? '已有保留结果' : '做法已保存';
    button.append(wbNode('small',status));
    button.dataset.workflow = id;button.setAttribute('aria-current', String(id === wbState.workflowId));
    button.onclick = () => {
      if (wbState.busy || wbState.workflowLoading) {wbNotice('正在读取或保存当前任务，请稍候再切换。');return;}
      if (wbState.editorDirty) { wbNotice('当前规则有未保存修改。请先保存，或放弃本页修改后再切换任务。', true);return; }
      wbLoadWorkflow(id).catch(error => wbNotice(error.message, true));
    };
    target.append(button);
  }
  wbEl('#workbench-workflows-empty').hidden = !!wbState.workflows.length;
}
function wbPopulateEditor(workflow, preserve) {
  if (preserve && wbState.editorDirty) {
    if (workflow.draft_sha256 !== wbState.editorBase) {
      wbState.editorConflict = true;wbEl('#workbench-editor-state').textContent = '规则已被其他会话更新。本页修改已保留，请重新载入后合并。';
    }
    return;
  }
  wbEl('#workbench-plan').value = JSON.stringify(workflow.document || {}, null, 2);
  wbState.editorBase = workflow.draft_sha256 || '';wbState.editorDirty = false;wbState.editorConflict = false;
  wbEl('#workbench-editor-state').textContent = '当前规则已保存。修改后需要重新运行核对。';
}
function wbRenderRules() {
  const steps = wbPlanSteps();
  const target = wbEl('#workbench-steps');target.replaceChildren();
  for (const step of steps) {
    const item = wbNode('li');item.append(wbNode('strong', wbStageName(step)));
    const details = [];
    const inputs = step.inputs || step.depends_on || (step.input ? [step.input] : []);
    if (inputs.length) details.push('使用 ' + wbValue(inputs));
    if (step.group_by?.length) details.push('汇总粒度 ' + step.group_by.join('、'));
    if (step.description || step.meaning) details.push(step.description || step.meaning);
    item.append(wbNode('p', details.join('；')));
    const more = document.createElement('details');more.append(wbNode('summary', '查看本步规则'));
    more.append(wbNode('pre', JSON.stringify(step, null, 2)));item.append(more);target.append(item);
  }
  wbEl('#workbench-steps-empty').hidden = !!steps.length;
}
function wbRenderSources() {
  const target = wbEl('#workbench-sources');target.replaceChildren();
  const context = wbState.result?.context;
  if (wbState.result) {
    const evidence = context?.input_evidence || {};
    for (const [name, value] of Object.entries(evidence)) {
      const item = wbNode('div', '', 'source-item');item.append(wbNode('strong', name));
      item.append(wbDefinition(value, [['来源','source_id'],['页签','sheet'],['完整记录数','rows']]));
      if (value.selection) item.append(wbDefinition(value.selection, [['页签','sheet'],['区域','cell_range'],['数据区域','data_range']]));
      const details = document.createElement('details');details.append(wbNode('summary','查看本结果的来源依据'));
      details.append(wbNode('pre',JSON.stringify(value,null,2)));item.append(details);target.append(item);
    }
    if (!target.children.length) target.append(wbNode('p','本结果未提供可展示的来源说明，可以下载完整结果与依据查看。','muted'));
    if (context && Object.keys(context).length) {
      const details = document.createElement('details');details.append(wbNode('summary','查看本结果的范围与依据'));
      const contextEvidence={...context};delete contextEvidence.data_contract;
      if(context.data_contract)details.append(wbNode('p','验收检查状态请看结果提示；原始记录保留在完整结果与依据中。','muted'));
      details.append(wbNode('pre',JSON.stringify(contextEvidence,null,2)));target.append(details);
    }
    return;
  }
  const inputs = wbState.workflow?.inputs || {};
  const entries = Array.isArray(inputs) ? inputs.map((v, i) => [v.name || '来源 ' + (i + 1), v]) : Object.entries(inputs);
  for (const [name, value] of entries) {
    const item = wbNode('div','','source-item');item.append(wbNode('strong',name+'（方案计划使用）'));
    item.append(wbDefinition(value.binding || value,[['来源','source_id'],['上一份结果','result_id']]));
    item.append(wbDefinition(value.schema || {},[['含义','meaning'],['粒度','grain'],['来源位置','source_ref']]));target.append(item);
  }
  if (!target.children.length) target.append(wbNode('p','尚无来源说明。','muted'));
}

function wbRenderStages() {
  const target = wbEl('#workbench-stages');target.replaceChildren();
  const stages = wbState.result?.stages || [];
  for (const stage of stages) {
    // A result belongs to the frozen run, not to the currently edited draft.
    // Old receipts may not include an operation; do not guess from the draft.
    const recorded = stage.lineage && typeof stage.lineage === 'object' && !Array.isArray(stage.lineage) ? stage.lineage : {};
    const kind = recorded.kind || stage.kind;
    const label = stage.label || stage.name || recorded.label || recorded.name || (kind ? wbStageName({kind}) : '步骤类型未记录');
    const item = wbNode('div', '', 'stage-item');item.append(wbNode('strong', label));
    item.append(wbDefinition(stage, [['状态','status'],['输入记录','input_rows'],['输出记录','output_rows'],['处理说明','description']]));
    const more = document.createElement('details');more.append(wbNode('summary', '查看校验与处理记录'));
    more.append(wbNode('pre', JSON.stringify(stage, null, 2)));item.append(more);target.append(item);
  }
  if (!target.children.length) target.append(wbNode('p', '运行后显示各步处理记录。', 'muted'));
}
function wbRenderFields() {
  const target = wbEl('#workbench-fields');target.replaceChildren();
  for (const field of wbState.result?.schema || []) {
    const semantic = field.semantic || field;
    const details = document.createElement('details');details.className = 'field-definition';
    details.append(wbNode('summary', field.label || field.name));
    details.append(wbDefinition({...semantic, name:field.name}, [['字段','name'],['含义','meaning'],['单位','unit'],['期间','period'],['粒度',v => v.grain || wbState.result?.context?.output_semantics?.grain || wbState.result?.context?.grain],['数值格式',v => v.value_format || v.data_type || v.type],['源位置','provenance']]));
    target.append(details);
  }
  if (!target.children.length) target.append(wbNode('p', '有可用结果后显示字段含义。', 'muted'));
}
function wbRenderResult() {
  const result = wbState.result;const workflow = wbState.workflow;
  wbEl('#workbench-title').textContent = workflow?.name || '我的业务任务';document.title = (workflow?.name || '我的业务工作空间') + ' · Atlas';
  const candidate = !!result && result.run_id===wbState.resultId && wbState.resultId === wbRunId(workflow?.candidate_run);
  wbEl('#workbench-result-state').textContent = !result ? '尚无结果' : wbFactSemanticBlocked() ? '需业务语义对齐' : candidate ? '新结果待确认' : '已采用';
  wbEl('#workbench-version').textContent = result ? '来源与做法随结果保留' : '运行后可以查看';
  wbEl('#workbench-record-count').textContent = result ? wbValue(result.total) + ' 条完整记录' : '';
  wbEl('#workbench-schedule').textContent = workflow?.cron ? '按计划重复' : workflow?.watch_sources ? '来源变化后运行' : '按需运行';
  const nextDue = wbDateTime(workflow?.next_due);
  wbEl('#workbench-schedule-note').textContent = workflow?.refresh_error ? '自动更新需要检查：' + workflow.refresh_error : workflow?.cron ?
    (nextDue ? '下次：' + nextDue : '按已保存的时间安排执行') + (workflow.watch_sources ? '；同时跟踪已接入的来源版本' : '') :
    workflow?.watch_sources ? '跟踪已接入的来源版本变化' : '也可以回到对话安排定时执行';
  wbEl('#workbench-candidate').hidden = !candidate;wbSyncSourceReview();wbSyncSingleFact();
  const head = wbEl('#workbench-table-head'), body = wbEl('#workbench-table-body');head.replaceChildren();body.replaceChildren();
  const schema = result?.schema || [];
  if (schema.length) {
    const row = document.createElement('tr');
    for (const field of schema) {
      const cell = wbNode('th', field.label || field.name);cell.scope = 'col';
      const meaning = field.semantic || field;
      if (meaning.unit) cell.append(wbNode('small', meaning.unit));row.append(cell);
    }
    head.append(row);
  }
  for (const record of result?.rows || []) {
    const row = document.createElement('tr');
    for (const field of schema) {
      const cell = document.createElement('td'), value = wbValue(record[field.name]);
      cell.append(wbNode('div', value, 'data-cell'));cell.title = value;row.append(cell);
    }
    body.append(row);
  }
  wbEl('#workbench-result-empty').hidden = !!result?.rows?.length;
  wbEl('#workbench-result-empty').textContent = result ? '当前结果没有业务记录。' : '尚无可查看的结果，运行后可在这里核对。';
  const total = result?.total;
  wbEl('#workbench-page-info').textContent = !result ? '尚无结果' : total === 0 ? '共 0 条' :
    '当前第 ' + (wbState.offset + 1) + '–' + (wbState.offset + (result.rows?.length || 0)) + ' 条，共 ' + wbValue(total) + ' 条';
  wbEl('#workbench-result-help').textContent = result ? wbUnalignedFactCandidate()?'当前仅展示这一页；该候选尚未完成业务语义对齐，暂不提供批量下载。':
    '当前仅展示这一页，完整数据与依据可通过下载获取。' + (result.context?.requires_semantic_review ? ' 部分字段含义或映射仍待核对。' : '') : '结果按页展示，下载可获取完整数据。';
  const contract=result?.context?.data_contract,checks=Array.isArray(contract?.checks)?contract.checks:[];
  const numericEvidence=contract?.numeric_policy==='exact_decimal_v1'&&Number.isSafeInteger(contract?.rows)&&contract.rows>=0&&Number.isSafeInteger(result?.total)&&result.total>=0&&contract.rows===result.total;
  const legacyNumeric=checks.some(check=>typeof check?.name==='string'&&/^(range|total):/.test(check.name))&&!numericEvidence;
  const failedCheck=checks.some(check=>check?.status==='fail'),substantive=checks.filter(check=>typeof check?.name==='string'&&check.name.trim()&&check.name!=='row_count');
  const notice=wbEl('#workbench-trust-notice');notice.hidden=!result;
  notice.textContent=!result?'':wbFactSemanticBlocked()?'此结果包含尚未完成业务语义对齐的事实数值，指标、单位、期间及可加性无法确认；仅供查看，不得用于正式财务计算、报告或采用。':
    legacyNumeric?'数值范围或对账检查的精度或记录范围未确认，请从原始资料重新运行并复核；范围检查本身不检查缺失值。':
    !checks.length?'未记录可验证的业务验收规则；请核对缺失值、来源、计算口径与结果。':failedCheck?'保存记录显示部分检查未通过，请先核对来源、计算口径与结果。':
    !substantive.length?'未记录可核对的行数范围或其他业务验收规则；请核对缺失值、来源、计算口径与结果。':'已记录的检查不代表来源齐全或财务口径正确；范围检查本身不检查缺失值，请确认是否另有缺失值检查。';
  const details=wbEl('#workbench-trust-checks'),list=wbEl('#workbench-trust-check-list');list.replaceChildren();details.hidden=!result||!checks.length;
  for(const check of checks){
    const name=typeof check?.name==='string'?check.name:'名称未记录',numeric=/^(range|total):/.test(name);
    const label=name.startsWith('range:')?'数值范围：'+name.slice(6):name.startsWith('total:')?'对账总额：'+name.slice(6):name;
    const status=name==='row_count'?'未记录行数范围，需核对':numeric&&!numericEvidence?'待重新复核':check?.status==='pass'?'已记录通过':check?.status==='fail'?'未通过':'状态未记录';
    list.append(wbNode('li',label+'：'+status));
  }


  wbEl('#workbench-prev').disabled = !result || wbState.offset === 0 || wbState.busy;
  wbEl('#workbench-next').disabled = !result || result.next_offset == null || wbState.busy;
  wbRenderSources();wbRenderStages();wbRenderFields();wbRenderTask();wbRenderButtons();
}
async function wbLoadResult(offset = 0) {
  const requestId = ++wbState.resultLoading;
  if (!wbState.resultId) {wbState.result = null;wbState.offset = 0;wbRenderResult();return;}
  const resultId = wbState.resultId;
  const result = await wbRequest(wbRoot() + '/data-results/' + wbEnc(resultId) + '?' + new URLSearchParams({offset:String(offset),limit:String(wbState.limit)}));
  if (requestId !== wbState.resultLoading || resultId !== wbState.resultId) return;
  wbState.result = result;wbState.offset = offset;wbRenderResult();
}
async function wbLoadWorkflow(id, options = {}) {
  const requestId = ++wbState.loading;
  const changed = id !== wbState.workflowId;
  wbState.workflowLoading = true;
  if (changed) {
    clearTimeout(wbState.pollTimer);wbState.activeJob = '';wbState.latestJob = null;wbState.workflow = null;wbState.result = null;wbState.resultId = '';
    wbState.editorBase = '';wbState.editorDirty = false;wbState.editorConflict = false;wbEl('#workbench-plan').value = '';wbEl('#workbench-editor-details').open = false;
    wbEl('#workbench-editor-state').textContent = '正在读取任务做法…';wbRenderRules();
  }
  wbState.workflowId = id;wbRenderNav();
  wbRenderResult();
  try {
    if (!id) {wbState.workflow = null;wbState.result = null;wbState.resultId = '';wbRenderResult();return;}
    const workflow = await wbRequest(wbWorkflowUrl());
    if (requestId !== wbState.loading) return;
    wbState.workflow = workflow;wbPopulateEditor(workflow, options.preserveEditor && !changed);wbRenderRules();
    const previousResult = wbState.resultId;
    wbState.resultId = wbRunId(workflow.candidate_run) || wbRunId(workflow.published_run);
    wbRenderResult();
    await wbLoadResult(options.keepOffset && previousResult === wbState.resultId ? wbState.offset : 0);
    if (requestId !== wbState.loading) return;
    const nextUrl = new URL(location.href);nextUrl.searchParams.set('workflow', id);history.replaceState(null, '', nextUrl);
    if (!wbState.activeJob && options.scanJobs !== false) await wbRestoreRunningState();
  } finally {
    if (requestId === wbState.loading) {wbState.workflowLoading = false;wbRenderTask();wbRenderButtons();}
  }
}
async function wbRestoreRunningState() {
  const workflowId = wbState.workflowId;
  try {
    const value = await wbRequest('/api/runs?' + new URLSearchParams({target:workflowId,limit:'20'}));
    if (workflowId !== wbState.workflowId) return;
    const jobs = Array.isArray(value) ? value : value.items || [];
    wbState.latestJob = jobs[0] || null;
    const active = jobs.find(job => ['queued','running','retrying'].includes(job.status));
    if (active) {wbState.activeJob = active.id;wbSchedulePoll();}
    wbRenderResult();
  } catch (error) {
    if (error.status !== 401) wbNotice('结果已加载，暂时无法取得后台运行状态。可以稍后刷新。',true);
  }
}
async function wbRefresh() {
  const list = await wbRequest(wbRoot() + '/data-workflows');wbState.workflows = list.items || [];
  wbRenderNav();
  const id = wbState.workflowId || wbState.workflows[0]?.workflow_id || wbState.workflows[0]?.id || '';
  await wbLoadWorkflow(id, {preserveEditor:true,keepOffset:true});
}
async function wbSave() {
  if (!wbState.workflow || wbState.workflowLoading || !wbState.editorDirty || wbState.editorConflict || wbState.busy) return;
  let document;
  try {document = JSON.parse(wbEl('#workbench-plan').value);} catch (_) {wbNotice('规则的填写格式有误。可以回到对话让 Agent 修改，或检查展开规则中的括号和逗号。', true);return;}
  wbSetBusy(true);
  try {
    await wbRequest(wbWorkflowUrl(), {method:'PUT',body:{expected_sha256:wbState.editorBase,document}});
    wbState.editorDirty = false;await wbLoadWorkflow(wbState.workflowId);
    wbNotice('规则已保存。运行后可以核对新结果。');
  } catch (error) {
    if (error.status === 409) {
      wbState.editorConflict = true;wbEl('#workbench-editor-state').textContent = '规则版本已变化，本页修改已保留。请重新载入最新规则后合并。';
      wbNotice('这份规则已被更新，本次没有覆盖其他会话的修改。本页内容已保留；请复制需要保留的内容，再重新载入最新规则后合并。', true);
    } else wbNotice(error.message, true);
  } finally {wbSetBusy(false);wbRenderTask();}
}
function wbSchedulePoll(delay = 3000) {
  clearTimeout(wbState.pollTimer);
  if (!wbState.activeJob) return;
  wbState.pollTimer = setTimeout(wbPoll, delay);
}
async function wbPoll() {
  if (!wbState.activeJob) return;
  const workflowId = wbState.workflowId, jobId = wbState.activeJob;
  try {
    const value = await wbRequest('/api/runs?' + new URLSearchParams({target:workflowId,limit:'20'}));
    if (workflowId !== wbState.workflowId || jobId !== wbState.activeJob) return;
    const jobs = Array.isArray(value) ? value : value.items || [];
    const job = jobs.find(j => j.id === jobId);
    if (job) wbState.latestJob = job;
    if (job && ['failed','cancelled'].includes(job.status)) {
      wbState.activeJob = '';
      await wbLoadWorkflow(workflowId, {preserveEditor:true,scanJobs:false});
      wbNotice(wbFailure(job).reason + ' 请在“任务概况”查看下一步。',true);return;
    }
    if (job && ['validated','succeeded'].includes(job.status)) {
      wbState.activeJob = '';await wbLoadWorkflow(workflowId, {preserveEditor:true,scanJobs:false});
      wbNotice('运行已完成，可以核对新结果、来源和处理记录。');return;
    }
    wbRenderTask();
    wbSchedulePoll(document.hidden ? 10000 : 3000);
  } catch (error) {
    wbNotice(error.status === 401 ? error.message : '正在重新连接运行状态。已接收的工作会继续处理。', true);
    if (error.status !== 401) wbSchedulePoll(10000);
  } finally {wbRenderButtons();}
}
async function wbRun() {
  if (!wbState.workflow || wbState.workflowLoading || wbState.busy || wbState.editorDirty || wbState.activeJob) return;
  wbSetBusy(true);
  try {
    const run = await wbRequest(wbWorkflowUrl() + '/run', {method:'POST',body:{}});
    wbState.activeJob = run.id;wbState.latestJob = run;wbNotice('任务已接收。完成后会保留新结果，供你核对与使用。');wbRenderResult();wbSchedulePoll(1000);
  } catch (error) {wbNotice(error.message, true);} finally {wbSetBusy(false);}
}
async function wbPublish() {
  wbRenderButtons();
  if (wbEl('#workbench-publish').disabled || !wbState.result) return;
  const result=wbState.result,workflowId=wbState.workflowId,runId=wbState.resultId,token=wbState.token;
  const plan=result.plan_sha256,handoff=result.manifest_sha256,requiresReview=result.context?.requires_source_review===true;
  const key=requiresReview?wbCandidateReviewKey():'';
  if(requiresReview&&(!key||wbState.sourceReviewAcknowledged!==key||!wbEl('#workbench-source-review-ack').checked))return;
  const body={run_id:runId,expected_plan_sha256:plan};
  if(requiresReview){body.confirm_source_review=true;body.expected_handoff_sha256=handoff;}
  const current=()=>wbCurrentProject()&&wbState.token===token&&wbState.workflowId===workflowId&&wbState.resultId===runId&&
    wbRunId(wbState.workflow?.candidate_run)===runId&&wbState.result?.run_id===runId&&wbState.result?.plan_sha256===plan&&
    (!requiresReview||wbState.result?.manifest_sha256===handoff);
  wbState.sourceReviewAcknowledged='';wbEl('#workbench-source-review-ack').checked=false;
  wbSetBusy(true);wbNotice('正在提交采用确认…');
  try {
    const adopted=await wbRequest(wbRoot() + '/data-results/' + wbEnc(runId) + '/publish', {method:'POST',body});
    if(!current())return;
    if(wbRunId(adopted?.published_run)!==runId)throw Error('采用结果尚未确认');
    await wbLoadWorkflow(workflowId, {preserveEditor:true});
    if(!wbCurrentProject()||wbState.token!==token||wbState.workflowId!==workflowId)return;
    if(wbRunId(wbState.workflow?.published_run)!==runId)throw Error('采用结果尚未确认');
    wbNotice('已采用这份结果。处理规则、数值和依据会一起保留。');
  } catch (error) {if(wbCurrentProject()&&wbState.token===token&&wbState.workflowId===workflowId&&
      (wbRunId(wbState.workflow?.candidate_run)===runId||wbRunId(wbState.workflow?.published_run)===runId))
    wbNotice(error.status>=400&&error.status<500?'本次请求未通过：'+error.message+' 请刷新任务并核对后再操作。':
      '未收到可确认的采用结果，请刷新任务核对状态后再操作。', true);}
  finally {if(wbCurrentProject()&&wbState.token===token)wbSetBusy(false);}
}
async function wbDownload(format) {
  if (!wbCurrentProject()||!wbState.result||wbState.result.run_id!==wbState.resultId||wbUnalignedFactCandidate()||wbState.workflowLoading||wbState.busy||!['xlsx','csv','handoff'].includes(format))return;
  const project=wbProject,workflow=wbState.workflowId,runId=wbState.resultId,token=wbState.token;
  const current=()=>wbCurrentProject()&&wbProject===project&&wbState.token===token&&wbState.workflowId===workflow&&wbState.resultId===runId&&wbState.result?.run_id===runId&&!wbUnalignedFactCandidate();
  wbSetBusy(true);
  try {
    const response = await wbRequest(wbRoot() + '/data-results/' + wbEnc(runId) + '/download?format=' + format, {binary:true});
    const blob = await response.blob();if(!current())return;const url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url;a.download = '数据结果-' + runId + {xlsx:'.xlsx',csv:'.csv',handoff:'.zip'}[format];
    document.body.append(a);a.click();a.remove();setTimeout(() => URL.revokeObjectURL(url), 1000);
    wbNotice('已准备完整结果下载。');
  } catch (error) {if(current())wbNotice(error.message, true);} finally {if(wbCurrentProject()&&wbState.token===token)wbSetBusy(false);}
}
function wbTab(name) {
  if (!['task','result','rules','semantics'].includes(name)) name = 'task';
  if (name === 'semantics' && !wbState.factsLoaded) wbLoadFacts().catch(error => wbNotice(error.message,true));
  if (name === 'semantics' && !wbState.catalogsLoaded) wbLoadCatalogs(false);
  if (name === 'semantics' && !wbVisual.loaded && !wbVisual.loading) wbRefreshVisual();
  for (const button of document.querySelectorAll('[data-tab]')) {
    const selected = button.dataset.tab === name;button.setAttribute('aria-selected', String(selected));button.tabIndex = selected ? 0 : -1;
    wbEl('#workbench-panel-' + button.dataset.tab).hidden = !selected;
  }
}
function wbOpenTab(name) {wbTab(name);wbEl('#workbench-tab-' + name).focus();}
function wbProposalTitle(record) {
  const status = {proposed:'待核对',approved:'已确认',rejected:'已拒绝'}[record.status] || record.status || '待核对';
  return [record.source_name || record.binding || record.mapping_id || '字段核对', status];
}
function wbFieldBlock(field, title) {
  const block = wbNode('div', '', 'proposal-block');block.append(wbNode('h3', title));
  block.append(wbDefinition(field || {}, [['名称','name'],['含义','meaning'],['类型','kind'],['单位','unit'],['期间','period'],['粒度','grain'],['数值格式','value_format'],['汇总方式','aggregation']]));
  return block;
}
function wbBlockerText(value) {
  const phrases = {unit_mismatch_requires_explicit_conversion:'单位不同，需要明确的换算规则',
    grain_mismatch_requires_explicit_rollup_or_crosswalk:'业务粒度不同，需要明确汇总或映射',
    period_mismatch:'期间或时点不同',value_format_mismatch:'数值格式不同',kind_mismatch:'字段类型不同',
    aggregation_mismatch:'汇总方式不同',business_meaning_requires_human_review:'业务含义需要人工核对',
    different_definition_text:'两份字段定义文字不同'};
  if (phrases[value]) return phrases[value];
  if (value.startsWith('source_missing:')) return '来源定义尚缺：' + value.slice(15);
  if (value.startsWith('target_missing:')) return '目标定义尚缺：' + value.slice(15);
  return value;
}
async function wbLoadProposal(record, details, body) {
  body.replaceChildren(wbNode('p', '正在读取完整定义…', 'muted'));
  try {
    const full = await wbRequest(wbRoot() + '/semantic-proposals/' + wbEnc(record.id));
    wbState.proposals.set(full.id, {...record,...full});wbRenderProposalBody({...record,...full}, details, body);
  } catch (error) {body.replaceChildren(wbNode('p', error.message, 'proposal-message error'));}
}
function wbRenderProposalBody(record, details, body) {
  body.replaceChildren();const payload = record.payload || {}, kind = record.kind;
  const locked = ['approved','rejected'].includes(record.status);
  let select = null;let chosen = null;
  if (kind === 'value_mapping') {
    body.append(wbDefinition(payload, [['来源字段','source_field_id'],['目标字段','target_field_id'],['映射版本','version'],['未匹配值处理','unmapped_policy']]));
    const table = document.createElement('table'), head = document.createElement('thead'), row = document.createElement('tr');
    row.append(wbNode('th','来源值'),wbNode('th','目标值'));head.append(row);table.append(head);
    const tbody = document.createElement('tbody');
    for (const entry of payload.entries || []) {const tr = document.createElement('tr');tr.append(wbNode('td',entry.source),wbNode('td',entry.target));tbody.append(tr);}
    table.append(tbody);const scroller = wbNode('div','','table-scroll');scroller.append(table);body.append(scroller);
    body.append(wbNode('p','需要修改映射时，可以回到项目对话生成新提案。','muted'));
  } else {
    const binding = record.catalog_binding;
    const origin = wbNode('section','','catalog-binding');
    if (binding) {
      origin.append(wbNode('h3',binding.trust_level === 'owner_reviewed' ? '目标来自 owner 已核阅语义包' : '目标语义包绑定'));
      origin.append(wbDefinition(binding,[['语义包','name'],['版本',v => v.version == null ? '未提供' : '第 '+v.version+' 版'],
        ['业务口径适用日','as_of_date'],['固定内容摘要','content_sha256']]));
      origin.append(wbNode('p','包的核阅不代表这次字段匹配已确认。请继续核对来源字段与目标定义。','muted'));
    } else {
      origin.append(wbNode('h3','临时目标定义，未绑定语义包'));
      origin.append(wbNode('p','以下目标来自本次提案，不能视为 owner 已核阅的业务口径。','muted'));
    }
    body.append(origin);
    const columns = wbNode('div','','proposal-content');columns.append(wbFieldBlock(payload.source,'来源字段'));
    const target = wbNode('div');target.append(wbNode('p','请选择目标字段后，核对它的完整定义。','muted'));columns.append(target);body.append(columns);
    const label = wbNode('label','要对齐到哪个字段');select = document.createElement('select');select.setAttribute('aria-label','选择目标字段');
    const blank = wbNode('option','请选择目标字段');blank.value = '';select.append(blank);
    for (const candidate of payload.candidates || []) {
      const option = wbNode('option',candidate.target.name + (candidate.state === 'blocked' ? '（条件不兼容）' : ''));
      option.value = candidate.target.id;select.append(option);
    }
    body.append(label,select);
    select.disabled = locked;
    select.onchange = () => {
      chosen = (payload.candidates || []).find(c => c.target.id === select.value) || null;
      target.replaceChildren(chosen ? wbFieldBlock(chosen.target,'目标字段') : wbNode('p','请选择目标字段后，核对它的完整定义。','muted'));
      const warning = body.querySelector('.proposal-warning');if (warning) warning.remove();
      if (chosen) {
        const reasons = [...(chosen.blockers || []),...(chosen.review_reasons || [])].map(wbBlockerText);
        if (reasons.length) body.append(wbNode('p',reasons.join('；'),'proposal-warning'));
      }
      approve.disabled = locked || !chosen || chosen.state === 'blocked';reject.disabled = locked || !chosen;
    };
  }
  const actions = wbNode('div','','review-actions'), approve = wbNode('button','确认此匹配','primary');approve.type = 'button';approve.dataset.review = 'approved';
  approve.disabled = locked || kind !== 'value_mapping';if (kind === 'value_mapping') approve.textContent = '确认此版本映射';
  const reject = wbNode('button','拒绝此建议');reject.type = 'button';reject.dataset.review = 'rejected';reject.disabled = locked || kind !== 'value_mapping';
  actions.append(approve);if (kind !== 'value_mapping') actions.append(reject);body.append(actions);
  const message = wbNode('p', locked ? '这份提案已有核对记录。' : '确认前请核对定义、单位、期间和粒度。','proposal-message');body.append(message);
  const decide = async decision => {
    if (locked || decision === 'approved' && approve.disabled || decision === 'rejected' && reject.disabled) return;
    approve.disabled = true;reject.disabled = true;if (select) select.disabled = true;
    try {
      const data = {expected_sha256:record.content_sha256,decision};
      if (select?.value) data.target_id = select.value;
      await wbRequest(wbRoot() + '/semantic-proposals/' + wbEnc(record.id) + '/review', {method:'POST',body:data});
      message.textContent = decision === 'approved' ? '已保存这份提案的核对记录。' : '已拒绝这份建议。';
      await wbLoadProposals(false);
    } catch (error) {
      message.classList.add('error');
      message.textContent = error.status === 409 ? '这份定义已发生变化，本次没有确认。请重新读取完整定义后核对。' : error.message;
      const reload = wbNode('button','重新读取定义');reload.type = 'button';reload.onclick = () => wbLoadProposal(record,details,body);body.append(reload);
    }
  };
  approve.onclick = () => decide('approved');reject.onclick = () => decide('rejected');
}
async function wbLoadProposals(append = false) {
  const offset = append ? wbState.proposalOffset : 0;if (offset == null && append) return;
  const data = await wbRequest(wbRoot() + '/semantic-proposals?' + new URLSearchParams({offset:String(offset),limit:'20'}));
  const list = wbEl('#workbench-proposals');if (!append) {list.replaceChildren();wbState.proposals.clear();}
  for (const record of data.items || []) {
    if (wbState.proposals.has(record.id)) continue;wbState.proposals.set(record.id,record);
    const details = document.createElement('details');details.className = 'proposal';details.dataset.proposal = record.id;
    const summary = document.createElement('summary'), labels = wbProposalTitle(record);
    summary.append(wbNode('span',labels[0]),wbNode('span',labels[1],'proposal-status'));details.append(summary);
    const body = wbNode('div','','proposal-body');details.append(body);let loaded = false;
    details.addEventListener('toggle', () => {if (details.open && !loaded) {loaded = true;wbLoadProposal(record,details,body);}});
    list.append(details);
  }
  wbState.proposalOffset = data.next_offset;wbState.proposalTotal = data.total ?? wbState.proposals.size;
  wbEl('#workbench-proposal-count').textContent = wbState.proposalTotal ? '(' + wbState.proposalTotal + ')' : '';
  wbEl('#workbench-proposals-empty').hidden = !!list.children.length;wbEl('#workbench-more-proposals').hidden = data.next_offset == null;
}
async function wbInitialize() {
  for (const id of ['back','chat','task-chat','failure-chat','rules-chat','catalog-chat']) wbEl('#workbench-' + id).href = '/?project=' + wbEnc(wbProject);
  if (!wbProject) {wbEl('#workbench-missing').hidden = false;return;}
  if (!wbState.token) {wbLogin();return;}
  wbEl('#workbench-login').hidden = true;wbEl('#workbench-shell').hidden = false;
  await Promise.all([wbRefresh(),wbLoadProposals(false)]);
  wbTab(wbParams.get('tab') || 'task');
}
wbEl('#workbench-login-form').addEventListener('submit', async event => {
  event.preventDefault();const nextToken=wbEl('#workbench-token').value.trim();
  if(!nextToken)return;wbEl('#workbench-token').value='';wbChangeAuthentication(nextToken);
});
wbEl('#workbench-refresh').onclick = () => wbRefresh().then(() => wbNotice('状态已更新。')).catch(error => wbNotice(error.message,true));
wbEl('#workbench-run').onclick = wbRun;wbEl('#workbench-publish').onclick = wbPublish;wbEl('#workbench-save').onclick = wbSave;
wbEl('#workbench-source-review-ack').onchange=()=>{const key=wbCandidateReviewKey();wbState.sourceReviewAcknowledged=key&&wbEl('#workbench-source-review-ack').checked?key:'';wbRenderButtons();};
wbEl('#workbench-open-result').onclick = () => wbOpenTab('result');wbEl('#workbench-open-rules').onclick = () => wbOpenTab('rules');
wbEl('#workbench-reset').onclick = () => wbLoadWorkflow(wbState.workflowId).then(() => wbNotice('已重新载入当前保存的规则。')).catch(error => wbNotice(error.message,true));
wbEl('#workbench-plan').addEventListener('input', () => {
  wbState.editorDirty = true;wbEl('#workbench-editor-state').textContent = wbState.editorConflict ? '规则版本已变化，请重新载入后合并。本页修改仍保留。' : '本页有未保存的修改。保存后再运行核对。';wbRenderTask();wbRenderButtons();
});
wbEl('#workbench-prev').onclick = () => wbLoadResult(Math.max(0,wbState.offset-wbState.limit)).catch(error => wbNotice(error.message,true));
wbEl('#workbench-next').onclick = () => {if (wbState.result?.next_offset != null) wbLoadResult(wbState.result.next_offset).catch(error => wbNotice(error.message,true));};
wbEl('#workbench-download-xlsx').onclick = () => wbDownload('xlsx');wbEl('#workbench-download-csv').onclick = () => wbDownload('csv');wbEl('#workbench-download-handoff').onclick = () => wbDownload('handoff');
wbEl('#workbench-refresh-proposals').onclick = () => wbLoadProposals(false).catch(error => wbNotice(error.message,true));
wbEl('#workbench-more-proposals').onclick = () => wbLoadProposals(true).catch(error => wbNotice(error.message,true));
for (const button of document.querySelectorAll('[data-tab]')) button.onclick = () => wbTab(button.dataset.tab);
for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('keydown',event => {
  const tabs = [...document.querySelectorAll('[data-tab]')], index = tabs.indexOf(button);let target = null;
  if (event.key === 'ArrowRight') target = tabs[(index+1)%tabs.length];
  if (event.key === 'ArrowLeft') target = tabs[(index+tabs.length-1)%tabs.length];
  if (event.key === 'Home') target = tabs[0];if (event.key === 'End') target = tabs[tabs.length-1];
  if (target) {event.preventDefault();target.focus();wbTab(target.dataset.tab);}
});
window.addEventListener('beforeunload',event => {if (wbState.editorDirty) {event.preventDefault();event.returnValue = '';}});
window.addEventListener('pagehide', () => {clearTimeout(wbState.pollTimer);wbVisual.generation++;wbClearVisualBlobs();wbSingle.generation++;wbSingleRelease();});
const workbenchReady = wbInitialize().catch(error => wbNotice(error.message,true));


function wbFactsCurrent(generation,project,token) {
  return !wbAuthClosed && generation === wbState.factsGeneration && project === wbProject &&
    token === wbState.token && new URLSearchParams(location.search).get('project') === project;
}
function wbAddSingleCandidateControl(record,kind,body,current){
  if(!['text','visual'].includes(kind)||record?.status!=='approved'||!Array.isArray(record.facts)||record.facts.length!==1||
     !Array.isArray(record.approval_blockers)||record.approval_blockers.length||
     !/^[-A-Za-z0-9_]{1,100}$/.test(record.id||'')||!/^[-A-Za-z0-9_]{1,100}$/.test(record.output_source_id||'')||
     !/^[a-f0-9]{64}$/.test(record.content_sha256||'')||!Number.isSafeInteger(record.source_revision)||record.source_revision<1||
     !/^[a-f0-9]{64}$/.test(record.source_file_sha256||''))return;
  const box=wbNode('div','','single-candidate-control'),description=wbNode('p','可以为这一条生成原值候选，之后仍需在结果页对照原件单独确认；不会自动发布、汇总或生成报告。','muted');
  const create=wbNode('button','生成单条原值候选');create.type='button';create.dataset.singleCandidateCreate='true';
  const view=wbNode('button','打开候选任务');view.type='button';view.dataset.singleCandidateOpen='true';view.hidden=true;
  const message=wbNode('p','','proposal-message');box.append(description,create,view,message);body.append(box);
  const token=wbState.token,valid=()=>!wbAuthClosed&&wbCurrentProject()&&wbState.token===token&&body.isConnected&&current();let candidate=null,busy=false;
  create.onclick=async()=>{
    if(!valid()||busy)return;busy=true;create.disabled=true;message.textContent='正在提交候选任务…';
    try{
      const value=await wbRequest(wbRoot()+'/single-fact-candidates',{method:'POST',body:{kind,fact_review_id:record.id,
        expected_fact_sha256:record.content_sha256,expected_original_revision:record.source_revision,expected_original_sha256:record.source_file_sha256}});
      if(!valid())return;
      if(!/^[-A-Za-z0-9_]{1,100}$/.test(value?.workflow_id||'')||!/^[-A-Za-z0-9_]{1,100}$/.test(value?.run_id||'')||
         !/^[a-f0-9]{64}$/.test(value?.plan_sha256||'')||!['queued','running','retrying','validated','failed','cancelled'].includes(value?.status))throw Error('候选状态未记录');
      candidate=value;create.hidden=true;view.hidden=false;
      message.textContent=value.status==='validated'?'候选已生成；打开后仍需对照原件逐项核对。':
        ['failed','cancelled'].includes(value.status)?'候选未完成，请打开任务查看原因。':'候选任务已接收，请打开任务查看进度。';
    }catch(error){if(valid()){message.textContent=error.status>=400&&error.status<500?'候选请求未通过：'+error.message+' 请刷新该记录后再操作。':'未收到候选结果，请刷新记录核对状态后再操作。';message.classList.add('error');}}
    finally{busy=false;}
  };
  view.onclick=async()=>{
    if(!valid()||!candidate||busy)return;
    if(wbState.editorDirty){message.textContent='当前任务有未保存的做法，请先保存或放弃修改再打开候选。';message.classList.add('error');return;}
    if(wbState.busy||wbState.workflowLoading){message.textContent='当前任务正在处理，请稍后再打开候选。';return;}
    busy=true;view.disabled=true;wbSetBusy(true);
    try{
      await wbLoadWorkflow(candidate.workflow_id,{preserveEditor:false});if(!valid())return;
      const workflow=wbState.workflow,bindings=Object.values(workflow?.document?.bindings||{});
      if(workflow?.workflow_id!==candidate.workflow_id||workflow?.draft_sha256!==candidate.plan_sha256||bindings.length!==1||bindings[0]?.source_id!==record.output_source_id)throw Error('候选任务与核对记录不一致');
      if(wbRunId(workflow.candidate_run)===candidate.run_id&&wbState.result?.run_id===candidate.run_id){
        wbOpenTab('result');wbNotice('已打开本次候选。请先核对单条原值和当次原件，不能据此自动采用或汇总。');
      }else{wbOpenTab('task');wbNotice('已打开候选任务；请查看运行进度或未完成原因。');}
    }catch(error){if(valid()){message.textContent='候选任务无法核对或打开，请刷新记录后重试。';message.classList.add('error');}}
    finally{busy=false;if(valid()){view.disabled=false;wbSetBusy(false);}}
  };
}
function wbFactsControls() {
  let more = wbEl('#workbench-more-facts');
  if (!more) {
    more = wbNode('button','加载更多来源数值');more.id = 'workbench-more-facts';more.type = 'button';
    more.onclick = () => wbLoadFacts(true).catch(error => wbNotice(error.message,true));
    wbEl('#workbench-facts-empty').insertAdjacentElement('afterend',more);
  }
  more.hidden = wbState.factsOffset == null;more.disabled = wbState.factsLoading || wbAuthClosed;
  wbEl('#workbench-refresh-facts').disabled = wbState.factsLoading || wbAuthClosed;
}
async function wbLoadFacts(append = false) {
  if (wbAuthClosed || !wbProject || wbState.factsLoading ||
      new URLSearchParams(location.search).get('project') !== wbProject ||
      append && (!wbState.factsLoaded || wbState.factsOffset == null)) return;
  const generation = ++wbState.factsGeneration, project = wbProject, token = wbState.token;
  const offset = append ? wbState.factsOffset : 0;
  wbState.factsLoading = true;wbFactsControls();
  try {
    const data = await wbRequest(wbRoot() + '/data-facts?' + new URLSearchParams({offset:String(offset),limit:'20'}));
    if (!wbFactsCurrent(generation,project,token)) return;
    const list = wbEl('#workbench-facts');
    if (!append) {list.replaceChildren();wbState.facts.clear();}
    for (const record of data.items || []) {
      if (!record.id || wbState.facts.has(record.id)) continue;
      wbState.facts.set(record.id,record);
      const details = document.createElement('details');details.className = 'proposal';details.dataset.facts = record.id;
      const summary = document.createElement('summary');
      summary.append(wbNode('span',record.name || record.source_name || record.source_id || '来源数值'),
        wbNode('span',{proposed:'待核对',approved:'已确认',rejected:'已拒绝'}[record.status] || record.status || '待核对','proposal-status'));
      const body = wbNode('div','','proposal-body');details.append(summary,body);let loaded = false;
      details.addEventListener('toggle',() => {if (details.open && !loaded) {loaded = true;wbLoadFactRecord(record.id,body);}});
      list.append(details);
    }
    wbState.factsOffset = data.next_offset ?? null;wbState.factsLoaded = true;
    wbEl('#workbench-facts-empty').hidden = !!list.children.length;
    wbEl('#workbench-facts-empty').textContent = '暂无已提取、待核对的来源数值。可以回项目对话上传资料并说明需要提取的内容。';
  } finally {
    if (generation === wbState.factsGeneration) wbState.factsLoading = false;
    if (wbFactsCurrent(generation,project,token)) wbFactsControls();
  }
}
async function wbLoadFactRecord(id,body) {
  const generation=wbState.factsGeneration,token=wbState.token;
  body.replaceChildren(wbNode('p','正在读取原文与数值…','muted'));
  try {const record=await wbRequest(wbRoot() + '/data-facts/' + wbEnc(id));if(wbFactsCurrent(generation,wbProject,token)&&body.isConnected)wbRenderFactRecord(record,body,()=>wbFactsCurrent(generation,wbProject,token));}
  catch (error) {if(wbFactsCurrent(generation,wbProject,token)&&body.isConnected)body.replaceChildren(wbNode('p',error.message,'proposal-message error'));}
}
function wbRenderFactRecord(record,body,current=()=>false) {
  body.replaceChildren();
  const locked = ['approved','rejected'].includes(record.status);
  const blockers = record.approval_blockers || [];
  if (blockers.length) body.append(wbNode('p','以下内容需要先补全或修正：'+blockers.map(wbValue).join('；'),'proposal-warning'));
  for (const [index,fact] of (record.facts || []).entries()) {
    const entry = wbNode('section','','fact-entry');entry.append(wbNode('h3','数值 '+(index+1)));
    entry.append(wbDefinition(fact,[['原文数值',v => v.value_as_printed ?? '未提供'],['标准化数值',v => v.normalized_value ?? '未提取'],
      ['具体含义',v => v.measure || '尚未说明'],['单位',v => v.unit || '尚未说明'],['期间',v => v.period || '尚未说明'],
      ['维度','dimensions'],['原文位置',v => v.start != null && v.end != null ? String(v.start)+'–'+String(v.end) : '未提供']]));
    entry.append(wbNode('blockquote',fact.quote || '未提供原文依据','fact-quote'));body.append(entry);
  }
  if (!(record.facts || []).length) body.append(wbNode('p','这份提案没有可确认的数值。','muted'));
  if (record.output_source_id) body.append(wbNode('p','已保存核对数据；使用前仍需完成业务语义对齐，当前不能用于正式数值加工或报告。','muted'));
  wbAddSingleCandidateControl(record,'text',body,current);
  const actions = wbNode('div','','review-actions');
  const approve = wbNode('button','确认这些数值','primary'), reject = wbNode('button','拒绝此提取');
  approve.type = reject.type = 'button';approve.dataset.factsDecision = 'approved';reject.dataset.factsDecision = 'rejected';
  approve.disabled = locked || blockers.length > 0 || !(record.facts || []).length;reject.disabled = locked;
  actions.append(approve,reject);body.append(actions);
  const message = wbNode('p',locked ? '这份提案已有核对记录；正式计算前还需完成业务语义对齐。' : '确认只记录原文核对；正式计算前还需完成业务语义对齐。','proposal-message');body.append(message);
  const decide = async decision => {
    if (locked || decision === 'approved' && approve.disabled || decision === 'rejected' && reject.disabled) return;
    approve.disabled = reject.disabled = true;
    try {
      await wbRequest(wbRoot() + '/data-facts/' + wbEnc(record.id) + '/review',
        {method:'POST',body:{expected_sha256:record.content_sha256,decision}});
      message.textContent = decision === 'approved' ? '已保存数值核对记录；使用前仍需完成业务语义对齐，当前不能用于正式数值加工或报告。' : '已拒绝这份提取。';
      await wbLoadFacts();
    } catch (error) {
      message.classList.add('error');message.textContent = error.status === 409 ? '原文或提取内容已发生变化，本次没有确认。请重新读取后核对。' : error.message;
      const reload = wbNode('button','重新读取原文与数值');reload.type = 'button';reload.onclick = () => wbLoadFactRecord(record.id,body);body.append(reload);
    }
  };
  approve.onclick = () => decide('approved');reject.onclick = () => decide('rejected');
}
wbEl('#workbench-refresh-facts').onclick = () => wbLoadFacts().catch(error => wbNotice(error.message,true));

// A transcription is submitted by a human; its upstream use of OCR is unknown.
function wbVisualCurrent(generation) {
  return !wbAuthClosed && generation === wbVisual.generation &&
    new URLSearchParams(location.search).get('project') === wbProject;
}
function wbClearVisualBlobs(){for(const url of wbVisual.blobUrls)URL.revokeObjectURL(url);wbVisual.blobUrls.clear();}
function wbVisualMessage(message,error=false){const node=wbEl('#visual-message');node.textContent=message;node.classList.toggle('error',error);}
function wbVisualReady(){return wbVisualCurrent(wbVisual.generation)&&!!wbProject;}
async function wbRefreshVisual(){
  if(!wbVisualReady()||wbVisual.loading)return;
  const generation=++wbVisual.generation;wbVisual.ocrGeneration++;wbEl('#visual-ocr-results').replaceChildren();wbVisual.loading=true;wbEl('#visual-refresh').disabled=true;
  wbEl('#visual-create').disabled=true;wbEl('#visual-more').hidden=true;wbClearVisualBlobs();
  wbVisualMessage('正在读取本项目资料和核对记录…');
  try{
    const sources=await wbRequest('/api/sources');
    if(!wbVisualCurrent(generation))return;
    const valid=(Array.isArray(sources)?sources:[]).filter(s=>s?.project_id===wbProject&&s.kind==='file'&&!s.archived&&
      typeof s.id==='string'&&/^[-A-Za-z0-9_]{1,64}$/.test(s.id)&&typeof s.name==='string'&&/\.(pdf|png|jpe?g|webp)$/i.test(s.name));
    wbVisual.sources=valid;const select=wbEl('#visual-source'),selected=select.value;select.replaceChildren();
    const blank=wbNode('option','请选择图片或 PDF');blank.value='';select.append(blank);
    for(const source of valid){const option=wbNode('option',source.name);option.value=source.id;select.append(option)}
    if(valid.some(source=>source.id===selected))select.value=selected;wbVisualOcrControls();
    await wbLoadVisualList(generation,false);
    if(!wbVisualCurrent(generation))return;
    wbVisual.loaded=true;wbVisualMessage(valid.length?'请选择资料并逐字录入原文。':'本项目暂无图片或 PDF，请先在项目对话上传。');
  }catch(error){if(wbVisualCurrent(generation)){wbVisual.loaded=false;wbVisualMessage('资料或核对记录读取失败，请刷新后重试。',true)}}
  finally{if(wbVisualCurrent(generation)){wbVisual.loading=false;wbEl('#visual-refresh').disabled=false;wbEl('#visual-create').disabled=false;}}
}
async function wbLoadVisualList(generation,append){
  const offset=append?wbVisual.offset:0;if(append&&offset==null)return;
  const page=await wbRequest(wbRoot()+'/visual-facts?'+new URLSearchParams({offset:String(offset),limit:'20'}));
  if(!wbVisualCurrent(generation))return;
  const list=wbEl('#visual-list');if(!append)list.replaceChildren();
  for(const record of Array.isArray(page.items)?page.items:[]){
    if(typeof record?.id!=='string'||!/^[-A-Za-z0-9_]{1,100}$/.test(record.id))continue;
    if([...list.querySelectorAll('[data-visual-record]')].some(node=>node.dataset.visualRecord===record.id))continue;
    const details=wbNode('details','','proposal');details.dataset.visualRecord=record.id;
    const summary=wbNode('summary');const source=wbVisual.sources.find(item=>item.id===record.source_id);
    summary.append(wbNode('span',(source?.name||'资料')+' · 第 '+wbValue(record.page)+' 页'),
      wbNode('span',({pending:'待对照确认',approved:'已人工确认',rejected:'已拒绝'})[record.status]||'状态待核验','proposal-status'));
    const body=wbNode('div','','proposal-body');details.append(summary,body);let loaded=false;
    details.addEventListener('toggle',()=>{if(details.open&&!loaded){loaded=true;wbLoadVisualRecord(record.id,body,generation)}});list.append(details);
  }
  wbVisual.offset=page.next_offset??null;wbEl('#visual-more').hidden=wbVisual.offset==null;
  wbEl('#visual-empty').hidden=!!list.children.length;
}
async function wbLoadVisualRecord(id,body,generation){
  body.replaceChildren(wbNode('p','正在读取待核对内容…','muted'));
  try{const record=await wbRequest(wbRoot()+'/visual-facts/'+wbEnc(id));if(wbVisualCurrent(generation)&&body.isConnected)wbRenderVisualRecord(record,body,generation)}
  catch(error){if(wbVisualCurrent(generation)&&body.isConnected)body.replaceChildren(wbNode('p','记录读取失败，请刷新后重试。','proposal-message error'))}
}
function wbRenderVisualRecord(record,body,generation){
  body.replaceChildren();body.append(wbNode('p','这是用户提交的转录，系统未验证是否借助 OCR，也未独立核验原图或财务口径。','proposal-warning'));
  body.append(wbDefinition(record,[['来源',v=>wbVisual.sources.find(s=>s.id===v.source_id)?.name||'原始资料'],['版本','source_revision'],['页码','page']]));
  body.append(wbNode('blockquote',record.quote||'原文未记录','fact-quote'));
  for(const fact of Array.isArray(record.facts)?record.facts:[]){
    body.append(wbDefinition(fact,[['原文数值','value_as_printed'],['指标','measure'],['单位','unit'],['期间','period'],
      ['范围',value=>value.dimensions&&typeof value.dimensions==='object'?Object.entries(value.dimensions).map(([name,item])=>name+'：'+item).join('；'):'未记录']]));
  }
  if(record.output_source_id)body.append(wbNode('p','已保存人工核对数据；使用前仍需完成业务语义对齐，当前不能用于正式数值加工或报告。','muted'));
  wbAddSingleCandidateControl(record,'visual',body,()=>wbVisualCurrent(generation));
  if(record.status!=='pending')return;
  if(!Array.isArray(record.approval_blockers)||record.approval_blockers.length||!Array.isArray(record.facts)||!record.facts.length||
     !/^[a-f0-9]{64}$/.test(record.content_sha256||'')||!Number.isSafeInteger(record.page)||record.page<1){
    body.append(wbNode('p','原文、指标、单位、期间、范围或记录依据未完整核对，请重新录入并核对。','proposal-warning'));return;
  }
  const preview=wbNode('div','','visual-original');const status=wbNode('p','请先打开当次原件，再逐项对照。','muted');
  const open=wbNode('button','打开当次原件');open.type='button';open.dataset.visualOpen='true';
  const content=wbNode('div');preview.append(status,open,content);body.append(preview);
  const ackLabel=wbNode('label','','catalog-acknowledgment'),ack=document.createElement('input');ack.type='checkbox';ack.disabled=true;ack.dataset.visualAck='true';
  ackLabel.append(ack,wbNode('span','我已对照所选原件的指定页，逐项核对原文、数值、指标、单位、期间和范围。'));body.append(ackLabel);
  const approve=wbNode('button','确认人工核对','primary');approve.type='button';approve.disabled=true;approve.dataset.visualApprove='true';body.append(approve);
  const message=wbNode('p','','proposal-message');body.append(message);let opened=false,objectUrl=null,previewSerial=0;
  const releaseOriginal=()=>{previewSerial++;if(objectUrl){URL.revokeObjectURL(objectUrl);wbVisual.blobUrls.delete(objectUrl);objectUrl=null;}opened=false;ack.checked=false;ack.disabled=true;approve.disabled=true;};
  const activePreview=serial=>wbVisualCurrent(generation)&&body.isConnected&&body.parentElement?.open&&serial===previewSerial;
  body.parentElement?.addEventListener('toggle',()=>{if(!body.parentElement.open){releaseOriginal();open.disabled=false;content.replaceChildren();status.textContent='请先打开当次原件，再逐项对照。';}});
  ack.onchange=()=>{approve.disabled=!(opened&&ack.checked&&wbVisualCurrent(generation))};
  open.onclick=async()=>{
    if(!wbVisualCurrent(generation))return;open.disabled=true;releaseOriginal();const serial=previewSerial;content.replaceChildren();
    try{
      if(!Number.isSafeInteger(record.source_revision)||record.source_revision<1||!/^[-A-Za-z0-9_]{1,64}$/.test(record.source_id||'')||!/^[a-f0-9]{64}$/.test(record.source_file_sha256||''))throw Error('来源依据不完整');
      const query=new URLSearchParams({revision:String(record.source_revision),expected_sha256:record.source_file_sha256});
      const artifact=await wbRequest(wbRoot()+'/sources/'+wbEnc(record.source_id)+'/artifact?'+query);
      if(!activePreview(serial))return;
      if(artifact?.kind!=='source'||artifact.sha256!==record.source_file_sha256||!/^[-A-Za-z0-9_]{1,100}$/.test(artifact.id||'')||
         !(artifact.source_refs||[]).some(ref=>ref.source_id===record.source_id&&ref.revision===record.source_revision))throw Error('来源版本不一致');
      const url=new URL(artifact.download_url,location.origin),expected=wbRoot()+'/artifacts/'+wbEnc(artifact.id)+'/download';
      if(url.origin!==location.origin||url.pathname!==expected||url.search||url.hash)throw Error('来源地址不一致');
      const response=await wbRequest(url.pathname,{binary:true}),blob=await response.blob();
      if(!activePreview(serial))return;
      const types=['image/png','image/jpeg','image/webp','application/pdf'];if(!types.includes(blob.type.toLowerCase()))throw Error('原件格式不支持预览');
      if(!window.crypto?.subtle)throw Error('此浏览器或连接无法核验原件，请使用安全连接');
      const digest=Array.from(new Uint8Array(await window.crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))).map(b=>b.toString(16).padStart(2,'0')).join('');
      if(!activePreview(serial))return;if(digest!==record.source_file_sha256)throw Error('原件内容与记录不一致');
      objectUrl=URL.createObjectURL(blob);wbVisual.blobUrls.add(objectUrl);
      if(blob.type.toLowerCase()==='application/pdf'){
        const frame=document.createElement('iframe');frame.title='原件 PDF，查看第 '+record.page+' 页';frame.src=objectUrl+'#page='+encodeURIComponent(String(record.page));frame.setAttribute('sandbox','');content.append(frame);
        const link=wbNode('a','在新页面查看原件 PDF');link.href=objectUrl;link.target='_blank';link.rel='noopener noreferrer';content.append(link);
      }else{const img=document.createElement('img');img.src=objectUrl;img.alt='所选原件图片';content.append(img)}
      status.textContent='已核验当次原件文件；请对照第 '+record.page+' 页，逐项确认。';opened=true;ack.disabled=false;
    }catch(error){if(activePreview(serial))status.textContent='原件无法打开或核验，本次不能确认。请检查权限、文件版本和连接后重试。';}
    finally{if(activePreview(serial))open.disabled=false;}
  };
  approve.onclick=async()=>{
    if(!opened||!ack.checked||!wbVisualCurrent(generation)||approve.disabled)return;
    approve.disabled=true;open.disabled=true;ack.disabled=true;
    try{
      const value=await wbRequest(wbRoot()+'/visual-facts/'+wbEnc(record.id)+'/review',{method:'POST',body:{expected_sha256:record.content_sha256,decision:'approved',confirmed_visual_review:true}});
      if(!wbVisualCurrent(generation)||!body.isConnected)return;
      if(value?.status!=='approved'||!value.output_source_id)throw Error('结果未确认');
      releaseOriginal();body.replaceChildren(wbNode('p','已保存人工核对记录；使用前仍需完成业务语义对齐，当前不能用于正式数值加工或报告。','proposal-message'));
      wbAddSingleCandidateControl(value,'visual',body,()=>wbVisualCurrent(generation));
    }catch(error){if(wbVisualCurrent(generation)&&body.isConnected){message.textContent='未收到确认结果，请刷新记录核对是否已保存，不要重复提交。';message.classList.add('error');}}
  };
}
wbEl('#visual-form').addEventListener('submit',async event=>{
  event.preventDefault();if(!wbVisualReady()||wbVisual.loading)return;
  const generation=wbVisual.generation,source=wbEl('#visual-source').value,page=Number(wbEl('#visual-page').value),quote=wbEl('#visual-quote').value;
  const value=wbEl('#visual-value').value,measure=wbEl('#visual-measure').value,unit=wbEl('#visual-unit').value,period=wbEl('#visual-period').value;
  const dimensionName=wbEl('#visual-dimension-name').value.trim(),dimensionValue=wbEl('#visual-dimension-value').value;
  if(!wbVisual.sources.some(item=>item.id===source)||!Number.isSafeInteger(page)||page<1||page>2000||!quote.trim()||!value.trim()||!measure.trim()||!unit.trim()||!period.trim()||!dimensionName||!dimensionValue.trim()){
    wbVisualMessage('请选择资料并填写原文、数值、指标、单位、期间和范围。',true);return;
  }
  const submit=wbEl('#visual-create'),controls=[...wbEl('#visual-form').querySelectorAll('input,textarea,select')];
  wbVisual.loading=true;submit.disabled=true;wbEl('#visual-refresh').disabled=true;for(const control of controls)control.disabled=true;
  wbVisualMessage('正在保存待核对记录…');
  try{
    const record=await wbRequest(wbRoot()+'/visual-facts',{method:'POST',body:{source_id:source,page,quote,facts:[{value_as_printed:value,measure,unit,period,dimensions:{[dimensionName]:dimensionValue}}]}});
    if(!wbVisualCurrent(generation))return;if(!record?.id||record.status!=='pending')throw Error('记录状态不明确');
    await wbLoadVisualList(generation,false);if(!wbVisualCurrent(generation))return;
    wbVisualMessage('已保存待核对记录。展开记录，打开原件并逐项确认后才能继续使用。');
  }catch(error){if(wbVisualCurrent(generation))wbVisualMessage(error.status===400||error.status===422?'保存未完成，请核对原文中的完整数字、指标、单位、期间和范围。':'保存未确认；请刷新核对记录后再操作。',true)}
  finally{if(wbVisualCurrent(generation)){wbVisual.loading=false;submit.disabled=false;wbEl('#visual-refresh').disabled=false;for(const control of controls)control.disabled=false;}}
});
wbEl('#visual-refresh').onclick=()=>wbRefreshVisual();
wbEl('#visual-more').onclick=()=>{const generation=wbVisual.generation;wbLoadVisualList(generation,true).catch(()=>{if(wbVisualCurrent(generation))wbVisualMessage('后续记录读取失败，请重试。',true)});};

function wbVisualOcrControls(){
  const source=wbVisual.sources.find(item=>item.id===wbEl('#visual-source').value),button=wbEl('#visual-ocr-run'),status=wbEl('#visual-ocr-status');
  const isImage=!!source&&/\.(png|jpe?g|webp)$/i.test(source.name),complete=Number.isSafeInteger(source?.revision)&&source.revision>0&&/^[a-f0-9]{64}$/.test(source?.content_hash||'');
  const page=Number(wbEl('#visual-page').value);
  button.disabled=wbVisual.ocrBusy||!wbVisualReady()||!isImage||!complete||page!==1;
  if(wbVisual.ocrBusy)status.textContent='正在识别图片，请稍候；识别结果需对照原件核对。';
  else if(!source)status.textContent='选择图片后可尝试识别；PDF 请先人工录入。';
  else if(!isImage)status.textContent='目前只支持图片识别；PDF 请先人工录入并对照原件。';
  else if(!complete)status.textContent='这份资料缺少可核验的版本或原件摘要，请重新上传。';
  else if(page!==1)status.textContent='图片只支持第 1 页，请先调整页码。';
  else status.textContent='可识别这张图片的文字；识别可能有遗漏或错误，不会自动生成或确认数值。';
}
wbEl('#visual-source').addEventListener('change',()=>{wbVisual.ocrGeneration++;wbEl('#visual-ocr-results').replaceChildren();wbVisualOcrControls();});
wbEl('#visual-page').addEventListener('input',()=>{wbVisual.ocrGeneration++;wbEl('#visual-ocr-results').replaceChildren();wbVisualOcrControls();});
wbEl('#visual-ocr-run').onclick=async()=>{
  if(!wbVisualReady()||wbVisual.ocrBusy)return;
  const source=wbVisual.sources.find(item=>item.id===wbEl('#visual-source').value);
  if(!source||Number(wbEl('#visual-page').value)!==1||!/\.(png|jpe?g|webp)$/i.test(source.name)||!Number.isSafeInteger(source.revision)||source.revision<1||!/^[a-f0-9]{64}$/.test(source.content_hash||''))return;
  const generation=wbVisual.generation,serial=++wbVisual.ocrGeneration,id=source.id,revision=source.revision,hash=source.content_hash;
  wbVisual.ocrBusy=true;wbEl('#visual-ocr-results').replaceChildren();wbVisualOcrControls();
  const current=()=>wbVisualCurrent(generation)&&serial===wbVisual.ocrGeneration&&wbEl('#visual-source').value===id&&Number(wbEl('#visual-page').value)===1;
  try{
    const result=await wbRequest(wbRoot()+'/sources/'+wbEnc(id)+'/ocr',{method:'POST',body:{expected_revision:revision,expected_sha256:hash,page:1}});
    if(!current())return;
    if(result?.source_revision!==revision||result.source_file_sha256!==hash||result.page!==1||result.ocr_executed!==true||
       !Number.isSafeInteger(result.image_width)||!Number.isSafeInteger(result.image_height)||!Array.isArray(result.lines))throw Error('识别结果与所选原件不一致');
    const box=wbEl('#visual-ocr-results');box.replaceChildren();
    box.append(wbNode('p','以下是未核对的识别文字，可能遗漏或识错；请选择需要的内容，编辑后仍需对照原件。','proposal-warning'));
    let place=0;for(const line of result.lines){
      if(typeof line?.text!=='string'||!line.text.trim())continue;
      place++;const item=wbNode('section','','visual-ocr-line');item.append(wbNode('p',line.text));
      if(Array.isArray(line.box)&&line.box.length===4&&line.box.every(point=>Array.isArray(point)&&point.length===2&&point.every(Number.isFinite))){
        item.append(wbNode('small','原图第 '+place+' 处；尚未高亮，请对照原件。','muted'));
      }else item.append(wbNode('small','原图位置未记录，请对照原件。','muted'));
      const use=wbNode('button','加入原文草稿');use.type='button';use.dataset.visualOcrUse='true';
      use.onclick=()=>{if(!current())return;const quote=wbEl('#visual-quote'),next=quote.value+(quote.value?'\n':'')+line.text;
        if(next.length>3000){wbEl('#visual-ocr-status').textContent='原文超过 3000 字，请选择更少的内容或手工编辑。';return;}
        quote.value=next;quote.focus();wbEl('#visual-ocr-status').textContent='已加入原文草稿；请人工核对、编辑并填写对应指标与数值。';};
      item.append(use);box.append(item);
    }
    if(box.children.length===1)box.append(wbNode('p','未识别到文字，请对照原件人工录入。','muted'));
    wbEl('#visual-ocr-status').textContent='识别完成；系统没有核实文字或金额是否正确。';
  }catch(error){if(current()){const msg=error?.status===400&&typeof error.detail==='string'&&/[\u3400-\u9fff]/.test(error.detail)?error.detail:'识别未完成，请检查本机 OCR 组件并对照原件人工录入。';wbEl('#visual-ocr-status').textContent=msg;}}
  finally{wbVisual.ocrBusy=false;if(wbVisualReady()){if(current())wbEl('#visual-ocr-run').disabled=false;else wbVisualOcrControls();}}
};

// A single verified transcription is an isolated original-value export, not
// permission to publish a workflow, aggregate facts, make reports or reuse it.
function wbSingleCurrent(generation,key){return wbSingle.generation===generation&&wbSingle.key===key&&wbSingleKey()===key;}
function wbSingleStatus(text,error=false){const node=wbEl('#single-fact-status');node.textContent=text;node.hidden=!text;node.classList.toggle('error',error);}
wbEl('#single-fact-load').onclick=async()=>{
  const key=wbSingleKey();if(!key)return;const generation=++wbSingle.generation,runId=wbState.resultId;
  wbSingle.preview=null;wbSingleRelease();wbEl('#single-fact-body').replaceChildren();wbSingleStatus('正在读取单条原值及来源依据…');
  const load=wbEl('#single-fact-load');load.disabled=true;
  try{
    const preview=await wbRequest(wbRoot()+'/data-results/'+wbEnc(runId)+'/single-fact-review');
    if(!wbSingleCurrent(generation,key))return;
    const original=preview?.original,fact=preview?.fact,source=preview?.source,review=preview?.fact_review;
    const proofs=Object.values(wbState.result?.context?.input_evidence||{});
    if(preview.project_id!==wbProject||preview.run_id!==runId||preview.plan_sha256!==wbState.result?.plan_sha256||
       preview.handoff_sha256!==wbState.result?.manifest_sha256||!/^[a-f0-9]{64}$/.test(preview.preview_sha256||'')||
       preview.scope!=='single_fact_original_value_only'||!source||!/^[-A-Za-z0-9_]{1,100}$/.test(source.id||'')||
       !Number.isSafeInteger(source.revision)||source.revision<1||!/^[a-f0-9]{64}$/.test(source.sha256||'')||
       !proofs.some(proof=>proof?.source_id===source.id&&proof.source_revision===source.revision&&proof.content_hash===source.sha256)||
       !review||!/^[-A-Za-z0-9_]{1,100}$/.test(review.id||'')||!/^[a-f0-9]{64}$/.test(review.sha256||'')||!['text','visual'].includes(review.kind)||
       !original||!/^[-A-Za-z0-9_]{1,100}$/.test(original.id||'')||!Number.isSafeInteger(original.revision)||original.revision<1||
       !/^[a-f0-9]{64}$/.test(original.sha256||'')||!fact||typeof fact.quote!=='string'||!fact.quote||
       typeof fact.value_as_printed!=='string'||!fact.value_as_printed||!fact.quote.includes(fact.value_as_printed)||typeof fact.normalized_value!=='string'||typeof fact.measure!=='string'||
       typeof fact.unit!=='string'||typeof fact.period!=='string'||!fact.dimensions||typeof fact.dimensions!=='object'||Array.isArray(fact.dimensions))throw Error('单条记录的版本或内容不完整');
    if(review.kind==='visual'&&(!Number.isSafeInteger(original.page)||original.page<1)||
       review.kind==='text'&&(!Number.isSafeInteger(original.text_start)||!Number.isSafeInteger(original.text_end)||original.text_start<0||original.text_end<=original.text_start||!/^[a-f0-9]{64}$/.test(original.text_sha256||'')))throw Error('原文或原图位置不完整');
    wbSingle.preview=preview;wbRenderSingleFact(preview,key,generation);wbSingleStatus('请打开当次原件，对照这条原值与全部业务信息。');
  }catch(error){if(wbSingleCurrent(generation,key))wbSingleStatus(error.status>=400&&error.status<500?error.message:'暂时无法读取单条原值，请刷新结果后重试。',true)}
  finally{if(wbSingleCurrent(generation,key))load.disabled=false;}
};
function wbRenderSingleFact(preview,key,generation){
  const body=wbEl('#single-fact-body'),fact=preview.fact,original=preview.original;body.replaceChildren();
  body.append(wbNode('p','这里只核对一条原值；系统不能证明原始资料真实，也没有确认这些数值可以相加或用于报告。','proposal-warning'));
  body.append(wbDefinition(fact,[['指标','measure'],['原文数值','value_as_printed'],['记录的精确值','normalized_value'],['单位','unit'],['期间','period'],
    ['范围',value=>Object.entries(value.dimensions).map(([name,item])=>name+'：'+item).join('；')||'未记录']]));
  body.append(wbNode('blockquote',fact.quote,'fact-quote'));
  const area=wbNode('div','','visual-original'),status=wbNode('p','请先打开当次原件并逐项对照。','muted'),open=wbNode('button','打开当次原件');
  open.type='button';open.dataset.singleOriginal='true';const content=wbNode('div');area.append(status,open,content);body.append(area);
  const label=wbNode('label','','catalog-acknowledgment'),ack=document.createElement('input');ack.type='checkbox';ack.disabled=true;ack.dataset.singleAck='true';
  label.append(ack,wbNode('span','我已对照当次原件，确认这一条的原文数值、指标、单位、期间和范围；这不代表可以汇总或生成报告。'));body.append(label);
  const confirm=wbNode('button','确认这一条原值','primary');confirm.type='button';confirm.disabled=true;confirm.dataset.singleConfirm='true';body.append(confirm);
  const feedback=wbNode('p','','proposal-message');body.append(feedback);let opened=false,opening=0;
  const valid=()=>wbSingleCurrent(generation,key)&&body.isConnected&&wbSingle.preview?.preview_sha256===preview.preview_sha256;
  ack.onchange=()=>{confirm.disabled=!(opened&&ack.checked&&valid())};
  open.onclick=async()=>{
    if(!valid())return;const serial=++opening;opened=false;ack.checked=false;ack.disabled=true;confirm.disabled=true;open.disabled=true;wbSingleRelease();content.replaceChildren();
    const current=()=>valid()&&serial===opening;
    try{
      const query=new URLSearchParams({revision:String(original.revision),expected_sha256:original.sha256});
      const artifact=await wbRequest(wbRoot()+'/sources/'+wbEnc(original.id)+'/artifact?'+query);if(!current())return;
      if(artifact?.kind!=='source'||artifact.sha256!==original.sha256||!/^[-A-Za-z0-9_]{1,100}$/.test(artifact.id||'')||
         !Array.isArray(artifact.source_refs)||!artifact.source_refs.some(ref=>ref.source_id===original.id&&ref.revision===original.revision))throw Error('原件版本不一致');
      const target=new URL(artifact.download_url,location.origin),expected=wbRoot()+'/artifacts/'+wbEnc(artifact.id)+'/download';
      if(target.origin!==location.origin||target.pathname!==expected||target.search||target.hash)throw Error('原件地址不一致');
      const response=await wbRequest(target.pathname,{binary:true}),blob=await response.blob();if(!current())return;
      if(!window.crypto?.subtle)throw Error('浏览器无法核验原件');
      const hash=Array.from(new Uint8Array(await window.crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))).map(b=>b.toString(16).padStart(2,'0')).join('');
      if(!current())return;if(hash!==original.sha256)throw Error('原件摘要不一致');
      const visual=Number.isSafeInteger(original.page)&&original.page>=1;
      if(!visual){
        if(!Number.isSafeInteger(original.text_start)||!Number.isSafeInteger(original.text_end)||original.text_start<0||original.text_end<=original.text_start||
           original.text_end-original.text_start>64000||!/^[a-f0-9]{64}$/.test(original.text_sha256||''))throw Error('原文位置未记录');
        const query=new URLSearchParams({offset:String(original.text_start),limit:String(original.text_end-original.text_start)});
        const doc=await wbRequest(wbRoot()+'/data-documents/'+wbEnc(original.id)+'?'+query);if(!current())return;
        if(doc.source_revision!==original.revision||doc.source_file_sha256!==original.sha256||doc.source_text_sha256!==original.text_sha256||
           doc.offset!==original.text_start||doc.end_offset!==original.text_end||doc.text!==fact.quote)throw Error('原文与记录不一致');
        content.append(wbNode('p','以下为从原件读取的文字片段；不代表图片、图表或文档排版已核对。','muted'),wbNode('pre',doc.text,'fact-quote'));
      }
      const type=blob.type.toLowerCase();if(visual&&!['image/png','image/jpeg','image/webp','application/pdf'].includes(type))throw Error('原件格式不支持预览');
      wbSingle.blobUrl=URL.createObjectURL(blob);
      if(type==='application/pdf'){
        const frame=document.createElement('iframe');frame.title=visual?'原件 PDF 第 '+original.page+' 页':'原件 PDF';frame.src=wbSingle.blobUrl+(visual?'#page='+encodeURIComponent(String(original.page)):'');frame.setAttribute('sandbox','');content.append(frame);
      }else if(visual){const img=document.createElement('img');img.alt='当次原件图片';
        const displayed=new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=()=>reject(Error('原件图片无法显示'));});
        img.src=wbSingle.blobUrl;content.append(img);await displayed;if(!current())return;}
      const link=wbNode('a','打开或下载当次原件');link.href=wbSingle.blobUrl;link.target='_blank';link.rel='noopener noreferrer';content.append(link);
      status.textContent=visual?'原件版本和摘要已核验；请对照第 '+original.page+' 页。':'原件版本和文字片段已核验；请对照原件内容。';opened=true;ack.disabled=false;
    }catch(error){if(current())status.textContent='原件或文字片段无法打开或核验，本次不能确认；请检查权限与版本后重试。';}
    finally{if(current())open.disabled=false;}
  };
  confirm.onclick=async()=>{
    if(!valid()||!opened||!ack.checked||confirm.disabled)return;confirm.disabled=true;ack.disabled=true;open.disabled=true;
    try{
      const result=await wbRequest(wbRoot()+'/data-results/'+wbEnc(preview.run_id)+'/single-fact-review/confirm',
        {method:'POST',body:{expected_preview_sha256:preview.preview_sha256,confirm_original_value:true}});
      if(!valid())return;
      const url=new URL(result?.download_url,location.origin),expected=wbRoot()+'/data-results/'+wbEnc(preview.run_id)+'/single-fact-review/download';
      if(result?.preview_sha256!==preview.preview_sha256||result.scope!=='single_fact_original_value_only'||!/^[-A-Za-z0-9_]{1,100}$/.test(result.receipt_id||'')||
         url.origin!==location.origin||url.pathname!==expected||url.search||url.hash)throw Error('确认回执不完整');
      wbSingleRelease();body.replaceChildren(wbNode('p','已保存这条原值的人工核对记录；只可单独下载这条原值，不能据此汇总、采用结果、生成报告或作为下游输入。','proposal-warning'));
      const download=wbNode('button','下载单条原值 JSON');download.type='button';download.dataset.singleDownload='true';body.append(download);
      download.onclick=async()=>{if(!valid()||download.disabled)return;download.disabled=true;try{
        const response=await wbRequest(expected,{binary:true}),blob=await response.blob();if(!valid())return;
        const local=URL.createObjectURL(blob),link=document.createElement('a');link.href=local;link.download='已核对单条原值.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(local),1000);
        wbSingleStatus('已准备单条原值下载；此文件不代表已确认可汇总或用于报告。');
      }catch(error){if(valid())wbSingleStatus('下载未完成，请重新核对当前原件与记录。',true)}finally{if(valid())download.disabled=false;}};
    }catch(error){if(valid()){feedback.textContent=error.status>=400&&error.status<500?'本次请求未通过：'+error.message:'未收到确认结果，请重新读取记录核对状态，不要重复提交。';feedback.classList.add('error');}}
  };
}

function wbCatalogStatus(status) {
  return {draft:'待 owner 核阅',approved:'owner 已核阅',retired:'已退休，保留历史'}[status] || '状态待核验';
}
function wbCatalogMessage(text, error = false) {
  const message = wbEl('#workbench-catalog-message');message.textContent = text;
  message.hidden = !text;message.classList.toggle('error',error);
}
function wbCatalogControls() {
  const busy = wbState.catalogsLoading || wbState.catalogActions.size > 0;
  wbEl('#workbench-refresh-catalogs').disabled = busy;
  wbEl('#workbench-more-catalogs').disabled = busy;
  wbEl('#workbench-catalogs').setAttribute('aria-busy',String(wbState.catalogsLoading));
}
function wbCatalogSummary(record, details) {
  const summary = details.querySelector('summary');summary.replaceChildren();
  summary.append(wbNode('span',(record.payload?.name || record.key || '业务语义包') +
    (record.version == null ? '' : ' · 第 '+record.version+' 版')),
    wbNode('span',wbCatalogStatus(record.status),'proposal-status catalog-status'));
}
function wbCatalogCard(record) {
  const details = document.createElement('details');details.className = 'proposal catalog';details.dataset.catalog = record.id;
  details.append(document.createElement('summary'));wbCatalogSummary(record,details);
  const body = wbNode('div','','proposal-body catalog-body');details.append(body);let loaded = false;
  details.addEventListener('toggle',() => {
    if (details.open && !loaded) {loaded = true;wbLoadCatalogRecord(record.id,details,body);}
  });
  return details;
}
async function wbLoadCatalogs(append = false) {
  if (wbState.catalogsLoading || wbState.catalogActions.size) return;
  const offset = append ? wbState.catalogOffset : 0;if (append && offset == null) return;
  wbState.catalogsLoading = true;wbCatalogControls();wbCatalogMessage('正在读取业务语义包…');
  try {
    const data = await wbRequest(wbRoot() + '/semantic-catalogs?' + new URLSearchParams({offset:String(offset),limit:'20'}));
    const list = wbEl('#workbench-catalogs');
    if (!append) {list.replaceChildren();wbState.catalogs.clear();}
    for (const record of data.items || []) {
      if (wbState.catalogs.has(record.id)) continue;
      wbState.catalogs.set(record.id,record);list.append(wbCatalogCard(record));
    }
    wbState.catalogOffset = data.next_offset ?? null;
    wbState.catalogTotal = data.total ?? wbState.catalogs.size;wbState.catalogsLoaded = true;
    wbEl('#workbench-more-catalogs').hidden = wbState.catalogOffset == null;
    wbEl('#workbench-catalog-page-info').textContent = '已加载 '+wbState.catalogs.size+' 个版本，共 '+wbState.catalogTotal+' 个';
    wbEl('#workbench-catalogs-empty').hidden = !!list.children.length;
    wbEl('#workbench-catalogs-empty').textContent = '还没有业务语义包。可以回到对话上传口径文档，让 Agent 整理草稿后在这里核阅。';
    wbCatalogMessage('');
  } catch (error) {
    wbCatalogMessage(error.status === 401 ? error.message : '暂时无法读取业务语义包。请点击“刷新语义包”重试，已有任务与结果仍可继续查看。',true);
  } finally {wbState.catalogsLoading = false;wbCatalogControls();}
}
async function wbLoadCatalogRecord(id, details, body) {
  const requestId = (body.catalogLoadId || 0) + 1;body.catalogLoadId = requestId;
  body.replaceChildren(wbNode('p','正在读取完整字段定义与原件依据…','muted'));
  try {
    const record = await wbRequest(wbRoot() + '/semantic-catalogs/' + wbEnc(id));
    if (requestId !== body.catalogLoadId) return;
    wbState.catalogs.set(record.id,record);wbRenderCatalogRecord(record,details,body);
  } catch (error) {
    if (requestId !== body.catalogLoadId) return;
    body.replaceChildren(wbNode('p',error.status === 401 ? error.message : '暂时无法读取此版本。本页未作批准或退休操作，请重新读取后核对。','proposal-message error'));
    const reload = wbNode('button','重新读取语义包');reload.type = 'button';
    reload.onclick = () => wbLoadCatalogRecord(id,details,body);body.append(reload);
  }
}
function wbCatalogField(field) {
  const entry = wbNode('section','','catalog-field');entry.append(wbNode('h4',field.name || field.id || '未命名字段'));
  entry.append(wbDefinition(field,[['字段标识','id'],['别名',v => v.aliases?.length ? v.aliases.join('、') : '未提供'],
    ['具体含义',v => v.meaning || '尚未声明'],['类型',v => ({dimension:'维度',measure:'度量',attribute:'属性',unknown:'尚未声明'}[v.kind] || v.kind || '尚未声明')],
    ['单位',v => v.unit || '尚未声明'],['时间属性',v => ({interval:'期间',instant:'时点',timeless:'无时间属性',unknown:'尚未声明'}[v.temporal_kind] || '尚未声明')],
    ['具体期间或时点',v => v.period || '未声明具体期间'],
    ['粒度',v => v.grain == null ? '尚未声明' : v.grain.length ? v.grain.join('、') : '标量（没有维度）'],
    ['数值格式',v => v.value_format || '尚未声明'],['原数值显示格式',v => v.number_format || '未提供'],
    ['汇总方式',v => ({sum:'求和',count:'计数',mean:'平均',ratio:'比率',last:'末值',none:'不汇总',unknown:'尚未声明'}[v.aggregation] || '尚未声明')]]));
  for (const evidence of field.provenance || []) {
    const origin = wbNode('div','','catalog-field-source');origin.append(wbDefinition(evidence,
      [['来源标识','source_id'],['字段原件定位','locator'],['原件内容摘要','content_sha256']]));entry.append(origin);
  }
  if (!field.provenance?.length) entry.append(wbNode('p','未提供单独字段定位，请核对上方原件。','muted'));
  return entry;
}
function wbRenderCatalogRecord(record, details, body) {
  wbCatalogSummary(record,details);body.replaceChildren();
  const definition = record.payload || {}, source = definition.source || {}, snapshot = record.source_snapshot || {};
  body.append(wbDefinition(definition,[['名称','name'],['业务定义标识','key'],['说明','description'],
    ['版本',() => '第 '+record.version+' 版'],['有效期起','effective_from'],['有效期止',v => v.effective_to || '未设结束日期']]));
  const origin = wbNode('section','','catalog-source');origin.append(wbNode('h3','原件依据与固定版本'));
  origin.append(wbDefinition({...source,...snapshot},[['来源标识','source_id'],['原件定位','locator'],
    ['来源修订','source_revision'],['冻结修订标识','source_revision_id'],['原件内容摘要','content_sha256']]));
  origin.append(wbDefinition(record,[['语义包内容摘要','content_sha256'],['提出时间','created_at'],
    ['提出人身份摘要','created_actor_sha256'],['owner 核阅时间',v => v.approval?.approved_at],
    ['核阅人身份摘要',v => v.approval?.actor_sha256],['退休时间',v => v.retirement?.retired_at]]));body.append(origin);
  body.append(wbNode('p',record.status === 'draft' ? '草稿尚未经 owner 核阅，不能作为可信语义包用于字段对齐。' :
    '此版本经工作空间 owner 核阅；不代表 IT 签名或组织审批。','catalog-trust'));
  if (record.status === 'retired') body.append(wbNode('p','此版本已退休，阻止未来新消费；历史提案、核对记录和结果继续保留。','proposal-warning'));
  const fields = wbNode('div','','catalog-fields');fields.append(wbNode('h3','完整字段定义（'+(definition.fields?.length || 0)+'）'));
  for (const field of definition.fields || []) fields.append(wbCatalogField(field));body.append(fields);
  const operation = record.status === 'draft' ? 'approve' : record.status === 'approved' ? 'retire' : null;
  if (!operation) return;
  const acknowledgment = wbNode('label','','catalog-acknowledgment'), check = document.createElement('input');
  check.type = 'checkbox';check.dataset.catalogAcknowledge = operation;
  acknowledgment.append(check,wbNode('span',operation === 'approve' ? '我已核对原件依据、每个字段的口径和有效期。' :
    '我已了解：退休会阻止此版本用于新的消费，历史记录和结果保留。'));body.append(acknowledgment);
  const actions = wbNode('div','','review-actions'), button = wbNode('button',operation === 'approve' ? '批准此版本语义包' : '退休此版本语义包',operation === 'approve' ? 'primary' : '');
  button.type = 'button';button.dataset.catalogAction = operation;button.disabled = true;actions.append(button);body.append(actions);
  const message = wbNode('p',operation === 'approve' ? '批准只针对上方完整内容和固定来源修订，后续字段匹配仍需单独核对。' : '退休后不能重新批准此版本；需要继续使用时请在对话中整理新版本。','proposal-message');
  message.setAttribute('role','status');message.setAttribute('aria-live','polite');body.append(message);let saving = false;
  const complete = Array.isArray(definition.fields) && definition.fields.length > 0 && /^[a-f0-9]{64}$/.test(record.content_sha256 || '');
  check.onchange = () => {button.disabled = !check.checked || !complete || saving || wbState.catalogActions.has(record.id);};
  button.onclick = async () => {
    if (button.disabled || !check.checked || saving || wbState.catalogActions.has(record.id)) return;
    saving = true;wbState.catalogActions.add(record.id);button.disabled = check.disabled = true;wbCatalogControls();
    message.textContent = operation === 'approve' ? '正在保存此版本的 owner 核阅记录…' : '正在保存退休记录…';
    try {
      const saved = await wbRequest(wbRoot() + '/semantic-catalogs/' + wbEnc(record.id) + '/' + operation,
        {method:'POST',body:{expected_sha256:record.content_sha256}});
      wbState.catalogActions.delete(record.id);wbState.catalogs.set(saved.id,saved);wbRenderCatalogRecord(saved,details,body);
      wbCatalogMessage(operation === 'approve' ? '已批准此版本，owner 核阅记录已绑定固定定义和来源修订。' :
        '此版本已退休，停止未来新消费；历史记录与结果保留。');
    } catch (error) {
      message.classList.add('error');message.textContent = error.status === 401 ? error.message : operation === 'approve' ?
        '本次未获批准。定义、来源或核阅条件可能已变化，请重新读取此版本后核对。' :
        '本次未完成退休，状态可能已变化。请重新读取此版本后核对。';
      const reload = wbNode('button','重新读取语义包');reload.type = 'button';
      reload.onclick = () => wbLoadCatalogRecord(record.id,details,body);body.append(reload);
    } finally {wbState.catalogActions.delete(record.id);wbCatalogControls();}
  };
}
wbEl('#workbench-refresh-catalogs').onclick = () => wbLoadCatalogs(false);
wbEl('#workbench-more-catalogs').onclick = () => wbLoadCatalogs(true);
