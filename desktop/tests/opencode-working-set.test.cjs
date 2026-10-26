const test=require('node:test'),assert=require('node:assert/strict');
const projection=import('../../agent_service/opencode_context.mjs');
function message(id,tool,observation,args={}){
 return {role:'tool',tool_call_id:'call_'+id,content:JSON.stringify({observation:{step_id:id,tool,status:'done',
  arguments:args,observation},available_tools:[]})};
}
function layout(){
 const sheets=[{name:'收入',candidate_regions:[]},{name:'成本',candidate_regions:[]}];
 for(let i=0;i<5;i++)sheets[i%2].candidate_regions.push({candidate_id:'region_'+i,
  proposed_options:{sheet:sheets[i%2].name,header_rows:[2,3],cell_range:'A2:H8'},
  percentage_fraction:true,header_preview:[{coordinate:'A2',text:'真实原始表头'+('x'.repeat(1000))}]});
 return {source_id:'s_layout',sheets,candidate_regions:structuredClone(sheets.flatMap(sheet=>sheet.candidate_regions)),
  warnings:[{code:'FORMULA_CACHE_UNKNOWN'}],region_discovery_truncated:false};
}

test('native layout view shares exact repeated candidates and retains one complete ordered value copy',async()=>{
 const {projectRequest}=await projection,original=layout(),body={messages:[message('layout','inspect_source_tables',original)]};
 const frozen=structuredClone(body),result=projectRequest(body),observation=JSON.parse(result.body.messages[0].content).observation.observation;
 assert.deepEqual(body,frozen);assert.equal(result.metrics.deduplicated_layout_candidates,1);assert(result.metrics.saved_characters>3000);
 assert.equal(observation.candidate_regions,undefined);assert.equal(observation.candidate_regions_reference.exact_values_already_present,true);
 const restored=observation.candidate_regions_reference.concatenate.flatMap(path=>observation.sheets[Number(path.split('/')[2])].candidate_regions);
 assert.deepEqual(restored,original.candidate_regions);assert.deepEqual(observation.warnings,original.warnings);
 assert.equal(result.body.messages[0].tool_call_id,'call_layout');
});

for(const difference of ['missing','changed','order','type'])test('native candidate sharing rejects '+difference+' differences',async()=>{
 const {projectRequest}=await projection,observation=layout();
 if(difference==='missing')observation.candidate_regions.pop();
 else if(difference==='changed')observation.candidate_regions[0].proposed_options.header_rows=[1];
 else if(difference==='order')observation.candidate_regions.reverse();
 else observation.candidate_regions[0].percentage_fraction=1;
 const body={messages:[message('layout','inspect_source_tables',observation)]};
 const result=projectRequest(body);assert.deepEqual(result.body,body);assert.equal(result.metrics.deduplicated_layout_candidates,0);
});

test('five bounded financial tables remain together past a plan and authoring guide, with exact metadata intact',async()=>{
 const {projectRequest}=await projection;
 const tables=Array.from({length:5},(_,i)=>message('table_'+i,'read_source_table',{
  source_id:'s_'+i,total:1,offset:0,next_offset:null,available_rows_only:true,
  fields:[{name:'amount',label:'金额',unit:'CNY',unit_multiplier:'10000',
    header_coordinates:[{coordinate:'B3'}],original_metadata:'保留字段元数据'.repeat(1000)}],
  rows:[{amount:'9007199254740993.000000000001',refund:'-0.000000000001',date:'2026/09/01',ratio:'13%',tax:'含税',budget_version:'v1'}],
  evidence:{source_revision:i+1,table_id:'sheet_'+i,selection:{sheet:'底稿'+i,header_rows:[2,3]},
    warnings:[{code:'PERCENTAGE_SEMANTICS_REQUIRED',coordinate:'F4'}],
    special_cells:[{coordinate:'E4',formula:'=B4-C4',cached_value:'0.13'}]}},{source_id:'s_'+i,options:{sheet:'底稿'+i}}));
 const plan=message('plan','set_task_plan',{steps:['根据真实表格编写加工方案']}),guide=message('guide','data_workflow_guide',
  {schema:{additionalProperties:false,properties:{description:{type:'string',description:'完整指南'.repeat(2500)}}}});
 const body={messages:[...tables,plan,guide]},frozen=structuredClone(body),result=projectRequest(body);
 assert.deepEqual(body,frozen);assert.equal(result.metrics.retained_table_views,5);assert.equal(result.metrics.compacted_receipts,0);
 for(let i=0;i<5;i++)assert.deepEqual(JSON.parse(result.body.messages[i].content).observation.observation,
                                   JSON.parse(tables[i].content).observation.observation);
 assert.deepEqual(result.body.messages.at(-1),guide);
});

test('requested complete guide stays available after a generic plan file and later planning steps',async()=>{
 const {projectRequest}=await projection;
 const tables=Array.from({length:5},(_,i)=>message('table_'+i,'read_source_table',{
  source_id:'s_'+i,fields:[{name:'amount',unit:'CNY'}],rows:[{amount:'-0.000000000001'}],
  evidence:{source_revision:1,table_id:'sheet_'+i}}));
 const guide=message('guide','data_workflow_guide',{schema:{additionalProperties:false,properties:{
  binding:{type:'string',description:'完整编写规范'.repeat(6000)}}},instructions:['按完整来源编写可核验加工方案']});
 const plan=message('saved_plan','write_file',{path:'notes/plan.md',sha256:'a'.repeat(64)},{path:'notes/plan.md'});
 const body={messages:[...tables,guide,plan,
  message('set_plan','set_task_plan',{steps:['编写加工方案']}),
  message('read_plan','read_file',{content:'先查表，后编写加工方案'},{path:'notes/plan.md'})]};
 const frozen=structuredClone(body),result=projectRequest(body);
 assert.deepEqual(body,frozen);assert.deepEqual(result.body.messages[5],guide);
 const kept=JSON.parse(result.body.messages[5].content).observation.observation;
 assert(kept.schema);assert.equal(kept.context_excerpt,undefined);assert.equal(kept.recover_with,undefined);
 assert.equal(result.metrics.compacted_receipts,0);
});
