# DOUBAO-REVIEW-v07 — Codex P0-①/P0-② 复现的独立复核（来源链标记传播与 fail-closed 边界）

- 审查者：豆包侧首席架构评审官（只读静态审查，未改源码、未触真实数据/密钥、未参与构包）
- 审查对象：`C:\Users\001\Documents\ChatGPT\New project\_github-publish\databuddy` 当前工作树
- 证据：`agent_service/project_data.py:183-227,1247-1269`、`agent_service/releases.py:61-71`、`agent_service/api.py:240-248`、`agent_service/agent_handoff.py:219-322`
- 范围：Codex 独立复现的两条 P0（① replace-file+extract_indented_source 丢标记；② build_input→import_bundle 跨项目丢标记）在**当前工作树**是否已堵、是否仍可绕过、历史发布结果兜底与回归边界。

---

## 【本轮结论】

- 状态：🔧 继续迭代（P0-① 已堵住；P0-② source 路径已堵住，**result 交接路径存在残留缺口，建议补 fail-closed**）
- 置信度：高（逐行读源码核对标记传播链）

**一句话**：当前工作树已含大部分修复——`source_requires_fact_alignment` 是强 fail-closed 的**不可变版本链祖先追溯**，`marked` 已纳入 `origin_agent_handoff/external_provenance`，`extract_indented_source` 已传播标记，`replace_file` 保留字段，`build_input:230` 对 source 交接已拦。**P0-①（replace→extract）与 P0-② 的 source 交接路径已堵住。** 但 **P0-② 的 result 交接路径疑似未堵**：`build_input` result 分支不检查可加性、`import_bundle` 导入结果只设 `requires_semantic_review` 而无 `semantic_alignment_required`，而 `marked` 不识别 `requires_semantic_review` → 导入结果作为下游输入时 `_requires_fact_alignment` 不阻断、manual publish 可过。此为待确认缺口。

---

## 【问题清单】

### P1（待确认，倾向需补）· P0-② 的 result 交接路径：导入结果不传播可加性义务

- **位置**：
  - `agent_handoff.py:239-248`（`build_input` result 分支）：metadata 只写 `requires_semantic_review`/`semantic_review_reasons`，**未检查 `_requires_fact_alignment` 也未传 `semantic_alignment_required`**；对照 source 分支 :230 已有 `source_requires_fact_alignment` 拦截。
  - `agent_handoff.py:305-308`（`import_bundle` 导入结果）：metadata 设 `requires_semantic_review=True, semantic_review_reasons=['EXTERNAL_AGENT_RESULT_REVIEW_REQUIRED']`，**无 `semantic_alignment_required`**。
  - `project_data.py:193-201`（`marked`）：检查 fact_review_id/visual_fact_review_id/semantic_alignment_required/origin_agent_handoff/external_provenance，**不含 `requires_semantic_review`**。
- **现象**：fact 来源加工成结果 → `build_input(result_ids)` 交接 → `import_bundle` 导入结果（requires_semantic_review=True，无 semantic_alignment_required）→ 下游 run 引用该导入结果 → `_requires_fact_alignment`（releases:69 调 source_requires_fact_alignment）对含 requires_semantic_review 但无 fact 标记的来源**返回 False（不阻断）** → manual publish（confirm_source_review + review_actor）可通过 → 下游可读取。
- **为什么是问题**：违反硬约束"未复核/未对齐上游不得被下游洗白"，且与 source 交接路径（:230 已拦）不一致——result 作为交接载体时丢掉了可加性义务，形成跨 Agent 洗白窗口。
- **期望行为**：输入"下游 run 引用一个经 build_input/import_bundle 交接的、由事实来源加工出的结果"，应输出发布阻断（或该导入结果须完成逐行语义+可加性对齐后才可发布）。
- **修复建议（fail-closed，伪代码级）**：
  ```python
  # 1) build_input result 分支：用 _requires_fact_alignment 判定，向 metadata 写入
  fact_alignment_required = any_ancestor_fact_marked(result_run)   # 复用 source_requires_fact_alignment 逻辑沿 result 的来源链
  metadata = {..., 'semantic_alignment_required': bool(fact_alignment_required or meta.get('fact_semantic_alignment_required'))}

  # 2) import_bundle 导入 result（:305-308）：一律 fail-closed
  metadata.update(semantic_alignment_required=True)   # 宁可多拦，待逐行语义+可加性核对后放行

  # 3)（兜底）marked/project_data 把 requires_semantic_review 纳入可加性判定，避免"仅来源复核"被当作可加性放行
  ```
