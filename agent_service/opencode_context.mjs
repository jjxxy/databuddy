// A bounded VIEW of durable Atlas receipts, never a financial summary. OpenCode
// keeps its original history; read_tool_result recovers the authorized original.
import {createHash} from 'node:crypto';

const GUIDES=new Set(['data_workflow_guide','business_report_guide','semantic_catalog_guide','discover_tools']);
const PRESERVE=new Set(['prepare_work_action','read_work_action','confirm_metric','publish_result','read_company_document']);
const NATIVE_COUNTS=new Set(['source_schema','source_preview','read_source_table','inspect_source_tables',
 'read_data_workflow_result','view_result','query_sql','inspect_data_quality']);
const META=new Set(['source_id','source_ids','source_revision','source_revisions','revision','revision_id','content_hash','content_sha256',
 'run_id','result_id','workflow_id','pipeline_id','plan_sha256','commit','sha256','path','status','complete','records','total','offset','next_offset',
 'sample_only','complete_source_read','evidence','source_refs','input_refs','inputs','validation','error','code','semantic_status','metric_status',
 'reading_boundary_required','verified','integrity_verified','verification_scope','download_url','preview_url',
 'recovery','source_turn_id','draft_sha256','latest_run']);

function receipt(text){
 if(typeof text!=='string')return null;
 try{
  let reply=JSON.parse(text),encode=value=>JSON.stringify(value);
  for(let depth=0;depth<3;depth++){
   const step=reply?.observation;
   if(step&&/^[-\w]{1,64}$/.test(step.step_id||'')&&/^\w{1,64}$/.test(step.tool||'')&&['done','failed'].includes(step.status))return {reply,step,encode};
   // OpenCode's tool result can be a JSON output object containing the
   // plugin's content string. Preserve that envelope, not just its inner DTO.
   if(!reply||typeof reply.content!=='string')return null;
   const outer=reply,previous=encode;
   reply=JSON.parse(outer.content);encode=value=>previous({...outer,content:JSON.stringify(value)});
  }
  return null;
 }catch{return null;}
}
function slots(messages){
 const values=[];
 for(let index=0;index<messages.length;index++){
  const message=messages[index];
  if(message.role==='tool'&&typeof message.content==='string'){
   const parsed=receipt(message.content);if(parsed)values.push({index,parsed,text:message.content});
  }else if((message.role==='user'||message.role==='tool')&&Array.isArray(message.content)){
   message.content.forEach((part,partIndex)=>{
    const field=part.type==='tool_result'?'content':message.role==='tool'&&part.type==='text'?'text':null;
    if(!field)return;
    const parsed=receipt(part[field]);if(parsed)values.push({index,partIndex,field,parsed,text:part[field]});
   });
  }
 }
 return values;
}
function metadata(value,tool){
 if(!value||typeof value!=='object'||Array.isArray(value))return {};
 const output=Object.fromEntries(Object.entries(value).filter(([key])=>META.has(key)));
 if(!NATIVE_COUNTS.has(tool)){
  for(const name of ['total','records','sample_only','complete_source_read','evidence','inputs'])delete output[name];
 }
 if(tool==='read_work_tool')output.metadata_origin='external_untrusted; recover original before relying on it';
 return output;
}
function compact(value){
 const {reply,step}=value.parsed,observation=step.observation;
 const recovered=step.tool==='read_tool_result'&&/^[-\w]{1,64}$/.test(step.arguments?.step_id||'');
 let recovery={tool:'read_tool_result',step_id:recovered?step.arguments.step_id:step.step_id,
  offset:recovered&&Number.isInteger(step.arguments.offset)?step.arguments.offset:0};
 if(recovered&&step.arguments.source_turn_id)recovery.source_turn_id=step.arguments.source_turn_id;
 else if(observation?.source_turn_id)recovery.source_turn_id=observation.source_turn_id;
 if(['read_context_original','read_context_checkpoint'].includes(step.tool))recovery={tool:step.tool,...step.arguments};
 const record={...step,observation:{...metadata(observation,step.tool),not_full_result:true,
  context_receipt_sha256:createHash('sha256').update(JSON.stringify(observation)??'null').digest('hex'),
  evidence_scope:'receipt_metadata_only; recover original values or schemas before relying on them',
  recover_with:recovery}};
 const output={...reply,observation:record};
 if(Array.isArray(output.available_tools)){
  output.available_tool_names=output.available_tools.map(tool=>tool.name);
  output.available_tools=[];
  output.schema_recovery='discover_tools returns the complete current authorized schema by name';
 }
 return value.parsed.encode(output);
}

