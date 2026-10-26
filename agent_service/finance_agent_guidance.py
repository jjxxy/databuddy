"""Business-facing data work guidance and deterministic observation scope.

This guides the planner; it is not a claim of real-model accuracy or a substitute
for the server's task/evidence and authorization checks.
"""
import re


FINANCE_DATA_GUIDANCE = (
    '处理财经或表格数据工作时，用户只需说明业务目标和提供资料。由你读取资料、选择工具并编写内部加工方案，'
    '不要要求普通用户填写WorkflowDocument、JSON Schema、列号、SQL或脚本，也不要把用户不会查脚本错误当作任务终点。'
    '先检查已有工作流、正式结果、业务规则和已核对映射，能复用就复用；存在语义包时先list_semantic_catalogs，'
    '读取已核阅版本的完整字段、原件来源和有效期。通过propose_catalog_alignment固定服务器版本及业务口径适用日，'
    '不把模型临时生成的目标字段称为IT提供的可信口径，不默认今天就是历史分析的口径日期；包已核阅也不等于具体字段匹配已核对。'
    '单来源简单看板可用数据应用，'
    '多页签、多级表头、跨表粒度、复杂SQL及多步依赖使用数据工作流。'
    '先inspect_source_tables读取真实页签、表格、表头候选和公式证据，再read_source_table核对预览与字段含义；'
    '优先使用命名表和明确范围，禁止盲目向下填充数值合并单元格，禁止把预览行复制成全部输入。'
    '同列混合Excel百分比数值和带百分号文本时，先查看每种单元格格式；确认均为百分比后，可在读取options.percentage_columns中按c_N声明fraction或points，服务保留逐格转换证据。'
    '普通无百分比格式的13或0.13仍须明确业务含义，不能借此选项猜比例；金额单位冲突、显示缩放和公式缺缓存仍需先解决。'
    '先从原文、表头和已有规则核对金额单位与倍率、币种、期间、明细粒度、含税或不含税、退款及重复汇总行；'
    '只核对影响本次目标的项目，已经有明确证据的不重复追问，不要求用户一次补完所有元数据。'
    '只追问当前阻塞的业务选择，尽量一到两个具体问题，并用已发现的字段或候选选项说明差别；'
    '可独立完成的读取、字段识别和其他处理先继续。省份不能唯一决定办事处时，要补关联键或业务映射，不能猜分摊比例。'
    '交易金额等可加量按一致期间与单位汇总；余额、存量、累计口径、比例和平均值不得直接跨期或跨维度求和。'
    '平均值需要权重或明确的简单平均口径，比例需要实际分子分母；缺成本不能称利润，缺基期不能编同比环比，'
    '零、空值、缺失和不适用分别处理，不能把未知补零以通过校验。'
    '由你将明确规则写入可版本管理的加工文件并校验、运行，给用户展示易读步骤、输入来源和验收条件；'
    '发现结构、字段引用或工具参数错误时，根据准确错误修复新候选并有界重试，不重复提交同一失败参数。'
    '不能靠修改业务阈值、删除失败行、改变口径或跳过核对让校验变绿。业务含义确实缺失时说明具体缺口再提问。'
    '等待真实队列回执，区分方案已保存、技术验证通过、语义待核对和正式采用。中断后复用已有真实回执与完整result_id；'
    '未持久化的中间表不宣称已恢复，必要时按固定方案完整重跑。正式结果在新候选失败时继续保留。'
    'Excel公式先保留公式、缓存值和缓存时效，不能声称openpyxl已计算公式；缺有效缓存则说明受影响单元格，'
    '请求重算保存后的文件或明确的原始值，不能静默使用旧缓存、执行宏或改写公式含义。'
    '网页报表优先使用已配置授权导出或报表接口；获取后核对完整性、实际筛选和报告上下文，'
    'requested筛选不等于verified筛选，HTTP成功、点到按钮或创建任务都不等于获取成功。'
    '描述与小结中的每个数必须来自当前真实结果或对完整输入执行的汇总步骤；data_evidence_scope只说明已读取范围，'
    '不提供任何新计算结论。只读了部分页时只能描述这些行，不能据此前25条推断总额、排名、占比或总体趋势。'
    '需要总体小结就增加针对完整result_id的汇总/SQL步骤，再核对期间、单位、记录数与金额控制数。'
    '输出实际数值、可核对表格、来源和完整交接/Excel下载，再给简洁业务解释；不得只交付一段结论。'
    '语义和事实批准只由鉴权核对页完成，模型不得代用户批准自己的提案；下载和共享以实际允许的范围为准。'
)


def guidance_for_turn(turn):
    text = turn.get('work_request') or turn.get('workflow_goal') or turn.get('text', '')
    data_work = bool(turn.get('source_id') or turn.get('data_workflow_job') or
                     re.search(r'财务|财经|报表|底稿|对账|收入|利润|金额|退款|预算|余额|数据|表格|页签|合并单元格|汇总|粒度|Excel|excel|SQL', text))
    return FINANCE_DATA_GUIDANCE if data_work else ''


def data_evidence_context(turn, *, allow_values=False):
    """Describe real result-page coverage without copying raw values or computing totals.

    Group by immutable run+manifest.  Duplicate pages, cross-version reads,
    failed calls and a tool's unverified `page_is_complete_result` flag cannot
    enlarge coverage.  Privacy gating remains explicit even when trace has rows.
    """
    groups = {}
    for step in turn.get('agent_trace', []):
        if step.get('status') != 'done' or step.get('tool') != 'read_data_workflow_result':
            continue
        observed = step.get('observation') or {}
        if not isinstance(observed, dict) or not isinstance(observed.get('rows'), list):
            continue
        run = observed.get('run_id')
        manifest = observed.get('manifest_sha256')
        total, offset = observed.get('total'), observed.get('offset')
        if (not isinstance(run, str) or not isinstance(manifest, str)
                or not re.fullmatch(r'[a-f0-9]{64}', manifest)
                or type(total) is not int or total < 0 or type(offset) is not int or offset < 0
                or offset > total or offset + len(observed['rows']) > total):
            continue
        key = (run, manifest)
        group = groups.setdefault(key, {'run_id': run, 'manifest_sha256': manifest,
                                       'total_rows': total, 'ranges': [], 'inconsistent_total': False})
        if total != group['total_rows']:
            group['inconsistent_total'] = True
        group['ranges'].append((offset, offset + len(observed['rows'])))
    pages = []
    for group in groups.values():
        merged = []
        for start, end in sorted(group.pop('ranges')):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        covered = sum(end - start for start, end in merged)
        entire = (not group['inconsistent_total'] and merged == [[0, group['total_rows']]])
        pages.append({**group, 'covered_rows': covered, 'full_result_read': entire,
                      'model_may_use_values': bool(allow_values),
                      'whole_result_summary_requires_new_aggregation': not (allow_values and entire)})
    return {'results': pages, 'model_content_access': bool(allow_values),
            'scope': 'observation_coverage_only; no_inferred_totals_or_business_conclusions',
            'next_action': 'compute_full_result_aggregation_when_whole_result_summary_is_needed'}
