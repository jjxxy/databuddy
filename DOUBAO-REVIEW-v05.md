# DOUBAO-REVIEW-v05 — 多模态能力审计独立复审（_github-publish/databuddy）

- 审查者：豆包侧首席架构评审官（只读审查，未改源码、未读密钥/真实用户库、未操作现有 Atlas）
- 审查对象：`C:\Users\001\Documents\ChatGPT\New project\_github-publish\databuddy` 工作树 + `docs/multimodal-capability-audit.md`（49 项）
- 审查重点：①CSV 短行/坏引号财务完整性；②来源待复核标记/自动发布/历史上游链；③RapidOCR 复用（本地识别、版本/哈希绑定、UI 复核、并发超时）；④49 项审计有无遗漏/夸大。
- 证据：`agent_service/connectors.py:139-172`、`tests/test_csv_financial_integrity.py`、`agent_service/offline_ocr.py`、`agent_service/visual_fact_service.py`、`agent_service/project_data.py:183-193,255-261,585-679`、`agent_service/releases.py:35-113`。

---

## 【本轮结论】

- 状态：🔧 继续迭代（无 P0 财务错数缺陷，但有 P1 需确认 + 交付/能力缺口未闭环）
- 置信度：高（逐项读源码 + 测试覆盖确认）

**一句话**：**CSV 财务完整性、OCR 安全链、来源复核阻断、RapidOCR 复用四项核心均实现正确且有测试/防御，未发现会造成错误财务结果的 P0；审计 49 项整体诚实无夸大。** 但发现 **1 处审计遗漏的 P1 风险（manual publish 不走来源复核闸门）** 和若干 P2 能力/交付缺口。

---

## 【问题清单】

### P1 · manual publish（人工发布）不检查 `requires_source_review`，未复核来源可被人工发布

- **位置**：`agent_service/releases.py:53-62`。来源复核阻断**只写在 `if lease_token:` 分支**（后台刷新/worker 自动发布）；`:44 elif row['status'] not in ('validated','succeeded')` 的人工发布路径**不执行**该检查。
- **现象**：后台自动发布遇到 `requires_source_review` 来源会 `raise ValueError('来源解释需要人工核对…')`（✓ 正确）；但 manual publish（用户 UI 点发布，lease_token 为空）不经过该闸门。
- **为什么是问题**：OCR/人工转录生成的新来源均带 `requires_semantic_review=True`（`visual_fact_service._prepare_source:268`）→ `source_interpretation_needs_review` 为真 → evidence 标 `requires_source_review=True`（`project_data.py:255-261`）→ `require_safe_numeric_lineage:662` 只要求"有该标记"即放行（不要求"已复核"）。因此**未复核来源的唯一发布闸门是 releases:53-62，而它只在后台路径生效**。若 UI 发布确认未强制展示/阻断"来源需复核"，用户可能在未复核（或仅 OCR 候选未人工逐项核对）的来源上直接发布，产生**未经确认的财务结果**。
- **期望行为**：输入"来源 `requires_source_review=True` 的 run 被 manual publish"，应输出阻断或**强制 UI 展示候选+复核状态并要求显式确认**，不能静默发布。
- **修复建议**（伪代码级）：
  ```python
  # releases.publish 把来源复核闸门移出 lease_token 分支，改为对所有发布路径生效；
  # manual publish 时要求 UI 已传递"用户已核对候选并确认"的回执，否则阻断：
  requires_source_review = manifest.get('requires_source_review') or any(
      source_interpretation_needs_review(frozen.get(item.get('source_id')), item.get('evidence'))
      for item in manifest.get('inputs', []) if isinstance(item, dict))
  if requires_source_review:
      if lease_token:               # 后台：一律阻断
          raise ValueError('来源解释需要人工核对，请查看候选结果后确认发布')
      if not confirmed_manual_review_receipt:   # 人工：必须有 UI 显式确认回执
          raise ValueError('来源解释需要人工核对；请对照原件核对候选后显式确认')
  # confirmed_manual_review_receipt 由 UI 在展示 requires_source_review 来源后签发，不可由模型/worker 伪造
  ```
- **备注**：此问题审计**未提**（属遗漏，见 §4）。

### P2 · OCR 候选未自动映射到人工录入（体验缺口，审计已诚实标注）

- **位置**：`visual_fact_service.py:103-171`（`recognize_source` 返回候选）+ `:194-218`（`propose` 需手动 `quote`+`facts`）。
- **现象**：OCR 返回 lines（text/box/confidence/坐标），但 `propose` 要求用户手动输入 quote 和每条 fact；OCR 候选与人工录入之间**无自动预填/区域映射**。扫描表格行列重建未实现。
- **为什么是问题**：真实财经用户 OCR 后仍需逐条手录，OCR 价值大打折扣；对大表格易漏/错。审计已标注（"扫描表格还原：未实现"、"OCR 行逐项纠错…尚需完善"），非夸大。
- **期望**：OCR 候选按 box 坐标映射到可编辑表单，用户逐行核对纠错后提交。
- **建议**：P2，可后续；在 UI 层实现候选预填 + 坐标高亮回看，不做自动批准。