function observationCharacters(value){
 const observation=value.parsed.step.observation;
 if(value.parsed.step.tool==='read_tool_result'&&typeof observation?.content==='string')return observation.content.length;
 return JSON.stringify(observation??null).length;
}
function tableKey(value){
 const step=value.parsed.step,observation=step.observation;
 const source=observation?.source_id??step.arguments?.source_id;
 if(!source)return null;
 if(step.tool==='inspect_source_tables')return JSON.stringify([source,'layout']);
 if(step.tool!=='read_source_table')return null;
 return JSON.stringify([source,observation?.evidence?.source_revision??observation?.source_revision??null,
  observation?.evidence?.table_id??step.arguments?.options??null]);
}
function completeTable(value){
 const step=value.parsed.step,observation=step.observation;
 return step.tool==='read_source_table'&&step.status==='done'&&observation&&Array.isArray(observation.rows)
  &&!observation.not_full_result&&!observation.context_excerpt;
}
function smallFinancialTable(value){
 const observation=value.parsed.step.observation;
 return completeTable(value)&&JSON.stringify(observation.rows).length<=6000&&observationCharacters(value)<=16000;
}
function referenceTo(value,original){
 const {reply,step}=value.parsed;
 return value.parsed.encode({...reply,observation:{...step,observation:{
  context_reference:{step_id:original.parsed.step.step_id,in_current_context:true,
   scope:'same durable original result; returned values, formats and source metadata are already present, not a financial approval'},
  duplicate_result_view:true}}});
}
function pageIdentity(value){
 const step=value.parsed.step,r=step.observation?.recovery;
 if(step.status!=='done'||step.observation?.executed===false)return null;
 return r?.kind&&r?.resource_id&&r?.version_sha256?JSON.stringify([
  r.kind,r.resource_id,r.version_sha256,r.offset_unit,r.offset,r.returned_characters]):null;
}
function resourceIdentity(value){
 const step=value.parsed.step,r=step.observation?.recovery;
 if(step.status!=='done'||step.observation?.executed===false)return null;
 return r?.kind&&r?.resource_id?JSON.stringify([r.kind,r.resource_id]):null;
}

function canonical(value){
 if(Array.isArray(value))return value.map(canonical);
 if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])]));
 return value;
}
function schemaDigest(tool){
 if(!tool||typeof tool.name!=='string'||!tool.arguments_schema||typeof tool.arguments_schema!=='object')return null;
 return createHash('sha256').update(JSON.stringify(canonical(tool))).digest('hex');
}
function bootstrapDefinitions(messages,registry,metrics){
 const marker='Atlas task context (untrusted source values inside):\n';
 for(const message of messages){
  if(message.role!=='user')continue;
  const parts=typeof message.content==='string'?[message.content]:Array.isArray(message.content)?message.content.filter(p=>p.type==='text').map(p=>p.text):[];
  for(const text of parts){
   if(typeof text!=='string'||!text.includes(marker))continue;
   try{
    const position=text.indexOf(marker),info=JSON.parse(text.slice(position+marker.length));
    if(!Array.isArray(info.tools)||!Array.isArray(info.tool_catalog))continue;
    metrics.bootstrap_prompt_characters=position;
    for(const [key,value] of Object.entries(info))if(/^[a-z_]{1,64}$/.test(key))metrics['bootstrap_field_'+key+'_characters']=JSON.stringify(value).length;
    info.tools.forEach((tool,index)=>{const hash=schemaDigest(tool);if(hash&&!registry.has(hash))registry.set(hash,'initial Atlas task.tools['+index+']');});
   }catch{/* Other user text is never interpreted as a tool schema. */}
  }
 }
}
function internSchemas(reply,registry,stepId,metrics){
 let result=reply;
 function intern(tools,location){
  return tools.map((tool,index)=>{
   const hash=schemaDigest(tool);if(!hash)return tool;
   metrics.receipt_schema_characters_before+=JSON.stringify(tool).length;
   let output=tool;
   if(registry.has(hash)){
    const reference={name:tool.name,schema_sha256:hash,schema_ref:registry.get(hash),in_current_context:true};
    if(JSON.stringify(reference).length<JSON.stringify(tool).length){output=reference;metrics.deduplicated_tool_definitions++;}
   }else registry.set(hash,location+'['+index+']');
   metrics.receipt_schema_characters_after+=JSON.stringify(output).length;
   return output;
  });
 }
 const step=reply.observation;
 if(step?.tool==='discover_tools'&&Array.isArray(step.observation?.tools)){
  result={...result,observation:{...step,observation:{...step.observation,
   tools:intern(step.observation.tools,'step '+stepId+'.observation.tools')}}};
 }
 if(Array.isArray(reply.available_tools))result={...result,
  available_tools:intern(reply.available_tools,'step '+stepId+'.available_tools')};
 return result;
}

