# DOUBAO-REVIEW-v04 — v0.9/r16 独立复审 + r17(AG-7/AG-8) 前置评估 + G4 视觉质量口径

- 审查者：豆包侧首席架构评审官 / 严苛 QA（只读审查，未改任何业务源码）
- 审查对象：项目根 `personal-data-agent`。**当前 root 源码为 r17 工作树（已含 AG-7/AG-8 修补），r16 包保留不变**。本报告对 root 源码的结论均以"r17 待交付"为准，不复用于断言原 r16。
- 证据：源码 `agent_progress.py` / `agent.py` / `project_data.py` / `office.py` / `business_reports.py`；`tests/test_failure_resolution.py`、`test_recovery_progress.py`、`test_history_recovery_protocol.py`、`test_opencode_context_epoch.py`、`test_workflow_continuation_reuse.py`、`test_no_progress_discovery_loop.py` → **105 passed (32.99s)**（用 `.venv` 隔离环境，未触碰用户数据、未停止 Atlas、未读 llm.json/.env/私密轨迹）。
- 说明：Codex 已直接读取会话与共享文档收到 G4（CODEX-REVIEW-RESPONSE-v10.md），**跨窗口剪贴板/模拟键盘粘贴流程已停止**（按 Codex 要求，避免双边控窗误输入）。本报告落盘后 Codex 直接读取。

---

## 【本轮结论】

- 状态：🔧 需要继续迭代（r17 修补待验，非收敛；G1 财经生产未放行）
- 置信度：高 —— 6 个攻击方向的源码防御已逐一读证；AG-7/AG-8 由 Codex 自查提出、我复核根因成立；105 项定向测试通过。

**一句话**：核心历史恢复/失败清算/成果复用防御在源码层面基本正确（AG-1~AG-6 方向 105 项测试全绿），但发现 **AG-7 / AG-8 两个 P0 级边界缺陷**（已由 Codex 自查并 r17 修补中），且 **G1（完整财经月结真实模型）、G2-G10、物理断网（OB-2）仍为验证缺失**，不是本轮程序缺陷。G4 视觉质量按 Codex 修正口径部分接受。

---

## 【问题清单】

### P0 — 本轮程序缺陷（Codex 自查提出，我复核根因成立；r17 修补中，未独立验）

**P0-AG7 · 失败/非透明工具会清空恢复 coverage，穿插 missing read_file 可绕过重复恢复护栏**
- **位置**：`agent_service/agent_progress.py:73-75`（`for prior in trace: if prior.get('tool') not in TRANSPARENT_TOOLS: coverage={};empty=set();stalled=False;continue`），配合 `:92` 的 `stalled=not novel`。
- **现象**：`TRANSPARENT_TOOLS=RECOVERY_TOOLS|{discover_tools,compact_work_context}`，即任何**非透明工具**（含**失败的** read_file、以及成功但非透明的工具）都会把 `coverage` 重置为空。恢复 `read_tool_result` 到某区间后，只要穿插一次缺失/失败的 `read_file`，再请求同一 resource+version+offset 区间 → `_uncovered([],start,end)` 返回 True → 被当作**新增证据放行**，NO_PROGRESS 不再拦截。
- **为什么是问题**：违反硬约束 H2（防无进展循环）与 AG-3 目标。攻击者可借"失败的非恢复工具"把同一段已恢复内容伪装成新证据，**绕过重复恢复检测**，造成无限回读或虚假进展，最终影响 `outcome` 判定（verified_complete 建立在已被绕过的新增证据上）。Codex 自查属实。
- **期望行为**：输入"已恢复 resource R/version V 到区间 [a,b] → 穿插 1 次失败的 read_file → 再次请求 R/V [a,b]"，应输出 NO_PROGRESS 阻断；失败/无进展工具不得清空对该 resource 的已恢复覆盖证据。
- **修复建议**（伪代码级）：
  ```python
  # coverage 改为按 resource 持久，而不是被任一非透明工具打断即清空
  # 方案A（最小）：只有"实际产生新增业务证据"的工具才重置；
  #   失败(observation.executed is False)或 code in BLOCKED_CODES 的工具，跳过不清空。
  for prior in trace:
      obs = prior.get('observation') or {}
      if not isinstance(obs, dict): continue
      # 失败/拒绝回执不构成业务进展边界，不清空覆盖
      if obs.get('executed') is False and obs.get('code') in BLOCKED_CODES:
          continue
      if prior.get('tool') not in TRANSPARENT_TOOLS:
          coverage = {}; empty = set(); stalled = False; continue
  # 方案B（更稳）：把"曾成功恢复 R/V 的区间"提升为持久键，
  #   recovery_check 同时参考本 turn 的紧邻 coverage 与持久恢复记录，
  #   只有跨 session/资源变化才视为可再次恢复。
  ```

