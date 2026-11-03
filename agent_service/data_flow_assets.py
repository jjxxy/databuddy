"""Versioned views of the typed data plan, without copying source or result records."""
import hashlib
import inspect
import json
import re
import subprocess
from functools import lru_cache

from fastapi import HTTPException

from . import data_workflows as engine, project_data as data, workspace
from . import connectors, result_delivery, workbook_reader
from .business_flow_assets import overview_groups
from .workspace_lifecycle import assert_project_live


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2)+'\n'


def _sha(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


@lru_cache(maxsize=16)
def _source(function):
    return '# '+function.__module__+'.'+function.__name__+'\n'+inspect.getsource(function)


def _implementation_path(function):
    return 'implementation/'+function.__module__.rsplit('.',1)[-1]+'/'+function.__name__+'.py'


def _input_helpers(pid,binding):
    if binding.result_id:return (result_delivery.read_all_handoff,data.require_safe_numeric_lineage)
    source=data.owned(pid,'source',binding.source_id)
    name=str(source.get('name') or '').lower()
    if source.get('kind')=='file':
        if name.endswith('.xlsx'):
            return (connectors.verified_file_source,workbook_reader.read_table_with_title_proposal,workbook_reader.read_table,data._project_text_columns)
        if name.endswith(('.html','.htm')):
            from .evidence_tables import parse_html_tables
            return (connectors.verified_file_source,parse_html_tables)
    return (connectors.read_source,connectors._read_source)


_STAGE_HELPERS={
    'sql':(engine.validate_sql,engine._validate_exact_sql_numeric),
    'aggregate':(engine._require,engine._sort_key),
    'compute':(engine._calculation_semantics,engine._calculate),
    'filter':(engine._matches,),
    'normalize':(workbook_reader.parse_localized_value,engine._coerce),
    'lookup_effective':(engine._blank_lookup_key,engine._columns),
}


def _index(nodes,groups):
    labels={'overview.json':'业务流程概览','dag.json':'详细流程与依赖','io.json':'输入资料与结果引用'}
    for node in nodes:
        labels[node['path']]=node['title']+' · 节点规则'
        for path in node.get('paths',[]):
            labels[path]=node['title']+' · 查询原文' if path.endswith('.sql') else '实现函数 · '+path.split('/')[-1]
        if node.get('handler_path'):labels[node['handler_path']]='登记时的 Python 实现 · '+node['handler_path'].split('/')[-1]
    languages={'py':'python','sql':'sql','json':'json'}
    return {'groups':groups,'nodes':nodes,'files':[{'path':path,'label':label,'language':languages[path.rsplit('.',1)[1]]}
                                                for path,label in labels.items()]}


def _parts(doc, include_handlers, pid=None):
    nodes=[];contents={};phases={}

    def file(path,content):
        contents[path]=content
        return path

    def node(ident,title,kind,phase,deps,definition,handler,helpers=(),sql=None):
        # Prefix + case-sensitive digest keeps valid workflow IDs such as CON, A and a distinct on Windows.
        stem='n_'+ident[:24]+'_'+_sha(ident)[:16]
        node_dir='nodes/'+stem+'/'
        path=file(node_dir+'rule.json',_json(definition))
        entry={'id':ident,'title':title,'kind':kind,'path':path,'depends_on':deps}
        related=[]
        if sql is not None:
            related.append(file(node_dir+'query.sql',sql.replace('\r\n','\n')))
        if include_handlers:
            entry['handler_path']=file(_implementation_path(handler),_source(handler))
            for fn in helpers:
                related.append(file(_implementation_path(fn),_source(fn)))
        if related:entry['paths']=related
        nodes.append(entry)
        phases[kind]=(phase,phase)

    for ident,schema in sorted(doc.plan.inputs.items()):
        binding=doc.bindings[ident]
        node(ident,schema.meaning,'source','读取来源资料',[],{'schema':schema.model_dump(),'binding':binding.model_dump()},
             data.read_input,_input_helpers(pid,binding) if include_handlers else ())
    for stage in doc.plan.stages:
        phase=('关联与合并资料' if stage.kind in ('lookup','lookup_multi','lookup_effective','union_rows') else
               '计算与汇总指标' if stage.kind in ('compute','aggregate','fx_convert','sql') else '整理明细与口径')
        handler={'sql':engine._sql,'aggregate':engine._aggregate}.get(stage.kind,engine._operate)
        node(stage.id,stage.description,stage.kind,phase,engine.dependencies_for(stage),stage.model_dump(),handler,
             _STAGE_HELPERS.get(stage.kind,(engine._columns,)),getattr(stage,'sql',None))
    node('__overview_result__','交付已选定的结果','output','交付结果',[doc.plan.output],
         {'output':doc.plan.output,'contract':doc.contract,'reviewed_mapping_ids':doc.reviewed_mapping_ids},
         result_delivery.write_handoff,(result_delivery.verify_handoff,result_delivery._pack))
    groups=overview_groups(nodes,phases)
    file('dag.json',_json({'version':1,'nodes':nodes,'output':doc.plan.output,'budget':doc.plan.budget.model_dump()}))
    file('overview.json',_json({'version':1,'groups':groups}))
    file('io.json',_json({'inputs':{k:v.model_dump(exclude_none=True) for k,v in doc.bindings.items()},
         'result_node':doc.plan.output,'result_location':'运行成功后在该次运行的交接包读取结果与来源凭据'}))
    return _index(nodes,groups),contents


def save(pid,wid,doc):
    _,files=_parts(doc,True,pid)
    workspace.write_flow_assets(pid,'data-flows/'+wid,files)
    return {path:workspace.flow_blob(content) for path,content in files.items()}


def _snapshot(pid,wid,expected,commit=None):
    assert_project_live(pid)
    record=data.owned(pid,'data_workflow',wid)
    if record.get('archived'):raise ValueError('流程不可用')
    doc=data.load_document(pid,record['path'])
    if data.digest(doc.model_dump())!=expected:raise HTTPException(409,'流程已变化，请重新打开')
    hashes=record.get('code_asset_hashes') if record.get('draft_sha256')==expected else None
    repo=workspace.folder(pid)
    if commit:
        if not re.fullmatch(r'[a-f0-9]{40}',commit):raise ValueError('项目版本无效')
        workspace.resolve(pid,commit)
    if hashes:
        commit=commit or workspace.git(repo,'rev-parse','HEAD')
        prefix='files/data-flows/'+wid+'/'
        try:
            frozen=workspace.git_content(repo,commit,'files/'+record['path'])
            if data.digest(data.WorkflowDocument.model_validate_json(frozen).model_dump())!=expected:raise ValueError()
            workspace.verify_flow_tree(repo,prefix,commit,hashes)
        except (ValueError,subprocess.CalledProcessError):
            raise HTTPException(409,'流程定义或节点文件与登记的版本不一致，请重新打开') from None
        def read(path):
            if path not in hashes:raise ValueError('文件不属于该流程版本')
            return workspace.git_content(repo,commit,prefix+path).decode('utf-8')
        index=_index(json.loads(read('dag.json'))['nodes'],json.loads(read('overview.json'))['groups'])
    else:
        if commit:raise HTTPException(409,'当前版本没有可核验的 Git 节点文件')
        index,derived=_parts(doc,False)
        read=derived.__getitem__
    index.update(flow_id=wid,digest=expected,commit=commit,storage='git' if hashes else 'derived')
    return index,read


def get_assets(pid,wid,expected,commit=None,path=None):
    with workspace.lock(pid):
        index,read=_snapshot(pid,wid,expected,commit)
        if path is None:return index
        file=next((f for f in index['files'] if f['path']==path),None)
        if not file:raise HTTPException(404,'所选文件不属于该流程版本')
        return {**file,**{k:index[k] for k in ('flow_id','digest','commit','storage')},'content':read(path)}


def export_files(pid,wid,expected):
    # The export caller holds the project lock for the entire bundle.
    index,read=_snapshot(pid,wid,expected)
    return {f['path']:read(f['path']) for f in index['files']}
