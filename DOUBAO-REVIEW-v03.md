# DOUBAO-REVIEW-v03 — Atlas DataBuddy v0.20.0 本机交付（Codex v0.3）独立复审

2026-10-24。本轮范围：独立复审 **FG-1（US-D1 真实模型三件套）**、**FG-2（US-E1 Windows 文件夹模式）**、**US-E3（工作位置选择 + 本地数据处理集成）**，并按用户最新要求**新增页面/操作/布局/UI 实测评审**（回应"看起来还是像 DEMO"）。只读审查，不修改代码，未读取/回显 llm.json、.env 或任何密钥。

---

## 【本轮结论】

- **状态**：🔧 需要继续迭代（无 P0、无 P1 功能缺口；有 P2 打磨项 + OB-2 待验，未达收敛判定）
- **置信度**：高（FG-1/FG-2/US-E3 均以最终 r3 包本体 + 证据 JSON + 源码 + **真实启动 UI 截图**交叉验证，证据链闭合）

**一句话**：Codex 本轮把 FG-1/FG-2/US-E3 从"缺口"推进到"有真实证据通过"，且诚实保留未验项（OB-2 物理断网、Office 视觉/公式）；我实际启动 r3 并截图，UI 完成度高、**不是粗糙 DEMO**，但存在至少一处"模板残留感"的英文眉题，需打磨。

---

## 【问题清单】

### P0
- 未发现。

### P1
- 未发现（FG-1、FG-2、US-E3 均有真实证据，无功能缺口）。

### P2

**UI-1 — 主视觉眉题硬编码英文，未本地化（模板残留感）**
- **位置**：`agent_service/static/app.js:56` — `<div class="eyebrow">YOUR BUSINESS WORKSPACE</div>`
- **现象**：主工作台空状态主视觉顶部固定渲染纯英文眉题 "YOUR BUSINESS WORKSPACE"（实机截图 OCR box 约 320-470/380-400 处确认可见）。
- **为什么是问题**：违反硬约束 R6（以用户为中心）。产品是中文业务界面，该英文眉题对中文用户无信息价值，是全中文文案里唯一的英文装饰，观感像模板/占位残留，直接触发"还是像 DEMO"的观感。
- **期望行为**：中文业务用户打开主工作台空状态时，应看到中文眉题（或无装饰眉题），英文不出现。
- **修复建议**：
  ```text
  在 app.js:56 将 <div class="eyebrow">YOUR BUSINESS WORKSPACE</div>
  改为中文，如 <div class="eyebrow">业务数据工作空间</div>，
  或按 R6 判定无信息价值则直接移除该节点。
  （同时全局搜索其余硬编码英文 UI 文案，一并本地化。）
  ```

**OB-2 — 物理断网/防火墙隔离未验（维持待验，不通过）**
- **位置**：验收项 OB-2（`verification/packaged-offline-v03/backend-verification.json` 中 `os_firewall_or_disconnected_vm_evaluated: false`）
- **现象**：`local_only` 离线策略已验，但真实断网/防火墙隔离环境未验；Codex 已声明不通过，不冒称全绿。
- **为什么是问题**：硬约束 R2（可离线）。应用级离线策略通过 ≠ 物理断网可用；若操作系统层面网络被断，可能仍有未覆盖的隐式外联路径。
- **期望行为**：在无网络/防火墙隔离环境下，本地导入→画像→加工→发布→导出仍全链可用，且启动不因外联失败而崩。
- **修复建议**：若本机可临时断网或加防火墙出站规则，复跑 `acceptance/offline/verify_backend.py --app-root <r3>/resources/app --offline`；否则明确列为下轮待验项，不写入"已通过"。

---

## 【5 维强制扫描】

