import React from 'react';
import {createRoot} from 'react-dom/client';
import {ReactFlow,Handle,Position,MarkerType,Controls} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './web-dag.css';

function BusinessNode({data}) {
 return <><Handle type="target" position={Position.Left} isConnectable={false}/><button className="nexus-node-action" type="button" onClick={event=>{event.stopPropagation();data.select(data.id)}}><strong>{data.label}</strong><span>{data.caption}</span></button><Handle type="source" position={Position.Right} isConnectable={false}/></>;
}
const nodeTypes={business:BusinessNode};
const labels={'node.a11yDescription.default':'按回车选择步骤查看说明。','node.a11yDescription.keyboardDisabled':'按回车选择步骤查看说明。','edge.a11yDescription.default':'流程步骤之间的依赖关系。','controls.ariaLabel':'流程图视图控制','controls.zoomIn.ariaLabel':'放大','controls.zoomOut.ariaLabel':'缩小','controls.fitView.ariaLabel':'显示完整流程'};
function Graph({items,onSelect,onInit}) {
 const nodes=items.map(item=>({id:item.id,type:'business',position:{x:item.x,y:item.y},data:{id:item.id,label:item.label,caption:item.caption||'',select:onSelect},draggable:false,connectable:false}));
 const known=new Set(nodes.map(node=>node.id));
 const edges=items.flatMap(item=>item.deps.filter(id=>known.has(id)).map(id=>({id:'e:'+encodeURIComponent(id)+':'+encodeURIComponent(item.id),source:id,target:item.id,type:'smoothstep',markerEnd:{type:MarkerType.ArrowClosed,color:'#303a35'},style:{stroke:'#303a35',strokeWidth:1.5},focusable:false})));
 return <ReactFlow ariaLabelConfig={labels} nodes={nodes} edges={edges} nodeTypes={nodeTypes} onInit={onInit} onNodeClick={(_,node)=>onSelect(node.id)} onNodeKeyDown={(event,node)=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();onSelect(node.id)}}} fitView fitViewOptions={{padding:.15,minZoom:.75,maxZoom:1}} minZoom={.35} nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false} elementsSelectable={false} deleteKeyCode={null} colorMode="light" proOptions={{hideAttribution:false}}><Controls showInteractive={false}/></ReactFlow>;
}
class NexusFlowElement extends HTMLElement {
 connectedCallback(){if(!this.reactRoot)this.reactRoot=createRoot(this);this.visible=false;this.observer=new ResizeObserver(entries=>{const rect=entries[0]?.contentRect;const visible=!!rect&&rect.width>0&&rect.height>0;if(visible&&!this.visible)requestAnimationFrame(()=>requestAnimationFrame(()=>{if(this.isConnected)this.instance?.fitView({padding:.15,minZoom:.75,maxZoom:1})}));this.visible=visible});this.observer.observe(this);this.paint()}
 disconnectedCallback(){this.observer?.disconnect();this.instance=null;if(this.reactRoot){this.reactRoot.unmount();this.reactRoot=null}}
 paint(){if(this.reactRoot&&this.items)this.reactRoot.render(<Graph items={this.items} onSelect={this.onSelect} onInit={instance=>{this.instance=instance}}/>)}
 setGraph(items,onSelect){this.items=items;this.onSelect=onSelect;this.paint()}
}
customElements.define('nexus-flow-graph',NexusFlowElement);
window.NexusFlowGraph={mount(parent,items,onSelect){const element=document.createElement('nexus-flow-graph');parent.append(element);element.setGraph(items,onSelect);return {update(){element.paint()}}}};
