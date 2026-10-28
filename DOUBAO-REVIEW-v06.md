# DOUBAO-REVIEW-v06 — Codex r24 复审（未复核洗白修复 / 单条原值下载 / PDF·OCR 新增）

- 审查者：豆包侧首席架构评审官（只读静态审查，未改源码、未触碰真实用户数据/密钥、未操作运行中服务、未参与构包与回归）
- 审查对象：`C:\Users\001\Documents\ChatGPT\New project\_github-publish\databuddy` 当前工作树
- 证据：`agent_service/releases.py`、`agent_service/single_fact_value.py`、`agent_service/offline_ocr.py`、`agent_service/visual_fact_service.py`、`agent_service/static/data-workbench.js`、`verification/pdf-ocr-browser-20261028.json`
- 范围：本轮按 Codex 要求只审「未复核上游洗白 / 单条下载 / PDF·OCR 新增」的财务准确性、来源串用、异常路径与高危漏洞；G1 真实模型月结、OCR 运行库随包、真实样本准确率**均未验收**。

---

## 【本轮结论】

- 状态：🔧 继续迭代（核心安全设计正确，有 1 项待验证 P1 + 多项未验收）
- 置信度：高（逐行读源码 + 前后端链路核对）

**一句话**：**单条原值下载与旧记录只读是干净设计，无法绕过 `_requires_fact_alignment`；OCR/PDF 新增在坐标空间、加密、子进程边界上处理正确，未发现明确可利用的高危漏洞。** 但「未复核上游被下游洗白」的修复存在**一项待验证的递归边界**：`_requires_fact_alignment` 只查直接输入的 fact 标记、不递归 `origin_source_id` 链，历史发布的事实派生结果作为下游输入时可能逃过该闸门。未经验证前不判 P0，但**不能判为已堵死**。

---

## 【问题清单】

### P1（待验证，非已证实）· `_requires_fact_alignment` 不递归 origin 链，历史发布的事实派生结果可能被下游洗白

- **位置**：`agent_service/releases.py:61-75`（`_requires_fact_alignment` 只遍历 `manifest['inputs']` 直接项，仅查 `source.get('fact_review_id') / visual_fact_review_id / semantic_alignment_required`，**不递归 `origin_source_id` 链**）；对照 `_verify_review_sources`（releases.py:78-126）虽沿 `origin_source_id` 校验派生资料核对依据，但那是"新 owner 复核回执"的原料校验，**不等同于可加性阻断**；`require_safe_numeric_lineage`（project_data.py:585-679）DFS 全祖先校验的是数值精度 handoff，**不校验事实可加性**。
- **现象**：新 run 直接引用带 fact 标记的源 → `_requires_fact_alignment` 阻断发布 ✓。但若一个**已发布的历史结果 source**（修复前由事实来源加工/汇总生成、自身记录不带 `fact_review_id`/`semantic_alignment_required`）被**下游 data_workflow 作为输入**引用，下游 run 发布时 `_requires_fact_alignment` 只查该结果 source 自身标记 → 可能**不阻断**，从而把历史错加结果（如"净收入 323.4500"）当可信上游洗白。
- **为什么是问题**：违反硬约束——"未复核上游不得被下游洗白"必须覆盖**所有**复用路径，包括复用已发布/已采用的历史结果；只堵"新 run 直接含事实源"不够。
- **期望行为**：输入"下游 run 引用任一来源，其 `origin_source_id` 递归链上存在带 `fact_review_id`/`visual_fact_review_id`/`semantic_alignment_required` 的来源"，应输出发布阻断（或该结果 source 在生成时已显式传播并冻结 `semantic_alignment_required=True`，则需证据）。
- **修复建议（伪代码级，二选一）**：
  ```python
  # 方案A：_requires_fact_alignment 递归 origin_source_id 链
  def _requires_fact_alignment(run, manifest):
      frozen = run.get('source_snapshot') or {}
      seen = set()
      def walk(sid):
          if sid in seen: return False
          seen.add(sid)
          s = frozen.get(sid) or {}
          if (s.get('fact_review_id') or s.get('visual_fact_review_id')
                  or s.get('semantic_alignment_required') is True): return True
          return walk(s.get('origin_source_id')) if s.get('origin_source_id') else False
      return any(walk(item.get('source_id')) for item in (manifest.get('inputs') or []) if isinstance(item, dict))

  # 方案B：发布结果 source 在生成时强制传播 semantic_alignment_required（若上游任一 fact 标记为真），并冻结到 source 记录
  ```
- **需要 Codex 确认/补证**：当前是否已有方案 B 的传播（发布结果 source 记录带 `semantic_alignment_required`）？若有，请给该传播点的源码行号 + 一条"下游引用历史发布事实派生结果被阻断"的定向测试；若无，请按方案 A 补递归并给回归证据。**在你给出证据前，此条保留为待验证 P1，不能判为已修复。**

### P2 · OCR/单条下载已实现但多项未验收（如实保留）

- OCR 运行库/模型未随包；真实脱敏财务样本未验证（G1 未通过）；`verification/pdf-ocr-browser-20261028.json` 是合成 PDF 的 Chrome+API+OCR 链，**不代表真实财务准确率**。Codex 已如实声明，未冒称通过 → 属"未验收"，不判缺陷。
- 表单批注（annotation）不在渲染像素内 → OCR 可能遗漏，Codex 已诚实标注；扫描表格行列重建、图中区域高亮仍未实现。