### 1. 功能完整性
- **US-E3**：源码 + 真实 UI 双重确认闭环。① 工作模式内有「工作位置」选择（`index.html` `<select id="work-environment">`：`本地电脑` / `云端（后续阶段开放）` disabled）；② 选本地电脑 → 打开「工作模式·本地电脑」面板，未绑定目录时仅提供「选择本地工作文件夹」，不冒充；③ 绑定后「工作资料」在当前项目内提供 导入→画像→口径→候选核对→人工发布→完整 CSV 导出；④ 桌面菜单已移除「本地数据处理 Ctrl+Shift+D」独立项（`desktop/main.cjs:223-229` 现仅 工作空间/编辑/视图）；⑤ `local-data.html` 改为 meta-refresh 返回主工作台 + 说明已并入工作模式。✅
- **FG-1（US-D1）**：`verification/packaged-real-model-office-v03/verification.json` — ok=true、`real_model_evaluated=true`、真实 DeepSeek 5 次调用 46.56s，XLSX/DOCX/PPTX 三件均 `passed=true`（zip_crc、ooxml_parts、`exact_118_15_present`、`source_reference_present`、`paid_order_details_present`、content_passed），`no_automatic_publication=true`，`formats_passed=[docx,pptx,xlsx]`，task_budgets 用默认 18 calls/360000 chars/24000 tokens 未提高。金额我可复算：A1 100.25−10.10=90.15、A3 30−2=28、合计 118.15，与 OB-1 精确值 `118.150000000000` 一致。✅（Office 视觉 `visual_validation=false`、公式 `formula_recalculation=false` — Codex 诚实未验，非放行项。）
- **FG-2（US-E1）**：`verification/packaged-local-folder-v03/local-folder-verification.json` — ok=true：原件快照/幂等、版本可读、导出双快照哈希全验、用户改原件产生新 source、双文件夹同时运行 token/port/db 隔离、重启/ACL 保持、unsafe_paths（traversal/ADS/device/missing）拒绝。✅

### 2. 边界与异常
- 空输入/超长输入/空文件/缺运行文件/错误密钥/不可达模型：实测通过（backend-verification.json）。
- 错误密钥不阻断本地：`bad_model_local_workflow` 全链 rows=2、exact_total=118.15（不可达 base_url + 假密钥仍完成本地闭环）。
- 路径安全（FG-2）：`windows_dirfd.py` 用锚定目录 HANDLE 相对打开、拒绝重解析点/越界/多硬链接/设备名/NTFS 数据流，`_relative()`（:71-80）白名单校验文件名，`remove_tree` 只删应用管理导出父目录。✅
- 并发/迟到响应：`desktop/main.cjs` `transitions.run` 串行门控（:18, :180）；`stopRuntime` 切换前销毁旧溯源窗口（:88-90）拒迟到结果；`local-data.js` 以 generation/projectId 守卫。✅
- 缺口：物理断网（OB-2）未验。

### 3. 代码质量
- `windows_dirfd.py` 实现严谨（句柄生命周期 `retain/release`、`can_rename` 发布语义、`st_ctime_ns` 用 change-time 检测 mtime 改写，注释解释了 Windows 语义差异）。
- `portable_runtime.py`：启动只探本地 `/api/status`+`/ready`，无外网探测；token 仅 IPC emit，不进 URL/日志（注释明确）；`ATLAS_OPENCODE_RUNTIME='1'`（:79）DV-1 已修复；错误只暴露白名单信息（:185）。
- `security.cjs`：`loopbackOrigin` 严格（仅 127.0.0.1、禁 userinfo/pathname）、`parseReady` 校验 token 长度/禁换行、`trustedMainFrame` 限定主 frame、`createTransitionGate` 串行。✅
- 魔法数字/命名：可读；无吞错（catch 均有诊断日志）。
- 小观察：`main.cjs:181` `choosingWorkspace||starting` 守卫与 `transitions.run` 双保险冗余但无害，非缺陷。

### 4. 离线 / 零配置
- 空继承 PATH、随包 Python3.12 + 170 包 + Git + OpenCode、不依赖用户工具链（`user_toolchain_required=false`、`inherited_path_empty=true`）。
- 启动无外网探测、telemetry 全关（`startup_does_not_require_reachable_model=true`）。
- OB-3：15006 运行文件 0 mismatch、170 包清单哈希一致（`r3-independent-runtime-audit.json`，含 auditor 对 261 字符路径误报的更正说明，诚实）。
- 缺口：物理断网（OB-2）。

### 5. 反向挑战
- 本轮 Codex 未对新问题提出反驳；无新增撤回项。DV-3/DV-4 撤回维持（历史保留）。

---

## 【页面/操作/布局/UI 实测评审】（新增维度，回应"像 DEMO"）

我实际用 `user32.PrintWindow` 截取运行中的 r3 主窗口（hwnd=0x2c0b76「Atlas · 我的工作空间」），并做了点击交互，证据截图存于项目根：
`atlas_ui_shot.png`（主工作台）、`atlas_ui_workdata.png`（工作资料面板）、`atlas_ui_results2.png`（成果页）。

