"""Business-readable projections of frozen, validated data application rules.

The executable plan.json remains the single source of rules. The accompanying
business-plan.json is committed alongside it, so a candidate and a published
version can be explained without consulting today's mutable pipeline record.
It describes the implementation, not proof of correctness or approval.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

from .process_control import executable

_MAX_BYTES = 4 * 1024 * 1024


def build(plan, application_id, project_id, title, *, source_name=None, origin='generated'):
    """Project an already validated/frozen plan into a business-facing view."""
    if not isinstance(plan, dict) or not isinstance(plan.get('spec'), dict):
        raise ValueError('该代码版本缺少可读取的处理规则')
    spec = copy.deepcopy(plan['spec'])
    fields = plan.get('fields') if isinstance(plan.get('fields'), list) else []
    labels = {f['name']: f.get('label') or f['name'] for f in fields
              if isinstance(f, dict) and isinstance(f.get('name'), str)}
    def label(name):return labels.get(name, name) if name else '未指定'
    sources = [{'id': plan.get('source_id'), 'name': source_name,
                'design_revision': plan.get('source_revision'), 'role': '主来源'}]
    joins = plan.get('joins') if isinstance(plan.get('joins'), list) else []
    for join in joins:
        if isinstance(join, dict):
            sources.append({'id': join.get('source_id'), 'name': join.get('source_name') or join.get('label'),
                            'design_revision': join.get('source_revision'), 'role': '关联来源'})
    source_details = [f"{item.get('name') or item.get('id') or '未指定来源'}"
                      + (f"（设计时来源版本 {item['design_revision']}）" if item.get('design_revision') is not None else '')
                      for item in sources]
    source_details.append('允许空来源' if spec.get('allow_empty_source') else '不允许空来源，空来源会使运行失败')
    relationships=[]
    for j in joins:
        if not isinstance(j,dict):continue
        right_labels={f.get('name'):f.get('label') or f.get('name') for f in j.get('fields',[]) if isinstance(f,dict)}
        relationships.append(f"{label(j.get('left_field'))} 关联 {j.get('label') or j.get('source_id')} 的 {right_labels.get(j.get('right_field'),j.get('right_field'))}；"
                             + ('要求匹配' if j.get('required_match') else '允许未匹配')+'；关联来源的关联键必须唯一')
    if spec.get('trim_fields'):
        relationships.append('去除以下字段首尾空白，空字符串转为空值：'+'、'.join(label(k) for k in spec['trim_fields']))
    operations = {'add': '加', 'subtract': '减', 'multiply': '乘以', 'divide': '除以'}
    calculations = [f"{c.get('label') or c.get('name')}：{label(c.get('left_field'))} {operations.get(c.get('operation'), c.get('operation', ''))} {label(c.get('right_field'))}"
                    for c in spec.get('computations', []) if isinstance(c, dict)]
    filter_names = {'eq':'等于','neq':'不等于','contains':'包含','gt':'大于','gte':'大于或等于',
                    'lt':'小于','lte':'小于或等于','in':'属于','not_in':'不属于','is_null':'为空','not_null':'不为空'}
    filters = []
    for rule in spec.get('filters', []):
        if not isinstance(rule, dict):continue
        op=rule.get('operator'); value=rule.get('value')
        text=f"{label(rule.get('field'))} {filter_names.get(op, op or '未知条件')}"
        if op not in ('is_null','not_null'):
            text+=' '+(json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else str(value))
        text+='（按'+{'text':'文本','number':'数值','date':'日期'}.get(rule.get('data_type'),'未说明类型')+'比较）'
        filters.append(text)
    quality = []
    if spec.get('primary_key'):quality.append(f"主键：{label(spec['primary_key'])}；在处理后结果中检查唯一和非空")
    elif plan.get('identity_policy')=='preserve_all_rows_occurrence_v1':quality.append('未指定业务主键，保留所有记录，包括内容完全相同的明细')
    else:quality.append('旧版本未指定业务主键，相同全部字段的记录会去重，仅保留一条')
    if spec.get('required_fields'):quality.append('必填字段：'+'、'.join(label(k) for k in spec['required_fields']))
    if spec.get('nonnegative_fields'):quality.append('非负字段：'+'、'.join(label(k) for k in spec['nonnegative_fields']))
    if type(spec.get('min_rows')) is int:quality.append(f"结果最少 {spec['min_rows']} 行")
    output=[]
    if spec.get('amount_field') and spec.get('group_by'):
        output.append(f"按 {label(spec['group_by'])} 汇总 {label(spec['amount_field'])}")
        if spec.get('metric_definition'):output.append('业务口径：'+spec['metric_definition'])
        if spec.get('currency'):output.append('单位 / 币种：'+spec['currency'])
        output.append('业务口径状态：'+('规则中标记为已确认（仍需核对依据）' if spec.get('metric_status')=='confirmed' else '待确认'))
    else:output.append('生成处理后的明细数据')
    app=plan.get('application') if isinstance(plan.get('application'),dict) else {}
    if app:
        output.append('展示字段：'+'、'.join(label(k) for k in app.get('columns',[])))
        output.append('页面类型：'+('汇总' if app.get('layout')=='summary' else '明细表'))
        output.append('图表：'+('柱状图' if app.get('chart')=='bar' else '无'))
        output.append('默认排序：'+label(app.get('default_sort'))+(' 降序' if app.get('descending') else ' 升序'))
        if type(app.get('page_size')) is int:output.append('每页显示 '+str(app['page_size'])+' 条')
    steps = [
        {'id':'sources','title':'读取资料','details':source_details},
        {'id':'prepare','title':'整理与关联','details':relationships or ['未配置关联来源']},
        {'id':'rules','title':'筛选与计算','details':filters+calculations or ['未配置筛选或计算规则']},
        {'id':'quality','title':'检查数据','details':quality},
        {'id':'output','title':'生成结果','details':output},
    ]
    warnings=['这是根据冻结规则生成的业务说明，并非业务口径确认或计算结果验收。',
              '方案显示的是设计时的来源版本；具体运行的数据版本以该次运行记录为准。']
    if not spec.get('primary_key') and plan.get('identity_policy')!='preserve_all_rows_occurrence_v1':
        warnings.append('旧版本未设置业务主键：完全相同的明细会被去重，仅保留一条；可能影响金额与数量，请业务人员核对。')
    elif not spec.get('primary_key'):
        warnings.append('未设置业务主键：系统保留所有明细，无法自动识别业务上应剔除的重复交易，请业务人员核对。')
    if spec.get('amount_field') and (not spec.get('metric_definition') or not spec.get('currency')):
        warnings.append('汇总口径或金额单位尚未完整填写，需要业务人员核对。')
    return {'version':1,'origin':origin,'project_id':project_id,'application_id':application_id,
            'title':str(title or '业务处理方案'),
            'user_request':plan.get('user_request') if isinstance(plan.get('user_request'),str) else None,
            'request_turn_id':plan.get('request_turn_id') if isinstance(plan.get('request_turn_id'),str) else None,
            'continuation_request':plan.get('continuation_request') if isinstance(plan.get('continuation_request'),str) else None,
            'summary':'依据当前版本的处理规则生成业务说明；修改处理规则将生成新的候选版本。',
            'steps':steps,'sources':sources,'rules':spec,
            'identity_policy':plan.get('identity_policy') or ('business_primary_key_sha256_v1' if spec.get('primary_key') else 'legacy_dedup_full_record_hash_v1'),
            'application':copy.deepcopy(plan.get('application')),'warnings':warnings}


def _show(repository, commit, filename):
    result=subprocess.run([executable('git'),'-C',str(repository),'show',f'{commit}:{filename}'],
                          capture_output=True,timeout=30,check=False)
    if result.returncode:return None
    if len(result.stdout)>_MAX_BYTES:raise ValueError('业务方案超过可读取范围')
    try:value=json.loads(result.stdout)
    except (ValueError,UnicodeError):raise ValueError('该版本的业务方案格式无效') from None
    if not isinstance(value,dict):raise ValueError('该版本的业务方案格式无效')
    return value


def for_commit(pipeline, commit):
    """Read immutable rules at a full commit ID; older versions use frozen plan.

    Callers must first authorize project and bind the requested commit to a
    known application/run. This function does not expose Git history itself.
    """
    if not isinstance(commit,str) or not re.fullmatch(r'[a-f0-9]{40}',commit):
        raise ValueError('代码版本标识无效')
    repository=Path(pipeline['repository'])
    frozen=_show(repository,commit,'plan.json')
    if frozen is None:raise ValueError('该代码版本的处理规则不可用')
    recorded=_show(repository,commit,'business-plan.json')
    app=frozen.get('application') if isinstance(frozen.get('application'),dict) else {}
    if recorded is None and frozen.get('business_plan_version') is not None:
        raise ValueError('代码版本缺少配套业务方案')
    if recorded is not None:
        # Every displayed claim is deterministically reconstructed from frozen
        # rules, never trusted merely because an adjacent JSON file says so.
        expected=build(frozen,pipeline['id'],pipeline.get('project_id'),app.get('title') or '业务处理方案',
                       source_name=frozen.get('source_name'))
        if recorded!=expected:
            raise ValueError('业务方案与代码版本的处理规则不一致')
        return expected
    return build(frozen,pipeline['id'],pipeline.get('project_id'),app.get('title') or '历史业务处理方案',
                 source_name=frozen.get('source_name'),origin='derived_from_frozen_plan')
