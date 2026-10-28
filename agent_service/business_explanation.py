"""Business-readable evidence for one frozen, verified report.

This projects recorded receipts only. It does not run a model, infer missing
business rules, or claim output-row/cell-level lineage or financial approval.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .data_workflows import digest, dependencies_for
from .jobs import get_job
from .platform_files import open_regular_read
from .store import get
from .workspace_artifacts import ArtifactError, ArtifactGone, _record


def _read_verified(path: Path, expected: str, limit: int = 2 * 1024 * 1024) -> bytes:
    """Read the actual bytes used for the explanation and bind them to the receipt."""
    descriptor = open_regular_read(path)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit:
            raise ArtifactError('报告依据超过读取范围，未返回未经核验的说明')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ArtifactError('报告依据在读取时发生变化，请重新打开')
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != expected:
        raise ArtifactError('报告文件与已保存的完整性记录不一致')
    return raw


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _field_names(plan):
    """Use labels only when a field identifier has one unambiguous meaning."""
    observed = {}
    def add(column):
        if not isinstance(column, dict):
            return
        name = column.get('name')
        if not isinstance(name, str):
            return
        label = column.get('label') or column.get('meaning') or name
        if isinstance(label, str):
            observed.setdefault(name, set()).add(label)
    for schema in plan.get('inputs', {}).values():
        for column in schema.get('columns', []):
            add(column)
    for stage in plan.get('stages', []):
        add(stage.get('output'))
        for item in stage.get('calculations', []):
            add(item.get('output'))
        for item in stage.get('aggregations', []):
            add(item.get('output'))
        for item in stage.get('rules', []):
            add(item.get('output'))
        for item in stage.get('outputs', {}).values():
            add(item)
        for item in (stage.get('output_schema') or {}).get('columns', []):
            add(item)
    return {name: next(iter(labels)) if len(labels) == 1 else name for name, labels in observed.items()}


_LABELS = {'select':'选择字段', 'drop':'移除字段', 'filter':'筛选数据',
           'reclassify':'按映射归类', 'compute':'计算指标', 'aggregate':'汇总数据',
           'lookup':'关联对照资料', 'union_rows':'合并记录', 'sql':'按保存的规则处理',
           'normalize':'规范数据格式'}
_OPS = {'eq':'等于','neq':'不等于','gt':'大于','gte':'大于或等于','lt':'小于',
        'lte':'小于或等于','in':'属于','not_in':'不属于','contains':'包含',
        'is_null':'为空','not_null':'不为空'}
_MATH = {'add':'加','subtract':'减','multiply':'乘','divide':'除以'}
_AGG = {'sum':'求和','min':'取最小值','max':'取最大值','count':'计数','concat':'按顺序合并文本'}


def _bounded(value, limit=1000):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + '…（说明已截断，请核对完整加工方案）'


def _stage_description(stage, names, table_names=None):
    def label(name):
        return names.get(name, name) if isinstance(name, str) else '未记录字段'
    kind = stage.get('kind')
    if kind in ('select', 'drop'):
        return _bounded(('保留' if kind == 'select' else '移除') + '字段：' + '、'.join(label(x) for x in stage.get('columns', [])))
    if kind == 'filter':
        descriptions = []
        for item in stage.get('conditions', []):
            operand = item.get('value')
            suffix = '' if item.get('operator') in ('is_null', 'not_null') else ' ' + json.dumps(operand, ensure_ascii=False)
            descriptions.append(label(item.get('column')) + ' ' + _OPS.get(item.get('operator'), '按已保存条件') + suffix)
        return _bounded('同时满足：' + '；'.join(descriptions))
    if kind == 'compute':
        descriptions = []
        for item in stage.get('calculations', []):
            output = item.get('output') or {}
            expression = label(item.get('left')) + ' ' + _MATH.get(item.get('operation'), '按已保存规则计算') + ' ' + label(item.get('right'))
            scale = item.get('rounding_scale')
            descriptions.append(label(output.get('name')) + ' = ' + expression + ('；保留' + str(scale) + '位小数' if type(scale) is int else '；不允许静默舍入'))
        return _bounded('；'.join(descriptions))
    if kind == 'aggregate':
        groups = stage.get('by', [])
        text = '按' + ('、'.join(label(x) for x in groups) if groups else '全部数据') + '汇总。'
        actions = []
        for item in stage.get('aggregations', []):
            actions.append(label((item.get('output') or {}).get('name')) + '：' + _AGG.get(item.get('function'), '按保存规则汇总') + (' ' + label(item.get('column')) if item.get('column') else ''))
        return _bounded(text + '；'.join(actions))
    if kind == 'lookup':
        action = '无法匹配时停止' if stage.get('unmapped') == 'error' else '无法匹配时留空'
        outputs = '、'.join(label(value.get('name')) for value in stage.get('outputs', {}).values() if isinstance(value, dict))
        right=(table_names or {}).get(stage.get('right'),'名称未记录的参考资料')
        return _bounded('用 ' + label(stage.get('left_key')) + ' 对照 '+right+' 的 ' + label(stage.get('right_key')) + '，补充 ' + outputs + '；' + action + '。')
    if kind == 'reclassify':
        mapping = stage.get('mapping', {})
        lines = [str(a) + ' → ' + str(b) for a, b in list(mapping.items())[:30]]
        if len(mapping)>30:
            lines.append('其余映射请查看完整加工方案')
        missing = '未匹配时停止' if stage.get('unmapped') == 'error' else '未匹配时保留原值'
        return _bounded('将 ' + label(stage.get('column')) + ' 映射为 ' + label((stage.get('output') or {}).get('name')) + '：' + '；'.join(lines) + '；' + missing + '。')
    if kind == 'normalize':
        items=[]
        for rule in stage.get('rules', []):
            output=rule.get('output') or {}
            text=label(rule.get('column'))+' → '+label(output.get('name'))
            if rule.get('source_unit'):
                text+='；原单位 '+str(rule['source_unit'])
            if rule.get('unit_multiplier') not in (None,'1'):
                text+='；转换系数 '+str(rule['unit_multiplier'])
            if output.get('unit'):
                text+='；结果单位 '+str(output['unit'])
            if output.get('value_format')=='percent_fraction':
                text+='；百分比按比例小数保存'
            elif output.get('value_format')=='percent_points':
                text+='；百分比按百分点保存'
            if rule.get('date_format'):
                text+='；日期格式 '+str(rule['date_format'])
            text+='；缺失值'+('停止处理' if rule.get('missing_policy')=='error' else '保留为空')
            items.append(text)
        return _bounded('；'.join(items))
    if kind == 'union_rows':
        return '把已声明的多份同口径数据按行合并；步骤回执记录各输入和输出行数。'
    if kind == 'sql':
        return '按保存的数据规则生成结果；字段含义和业务口径需要结合完整加工方案复核。'
    return '该步骤缺少可解释的规则，请查看冻结的加工方案。'


def _selection(value):
    if not isinstance(value, dict):
        return None
    sheet = value.get('sheet') if isinstance(value.get('sheet'), str) else None
    area = value.get('range')
    area = area if isinstance(area, list) and len(area) == 4 and all(type(x) is int and x >= 1 for x in area) else None
    headers = value.get('header_rows')
    headers = headers if isinstance(headers, list) and all(type(x) is int and x >= 1 for x in headers) else None
    return {'sheet': sheet, 'range': area, 'header_rows': headers}


def _source_version_state(pid, source_id, revision, content_hash):
    """Compare local version records only; this is not upstream freshness."""
    from sqlalchemy.exc import SQLAlchemyError
    try:
        current = get('source',source_id)
    except (KeyError,ValueError,TypeError,SQLAlchemyError):
        return 'unknown'
    if (not isinstance(current,dict) or current.get('project_id')!=pid or current.get('id')!=source_id or current.get('kind')!='file'
            or current.get('archived') or current.get('deleted_at')):
        return 'unknown'
    latest = current.get('revision')
    latest_hash = current.get('content_hash')
    if (type(latest) is not int or latest<1 or not isinstance(latest_hash,str)
            or not re.fullmatch(r'[a-f0-9]{64}',latest_hash)):
        return 'unknown'
    if latest==revision and latest_hash==content_hash:
        return 'current'
    if latest>revision:
        return 'changed'
    return 'unknown'


def _contract_checks(contract, declared=None, *, expected_rows=None, evidence_consistent=True, externally_imported=False):
    if not isinstance(contract, dict) or not isinstance(contract.get('checks'), list):
        return [{'label':'数据验收规则', 'status':'not_checked', 'detail':'本次运行没有记录数据验收契约；不能据此判断业务数据准确。'}]
    from .data_contracts import require_exact_numeric_evidence
    expected = {'row_count'}
    if isinstance(declared,dict):
        expected.update('not_null:'+field for field in declared.get('not_null',[]) if isinstance(field,str))
        expected.update('unique:'+','.join(fields) for fields in declared.get('unique',[])
                        if isinstance(fields,list) and all(isinstance(field,str) for field in fields))
        for prefix,key in [('range:','ranges'),('total:','totals')]:
            values=declared.get(key) if isinstance(declared.get(key),dict) else {}
            expected.update(prefix+field for field in values if isinstance(field,str))
    recorded_names=[item.get('name') for item in contract['checks'] if isinstance(item,dict)]
    complete=all(recorded_names.count(name)==1 for name in expected)
    bound = (isinstance(declared,dict) and complete and evidence_consistent
             and type(expected_rows) is int and expected_rows>=0
             and type(contract.get('rows')) is int and contract['rows']==expected_rows
             and hashlib.sha256(json.dumps(declared,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()==contract.get('contract_sha256'))
    try:
        require_exact_numeric_evidence(contract,declared,expected_rows=expected_rows)
        exact = bound and contract.get('numeric_policy')=='exact_decimal_v1' and not externally_imported
    except ValueError:
        exact = False
    result = []
    recorded = set()
    for item in contract['checks']:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        name = item['name']
        recorded.add(name)
        kind, _, field = name.partition(':')
        labels = {'row_count':'行数范围','missing_column':'必需字段','not_null':'缺失值',
                  'unique':'唯一性','range':'数值范围','total':'总额对账'}
        label = labels.get(kind, '已配置的检查') + ('：' + field if field else '')
        if kind not in labels:
            status, detail = 'not_checked', '无法识别该检查的含义，不能把它作为已核验依据。'
        elif not bound:
            status, detail = 'needs_review', '检查回执不能与冻结的数据验收规则完整绑定，请核对后重新运行。'
        elif kind == 'row_count' and 'min_rows' not in declared and 'max_rows' not in declared:
            status, detail = 'not_checked', '未设置行数上下限，未按预期数量核对行数。'
        elif kind in ('range', 'total') and not exact:
            status, detail = 'needs_review', '历史数值检查没有可靠的精确计算版本记录；请重新运行并核对，不据旧通过结果认定精确。'
        elif item.get('status') == 'pass' and type(item.get('failures')) is int and item['failures']==0:
            status = 'passed'
            if kind=='row_count':
                bounds = []
                if 'min_rows' in declared:bounds.append('至少 '+str(declared['min_rows'])+' 行')
                if 'max_rows' in declared:bounds.append('至多 '+str(declared['max_rows'])+' 行')
                detail = '按保存的行数范围（'+'，'.join(bounds)+'）检查通过；规则与来源仍需业务核对。'
            elif kind=='range':
                bounds = declared.get('ranges',{}).get(field,{})
                detail = '按保存的数值范围 '+json.dumps(bounds,ensure_ascii=False)+' 检查非空值；空值需要另行配置非空检查。范围口径仍需业务核对。'
            elif kind=='total':
                bounds = declared.get('totals',{}).get(field,{})
                detail = '按保存的预期总额及容差 '+json.dumps(bounds,ensure_ascii=False)+' 对账通过；预期值和业务口径仍需业务核对。'
            elif kind=='missing_column':
                status,detail = 'needs_review','字段缺失记录与通过状态矛盾，请重新核对。'
            else:
                detail = '按本次保存的规则检查通过；规则本身和来源真实情况仍需业务核对。'
        elif item.get('status') == 'fail':
            status, detail = 'needs_review', '该已配置检查未通过，请核对数据和规则。'
        else:
            status, detail = 'not_checked', '检查结果未记录或无法识别。'
        result.append({'label':label, 'status':status, 'detail':detail})
    if isinstance(declared,dict):
        for prefix,key,title in [('range:','ranges','数值范围'),('total:','totals','总额对账')]:
            fields=declared.get(key) if isinstance(declared.get(key),dict) else {}
            for field in fields:
                if prefix+field not in recorded:
                    result.append({'label':title+'：'+field,'status':'not_checked',
                                   'detail':'方案声明了这项检查，但结果没有保存对应回执，请重新运行并核对。'})
        for field in declared.get('not_null',[]):
            if isinstance(field,str) and 'not_null:'+field not in recorded:
                result.append({'label':'缺失值：'+field,'status':'not_checked','detail':'方案声明了非空检查，但结果未保存对应回执。'})
        for fields in declared.get('unique',[]):
            if isinstance(fields,list) and all(isinstance(field,str) for field in fields):
                joined=','.join(fields)
                if 'unique:'+joined not in recorded:
                    result.append({'label':'唯一性：'+joined,'status':'not_checked','detail':'方案声明了唯一性检查，但结果未保存对应回执。'})
        if declared.get('required'):
            result.append({'label':'必需字段','status':'not_checked',
                           'detail':'当前回执只记录缺失字段，没有逐字段的独立通过记录；请核对字段含义。'})
    return result or [{'label':'数据验收规则','status':'not_checked','detail':'契约没有记录具体检查结果。'}]


def explain_artifact(pid: str, artifact_id: str) -> dict:
    """Explain only the exact report artifact selected by its authenticated owner."""
    from .business_reports import report_file
    from .project_data import WorkflowDocument, result_folder, require_safe_numeric_lineage
    from .data_contracts import NumericLineageError
    from .result_delivery import read_handoff

    artifact = _record(pid, artifact_id)
    if artifact.get('kind') != 'report':
        raise ArtifactError('该成果暂未记录可验证的业务依据')
    private = artifact.get('private') or {}
    report_id = private.get('report_id')
    if not isinstance(report_id, str) or report_id != artifact.get('resource_id'):
        raise ArtifactError('报告与所选成果的版本记录不一致')
    record = get('business_report', report_id)
    path = report_file(pid, report_id, 'manifest')
    manifest = json.loads(_read_verified(path, record['manifest_sha256']))
    # This also binds the visible Markdown to its independent server receipt.
    markdown = report_file(pid, report_id, 'markdown')
    _read_verified(markdown, record['markdown_sha256'])
    run_id = record.get('run_id')
    if (manifest.get('project_id') != pid or manifest.get('report_id') != report_id
            or not isinstance(run_id, str) or manifest.get('run_id') != run_id
            or manifest.get('markdown_sha256') != record.get('markdown_sha256')
            or artifact.get('sha256') != record.get('manifest_sha256')):
        raise ArtifactError('报告与所选项目或版本的记录不一致')
    run = get_job(run_id)
    folder, meta = result_folder(pid, run_id)
    artifact_sources = artifact.get('source_refs')
    if artifact_sources and (not isinstance(artifact_sources, list) or len(artifact_sources) != 1
            or not isinstance(artifact_sources[0], dict)
            or artifact_sources[0].get('result_id') != run_id
            or artifact_sources[0].get('handoff_sha256') != meta.get('handoff_sha256')):
        raise ArtifactError('所选成果的来源版本与报告结果不一致')
    document = WorkflowDocument.model_validate(run.get('workflow_document'))
    doc = document.model_dump()
    if (digest(doc) != run.get('commit') or manifest.get('plan_sha256') != run.get('commit')
            or manifest.get('handoff_sha256') != meta.get('handoff_sha256')
            or record.get('handoff_sha256') != meta.get('handoff_sha256')
            or digest(manifest.get('recipe')) != record.get('recipe_sha256')):
        raise ArtifactError('报告、加工方案与结果版本不一致，未返回说明')
    page = read_handoff(folder / 'handoff', offset=0, limit=1,
                        expected_manifest_sha256=meta['handoff_sha256'])
    context = page.get('context')
    if not isinstance(context, dict) or context.get('plan_sha256') != run['commit']:
        raise ArtifactError('结果依据与冻结方案不一致，未返回说明')
    plan = doc['plan']; names = _field_names(plan)
    evidence = context.get('input_evidence') if isinstance(context.get('input_evidence'), dict) else {}
    snapshot = run.get('source_snapshot') if isinstance(run.get('source_snapshot'), dict) else {}
    sources = []
    for alias, binding in doc['bindings'].items():
        proof = evidence.get(alias) if isinstance(evidence.get(alias), dict) else {}
        sid = binding.get('source_id'); frozen = snapshot.get(sid) if sid else None
        if isinstance(frozen, dict) and (frozen.get('id') != sid or frozen.get('project_id') != pid):
            raise ArtifactError('冻结来源与当前项目不一致')
        if sid and proof.get('source_id') not in (None, sid):
            raise ArtifactError('来源依据与冻结绑定不一致')
        if isinstance(frozen, dict) and proof.get('content_hash') and frozen.get('content_hash') != proof['content_hash']:
            raise ArtifactError('来源版本与已保存的内容摘要不一致')
        selection = proof.get('selection')
        if isinstance(selection,dict) and selection.get('file_sha256') and selection['file_sha256'] != proof.get('content_hash'):
            raise ArtifactError('表格选择记录与来源文件摘要不一致')
        revision = proof.get('source_revision')
        if type(revision) is not int or revision < 1:
            revision = None
        if isinstance(frozen, dict) and revision is not None and frozen.get('revision', 1) != revision:
            raise ArtifactError('来源版本与冻结快照不一致')
        source_hash = proof.get('content_hash')
        recorded_file = (isinstance(sid,str) and isinstance(frozen,dict) and frozen.get('kind')=='file'
                         and proof.get('source_id')==sid and revision is not None
                         and isinstance(source_hash,str) and re.fullmatch(r'[a-f0-9]{64}',source_hash)
                         and frozen.get('content_hash')==source_hash)
        name = '上游保存的数据结果（业务仍需复核）' if binding.get('result_id') else '来源名称未记录'
        if recorded_file and isinstance(frozen.get('revision_id'),str):
            try:
                version = get('source_version',frozen['revision_id'])
            except KeyError:
                version = None
            if (isinstance(version,dict) and version.get('project_id')==pid and version.get('source_id')==sid
                    and version.get('revision')==revision and version.get('content_hash')==source_hash
                    and isinstance(version.get('name'),str)):
                name = version['name']
        sources.append({'name':name,'source_id':sid if recorded_file else None,'revision':revision,
                        'rows':_count(proof.get('rows')),'selection':_selection(proof.get('selection')),
                        'content_hash':source_hash if recorded_file else None,
                        'availability':'recorded' if recorded_file else 'unknown',
                        'source_version_state':_source_version_state(pid,sid,revision,source_hash) if recorded_file else 'unknown'})
    frozen_stages = {value['id']: value for value in plan.get('stages', [])}
    frozen_stage_objects = {value.id:value for value in document.plan.stages}
    stage_order = {value['id']:index for index,value in enumerate(plan.get('stages',[]))}
    table_names = {alias:(source['name'] if source['name']!='来源名称未记录' else '输入名称未记录')
                   for alias,source in zip(doc['bindings'],sources)}
    stage_numbers = {}
    grains = {alias:schema.get('grain') for alias,schema in plan.get('inputs',{}).items()
              if isinstance(schema,dict)}
    receipts = page.get('stages') if isinstance(page.get('stages'), list) else []
    steps = []; seen = set(); prior = {}; previous_order = -1
    for receipt in receipts:
        if not isinstance(receipt, dict) or receipt.get('stage_id') not in frozen_stages or receipt['stage_id'] in seen:
            raise ArtifactError('加工步骤记录不完整或与方案不一致')
        position=stage_order[receipt['stage_id']]
        if position<=previous_order:raise ArtifactError('加工步骤顺序与冻结方案不一致')
        previous_order=position
        stage = frozen_stages[receipt['stage_id']]; seen.add(receipt['stage_id'])
        if receipt.get('stage_sha256') != digest(stage) or digest(receipt.get('lineage')) != digest(stage):
            raise ArtifactError('加工步骤与冻结方案不一致')
        saved_inputs = receipt.get('inputs')
        if saved_inputs is not None:
            if (not isinstance(saved_inputs,list) or any(not isinstance(item,dict) for item in saved_inputs)
                    or [item.get('table') for item in saved_inputs] != dependencies_for(frozen_stage_objects[receipt['stage_id']])):
                raise ArtifactError('加工步骤的输入与冻结方案不一致')
            for item in saved_inputs:
                earlier=prior.get(item['table'])
                if earlier is not None and (item.get('sha256')!=earlier.get('output_sha256')
                                             or item.get('rows')!=earlier.get('output_rows')):
                    raise ArtifactError('加工步骤之间的数据记录不一致')
        if stage['kind']=='aggregate':
            grain = stage.get('by')
        elif stage['kind']=='sql':
            grain = (stage.get('output_schema') or {}).get('grain')
        else:
            dependency = stage.get('input')
            if dependency is None and stage.get('inputs'):
                dependency = stage['inputs'][0]
            grain = grains.get(dependency)
        grains[receipt['stage_id']] = grain
        inputs = []
        for item in receipt.get('inputs', []):
            if isinstance(item,dict):
                key=item.get('table')
                name=table_names.get(key) or ('第'+str(stage_numbers[key])+'步结果' if key in stage_numbers else '输入名称未记录')
                inputs.append({'name':name,'rows':_count(item.get('rows'))})
        checks = receipt.get('checks') if isinstance(receipt.get('checks'),dict) else {}
        step_checks = []
        for key, title, detail in [('complete_table','完整数据处理','按已保存输入与输出记录检查；不代表来源真实。'),
                                    ('grain_unique','声明的行粒度','按方案声明的粒度检查，不代表业务口径已确认。'),
                                    ('types_and_units_declared','类型与单位','按方案声明检查，单位和含义仍需业务核对。')]:
            recorded = saved_inputs is not None and checks.get(key) is True and (key != 'grain_unique' or bool(grain))
            missing = ('该步骤未记录完整输入依据。' if saved_inputs is None else
                       '该步骤未声明可核对的行粒度。' if key == 'grain_unique' and not grain else '该步骤未记录这项检查。')
            step_checks.append({'label':title,'status':'passed' if recorded else 'not_checked','detail':detail if recorded else missing})
        steps.append({'label':_LABELS.get(stage['kind'],'已保存的处理步骤'),
                      'description':_stage_description(stage,names,table_names),
                      'input_rows':inputs[0]['rows'] if len(inputs)==1 else None,
                      'inputs':inputs,'output_rows':_count(receipt.get('output_rows')),'checks':step_checks})
        stage_numbers[receipt['stage_id']]=len(steps)
        table_names[receipt['stage_id']]='第'+str(len(steps))+'步结果'
        prior[receipt['stage_id']]=receipt
    evaluation = manifest.get('evaluation')
    recipe = manifest.get('recipe')
    if not isinstance(evaluation,dict) or not isinstance(recipe,dict) or not isinstance(evaluation.get('assertions'),list):
        raise ArtifactError('报告未保存可核对的指标依据')
    if (type(evaluation.get('input_rows')) is int and evaluation['input_rows']!=page['total']
            or type(evaluation.get('selected_rows')) is int and not 0<=evaluation['selected_rows']<=page['total']):
        raise ArtifactError('报告计算范围与完整结果的行数不一致')
    output_semantics = manifest.get('output_semantics') or {}
    formats = {column.get('name'):column.get('value_format')
               for column in output_semantics.get('columns',[]) if isinstance(column,dict)}
    metrics=[]
    for item in evaluation['assertions']:
        if not isinstance(item,dict):
            raise ArtifactError('报告指标依据格式无效')
        if (type(evaluation.get('selected_rows')) is int and type(item.get('rows')) is int
                and item['rows']!=evaluation['selected_rows']):
            raise ArtifactError('报告指标的覆盖行数与计算范围不一致')
        label = item.get('label') if isinstance(item.get('label'),str) else '未命名指标'
        kind = item.get('kind')
        columns = [names.get(c,c) for c in item.get('columns',[]) if isinstance(c,str)]
        calculation = {'sum':'求和','value':'单个值','row_count':'计数','ratio':'实际值除以比较基数','variance':'实际值减比较基数'}.get(kind,'按保存的指标规则计算')
        description = calculation + ('：' + '、'.join(columns) if columns else '')
        if kind in ('ratio','variance'):
            actual, baseline = item.get('actual'), item.get('baseline')
            if isinstance(actual,str) and isinstance(baseline,str):
                reduction = '分别求和后' if item.get('reduction')=='sum' else '分别取值后'
                description += '；' + reduction + '实际值 ' + actual + '，比较基数 ' + baseline
                if kind=='variance' and item.get('unit')=='百分点' and item.get('columns') and formats.get(item['columns'][0])=='percent_fraction':
                    description += '；两者相减后乘以100，结果以百分点表示'
            if kind=='ratio' and not str(item.get('value','')).startswith('不适用'):
                description += '；结果为原始小数比值，1 相当于 100%'
                if item.get('rounding_scale') is not None:
                    description += '；比率保留' + str(item['rounding_scale']) + '位小数（四舍六入五取偶）'
            if kind=='variance' and isinstance(item.get('relative_change'),str):
                description += '；相对变化率 '
                if item.get('relative_change_unit')=='1' and not item['relative_change'].startswith('不适用'):
                    description += '（原始小数比值，1 相当于 100%）'
                description += item['relative_change']
                if item.get('rounding_scale') is not None and not item['relative_change'].startswith('不适用'):
                    description += '（变化率保留' + str(item['rounding_scale']) + '位小数，四舍六入五取偶）'
        elif item.get('rounding_scale') is not None:
            description += '；此求和或取值未应用舍入参数'
        if kind in ('sum','value') and item.get('value_format')=='percent_fraction':
            description += '；来源按比率存储，显示的百分数乘以100'
        if item.get('period'):
            if recipe.get('period_column'):
                description += '；按期间字段 ' + names.get(recipe['period_column'],recipe['period_column']) + ' 等于 ' + str(item['period']) + ' 筛选'
            else:
                description += '；期间依据结果字段的声明：' + str(item['period']) + '（未按日期列逐行过滤）'
        if item.get('filters'):
            description += '；筛选条件：' + json.dumps(item['filters'],ensure_ascii=False)
        value = item.get('display_value',item.get('value'))
        unit = item.get('display_unit',item.get('unit'))
        if isinstance(value,str) and value.startswith('不适用'):
            unit = ''
        metrics.append({'label':label,'value':value if isinstance(value,str) else '精确值未记录',
                        'unit':unit if isinstance(unit,str) else '未记录','description':_bounded(description),
                        'rows':_count(item.get('rows'))})
    checks = [{'label':'报告与结果文件完整性','status':'passed',
               'detail':'报告清单、文字版、冻结方案与完整结果的保存摘要一致；Office 文件下载时另行核验。这不证明来源或业务口径正确。'}]
    if (type(evaluation.get('input_rows')) is not int or type(evaluation.get('selected_rows')) is not int
            or any(not isinstance(item,dict) or type(item.get('rows')) is not int for item in evaluation['assertions'])):
        checks.append({'label':'报告覆盖范围','status':'not_checked',
                       'detail':'报告没有完整记录输入或指标覆盖行数，不能确认其范围。'})
    try:
        require_safe_numeric_lineage(pid,run_id,root_meta=meta,root_page=page)
    except NumericLineageError as error:
        detail = ('本次或上游历史计算包含可能丢失精度的 SQL 数值运算；请从原始资料重新计算并核对。'
                  if error.reason=='unsafe_sql' else
                  '本次或上游的历史数值计算证据无法完整核对；请从原始资料重新计算并核对。')
        checks.append({'label':'历史数值计算','status':'needs_review','detail':detail})
    evidence_consistent = (type(meta.get('records')) is int and meta['records']==page['total']
                           and context.get('data_contract')==meta.get('contract'))
    checks.extend(_contract_checks(context.get('data_contract'),doc.get('contract'),expected_rows=page['total'],
                                   evidence_consistent=evidence_consistent,externally_imported=bool(run.get('external_agent_import'))))
    if not evidence_consistent:
        checks.append({'label':'验收回执一致性','status':'needs_review',
                       'detail':'结果行数或验收回执与冻结结果不一致，请从原始资料重新运行并核对。'})
    for assumption in recipe.get('assumptions',[]):
        if isinstance(assumption,str):
            checks.append({'label':'用户声明的假设','status':'not_checked',
                           'detail':_bounded(assumption)+'；该假设未独立核实。'})
    if len(receipts) != len(plan.get('stages', [])):
        checks.append({'label':'加工步骤记录','status':'not_checked','detail':'本次历史结果未记录全部步骤回执，不能补猜缺失过程。'})
    if manifest.get('requires_semantic_review'):
        checks.append({'label':'业务口径','status':'needs_review','detail':'该结果含需要业务核对的语义或规则；采用状态不能证明本次逐项人工复核。'})
    else:
        checks.append({'label':'业务口径','status':'not_checked','detail':'未记录独立的业务正确性认证；请核对期间、单位、取数范围和规则。'})
    adopted = manifest.get('server_adopted_result')
    checks.append({'label':'报告生成时的结果状态','status':'not_checked',
                   'detail':'生成报告时结果已采用，可能由已采用方案自动刷新；未记录逐次人工复核。' if adopted is True else
                            '生成报告时该运行尚未采用；未记录逐次人工复核。' if adopted is False else '生成报告时的采用状态未记录。'})
    limitations = ['完整性校验和所列规则通过不等于来源真实、业务口径正确或报告已经财务审批。',
                   '本页展示当时保存的数值断言并核对文件摘要；本次打开未独立重算，也未逐格核对。',
                   '目前记录来源和加工步骤之间的依赖，不提供每个结果数字到原件单元格的完整反向追溯。',
                   '来源版本状态只比较本地登记，不代表原件仍可访问或上游业务系统数据最新；不能自动改用最新版本。']
    if any(isinstance(proof,dict) and proof.get('formula_cache_freshness')=='unknown' for proof in evidence.values()):
        limitations.append('如果 Excel 含公式，使用文件内保存的结果；公式缓存是否最新未核验，未重新计算。')
    if recipe.get('assumptions'):
        limitations.append('报告包含用户声明的假设，假设与业务原因未自动核实。')
    identity = [pid,'result',run_id,'data',meta['handoff_sha256']]
    candidate = 'wa_'+hashlib.sha256(json.dumps(identity,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:44]
    try:
        result_artifact = _record(pid,candidate)
        if (result_artifact.get('kind')!='result' or result_artifact.get('private',{}).get('run_id')!=run_id
                or result_artifact.get('sha256')!=meta['handoff_sha256']):
            raise ArtifactError('完整结果成果与报告依据不一致')
        result_artifact_id = candidate
    except (KeyError,ArtifactGone):
        result_artifact_id = None
    return {'report':{'id':report_id,'title':recipe.get('title') or '业务报告','period':recipe.get('period')},
            'run_id':run_id,'integrity':{'status':'verified','detail':'报告清单、文字版与完整结果摘要已核对；Office 文件下载时另行核验，业务正确性仍需核实。'},
            'result_artifact_id':result_artifact_id,'sources':sources,'steps':steps,'metrics':metrics,'checks':checks,'limitations':limitations}