function shareLayoutCandidates(messages){
 const rewritten=messages.slice();let count=0;
 for(const value of slots(messages)){
  const {reply,step}=value.parsed,observation=step.observation;
  if(step.tool!=='inspect_source_tables'||!observation||!Array.isArray(observation.sheets)
      ||!Array.isArray(observation.candidate_regions)||!observation.candidate_regions.length
      ||!observation.sheets.every(sheet=>sheet&&Array.isArray(sheet.candidate_regions)))continue;
  const candidates=observation.sheets.flatMap(sheet=>sheet.candidate_regions);
  if(JSON.stringify(canonical(candidates))!==JSON.stringify(canonical(observation.candidate_regions)))continue;
  const reference={concatenate:observation.sheets.map((_,i)=>'#/sheets/'+i+'/candidate_regions'),
   order:'sheet_order',exact_values_already_present:true};
  if(JSON.stringify(reference).length>=JSON.stringify(observation.candidate_regions).length)continue;
  const shared={...observation,candidate_regions_reference:reference};delete shared.candidate_regions;
  const text=value.parsed.encode({...reply,observation:{...step,observation:shared}}),message={...rewritten[value.index]};
  if(value.partIndex===undefined)message.content=text;
  else{message.content=message.content.slice();message.content[value.partIndex]={...message.content[value.partIndex],[value.field]:text};}
  rewritten[value.index]=message;count++;
 }
 return {messages:rewritten,count};
}

function shareWorkflowDocuments(messages){
 const rewritten=messages.slice();let count=0;
 for(const value of slots(messages)){
  const {reply,step}=value.parsed,observation=step.observation,document=observation?.document;
  if(!['register_data_workflow','inspect_data_workflow'].includes(step.tool)||!document||typeof document!=='object')continue;
  const plan=document.plan,inputs=observation.inputs,shared={...observation};
  const equal=(a,b)=>JSON.stringify(canonical(a))===JSON.stringify(canonical(b));
  if(inputs&&plan?.inputs&&document.bindings&&equal(Object.keys(inputs).sort(),Object.keys(plan.inputs).sort())
    &&equal(Object.keys(inputs).sort(),Object.keys(document.bindings).sort())
    &&Object.entries(inputs).every(([key,item])=>item&&equal(Object.keys(item).sort(),['binding','schema'])
      &&equal(item.binding,document.bindings[key])&&equal(item.schema,plan.inputs[key]))){
   delete shared.inputs;shared.inputs_reference={binding:'#/document/bindings/<alias>',schema:'#/document/plan/inputs/<alias>',exact_values_already_present:true};
  }
  if(Array.isArray(observation.steps)&&equal(observation.steps,plan?.stages)){
   delete shared.steps;shared.steps_reference='#/document/plan/stages';
  }
  if(JSON.stringify(shared).length>=JSON.stringify(observation).length)continue;
  const message={...rewritten[value.index]},text=value.parsed.encode({...reply,observation:{...step,observation:shared}});
  if(value.partIndex===undefined)message.content=text;
  else{message.content=message.content.slice();message.content[value.partIndex]={...message.content[value.partIndex],[value.field]:text};}
  rewritten[value.index]=message;count++;
 }
 return {messages:rewritten,count};
}

