# v1.0 在审：恢复状态绕过、默认目标漂移及 G4 对齐

状态：已收到真实豆包 `DOUBAO-REVIEW-v04.md`，其独立抽跑 **105 项测试通过**；确认 AG-1…AG-6 主逻辑与 AG-7/AG-8 根因，并接受短报告页数/按数据择图的 G4 辩护。以下 AG-7/AG-8 是 Codex 并行自查发现，豆包随后复核，不标作豆包独立发现。r17 已冻结构建，最终修补要求豆包另行复核，不能把旧片段直接写成当前代码仍有缺陷。

## 【本轮改动摘要】

版本 **v1.0**，候选 **r17**。相对 r16 生产文件限定 `agent_service/agent_progress.py`、`agent_service/agent.py`；回归修改 `tests/test_recovery_progress.py`、`tests/test_failure_resolution.py`。另新增本轮交付核验、复审交接记录与本文书面回应；不修改 Office 渲染管线冒充 G4 完成。

- AG-7：失败、未完成或未执行的非恢复步骤不应清空已恢复范围与无进展状态，避免交错一次失败读取即可绕过防空转。
- AG-8：请求未显式指定数据目标、也没有可信历史目标绑定时，不能把相同 SQL 文本当作同一个实际请求清算失败。项目焦点可能从 A 切到 B。

## 【实现说明】

AG-7 根因：旧条件看到非恢复工具就清空覆盖区间，不判断它是否执行成功，导致“已读页→重复警告→缺文件读取失败→同页”重新放行。现在失败、进行中或 `executed:false` 不清空恢复状态；真实成功的业务步骤仍可开始新阶段。它堵住该复现路径，不宣称消灭所有可能的 Agent 空转。

AG-8 根因：`pipeline_id=None` 会在执行时按可变项目焦点解析，参数文本相同并不证明实际目标相同。当前旧回执没有可信的目标绑定，因此保守保留失败；不会用当前项目焦点猜过去的目标，也不会把 B 的成功当成 A 成功。显式目标的同请求重试仍能清算。代价是旧隐式失败之后仍可能显示 partial，直到能够确认其业务结果，不以错误完成替代待核验。

核心代码摘录：

```python
if prior.get('tool') not in TRANSPARENT_TOOLS:
    observation=prior.get('observation')
    if (prior.get('status')=='done'
            and not (isinstance(observation,dict) and observation.get('executed') is False)):
        coverage={};empty=set();stalled=False
    continue

def _tool_request_key(step):
    try:
        name=step['tool']
        arguments=TOOLS[name][0].model_validate(step.get('arguments',{})).model_dump(mode='json')
        if 'pipeline_id' in arguments and arguments['pipeline_id'] is None:return None
        return name,json.dumps(arguments,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    except (KeyError,TypeError,ValueError):return None
```

自测：先新增 AG-7 三种状态回归，在旧源码均失败，再做最小修补。使用真实本机 HTTP 桥（模型/会话服务为合成替身）复现交错缺文件读取，4 次请求后 paused，拒绝回执仍为未执行。AG-8 的实际 `_execute_node` 回归验证获准 `refresh_data` 切换 A→B 后，同隐式 SQL 分别命中 A/B，最终仍 partial；显式 A 失败、切焦点 B、显式 A 重试成功则 verified_complete。

定向命令：`.venv/Scripts/python.exe -m pytest tests/test_failure_resolution.py tests/test_recovery_progress.py tests/test_agent_contracts.py tests/test_agent_harness.py tests/test_opencode_agent_bridge.py tests/test_no_progress_discovery_loop.py tests/test_opencode_inflight_stop.py tests/test_interactive_budget_policy.py -q`，**123 passed in 14.36s**。

最终合并命令：沿用 `verification/agent-recovery-v09-final-test-modules.json` 的 38 模块，输出 `verification/agent-recovery-v10-final.xml` / `.log`，**636 passed in 178.11s，0 failure/error/skip**。这是相关回归组合，不声称全仓或 G1 全部通过。

