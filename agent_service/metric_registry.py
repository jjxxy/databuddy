"""Versioned declarations and owner confirmations of the built-in summary metric.

Confirmation binds a code commit and semantic digest, never a mutable draft.
This is an owner-workspace confirmation, not an organizational approval system.
"""
import json
from sqlalchemy import select, update
from pydantic import Field
from .models import StrictModel
from .store import get, items, now, transaction
from .column_lineage import digest


class ConfirmMetric(StrictModel):
    application_id: str = Field(min_length=1, max_length=64)
    expected_run_id: str = Field(min_length=1, max_length=64)
    expected_metric_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmation_quote: str = Field(min_length=1, max_length=2200)


def declaration(pipeline, run, manifest):
    spec = manifest.get('spec', {})
    if not spec.get('amount_field'):
        return None
    semantics = {k: spec.get(k) for k in (
        'amount_label', 'amount_field', 'group_by', 'metric_definition', 'currency',
        'filters', 'computations', 'joins', 'trim_fields', 'primary_key')}
    # The registration key includes the Git commit, including its SQL implementation.
    semantics['aggregation'] = manifest.get('metric_aggregation','legacy_summary_decimal_20_4')
    sha = digest(semantics)
    key = 'metric_' + digest([pipeline['id'], run['commit'], sha])[:40]
    return {'id': key, 'project_id': pipeline.get('project_id'),
            'application_id': pipeline['id'], 'commit': run['commit'],
            'metric_sha256': sha, 'semantics': semantics, 'status': 'proposed',
            'first_run_id': run['id'], 'created_at': now(),
            'scope': 'owner_workspace; generated_summary_metric; no_organization_approval'}


def register(connection, pipeline, run, manifest):
    value = declaration(pipeline, run, manifest)
    if value is None or not value['project_id']:
        return
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = pg_insert if connection.dialect.name == 'postgresql' else sqlite_insert
    connection.execute(insert(items).values(id=value['id'], kind='metric',
                       body=json.dumps(value, ensure_ascii=False)).on_conflict_do_nothing(index_elements=['id']))


def current(project_id, application_id):
    from .data_governance import owned
    from .jobs import get_job
    from .releases import manifest_for
    p = owned(project_id, application_id)
    if not p.get('published_run'):
        raise ValueError('应用尚未正式发布，不能登记或确认正式指标')
    run = get_job(p['published_run'])
    if run['target'] != p['id']:
        raise ValueError('指标运行不属于应用')
    folder, manifest = manifest_for(run)
    if manifest.get('commit') != run['commit'] or manifest.get('pipeline_id') != p['id']:
        raise ValueError('指标快照版本不一致')
    from .releases import verify_semantics
    verify_semantics(p, run, manifest, folder)
    value = declaration(p, run, manifest)
    if value is None:
        raise ValueError('此应用没有汇总指标')
    try:
        stored = get('metric', value['id'])
    except KeyError:
        stored = None
    if stored:
        value = stored
    return dict(value, run_id=run['id'], data_as_of=manifest.get('data_as_of'))


def confirm(turn, value):
    """Use the persisted user message; model arguments cannot grant authority."""
    value = ConfirmMetric.model_validate(value)
    stored_turn = get('turn', turn['id'])
    if stored_turn.get('project_id') != turn['project_id']:
        raise ValueError('确认对话不属于当前项目')
    if stored_turn.get('interaction_mode', 'work') != 'work':
        raise ValueError('聊天模式不能确认指标')
    from .tasks import check_target
    check_target(stored_turn, value.application_id)
    metric = current(turn['project_id'], value.application_id)
    spec = metric['semantics']
    definition = spec.get('metric_definition') or ''
    unit = spec.get('currency') or ''
    if not definition.strip() or not unit.strip():
        raise ValueError('指标缺少口径或单位，请先生成并发布完整定义')
    if spec.get('aggregation')!='sum_decimal_38_12':
        raise ValueError('历史汇总精度不一致，请先构建并发布新版本后确认')
    required = confirmation_text(metric)
    text = stored_turn['text'].strip()
    if text != required or value.confirmation_quote != text:
        raise ValueError('需用户在当前对话明确回复：' + required)
    if metric['run_id'] != value.expected_run_id or metric['metric_sha256'] != value.expected_metric_sha256:
        raise ValueError('正式指标版本已变化，请重新查看并确认')
    # Row lock also serializes publication. Recheck after waiting for it.
    with transaction() as c:
        row = c.execute(select(items.c.body).where(items.c.id == value.application_id,
                        items.c.kind == 'pipeline').with_for_update()).first()
        p = json.loads(row[0]) if row else {}
        if p.get('project_id') != turn['project_id'] or p.get('published_run') != value.expected_run_id:
            raise ValueError('正式指标版本已变化，请重新查看并确认')
        register(c, p, {'id': metric['run_id'], 'commit': metric['commit']},
                 {'spec': spec,'metric_aggregation':spec['aggregation']})
        row = c.execute(select(items.c.body).where(items.c.id == metric['id'], items.c.kind == 'metric').with_for_update()).first()
        saved = json.loads(row[0])
        if saved['status'] != 'confirmed':
            saved.update(status='confirmed', confirmation={'turn_id': turn['id'], 'confirmed_at': now(),
                         'run_id': metric['run_id'], 'quote_sha256': digest(text)})
            c.execute(update(items).where(items.c.id == metric['id'], items.c.kind == 'metric')
                      .values(body=json.dumps(saved, ensure_ascii=False)))
    return dict(saved, run_id=metric['run_id'])