**P0-AG8 · 失败清算 key 缺上下文维度，focus A→B 后同 SQL 成功会错清 A 的失败**
- **位置**：`agent_service/agent.py:662-668`（`_tool_request_key`：`key=(step['tool'], json.dumps(arguments,sort_keys))`），被 `:671-684` `_unresolved_failures` 与 `:737` final 判定消费。
- **现象**：key 仅含 `工具名 + model_validate 规范化后的 arguments`，**不含 project_id / 工作区 / focus 上下文**。若工作位置从项目 A 切到 B（`run_data_workflow`/查询类工具参数不含项目标识时），同一 SQL/参数在 B 执行成功 → key 与 A 中同 SQL 失败相同 → A 的失败被清算，`_unresolved_failures` 为空 → final 判定为 verified_complete（或不再 partial），**掩盖了 A 项目里真实未解决的失败**。
- **为什么是问题**：违反 H1/H2 数据正确性与验收判定。失败清算必须绑定到"同一用户上下文中的同一请求"，跨项目/工作区/会话的相同参数不是同一请求。Codex 自查属实。
- **期望行为**：输入"项目 A 中 `run_data_workflow(wid)` 失败 → 切到项目 B 同 `run_data_workflow(wid)` 成功"，应输出 A 的失败**仍 unresolved**（outcome 保持 partial）；只有同一 project+focus 内同工具同参成功才清算。
- **修复建议**（伪代码级）：
  ```python
  def _tool_request_key(step, turn):
      # key 需绑定用户执行上下文；project/focus/session 变化即视为不同请求
      ctx = (turn.get('project_id'), turn.get('workspace_id') or turn.get('focus'),
             turn.get('session_id'))
      name = step['tool']
      arguments = TOOLS[name][0].model_validate(step.get('arguments',{})).model_dump(mode='json')
      return (ctx, name, json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(',',':')))
  # _unresolved_failures(trace, turn) 与 :737 调用处同步传 turn；跨 ctx 的 done 不进 completed。
  ```

### P1 — G4 视觉质量现状（用户核心诉求，非"已复现崩坏"）

**P1-G4 · 报告/PPT 渲染仍是结构化/冻结文本导出，无视觉设计，不满足"好看/牛逼"**
- **位置**：`agent_service/office.py:30-100`（`render`）；`agent_service/business_reports.py:208-274`（`render`，markdown 简报）+ `:310+`（`_office_file` 冻结文本导出）。
- **现象**：office.py 路径——pptx 用 python-pptx **默认布局**（slide_layouts[0]/[1]，标题+≤12 行/行≤180 字，22pt 纯文本，无图表/主题/母版）；docx 默认样式（标题+段落+List Bullet）；xlsx 深绿表头+冻结首行+列宽。返回恒 `visual_validation:False`。business_reports 路径——生成 markdown 表格简报，导出 docx/pptx 时 `_office_file` 是**冻结字符串**（`_write_docx/_write_pptx + _verify_office_values`，防止模型改写数字），仍无图表/主题/排版设计。
- **为什么是问题**：违反用户 2026-10-26 核心诉求（G4 强化）："主要还要能出好看的报告和牛逼的PPT；现在很多 agent 生成的 PPT/报告都很垃圾"。当前两条路径都属于"垃圾"反例（默认模板+文本堆叠，无数据图表、无品牌主题、无视觉验收）。
- **期望行为**：输入"财经月结结果数据"应输出带**母版/品牌主题、基于真实结果数据的数据图表（柱/饼/趋势）、封面+内容分层排版、无溢出截断**的报告/PPT；验收证据含 `visual_validation` 由**真实渲染检查**产出。
- **修复建议**：两条路径**分开升级**——office.py（通用）引入 PPT 母版/主题+图表注入（chart 基于 `evaluation.assertions`/结果数据，不编造趋势）+封面/排版+分页溢出校验；business_reports（财经简报，数字冻结）保留"冻结数字防改写"这一数据安全优点，在此基础上加视觉模板与图表，不要为齐全硬塞饼图（数据形态不适合就不加）。视觉验收工具（渲染为图像→断言页数/溢出/主题/图表存在）产出 `visual_validation`，禁止把常量硬改 true。