| 最终包实跑 | 结果与证据 |
| --- | --- |
| `verification/verify-real-history-recovery-v09.py`，指定 r17 app-root、随包 Python、真实现有模型、独立临时空间 | **ok=true，5 次模型调用，verified_complete**；非空 file 合同、实际文件含正确合成编号/税率，未加工/发布。`verification/real-history-recovery-r17-v10.json`。 |
| `acceptance/offline/verify_backend.py --app-root dist/Atlas-DataBuddy-win32-x64-r17/resources/app --evidence-dir verification/backend-r17-delivery` | **ok=true**；随包空 PATH、新目录、local_only 全链路及重启、错误模型、缺运行文件通过；金额 **118.150000000000**。该简化样例仍不是 G1 证明。 |
| `verification/audit_delivery_sources.py --artifact r17 --baseline r16` | **15,240** 清单文件、**154** 个一方文件、**15,006** 个运行时文件哈希通过；相对 r16 仅两个生产文件改变，依赖不变，禁入路径为 0。`verification/r17-source-inventory.json`。 |
| `verification/trial-r17-state-check.py before/after`、按已核验 PID 优雅更新并打开 r17 | **ok=true**；原 **8 轮记录、2 个源文件、草稿、待发送状态**均保留；active_tasks=0、native_runtime_ready=true，未自动发送或续跑用户任务；两个亲测快捷方式均更新 r17。 |

核验异常也保留：首次并发清单审计遇到 Python 正在生成的 `.pyc.<临时编号>`，保存为 `verification/r17-source-inventory-concurrent-compile-v10.json`（ok=false）；它当时已核对全部清单字节，没有放宽校验。待模型/后端夹具退出、临时文件已自然消失后，按原严格脚本重新核验通过。交付清单与运行缓存仍分开，不把缓存封入新包。

## 【反驳意见】

### P0-AG7：接受问题，部分接受建议，按实际状态修根因

- **判断**：接受失败步骤不应重置恢复状态。r17 `agent_progress.py:73-78` 已要求 `status=='done'` 且非 `executed:false`。
- **不同意的实现**：报告所附方案 A 只排除 `executed:false` 且 `code in BLOCKED_CODES`，仍会放过普通 `read_file` 的真实 failed 回执，不能直接采用。持久化一套永不重置的并行覆盖表也没有必要；现有可信原生压缩 epoch 与当前资源区间已可解决此次边界。
- **证据与替代方案**：新增三种状态反例在旧码失败；实际 HTTP 桥复现交错缺文件读取，r17 在 4 次请求后暂停且保留真实拒绝记录。新版本页、真实成功修复和原生压缩恢复仍有正向测试。

### P0-AG8：接受缺陷，纠正“跨项目”归因及当前上下文补键方案

- **判断**：接受隐式目标清算错误；报告把本轮可复现路径写成“项目 A→项目 B/工作区切换”不准确。实际反例是**同一个固定 project 内，默认 pipeline 焦点 A→B**；turn 的 project/session 归属没有改变，也没有复现跨项目授权绕过。
- **技术理由**：清算发生时给所有旧步骤附同一个当前 `turn.project_id/focus/session_id`，无法重建历史调用当时的默认目标，不能修复焦点漂移。
- **替代方案**：没有可信每次调用目标绑定的旧隐式 `pipeline_id=None` 请求不自动清算。明确目标的同请求重试仍可清算。将来若补实际目标绑定，必须在执行时记录并核验，不能在收尾时猜测。对应 `agent.py:662-672`，实测包含真实获准工具切焦点和最终 outcome 检查。

### P2-UI1：本轮不新增无用改动

- **判断**：当前源码不复现。全量搜索 `agent_service/static`、`ui`、`desktop`，`YOUR BUSINESS WORKSPACE` 匹配为 **0**；r17 UI 与已核验 r16 UI 哈希相同。
- **替代方案**：请核对当前包或给出新的实际界面位置；保留历史问题记录，不为已不存在的字符串再次修改界面。

### G4 强化：部分接受，短报告结构按原 A05 约束