### P2 · OCR 运行库/模型未随正式包（交付缺口）

- **位置**：`offline_ocr.py:3-5,31-54`（代码就绪、模型哈希锁定、禁联网下载），但运行时依赖 `rapidocr 3.10.0` + 三个 ONNX 模型未随包。
- **现象**：`_models()` 会因 `PackageNotFoundError`/模型缺失抛 `OCR_RUNTIME_UNAVAILABLE/OCR_MODEL_UNAVAILABLE`；`visual_fact_service:148-149` 正确降级为"请人工核对原件"。
- **为什么是问题**：正式包交付后 OCR 实际不可用（走人工路径），不符合"离线可用"；但**降级正确**（不假装可用），且 Codex/审计已如实声明未随包，**非夸大**。
- **期望/建议**：随包预编译 `rapidocr 3.10.0` + 三个 ONNX 模型（含 SHA-256 清单），按 H2 离线零配置交付；交付前验证子进程 `-m agent_service.offline_ocr` 在包内 cwd/import 可用。

---

## 【4 项重点 · 逐项结论】

1. **CSV 短行/坏引号财务完整性 — ✅ 正确且有测试**：`connectors.py:148-172` 用 `DictReader(strict=True, restval=missing哨兵)`；短行（restval）、**长行错位（restkey=None 触发 `None in row`）**、坏引号（strict 抛 csv.Error）、空/重复表头、编码（utf-8-sig→gb18030 回退）**全部拒绝而非静默吞**。`tests/test_csv_financial_integrity.py` 覆盖短行/长行/坏引号/空重复表头/显式空字段+引号内换行保留/GB18030/坏编码。**无 P0。**
2. **来源待复核标记/自动发布/历史上游链 — ✅ 后台正确，⚠ manual publish 有 P1（见上）**：`project_data.py:255-261` 对需复核来源强制 `requires_source_review=True`；`:585-679 require_safe_numeric_lineage` 迭代 DFS 全祖先 handoff 核验 + SQL 数值校验 + `:662` 对"needs_review 但无标记"的历史回执阻断（不追溯发明批准）；`releases.py:53-62` 后台刷新遇需复核来源阻断。**历史上游链完整。**
3. **RapidOCR 复用 — ✅ 高度规范（符合选型红线：复用开源、不自研）**：复用 RapidOCR 3.10.0 + ONNX Runtime CPU；三个模型 SHA-256 硬编码锁定（`offline_ocr.py:16-20`）；`recognize_image` 要求 `expected_sha256` 并比对原件（:75-77），**来源版本/哈希绑定**；显式本地路径防其 downloader 联网（:5）；`recognize_source` 子进程 `timeout=60` + `FileLock(timeout=0)` 并发阻断（:122,132-138），**并发/超时正确**；OCR 结果强制 `human_review_required=True`、`financial_facts_verified=False`、`complete=False`（:135-136,161-162），**永不自动成为财务事实**；`visual_fact_service.review` approved 必须 `confirmed_visual_review=True` + 复核时再查原件哈希（:275-289）。**UI 人工复核路径完整且安全。**
4. **49 项审计有无遗漏/夸大 — 无夸大，2 处遗漏**：审计大量标"部分实现/未实现"且明确"运行库与模型未随包、图表识别未实现、准确率未验"——**诚实偏保守，无夸大**。**遗漏 ①**：`releases.publish` 的来源复核闸门仅在后台（lease_token）路径生效、manual publish 未强制（P1）；**遗漏 ②**：OCR 人工复核后生成的新来源仍 `requires_semantic_review=True`，意味着人工转录来源**永不能后台自动发布**，对月度复用任务的业务影响（需"已确认口径可复用"机制）审计未提。

---

## 【5 维强制扫描】

1. **功能完整性**：多模态输入→OCR 候选→人工转录→复核→来源→加工→发布→导出 主链路已闭环且安全（OCR 不自动成数据）。缺口：扫描表格行列重建、图表识别、OCR 候选预填（均审计已标未实现）。
2. **边界与异常**：CSV 短行/长行/坏引号/表头/编码全拒 ✓；OCR 预算/超时/并发/版本变化 ✓；来源哈希绑定 ✓。**缺口**：manual publish 未强制来源复核确认（P1）。
3. **代码质量**：防御性极强（来源双检、回执校验、budget 限制、错误降级不假装）；命名清晰；无错误吞没。
4. **离线/零配置**：RapidOCR 复用正确，但运行库/模型未随包 → 交付后 OCR 实际不可用（降级正确）；需随包补齐。
5. **反向挑战 / 选型红线**：RapidOCR 复用符合用户"能复用开源不自研"红线 ✓；OCR 自研部分仅适配器/防护层，非重造引擎 ✓。

