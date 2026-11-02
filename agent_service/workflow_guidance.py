"""On-demand executable schemas for the agent; users need not author JSON."""
from .project_data import WorkflowDocument


def _compact(schema):
    """Drop Pydantic's redundant per-model ``title`` keys.

    Each title repeats its ``$defs`` name and only inflates the on-demand
    guide past the inline character budget; the structural schema, field
    descriptions and constraints stay intact for the authoring agent.
    """
    if isinstance(schema, dict):
        return {key: _compact(value) for key, value in schema.items() if key != 'title'}
    if isinstance(schema, list):
        return [_compact(value) for value in schema]
    return schema


def guide():
    # A validated example, deliberately carrying no real source or financial assumptions.
    example={
        'version':1,
        'plan':{'version':1,'name':'按已确认口径筛选明细',
                'inputs':{'detail':{'columns':[
                    {'name':'record_id','data_type':'text','meaning':'原始记录唯一标识'},
                    {'name':'amount','data_type':'decimal','meaning':'已确认的本期金额',
                     'unit':'CNY','period':'2026-09','aggregation':'additive'}],
                    'grain':['record_id'],'meaning':'一行一笔已确认的本期业务明细'}},
                'stages':[{'id':'kept','kind':'filter','input':'detail',
                           'description':'按用户明确条件保留金额大于零的记录',
                           'conditions':[{'column':'amount','operator':'gt','value':'0'}]}],
                'output':'kept'},
        'bindings':{'detail':{'source_id':'REPLACE_WITH_REAL_SOURCE_ID',
                             'column_map':{'c_1':'record_id','c_2':'amount'}}}}
    WorkflowDocument.model_validate(example)
    return {'schema':_compact(WorkflowDocument.model_json_schema()),'example':example,
            'scope':'agent_authoring_reference; examples_are_not_user_data_or_approved_semantics',
            'instructions':[
                '由Agent编写workflows/*.json，用户只需描述业务目标；先查项目已有流程再新建。',
                '例子中的来源、字段、期间、币种、条件和唯一键全部需要替换为实际证据，不能当默认口径。',
                '先检查页签和候选区域，边界明确后才读取；预览不可用作全量输入。',
                '跨法人、账簿、客户、币种或期间关联时，可用 lookup_multi 的 keys 指定多组 left/right 字段；逐项确认业务键完整及右表唯一，不能假设已核对。',
                '规则随日期生效时，可用 lookup_effective 显式指定业务键、业务日期、规则生效起止及输出版本字段；开始日包含、结束日不包含，空结束日默认停止，仅经业务确认并显式设置 null_end_policy=open 才表示持续有效。核对企业口径、区间边界与业务键，不能把技术匹配当成业务确认。',
                '需要币种换算时，先按明确的法人、币对、汇率类型和业务日期使用 lookup_effective 匹配完整汇率来源及版本，再用 fx_convert 指定原币、目标币和汇率类型。汇率是 1 原币对应多少目标币，汇率字段的单位写为目标币/原币（如 CNY/USD）；不自动倒数或猜测同币汇率。必须由用户明确是否允许舍入、舍入方式和位数（舍入按每条记录执行，不等于先汇总再舍入）。若后续汇总，请另行保留完整换算明细，不能仅凭汇总结果回查逐行；最终口径和数据仍需业务核对。',
                '当本次所有记录适用同一个、经业务确认的税率时，价税分离可以使用 compute 的 divide 并设置 constant_scaling=true。右操作数须明确声明单位 "1"、aggregation 为 non_additive，且每行均为同一精确因子；左操作数须为 additive 度量。引擎会核验各行因子相同并要求业务语义复核；件数或行级变化的税率不能冒充固定因子。compute 的 rounding_scale 按行使用 half-even（银行家舍入），并非通常所说的 half-up 四舍五入，且逐行舍入不等于先汇总再舍入。若业务要求 half-up，当前 compute 不能满足该口径，须明确提示未完成，不能自动替代；最终税率与业务口径仍需人工核对。',
                '失败先读真实workflow_failure和阶段回执，修改新草稿并重新运行；不能删除校验或伪造通过。',
                'normalize 只会把“空值或显式 missing_tokens 声明的缺失文本”转 null（需 missing_policy="null"）；来源里无法解析的日期/数值文本（如“无效日期”、空金额占位、非法字符）会让整个 normalize stage 报 LOCALIZED_VALUE_INVALID 失败，missing_policy 兜不住未声明的异常文本。因此 normalize 前先 read_source_table 检查各列真实取值，对异常值先 filter 排除：空值(None)用 is_null、空字符串用 not_in [""]、具体异常文本用 not_in 填实际值(如 ["无效日期"])、无效映射或异常订单按业务键挑出。被排除的异常行要单独保留异常清单（财务场景“有疑问先列异常，不要自行猜测”，不能静默丢弃或转零）。',
                '来源明细可能含完全重复的物理行（同一业务行原样录入两次），此时任何业务列组合都无法唯一；在 plan.inputs 中显式声明一列 {name:"nexus_row",data_type:"text",meaning:"本次资料行号"} 并把它加入 grain（可单独 grain:["nexus_row"]），引擎会为每行注入快照内唯一行号，去重/冲突排除必须在后续 stage 显式完成（如按业务键 count>1 用 filter 排除冲突、按全列重复用 aggregate 只计一次），不能把 nexus_row 当业务键或直接当成去重结果。nexus_row 是引擎自动注入的合成列，不要在 bindings 的 column_map 里映射它（不要写 "__row__" 之类的键），只要在 plan.inputs 声明列名即可。',
                'grain 声明的是"一行代表什么"的唯一键，不是业务主键；当业务键本身有重复时用 nexus_row 保证物理行唯一，再在 stage 里实现业务去重与冲突标记。',
                '金额结果由完整数据运行产生；报告中的数字引用已校验结果，不能按预览心算。']}


def catalog(pid):
    from .store import get,all_items
    get('project',pid)
    keys=('id','name','path','published_run','candidate_run','active_sha256','draft_sha256',
          'cron','watch_sources','next_due','refresh_error')
    return {'items':[{k:w.get(k) for k in keys} for w in all_items('data_workflow')
                     if w.get('project_id')==pid and not w.get('archived')],
            'reuse':'先inspect_data_workflow核对口径，修改草稿或沿原方案重跑；不覆盖正式版本'}
