"""On-demand executable schemas for the agent; users need not author JSON."""
from .project_data import WorkflowDocument


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
    return {'schema':WorkflowDocument.model_json_schema(),'example':example,
            'scope':'agent_authoring_reference; examples_are_not_user_data_or_approved_semantics',
            'instructions':[
                '由Agent编写workflows/*.json，用户只需描述业务目标；先查项目已有流程再新建。',
                '例子中的来源、字段、期间、币种、条件和唯一键全部需要替换为实际证据，不能当默认口径。',
                '先检查页签和候选区域，边界明确后才读取；预览不可用作全量输入。',
                '失败先读真实workflow_failure和阶段回执，修改新草稿并重新运行；不能删除校验或伪造通过。',
                '金额结果由完整数据运行产生；报告中的数字引用已校验结果，不能按预览心算。']}


def catalog(pid):
    from .store import get,all_items
    get('project',pid)
    keys=('id','name','path','published_run','candidate_run','active_sha256','draft_sha256',
          'cron','watch_sources','next_due','refresh_error')
    return {'items':[{k:w.get(k) for k in keys} for w in all_items('data_workflow')
                     if w.get('project_id')==pid and not w.get('archived')],
            'reuse':'先inspect_data_workflow核对口径，修改草稿或沿原方案重跑；不覆盖正式版本'}