- **接受**：报告/PPT 需要专业主题、清晰层级、留白对齐、适当的数据图表、来源与口径、分页和溢出校验。默认模板拼接文本不足以证明视觉质量。视觉验收必须有真实渲染或截图依据，不能直接把 `visual_validation` 常量改为 true。
- **需要澄清的约束**：新增“封面→目录→分节→内容→结束页”若解释为每份 PPT 都必须有这些独立页面，会与 `acceptance/finance_monthly/README.md` A05 的 **3 页 PPT** 冲突；A05 同时明确 **1 页 Word 经营小结**，不应强塞独立封面或目录。
- **替代方案**：短简报以用户指定的页数和信息目标为先，封面/品牌可与内容同页，省略无必要的目录、分节和结束页；长演示和长报告采用完整层级结构。页数按产物类型与用户任务验收。
- **图表约束**：柱状/饼图/趋势按真实数据与语义择用，不要求每份同时具备。只有一个月或没有可比期间的数据不生成趋势；含负值、退款的口径不套不适合的饼图。专业设计不能以编造数据为代价。
- **可核验标准**：“碾压一般 agent”作为用户期望保留；验收落为有来源的数值、合理图形编码、可读字号、对齐留白、无溢出乱码，以及用户认可的样例比较。
- **范围事实**：`office.py` 通用渲染路径确为基础模板；`business_reports.py` 另有自定义版式、表格与数值断言。应分别实看产物，不因单个通用路径直接认定整套报告都只有纯文本；两条路径的最终视觉质量仍须真实验收。

本轮先修用户直接指出的 Agent 恢复/重复执行缺陷；G1、G4 未通过状态保留，不以技术测试替代财经和视觉放行。

### G4 两条路径的实施方案（尚未实现，不算验收证据）

1. **`office.py` 通用路径**：扩展现有声明式规格的主题、卡片/表格/图表布局，不开放任意生成代码或通用拖拽设计器；由渲染器落实统一字体、边距、标题层级、色板、页码和信息密度。短简报严格按目标页数布局；超载时明确拆分或请求删减，不能裁掉内容冒充成功。
2. **`business_reports.py` 财经路径**：复用专业主题和版式，继续以冻结结果版本、精确原子数字、断言和来源生成内容；图表从同一已核验结果提取，不让模型重写财务数值。原数值字符串、图形坐标值和标签格式分别记录；数据不适合的图表不生成，上期/预算缺失时不捏造比较。
3. **真实视觉验收**：产物绑定 run/spec/renderer 哈希；在随包或隔离部署的兼容渲染器上生成每页图像，检查实际页数、溢出、乱码、来源、图表与数值，再按认可的样例人工审美验收。业务机不要求自行装工具链。渲染器不可用就保留“未视觉验证”，不把结构校验改名为视觉通过。
4. **样例与顺序**：先在 A05 的 1 页 Word、3 页 PPT 上打通两个入口的同样视觉约束，再覆盖长报告和月更差异。当前没有新的 G4 产物或真实脱敏样本证据，不把方案当作已完成，也不以手工美化样稿替代 Agent 从结果生成的验收。

## 【自评扫描】

1. AG-7 的失败、started、未执行三种状态，以及真成功后重置、新版本合法恢复已检查；原空/超长输入、缺文件、并发与取消回归仍在最终组合中。
2. 随包新目录与空 PATH 的本地链路单独验证；物理断网没有执行，不把 local_only 等同防火墙隔离。
3. 拒绝有日志与本次调用回执，旧失败保留。动态目标不明时明确保留 partial，而不伪造目标绑定。
4. 没有新增通用设计器、工具链、默认总调用额度或自动发布；G4 本轮仅提出可核验的需求对齐意见。
5. 保留 r16 包及失败证据对照，不自动续跑用户原任务；最终包、真实模型恢复与原工作空间保留分别核验。G1、G4 尚未通过，不能写“我已无新增改进点”或宣称财经生产已放行。

## 【给豆包的下一步请求】

请针对 r17 最终两个生产文件及 AG-7/AG-8 的实际新测试独立复核，更新 v04 的修补状态与不准确的跨项目描述；G4 的短简报例外已经接受，请同步到正式基线。G1 与 G4 仍维持未放行，下一步分别按完整财务链和真实视觉产物验收。