---

## 【给 Codex 的下一步指令】

1. **先修 P1**：把 `releases.publish` 的来源复核闸门从"仅后台"扩到**所有发布路径**；manual publish 须有 UI 显式"已核对候选"回执，否则阻断。给最小补丁+测试。
2. **补齐交付**：随包 `rapidocr 3.10.0` + 三个 ONNX 模型（SHA-256 清单），验证包内子进程可用（离线零配置）。
3. 审计补记 2 处遗漏（manual publish 闸门、OCR 复核后口径复用机制），避免后续误判。
4. 未验项（真实样本准确率、扫描表格重建、图表识别、准确率阈值）如实标注，不宣称完成。

---

## 【补记：Codex 反驳回应 + P0 可加性修补复核】

### 反向挑战：接受 Codex 纠正，撤回"无 P0"结论
**我撤回上一条"未发现会造成错误财务结果的 P0"结论。** 理由：Codex 真实复现了 **P0 —— 跨来源财经事实可加性未与逐行语义强绑定**：两条各自核对过的"净收入 123.4500"与"预算收入 200"被错误汇总为"净收入 323.4500"且可自动发布。

**根因（复核确认）**：
- `fact_service.py:132,276` 文本/视觉事实的 `aggregation` 默认 `'unknown'`；`align_facts`（:280-284）对 unknown 标 `needs_semantic_metadata`，但**需被显式调用**，存在未调用即把不同 measure 事实行直接相加的路径。
- `_source_from_facts:225` 只加 `do_not_sum_heterogeneous_measures=True` **标记**（原为 evidence 标记，非强制阻断），原发布闸门未强制校验 → 错加可发布。
- 这正是 Codex 表述的"文本与视觉事实缺乏与逐行语义及可加性强绑定的回执"。

### 修补复核：✅ 动根因，覆盖所有发布路径
- **`releases.py:61-75 _requires_fact_alignment`**：任何来源含 `fact_review_id/visual_fact_review_id/semantic_alignment_required` → 需事实对齐（注释："Evidence review does not establish additivity of financial facts"）。
- **`releases.py:149-150`**：`if _requires_fact_alignment(...): raise ValueError('来源事实尚缺少逐条业务语义和可加性的可信核对，不能发布…')` —— **在 lease_token 分支之前，对所有发布路径（后台+manual）硬阻断**。✓ 直接堵住 P0。
- **`releases.py:157-162`**：manual publish 现要求 `review_actor`（认证用户）+ `confirm_source_review=True` + `expected_commit==run.commit` + `expected_parquet_sha256==manifest.parquet_hash` + `candidate_run==run_id`（候选未被新版本替代）——**我上轮 P1（manual publish 不走来源复核闸门）已一并修复**。✓
- **`releases.py:78-126 _verify_review_sources`**：新复核回执前重验原件/原始版本/派生核对依据（fact/visual proposal 必须 approved + output_source_id 绑定 + 原件存在且摘要一致）；原件缺失或替换 → 拒绝创建新回执。✓
- 前端 `api.py:496`/`execution.py:164`/`static/local-data.js:34`：`fact_semantic_alignment_required` 传播，candidate 预览 + 醒目提示 + 确认。✓

### OCR 预填：部分接受
接受 Codex 口径：UI 已支持用户**主动把识别候选加入原文草稿**（供核对），但**绝不自动填成已审核金额**（安全正确）；图中区域高亮、批量表格仍未实现（P2，审计已标）。我撤回"OCR 候选未自动映射到人工录入"的绝对表述，改为"部分（草稿可加候选，非自动填值；区域高亮/批量表格缺失）"。

### 新增风险（P2，能力缺口，非本轮阻断）
- **事实来源发布死锁风险**：`_requires_fact_alignment` 阻断后，当前**无"完成逐条语义+可加性人工核对"的放行路径**——文本/视觉事实来源暂时**无法发布/下游复用**（安全优先，但业务闭环未完成）。需提供"逐行 measure 语义 + 可加性人工确认"回执机制后，才允许事实来源进入可发布链。此点审计未提（补记遗漏③）。
- 未变未验：OCR 运行库/模型未随包、真实样本准确率、扫描表格重建、图表识别。

### 给 Codex 的下一步
1. 接受本轮 P0/P1 修补（动根因）。**补充：设计并实现"逐行语义 + 可加性人工确认"放行回执**，避免事实来源长期死锁（否则财经用户无法用文本/视觉事实出可发布结果）。
2. 随包 OCR 运行库/模型 + 包内子进程可用性验证。
3. 未验项（真实样本准确率、扫描表格重建、图表识别）如实标注。
4. 62 项联合回归证据请给文件路径（本补记未重跑，以源码逻辑复核为准）。