---

## 【3 项重点 · 逐项结论】

### 1. 未复核上游洗白修复 — ⚠ 直接输入已堵，递归边界待验证（P1，见上）
- 发布层 `releases.publish:149-150` 对所有路径硬阻断"直接含事实标记来源"的 run（上一轮 v05 补记已确认，本轮仍有效）。
- `single_fact_value.preview:80`"只能核对**尚未采用**的候选；历史成果保持只读" → **防旧记录被复核/误追认** ✓。
- 直接输入链：已确认堵死；**递归/历史发布结果复用链：未验证（P1）**。

### 2. 单条原值下载 — ✅ 干净隔离，不可绕过
- `single_fact_value.py` 是独立只读工作流：`create_candidate` 强制 value 列 `aggregation='non_additive'`（:42）且仅"恰好一条事实"（:28-29）；`preview` 逐字段强校验（fact_id/measure/value/value_as_printed/unit/period/dimensions/source_locator/三哈希/核对记录，:118-153），并校验 handoff 输出==单行原值（:156-158）；`confirm` 需认证 actor + `preview_sha256` 重算（:188-192）+ 回执 method/scope 硬校验（:177-185）；`export` 需有效回执（:205-210）。
- **关键**：`confirm/export` **不创建任何可消费 source、不发布、不进下游链**，WARNING 明确"仅核对一条原文数值…不允许计算、汇总、关联、图表、报告或下游加工"（:15）。前端 `data-workbench.js:1130-1142` 确认回执 `scope=='single_fact_original_value_only'` + 下载 URL 白名单 + 明确提示。**无法绕过 `_requires_fact_alignment`** ✓。
- 边界正确：前端下载原件并 SHA-256 重算（:1104-1105）、文本事实三哈希与文本区间校验（:1108-1113）、PDF 用 sandbox iframe 定位页码（:1119）。

### 3. PDF·OCR 新增 — ✅ 坐标/加密/子进程边界正确；财务准确性未验收
- `offline_ocr.py:145-191 recognize_pdf`：pypdfium2 5.14.0（版本锁定，:158）、页数 1-500（:149,172）、**显式处理 PDFium 空密码可打开加密 PDF** 并拒加密（:166-170）、渲染尺寸上限（:178-182）、`coordinate_space='rendered_pdf_page_pixels_not_pdf_coordinates'`（:191）→ **正确标注渲染像素坐标、非 PDF 坐标**，防坐标误用。
- `visual_fact_service.py:192-206 _ocr_original`：**主进程只读字节、PDF 解析交给 bounded worker**（防主进程被恶意 PDF 攻击/卡死）；:265-276 强校验 OCR 结果（page/revision/SHA/coordinate_space/render_scale/pdf_rotation_degrees/pdf_page_count）；:281 识别后重验原件未变。OCR 结果仍强制 `human_review_required=True`、永不自动成财务事实。
- **高危漏洞**：未发现明确可利用的高危（子进程无 `shell=True`、输入为可信路径 JSON、`timeout=60`、输出 ≤4MB、请求 ≤8192、FileLock 并发阻断）。**但子进程非 OS 沙箱**（Codex 已注明）——恶意 PDF 可致子进程崩溃/资源占用，最坏是拒绝服务而非数据泄露（主进程不解析、结果有界）。可接受，标注即可。

---

## 【5 维强制扫描】

1. **功能完整性**：单条原值核对/下载闭环完整且只读；OCR 图片+PDF 单页候选链路完整（人工核对必经）；但扫描表格重建、图表识别、区域高亮未实现（未验收）。**G1 真实模型完整月结未通过**。
2. **边界与异常**：PDF 加密/页数/尺寸/页码、OCR 超时/并发/来源版本变化、单条下载防过期（preview_sha256）均正确；**P1 递归边界未验证**。
3. **代码质量**：防御性极强（逐字段校验、回执不可变、scope 白名单、URL 白名单）；命名清晰；无错误吞没。
4. **离线/零配置**：OCR 运行库/模型未随包 → 交付后 OCR 实际不可用（降级为人工核对正确）；需随包补齐。
5. **反向挑战**：Codex 对 v05 的 P0/P1 修补在本轮工作树仍然有效（`_requires_fact_alignment`/`single_fact_value`）；本轮新增 P1 递归边界需 Codex 用证据裁定，我不预设它一定存在缺陷。

---

## 【给 Codex 的下一步指令】

1. **裁定 P1**：给出"`_requires_fact_alignment` 是否递归 `origin_source_id` / 发布结果 source 是否传播并冻结 `semantic_alignment_required`"的明确结论 + 源码行号 + 一条"下游引用历史发布事实派生结果被阻断"的定向测试；无证据则按方案 A 补递归并回归。
2. 明确"禁止下游复用"对**历史已发布/已采用结果**的覆盖范围（只读 ≠ 不可复用，需说明已发布结果是否仍可被新 run 引用及是否被阻断）。
3. 未验收项（OCR 随包、真实样本准确率、扫描表格/图表识别、G1 真实模型月结）保持如实标注，不冒称通过。
4. r24 包本体验证完成后补交证据（当前不预先声称通过）。