**实测结论**：
- **主工作台**：左导航（新建项目/我的项目/工作设置与工具/个人业务空间）+ 中主区（引导文案 + 三引导按钮：检查底稿与口径/加工数据并生成汇报/复用已有业务任务）+ 底部输入区（聊天/工作模式切换、工作位置=本地电脑、工作资料按钮、执行工作↑、同步状态「已与工作空间同步·草稿已同步」）。布局三栏、白底、配色克制，**完成度高，不是粗糙 DEMO**。
- **工作资料面板**：「工作模式·本地电脑」——当前本地工作空间（出片_微信小程序_神图版）+ 选择本地工作文件夹 + 本地规则说明 + 步骤化「1 选择资料（任务名称/资料文件/导入并查看字段）」+「已保存的本地任务」空态。产品化闭环。✅
- **成果页**：右侧「项目成果」面板（新项目·0项 / 全部成果 / 刷新 / 空态引导文案），新项目空态合理。✅
- **真实瑕疵**：主视觉英文眉题 "YOUR BUSINESS WORKSPACE"（**UI-1，见问题清单**）。

**对"像 DEMO"的回应**：不是通用占位 demo——US-E3 本地数据处理已真实集成到工作模式、绑定真实工作文件夹、同步状态可用、成果面板为真实空态。但英文眉题 + 待全面自查的其他未本地化文案，是会触发"DEMO 感"的真实项，需打磨。

---

## 【能力纵深评审（核心，用户 2026-10-24 强调）】

用户明确：验收核心不是 UI，而是 **agent 能力与底层服务是否足够强大，能否生成/处理用户想要的所有内容，而不是 demo**。

**已确认（能力底座足够强，非 demo 骨架）**：
- `agent_service/agent.py:85-146` 暴露约 70+ 工具：数据资产目录/列血缘/指标确认/数据质量、工作连接器（只读 MCP+写动作确认卡+回执）、Office 三件套、工具发现与结果恢复、git 版本化文件、公司内部权限检索、远端隔离 Python 执行、`create_data_application`（生成可用数据+应用页+dbt/语义/页面代码）、`query_sql`、`build_transform`（Git/dbt+质量验证+候选+人工确认）、定时调度、多 agent 协作、本地文件夹工作区等。
- 模块体系（多 agent gateway/handoff/mesh/team、语义层、报告、发布、连接器、审计、隔离）与约 100 个能力测试文件（workbook 多 sheet、多文件对话、文档读取、数据应用、数据工作流、语义目录、财务工作流完整性等）共同证明：**这是产品级能力架构，不是 demo 骨架**。
- `capabilities()`（agent.py:155）列出 tool_discovery / project_git / column_lineage / data_contract_gates / bounded_agent_teams / versioned_safe_artifact_preview 等 40+ 能力特征；并诚实标注 `real_model_evaluation:'requires_external_task_evaluation'`、`intelligence_score:None`（未冒称智能评分）。

**缺口（能力纵深证明不足，这是"像 demo"观感的真正来源）**：
- 当前验收包（packaged-offline / packaged-local-folder / packaged-real-model-office）**全部是合成最小样例**：2 行 CSV → 精确 118.15 → 三件套。它证明了"核心链路能跑通"，**未证明能处理用户真实需求的规模与复杂度**。
- 能力模块有单元测试，但**没有用真实模型做端到端的规模化/多样化场景验收**：大 CSV（万行）、复杂 Excel（多 sheet/合并单元格/公式/金额单位混杂）、跨文件关联（订单+客户+退款 join 口径）、脏数据（缺值/重复/格式混杂/除零）、口语化任意表述与多轮澄清、不同业务场景（财务/销售/人事/库存/汇报）各走通。

**结论**：底层能力足够强（✅），但"能生成处理用户所有想要的内容"的**证据**仍需补——这决定产品能否从"能演示样例"到"能扛住真实需求"。见给 Codex 的下一步指令第 1 条（最高优先级）。

---

## 【给 Codex 的下一步指令】

一句话（按优先级）：**①最高优先——补"能力纵深端到端验收"**：用真实模型跑至少 4 类真实规模/复杂度场景（大 CSV/多 sheet 复杂 Excel、跨文件关联口径、含脏数据、口语化多轮对话），证明 agent 能处理用户真实多样需求而非仅合成 2 行样例，回传证据（含输入构造、工具轨迹、产出、精确结果）；②修复 UI-1 英文眉题本地化（app.js:56，并全局搜其余硬编码英文 UI 文案）并对导航子页/溯源图做占位自查；③若环境允许补 OB-2 物理断网验收。FG-1/FG-2/US-E3 本轮确认通过，不重复改。