def status(pipeline,run,manifest):
    """Cheap status lookup for UI; never trust the model's metric_status flag."""
    value=declaration(pipeline,run,manifest)
    if value is None:return 'draft'
    try:record=get('metric',value['id'])
    except KeyError:return 'draft'
    return 'confirmed' if record.get('status')=='confirmed' else 'draft'


def manifest_status(manifest):
    try:p=get('pipeline',manifest.get('pipeline_id'))
    except KeyError:return 'draft'
    return status(p,{'id':manifest['run_id'],'commit':manifest['commit']},manifest)


def confirmation_text(metric):
    s=metric['semantics']
    return '确认指标：'+(s.get('metric_definition') or '')+'；单位：'+(s.get('currency') or '')+'；版本：'+metric['commit'][:12]


def handle_command(turn):
    """Exact conversation commands also work without an external model."""
    import re
    text=turn['text'].strip()
    if text not in ('查看指标','查看字段血缘') and not re.fullmatch(r'确认指标[：:][\s\S]+',text):return False
    from .tasks import ensure,verify
    from .conversation import finish
    from .store import patch
    targets=ensure(turn)['application_ids']
    if len(targets)!=1:
        finish(turn,'请先选中要查看或确认指标的应用。',outcome='needs_input');return True
    aid=targets[0]
    if text=='查看字段血缘':
        from .data_governance import lineage
        value=lineage(turn['project_id'],aid,limit=40)
        answer='字段血缘尚不可用，请为该应用构建新候选。'
        if value['available']:
            answer='正式版本 '+value['commit'][:12]+' 的字段来源：\n'+'\n'.join(
                r['column']+' ← '+', '.join(i['source_id']+'.'+i['column'] for i in r['inputs']) for r in value['columns'])
            if value['next_offset'] is not None:answer+='\n当前展示前40项，可继续通过字段血缘查询工具分页读取。'
        finish(turn,answer);return True
    try:metric=current(turn['project_id'],aid)
    except (ValueError,KeyError,OSError):
        finish(turn,'当前应用尚无可确认的正式指标，请先生成完整口径并确认发布。',outcome='needs_input');return True
    if text=='查看指标':
        s=metric['semantics']
        guidance='\n请先补全口径和单位，构建并发布新版本。'
        if s.get('metric_definition') and s.get('currency') and s.get('aggregation')=='sum_decimal_38_12':
            guidance='\n核对口径及计算规则后，可在工作模式回复：\n'+confirmation_text(metric)
        finish(turn,'正式指标：'+(s.get('amount_label') or s['amount_field'])+'\n口径：'+(s.get('metric_definition') or '待补全')+
               '\n单位：'+(s.get('currency') or '待补全')+'\n状态：'+('已确认' if metric['status']=='confirmed' else '待确认')+
               '\n代码版本：'+metric['commit'][:12]+guidance);return True
    if turn.get('interaction_mode','work')!='work':
        finish(turn,'请切换到工作模式再确认指标。',outcome='needs_input');return True
    from .agent import _execute_node
    args={'application_id':aid,'expected_run_id':metric['run_id'],'expected_metric_sha256':metric['metric_sha256'],'confirmation_quote':text}
    _execute_node({'turn_id':turn['id'],'step':{'kind':'tool','tool':'confirm_metric','arguments':args}})
    fresh=get('turn',turn['id'])
    result=verify(fresh)
    if result['verified']:finish(fresh,'已确认指标口径和单位，确认记录绑定代码版本 '+metric['commit'][:12]+'。',outcome='verified_complete')
    else:
        observation=(fresh.get('agent_trace') or [{}])[-1].get('observation') or {}
        finish(fresh,observation.get('error','本次指标确认未完成，请先查看当前正式指标。'),outcome='needs_input')
    return True