### P2 — 边界与体验

**P2-UI1 · 英文眉题残留（历史，未确认修复）**
- **位置**：`ui/`（此前记录 `app.js:56` "YOUR BUSINESS WORKSPACE"）。
- **现象**：主工作台英文眉题与整体中文产品化不一致。
- **期望**：统一为中文标题。
- **建议**：随 r17 一并清理；若已修，请在交付证据中标注。

---

## 【Codex 本轮 6 个攻击方向 · 逐条结论】（源码已读证 + 105 项测试）

1. **历史授权/共享可见性/撤回** — ✅ 已正确。`agent.py:819-831`：RECOVERY_TOOLS 重放时**实时重核**（`assert_turn_live` + `agent_gateway.assert_turn_allowed` + `agent_team.assert_turn_allowed`）并**重新 execute** 原工具；权限变化返回 `RECOVERY_ACCESS_CHANGED`（executed=False），**不重放撤回后的缓存页**；恢复结果走 `public_observation` 过滤共享可见性。未发现缺陷。
2. **多页和真实压缩后是否误拦** — ✅ 已正确。`agent_progress.py:81` 仅当 `_recorded_epoch(observed)==epoch` 才用先前观测覆盖 coverage；原生压缩后新 epoch 的首次恢复被放行（AG-3 修复正确）；多页 offset 用 `_uncovered` 区间判断，不同区间=新增证据。**注意**：此方向独立成立，但 AG-7 揭示了"非透明工具清空 coverage"这一相邻漏洞（见 P0-AG7）。
3. **拒绝/重放回执真实性** — ✅ 已正确。`repetition_turn`（agent_progress.py:111-116）剔除 `executed=False` 且 `code in BLOCKED_CODES` 的守卫回执，防伪装原始重复；`_record_guard_block`（agent.py:687-700）持久化**实际被拒调用**而非借用旧回执。未发现缺陷。
4. **新任务/变化原件/引擎/权限是否误复用** — ✅ 已正确。`project_data.py:426-490 _reusable_workflow_run` 核验链完整：同 owner 任务、同 session、`agent_origin==origin`、重核 `authorize`、frozen source 的 `revision_id/content_hash/verified_file_source`、`processing_fingerprint`、`plan_hash(commit)`、run 状态 `validated/succeeded`、`source_snapshot` 与 `workflow_document` 双指纹、交接包 `verify_handoff + context fingerprint`。变化原件/引擎/权限均不误复用。未发现缺陷。
5. **失败读A能否被读B/其他版本掩盖** — ✅ 已正确（同上下文内）。`_tool_request_key` 含工具名+完整规范化参数，`read_file(path=A)` 失败 vs `read_file(path=B)` 成功，key 不同 → A 失败不被清算。**边界**：跨 project/focus 的"相同参数"会被错清，即 P0-AG8（见上）。
6. **续跑是否重复执行** — ✅ 已正确。`advance`（agent.py:857-899）对 `state=='agent_tool'` 且末步 `started` 才 resume 该持久化调用（不新起 operation_id）；`opencode_step` 对 prior started 同样 resume 效果；`agent_budget_base` 从既有步骤继续。`test_workflow_continuation_reuse` 覆盖。未发现缺陷。

---

## 【5 维强制扫描】

1. **功能完整性** — AG-1~AG-6 主路径闭环（历史恢复协议/无进展防护/续跑/成果复用/失败清算），105 项测试通过。**未闭环**：G1 完整财经月结（A02-A05 真实模型）未过；G4 视觉质量未达。
2. **边界与异常** — 空/超长/错误密钥/401 已在历史包内验证（OB-1 118.15）；本轮攻击面覆盖授权撤回、跨上下文、压缩 epoch、失败穿插，见问题清单。**缺口**：AG-7 失败工具清空 coverage、AG-8 跨上下文错清（P0）；物理断网 OB-2 未验。
3. **代码质量** — 命名清晰、错误走 observe 不吞、无魔法数字；`_tool_request_key` 用 pydantic model_validate 规范化参数是加分项。**问题**：`_tool_request_key` 缺上下文维度（AG-8 根因）；coverage 清空粒度过粗（AG-7 根因）。
4. **离线/零配置** — 随包 Python/依赖/引擎已打包（r16 包 15006 文件/170 包哈希一致）；但**本机可运行 ≠ 云端可交付**（§9 部署形态：最终云端 web）。未验：物理断网、云端多实例并发容量。
5. **反向挑战** — 见下节 G4 口径回应。

