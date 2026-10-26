import React,{useEffect,useState} from 'react';
import {createRoot} from 'react-dom/client';
import {ReactFlow,Controls} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './style.css';
import {lineageGraph} from './graph.mjs';

const graphLabels={
 'node.a11yDescription.default':'按回车或空格选择方框，按 Escape 取消选择。此图只用于核对。',
 'node.a11yDescription.keyboardDisabled':'按回车或空格选择方框，按 Escape 取消选择。此图只用于核对。',
 'node.a11yDescription.ariaLiveMessage':()=> '已选择来源或处理记录。',
 'edge.a11yDescription.default':'按回车或空格选择关系线，按 Escape 取消选择。此图只用于核对。',
 'controls.ariaLabel':'处理链视图控制',
 'controls.zoomIn.ariaLabel':'放大',
 'controls.zoomOut.ariaLabel':'缩小',
 'controls.fitView.ariaLabel':'显示完整处理链',
 'controls.interactive.ariaLabel':'切换交互',
 'minimap.ariaLabel':'处理链概览',
 'handle.ariaLabel':'来源与处理的连接点'
};

function App(){
 const [items,setItems]=useState([]),[selected,setSelected]=useState(''),[lineage,setLineage]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[revision,setRevision]=useState(0);
 useEffect(()=>{let live=true;setBusy(true);setError('');
  if(!window.atlasLineage){setError('请从 Atlas 桌面菜单打开此页。');setBusy(false);return;}
  window.atlasLineage.list().then(value=>{if(live){setItems(value);setSelected(old=>value.some(v=>v.id===old)?old:value[0]?.id||'');}}).catch(()=>{if(live)setError('暂时无法读取数据任务，请确认工作台已启动后刷新。');}).finally(()=>{if(live)setBusy(false);});
  return()=>{live=false;};
 },[revision]);
 useEffect(()=>{let live=true;setLineage(null);if(!selected)return;setBusy(true);setError('');window.atlasLineage.get(selected).then(value=>{if(live)setLineage(value);}).catch(()=>{if(live)setError('这项任务的溯源暂时无法读取，请回到工作台核对任务状态。');}).finally(()=>{if(live)setBusy(false);});return()=>{live=false;};},[selected,revision]);
 const pipeline=items.find(value=>value.id===selected),graph=lineageGraph(pipeline,lineage);
 return <main><header><div><p className="eyebrow">ATLAS DATABUDDY</p><h1>数据来源与处理链</h1><p>核对资料经过哪些处理、对应哪个版本，以及哪些成果已正式发布。</p></div><button onClick={()=>setRevision(v=>v+1)} disabled={busy}>刷新</button></header>
 <section className="toolbar" aria-label="选择数据任务"><label>数据任务<select value={selected} onChange={e=>setSelected(e.target.value)} disabled={!items.length||busy}><option value="" disabled>选择数据任务</option>{items.map(item=><option key={item.id} value={item.id}>{item.name||item.id}</option>)}</select></label><span>{busy?'正在读取本机记录…':error?'处理链状态暂时无法确认':!selected?'选择任务后显示处理链':lineage?.run_id?'显示正式版本及来源':'尚未发布正式成果'}</span></section>
 {error?<p className="notice" role="alert">{error}</p>:!items.length&&!busy?<section className="empty"><h2>还没有可显示的数据处理任务</h2><p>回到工作台导入资料并生成处理方案后，这里会展示真实的来源和处理链。</p></section>:<div className="graph" aria-label="来源到处理任务及正式成果的只读关系图"><ReactFlow ariaLabelConfig={graphLabels} nodes={graph.nodes} edges={graph.edges} fitView fitViewOptions={{padding:.3}} nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false} elementsSelectable={true} deleteKeyCode={null} colorMode="light" proOptions={{hideAttribution:false}}><Controls showInteractive={false}/></ReactFlow></div>}
 <footer><p>方框与箭头来自本机 API 的来源 ID、代码版本及已发布运行记录；未发布结果不会显示为正式成果。</p><p>此视图只用于核对，不修改资料、处理规则或发布状态。</p></footer></main>;
}
createRoot(document.getElementById('root')).render(<App/>);