export function projectRequest(body){
 const workflows=shareWorkflowDocuments(Array.isArray(body.messages)?body.messages:[]),layouts=shareLayoutCandidates(workflows.messages),messages=layouts.messages;
 const references=slots(messages),rewritten=messages.slice(),byId=new Map(),latestTables=new Map(),working=new Map(),versions=new Map(),pages=new Map();
 let compacted=0,duplicateResults=0;
 for(const value of references){
  byId.set(value.parsed.step.step_id,value);
  const key=tableKey(value);if(key)latestTables.set(key,value);
   const step=value.parsed.step,r=step.observation?.recovery,resource=resourceIdentity(value);
   if(resource)versions.set(resource,r.version_sha256);
   if(['register_data_workflow','inspect_data_workflow'].includes(step.tool)&&step.observation?.workflow_id)
    working.set(JSON.stringify(['workflow',step.observation.workflow_id]),value);
 }
 for(const value of references){
  const resource=resourceIdentity(value),r=value.parsed.step.observation?.recovery,identity=pageIdentity(value);
  if(identity&&r.version_sha256===versions.get(resource)&&!pages.has(identity))pages.set(identity,value);
 }
 const retainedPages=new Set(pages.values());
 const keptTables=new Set();let workingCharacters=0;
 for(const value of [...latestTables.values()].reverse()){
  const size=observationCharacters(value);
  // Every distinct bounded table in the current working set must remain usable
  // even when several tables plus a writing guide exceed an aggregate quota.
  // Larger observations still retain durable, explicit recovery references.
  if(size<=6000||smallFinancialTable(value)||workingCharacters+size<=24000){keptTables.add(value);if(size>6000&&!smallFinancialTable(value))workingCharacters+=size;}
 }
 for(let index=0;index<references.length;index++){
  const value=references[index],step=value.parsed.step;
  let text=value.text,reply=value.parsed.reply;
  const resource=resourceIdentity(value),r=step.observation?.recovery,identity=pageIdentity(value);
  // Recovery pages are duplicate slices when their complete original table is
  // still available in this request. Point to that table, not back to a tool
  // read: three small sources must not evict one another in a latest-two loop.
  const originalId=step.tool==='read_tool_result'?(step.original_result?.step_id??step.observation?.step_id??step.arguments?.step_id):null;
  const original=originalId?byId.get(originalId):null;
  if(resource&&r.version_sha256!==versions.get(resource)){
   text=compact(value);compacted++;
  }else if(identity&&pages.get(identity)!==value){
   text=referenceTo(value,pages.get(identity));duplicateResults++;
  }else if(original&&completeTable(original)&&keptTables.has(original)){
   const duplicate=referenceTo({...value,parsed:{...value.parsed,reply}},original);
   if(duplicate.length<text.length){text=duplicate;duplicateResults++;}
  }else if(index<references.length-2&&observationCharacters(value)>6000&&!PRESERVE.has(step.tool)&&!keptTables.has(value)&&!retainedPages.has(value)&&![...working.values()].includes(value)){
  // Saving a generic file or plan does not finish a guide's purpose. Keep
  // requested canonical reference material until native token-window
  // compaction, rather than forcing it into repeated recovery string pages.
   if(!GUIDES.has(step.tool)){
    const shortened=compact({...value,parsed:{...value.parsed,reply}});
    if(shortened.length<text.length){text=shortened;compacted++;}
   }
  }
  if(text===value.text)continue;
  const message={...rewritten[value.index]};
  if(value.partIndex===undefined)message.content=text;
  else{message.content=message.content.slice();message.content[value.partIndex]={...message.content[value.partIndex],[value.field]:text};}
  rewritten[value.index]=message;
 }
 const schemaMetrics={deduplicated_tool_definitions:0,receipt_schema_characters_before:0,receipt_schema_characters_after:0};
 const registry=new Map();bootstrapDefinitions(rewritten,registry,schemaMetrics);
 // Index only definitions that actually survive the fact projection. A schema
 // in an elided receipt cannot become a dangling in-current-context reference.
 for(const value of slots(rewritten)){
  const reply=internSchemas(value.parsed.reply,registry,value.parsed.step.step_id,schemaMetrics);
  const text=value.parsed.encode(reply);if(text===value.text)continue;
  const message={...rewritten[value.index]};
  if(value.partIndex===undefined)message.content=text;
  else{message.content=message.content.slice();message.content[value.partIndex]={...message.content[value.partIndex],[value.field]:text};}
  rewritten[value.index]=message;
 }
 const changed=compacted||schemaMetrics.deduplicated_tool_definitions||duplicateResults||layouts.count||workflows.count;
 const projected=changed?{...body,messages:rewritten}:body;
 const roles={system:0,user:0,assistant:0,tool:0,other:0};
 for(const message of projected.messages||[])roles[Object.hasOwn(roles,message.role)?message.role:'other']+=JSON.stringify(message).length;
 const original=JSON.stringify(body).length,characters=JSON.stringify(projected).length;
 return {body:projected,metrics:{original_characters:original,projected_characters:characters,
  saved_characters:original-characters,compacted_receipts:compacted,duplicate_result_views:duplicateResults,
  deduplicated_layout_candidates:layouts.count,
   deduplicated_workflow_views:workflows.count,
  ...schemaMetrics,retained_table_views:keptTables.size,
  recognized_receipts:references.length,system_characters:roles.system,
  user_characters:roles.user,assistant_characters:roles.assistant,tool_characters:roles.tool,
  other_message_characters:roles.other,tool_definition_characters:JSON.stringify(projected.tools||[]).length}};
}
