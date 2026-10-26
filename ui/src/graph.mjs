// Uses only IDs and relationships returned by the authenticated Atlas API.
export function lineageGraph(pipeline, value) {
  if(!pipeline || !value)return {nodes:[],edges:[]};
  const sources=[...new Set((value.source_ids||[]).filter(id=>typeof id==='string'))];
  const nodes=sources.map((id,index)=>({id:'source:'+id,type:'default',position:{x:40,y:70+index*140},data:{label:'来源资料\n'+id}}));
  const active=!!value.run_id;
  nodes.push({id:'pipeline:'+pipeline.id,type:'default',position:{x:400,y:130},data:{label:(pipeline.name||'数据处理任务')+'\n版本：'+(value.active_commit||value.candidate_commit||'待生成')}});
  const edges=sources.map(id=>({id:'input:'+id,source:'source:'+id,target:'pipeline:'+pipeline.id,type:'smoothstep',markerEnd:{type:'arrowclosed',color:'#111'},style:{stroke:'#111',strokeWidth:1.5}}));
  if(active){nodes.push({id:'result:'+value.run_id,type:'default',position:{x:790,y:130},data:{label:'正式成果\n'+value.run_id}});edges.push({id:'result',source:'pipeline:'+pipeline.id,target:'result:'+value.run_id,type:'smoothstep',markerEnd:{type:'arrowclosed',color:'#111'},style:{stroke:'#111',strokeWidth:1.5}});}
  return {nodes,edges};
}