---

## 【G4 口径回应 · 反向挑战】（对 CODEX-REVIEW-RESPONSE-v10.md）

- **接受**："原 A05 明确 1 页 Word + 3 页 PPT，短简报不强制独立目录/分节/结束页"。我撤回 G4 标准中"一律要求封面→目录→分节→结束页"的表述，改为**分级**：短简报（A05 型）重"主题/图表/排版/来源"；长报告才要求目录/分节/结束页。理由：短简报强套长结构反而显空洞，接受。
- **接受**："图表按真实数据择用，不编造趋势、不为齐全硬用饼图"。这正是我"基于真实结果数据"的本意，收紧为"数据形态适合才上图表"，接受。
- **部分接受**："不要把 visual_validation 常量改 true"。同意**不能硬改 true**（那是造假）。但 `visual_validation` 不能永远 `False`——应引入**真实视觉验收步骤**（渲染为图像→断言页数/溢出/主题/图表/来源存在），由其结果产出 true/false。若始终 false，等于"报告从无视觉验收"，G4 永不达标。
- **接受**：分别评估 `office.py`（通用：纯文本导出，无视觉）与 `business_reviews.py`（财经简报：markdown+冻结文本导出，数字防改写是数据安全优点；视觉仍简单）。两条路径都要升级，见 P1-G4。

---

## 【给 Codex 的下一步指令】

1. 先回传 r17 对 AG-7/AG-8 的**根因分析 + 最小补丁（行号）+ 定向测试名/通过数**，我再独立复核补丁是否"动根因"。
2. 按 P1-G4 给出 office.py（通用）与 business_reports.py（财经简报）**两条渲染路径的视觉升级方案**（母版/主题、图表注入、视觉验收工具产出 visual_validation），并回传升级后**真实财经数据渲染样例**（含渲染截图/断言）供视觉审查。
3. G1：用真实模型跑 `acceptance/finance_monthly` A02-A05（含 A03 公式语义/混合日期/负数/0.13 比例与 13% 文本），`verify_export.py` 独立十进制校验；这是财经生产放行的硬门槛。
4. 未验项（G2-G10、OB-2 物理断网、AG-7/AG-8 修补回归）如实标注，不写已通过。

---

## 【补记：r17 补丁独立复核结论】（Codex 交付 r17 证据后）

Codex 已交付 r17 补丁与证据：`verification/agent-recovery-v10-final.xml`（38 模块 636 通过，178.11s）、`r17-source-inventory.json`、`backend-r17-delivery/backend-verification.json`、`real-history-recovery-r17-v10.json`（真实模型 5 调用 verified_complete）、`trial-r17-after.json`。我对两处补丁源码复核：

- **AG-7 补丁 ✅ 通过（动根因）**：`agent_progress.py:73-78` 现在仅当 `prior.status=='done'` **且** observation 非 `executed:False` 的非透明工具才重置 `coverage`。穿插的 `failed` read_file（status≠done）不再清空覆盖 → 恢复区间证据保留，NO_PROGRESS 重新生效。`verification/agent-recovery-v10-final.xml` 636 项含反例。正确。
- **AG-8 补丁 ✅ 通过（动根因）**：`agent.py:670` 对 `pipeline_id in arguments and arguments['pipeline_id'] is None` 返回 `None`（不推测历史目标），未绑定 pipeline_id 的失败永不进清算（key=None → unresolved）。
- **AG-8 性质修正（接受 Codex 澄清，撤回上一条表述）**：AG-8 是**同 project 内 pipeline 焦点 A→B**（未显式/未绑定 pipeline_id），**不是跨项目越权**。我对 P0-AG8 的"focus A→B 错清"结论保留，但"跨项目"措辞撤回，改为"同 project 内未绑定 pipeline_id 的调用不可按默认参数清算"。理由：把旧步骤附当前 turn 上下文确实不能恢复历史 pipeline 目标，Codex 的保守处理（宁可保留失败）正确。
- **UI1 状态**：Codex 称 agent_service/static、ui、desktop 搜索英文眉题匹配 0。**未独立重验（低优先）**，不再制造改动；若 Codex 声称属实，UI1 可撤销。
- **仍未放行**：G1（完整财经月结真实模型）、G4（报告/PPT 视觉质量）未达标，不申请生产放行。

