"""Conservative server-side intent checks, independent of model tool selection."""
import re

# “别” is an imperative veto, but the same character inside 分别/识别/区别/
# 特别 and ordinary classification words is not a prohibition.
_DONT=r'(?:不要|(?<![分识区差特个类级性辨鉴判国派])别)'

VERBS={
 'remember':r'记住|保存.{0,5}记忆|记录.{0,5}偏好',
 'forget':r'忘记|删除.{0,8}记忆|取消.{0,8}偏好',
 'local_export':r'导出|保存|写入|同步|生成',
 'confirm_metric':r'确认指标',
 'publish':r'发布|采用|确认使用', 'schedule':r'更新|刷新|同步|调度|定时',
 'pause':r'暂停|关闭|停止|取消', 'refresh':r'重跑|重新运行|运行|刷新|重新处理|重新分析|同步|更新数据',
 'rename':r'改名|重命名', 'analyze':r'分析|整理|汇总|生成|创建|加工|修改|调整|做|筛选|统计|扣除',
 'write':r'写|保存|创建|新增|修改|调整|生成|更新|改', 'delete':r'删除|删掉|移除',
 'switch':r'切换|使用|分析', 'connect':r'连接|接入|采集',
}
ALIASES={'remember_memory':'remember','forget_memory':'forget','export_local_workspace':'local_export','publish_result':'publish','schedule_daily':'schedule','schedule_updates':'schedule',
 'run_work_command':'analyze','publish_work_file':'write',
 'pause_updates':'pause','run_data_application':'refresh','refresh_data':'refresh',
 'rename_project':'rename','run_python_file':'refresh','build_transform':'analyze','create_data_application':'analyze',
 'revise_data_application':'analyze','write_file':'write','edit_file':'write','delete_file':'delete','switch_source':'switch',
 'register_data_workflow':'write','run_data_workflow':'refresh','publish_data_workflow_result':'publish',
 'schedule_data_workflow':'schedule','propose_semantic_alignment':'write','propose_value_mapping':'write',
 'propose_semantic_catalog':'write','propose_catalog_alignment':'analyze',
 'acquire_report_data':'refresh','propose_extracted_data_facts':'write','extract_indented_source':'analyze',
 'render_business_report':'write'}

# A business request need not use an internal tool's storage/execution verb.
# Keep these synonyms scoped to the actual operation: refusing a download must
# not also refuse an unrelated refresh or saving a processing plan.
TOOL_VERBS={
 'register_data_workflow':r'登记|注册|固化|录入',
 'run_data_workflow':r'执行|处理|重算',
 'propose_semantic_alignment':r'对齐|匹配|提交|拉齐',
 'propose_semantic_catalog':r'登记|注册|提交|定义|建立',
 'propose_catalog_alignment':r'对齐|匹配|提交|拉齐',
 'propose_value_mapping':r'映射|对照|提交|拉齐',
 'acquire_report_data':r'获取|采集|下载|导出|读取',
 'propose_extracted_data_facts':r'提取|抽取|提交|存储',
 'extract_indented_source':r'提取|抽取|解析|转换',
 'render_business_report':r'导出|渲染|制作|出具',
}


def _without_user_code_burden(text):
    """A no-code product requirement is about the human, not an Agent veto.

    Remove only explicit human/consumer subjects doing coding work. Keep the
    rest of the clause, so "用户不需要写代码，也不要修改文件" still vetoes
    file writes. "你不需要写代码" and "不允许用户写代码" are not exemptions.
    This text is solely an intent view; the original instruction stays intact.
    """
    human=r'(?:业务用户|最终用户|普通用户|用户|业务人员|使用者|我|本人)'
    work=r'(?:(?:亲自|自己|手动)\s*)?(?:编写|写|修改|修|调试|维护)(?:代码|脚本)'
    work+=r'(?:\s*(?:或|和|及|以及|并|、)\s*'+work+r'){0,3}'
    need=r'(?:不需要|无需|不用|不必)'
    patterns=(human+r'\s*'+need+r'\s*'+work,
              need+r'\s*'+human+r'\s*'+work,
              _DONT+r'(?:让|要求|叫)\s*'+human+r'\s*'+work)
    for pattern in patterns:text=re.sub(pattern,'',text)
    return text

def denied(text,action):
    extra=TOOL_VERBS.get(action)
    action=ALIASES.get(action,action);verbs=VERBS.get(action)
    if extra:verbs='|'.join(filter(None,(verbs,extra)))
    if not verbs:return False
    if re.search(r'只(?:需要)?(?:解释|讨论|聊聊)|仅(?:解释|讨论)|先(?:讨论|聊聊)|'+_DONT+r'.{0,6}(?:做任何|动任何|执行任何|操作任何)',text):return True
    # Instructions quoted for discussion cannot grant authority. Remove quotes
    # before detecting a positive imperative, but still reject an explicit veto.
    intent_text=_without_user_code_burden(text) if action=='write' else text
    clauses=re.split(r'[，,。；;\n]|但是|而是|不过|只要',intent_text)
    for clause in clauses:
        if re.search(r'(?:'+_DONT+r'|不用|不必|不需要|不允许|不准|没让你|没叫你|禁止|不能|暂不|先不|不(?=执行|运行|重跑|发布|删除|修改|暂停)).{0,14}(?:'+verbs+r')',clause):return True
    unquoted=re.sub(r'[“「『"].*?[”」』"]','',text,flags=re.S)
    if action=='write':unquoted=_without_user_code_burden(unquoted)
    if re.search(r'解释|说明|什么意思|如何|怎么|为什么|假如|假设|如果|举例|是否已经|有没有',unquoted):
        # An explanation request is not permission to perform its subject.
        explicit=bool(re.search(r'(?:请|帮我|现在|立即|直接)(?:(?!(?:解释|说明|什么意思|如何|怎么|为什么|假如|假设|如果|举例|是否|有没有)).){0,6}(?:'+verbs+r')',unquoted))
        parts=re.split(r'[，,。；;\n]|并(?=解释|说明)',unquoted)
        for part in parts:
            if re.search(r'解释|说明|什么意思|如何|怎么|为什么|假如|假设|如果|举例|是否|有没有|[？?]',part):continue
            if re.match(r'\s*(?:(?:根据|按照|依据).{1,80}?)?(?:'+verbs+r')',part):explicit=True
        if not explicit:return True
    if re.search(r'[“「『"].*?(?:'+verbs+r').*?[”」』"]',text,flags=re.S) and not re.search(verbs,unquoted):return True
    return False

def require(turn,action):
    if turn.get('interaction_mode','work')!='work':raise ValueError('聊天模式不能修改项目')
    # A continuation inherits the original processing goal, never permission to
    # ignore a newer veto or explanation-only request.
    current=turn.get('text','')
    requested=turn.get('work_request') or turn.get('workflow_goal') or current
    if denied(current,action) or (requested!=current and denied(requested,action)):
        raise ValueError('本轮是在否定或讨论这项操作，未执行；请明确说明要做的变更')