- **需要 Codex 确认**：当前导入 result 作为下游 input 时，source_snapshot 冻结项是否带 `requires_semantic_review`/`origin_agent_handoff`/`external_provenance`？若带 `origin_agent_handoff`/`external_provenance` 则 `marked` 已拦截（则此条撤回）；若只带 `requires_semantic_review` 则**此缺口成立**，请补。请给一条"跨 Agent result 交接 → 下游引用 → manual publish"的定向测试结果。

### 已确认堵住（供 Codex 结项）
- **P0-①（replace-file + extract_indented_source）— ✅ 已堵**：`source_requires_fact_alignment`（:200-227）沿**不可变 `source_version`** 递归 `origin_source_id`（缺失/环/歧义/版本记录不唯一 → fail-closed True）；`replace_file`（api.py:246）用 `patch` 保留 fact 字段、`revision` 生成新版本记录（原 fact 标记版本仍在链上）；`extract_indented_source:1266` 用 `source_requires_fact_alignment(s,{})` 传播 `semantic_alignment_required` → 派生后仍被 `_requires_fact_alignment` 阻断。**即使 replace 换文件，递归链仍能追溯到原 fact 标记。**
- **P0-② source 交接 — ✅ 已堵**：`build_input:230` 对 `source_requires_fact_alignment(s,{})` 为真的来源直接 `raise HandoffError`；`import_bundle:288` 导入 source 设 `semantic_alignment_required=True` + `origin_agent_handoff` + `external_provenance` → `marked` 命中 → 阻断。

### P2 · 未验收/未覆盖（如实保留）
- OCR 运行库/模型未随包、真实财务样本准确率、G1 真实模型月结未通过（Codex 已如实声明）。
- 历史发布结果 source 的兜底：Codex 说"与上面是否可达是不同结论"——需 Codex 明确：**修复前已发布/已采用的事实派生结果**是否仍可被下游引用及是否被 `_requires_fact_alignment`/`source_requires_fact_alignment` 递归拦截（若其 source 记录带 origin 链/交接标记则拦截；若为旧普通记录则需说明兜底策略：禁用/标记/重新对齐）。此为待验证，不能自动判为已覆盖。

---

## 【回归边界建议（给 Codex 的测试清单）】

1. **P0-①**：fact 来源 → replace-file(缩进TXT) → extract_indented_source → legacy pipeline 含"净收入+预算收入" → 发布应被 `_requires_fact_alignment` 阻断（断言 raise），而非得 323.45。
2. **P0-② source**：fact 来源 → build_input(source_ids) 应 raise HandoffError；import source 后下游发布应被阻断。
3. **P0-② result（待补）**：fact 来源加工成结果 → build_input(result_ids) → import_bundle → 下游引用 → manual publish 应被阻断（当前若放行即为缺口证据）。
4. **历史发布结果**：构造修复前发布的事实派生结果作为下游输入，验证是否被递归拦截（当前 source_requires_fact_alignment 沿 origin_source_id 链查不可变版本 → 若旧记录带 fact 标记版本则拦截；不带则需说明兜底）。
5. 全仓回归：P0-①/② 补丁后跑既有 62 项 + 上述定向，确认不误伤"普通 CSV/XLSX 来源"的正常发布（`marked` 对无 fact 标记/无交接痕迹的来源仍返回 False）。

---

## 【给 Codex 的下一步指令】

1. **裁定 P0-② result 交接缺口**：给出导入结果作为下游 input 的 source_snapshot 冻结项证据（是否带 origin_agent_handoff/external_provenance）；若仅带 requires_semantic_review，则按 fail-closed 补 `semantic_alignment_required`（build_input result 分支 + import_bundle 导入结果 + 必要时 marked 纳入 requires_semantic_review），并给定向测试。
2. **说明历史发布结果兜底**：修复前已发布的事实派生结果如何被处理（禁用/标记重对齐/递归拦截），给出对应测试。
3. 按上述回归边界清单补测试并回传结果。
4. r24 包本体验证完成后补交证据；未验收项（OCR 随包、真实样本、G1）保持如实。
