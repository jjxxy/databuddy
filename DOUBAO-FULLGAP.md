# DOUBAO-FULLGAP — Atlas DataBuddy 全量能力与缺口矩阵（财经业务用户视角）

- 审查者：豆包侧首席架构评审官（主动全量排查，非等点名）
- 审查对象：项目根 `personal-data-agent`（r17 工作树，已含 AG-7/AG-8 补丁）
- 原则：按"真实财经用户要拿它工作"逐环节过，分 **程序缺陷 / 能力缺口 / 未验证** 三类；每条给用户痛点、源码位置、复现/证据、建议。数值正确 ≠ 产品能交付，能生成 ≠ 好看好用。
- 已读证据：`data_tools.py`、`data_workflows.py`、`workbook_reader.py`、`document_reader.py`、`project_reports.py`、`office.py`、`business_reports.py`、`project_data.py`、`agent_progress.py`、`agent.py`、`acceptance/finance_monthly/README.md`；105 项定向测试（AG-1~AG-6）。

---

## 0. 能力现状（确认扎实、可放行的部分）

| 维度 | 能力 | 证据 |
|---|---|---|
| 数据加工引擎 | select/drop/filter/reclassify/compute(+−×÷)/aggregate(sum/min/max/count/concat)/lookup/union_rows/sql/normalize(decimal/percent/date，币种 CNY/USD/EUR/JPY) | `data_workflows.py:113-205` |
| 输入结构理解 | 多 sheet、两级合并表头、同页多表、B/C 列起表、日期/金额/比例/负数/文本数字、公式语义 | `workbook_reader.py`（openpyxl+流式，不整表加载，不 forward-fill 合并单元格） |
| 数据正确性 | decimal 定点不提前舍入、来源坐标/版本/规则保留、审计、交接包哈希、独立十进制校验器 | `project_data.py`、`verify_export.py` |
| 业务闭环 | 登记→运行→预览确认→发布→导出→复用（参数化）→月更→更正→改口径→重启恢复 | A01-A11 设计 + AG 系列 |
| 安全边界 | 报表源只走预配置 connector、不越权 URL/认证头、密钥脱敏、SSRF 防护 | `project_reports.py:3,115-119` |

---

## 1. 程序缺陷（已确认）

| ID | 级别 | 位置 | 现状 |
|---|---|---|---|
| P0-AG7 | 已修(r17) | `agent_progress.py:73-78` | 失败/拒绝的非透明工具不再清空恢复 coverage；穿插 failed read_file 不能绕过 NO_PROGRESS。已复核补丁动根因 ✅ |
| P0-AG8 | 已修(r17) | `agent.py:670` | 未绑定 `pipeline_id`（None）的调用 `_tool_request_key` 返回 None，失败永不进清算；同 project 内 pipeline 焦点 A→B 不会错清 A 失败。已复核 ✅ |
| 其他 6 攻击方向 | 未发现 | agent.py:795-855 / project_data.py:426-490 | 历史授权/撤回、压缩 epoch、拒绝回执、成果复用核验、读B不清A、续跑不重复——均正确（105 项测试全绿） |

## 2. 能力缺口（真实财经用户会卡住 / 不满足"好看好用"）

> 这些不是"能跑"层面的 bug，是"用户真要用它干活"时的缺口。每条是真实业务场景。

**G-OCR · OCR 未实现 —— 扫描件/图片报表无法自动提取数据 [P1，能力缺口]**
- **位置**：`document_reader.py:475,485`（`'image_source; no_ocr_executed'`、`ocr_executed=False`）
- **用户痛点**：国内财务大量扫描凭证、盖章报表、截图银行流水。当前只能"提示需 OCR/人工"，不能自动读取 → 核心输入环节直接断。
- **证据**：`representation='image_source; no_ocr_executed'`，`ocr_executed=False`；PDF 也仅 pypdf 文字层，无文字层即失败（:449 `layout_and_graphics_not_verified`）。
- **建议**：评估纳入**离线 OCR**（本地引擎，符合零配置/离线 H2）。至少对扫描 PDF/图片做表格结构化提取；未配置时明确阻断并标"该格式未验证"，不得静默空读。（对应基线 G6）

**G-XLS · 老式 Excel `.xls/.xlsb/带宏/密码` 不支持 [P1，能力缺口]**
- **位置**：`workbook_reader.py`（openpyxl，仅 .xlsx）
- **用户痛点**：国内财务/ERP 导出的老 `.xls`、加密工作簿常见，用户上传即无法解析。
- **证据**：`from openpyxl import load_workbook`，openpyxl 不支持 .xls/.xlsb 二进制老格式。
- **建议**：视用户群评估是否引入 xlrd(.xls)/pyxlsb，或要求转存 .xlsx 并在 UI 明确提示支持范围。（对应基线 G10）

**G-REPORT · 报告/PPT 无视觉设计（用户核心诉求）[P1，能力缺口]**
- **位置**：`office.py:30-100`、`business_reports.py:208-274,310+`
- **用户痛点**：用户 2026-10-26 明示"要能出好看的报告和牛逼的PPT；很多 agent 生成的 PPT/报告都很垃圾"。当前纯文本模板（pptx 默认布局+标题+≤12行文本22pt、docx 默认样式、xlsx 深绿表头）→ 属于"垃圾"反例。
- **证据**：恒 `visual_validation:False`；无图表/主题/母版/封面排版。`business_reports` 冻结字符串导出（防改写数字是数据安全优点），但视觉仍简单。
- **建议**：按 G4 口径（短简报不强套目录/分节；图表按真实数据择用、不编造趋势、不硬用饼图；`visual_validation` 由真实渲染检查产出，不硬改 true）。见 v04。

**G-ENGINE · 真实 Excel 计算引擎未接入 [P2，能力缺口]**
- **位置**：`workbook_reader.py`（公式 Tokenizer 提取语义）；A03 要求核对预算公式缓存与公式一致
- **用户痛点**：含公式的工作簿，系统只"解释公式语义/拒绝"，不调用 Excel 引擎重算缓存 → 用户不能确认公式结果的正确性。
- **证据**：README:27,54,99（N04 除零公式必须拒绝，不得声称已重算）；校验器 `只提取公式语义`。
- **建议**：如实标注"未调用计算引擎重算"，由用户在源端核对；不新增假"已重算"。若引入引擎需离线、受控。（对应基线 G8）

**G-NODE · 加工节点缺 pivot/窗口/排名/滚动累计 [P2，能力边界]**
- **位置**：`data_workflows.py:113-205`（无 pivot/window/rank/cumsum 节点）
- **用户痛点**：同比环比、滚动 12 月、排名、透视表需绕 `sql` 节点，普通业务用户不会写 SQL。
- **证据**：节点类型枚举无上述。
- **建议**：非本轮硬门槛；在 UI 透出"复杂分析可用 SQL 或说明需扩展"，不作为财经月结放行前提。

**G-CONN · 报表/ERP 源需运营者预配置，LLM 不能自助接入 [P2，合理边界]**
- **位置**：`project_reports.py:54-119`（connector 由运营者 configure，Agent 只能选）
- **说明**：安全正确（凭据不交 LLM），但真实用户接 SAP/金蝶/用友/BI 需运营者先行配置。属合理边界，需在文档/UI 明确"接入需管理员配置"。

## 3. 未验证（不得宣称已通过）

| ID | 项 | 用户影响 | 依据 |
|---|---|---|---|
| G1[P0] | 完整财经月结 A02-A05 用**真实模型**端到端 + `verify_export.py` | 财务数一分钱都不能错；当前只跑简化 118.15 三件套 | `finance_monthly/README.md:3`、FG-1 |
| G2 | 真实脱敏业务样本验收集 | 合成数据证明不了真实口径 | `BUSINESS-WORKSPACE.md:91` |
| G5 | 百万行性能/并发容量 | 财经数据量大，未验 | `finance_monthly/README.md:138` |
| G7 | 会计期间/节假日/调休调度 | 工作日≠法定假日，未验 | `BUSINESS-WORKSPACE.md:100` |
| G9 | 多租户隔离/共享权限/备份恢复/并发 | 多用户共用时未验 | `docs/v19-review.md:55` |
| OB-2 | 物理断网（真实断网/防火墙） | 离线承诺未验 | 各轮 |
| G8 | 真实 Excel 引擎 | 未接入 | 上表 |

---

## 4. 给 Codex 的下一步（按优先级）

1. **G1**：真实模型跑完整财经月结 A02-A05（含 A03 公式语义/混合日期/负数/0.13 比例与 13% 文本），`verify_export.py` 独立十进制校验——财经生产放行硬门槛，最高优先。
2. **G-OCR / G-XLS**：评估并明确支持范围（本地离线 OCR 是否纳入；.xls/.xlsb 是否支持或明确提示转存）。这直接决定"真实用户能否把扫描件/老 Excel 喂进来"。
3. **G4**：office.py（通用）与 business_reports.py（财经简报）两条渲染路径视觉升级（母版/主题、真实数据图表、视觉验收产出 visual_validation）。
4. 未验项（G2/G5/G7/G9/OB-2/G8）如实标注，不写已通过。

---

## 5. 结论

**核心能力（数据加工、结构理解、正确性、业务闭环、安全）扎实，接近可用；但"输入广度（OCR/老 Excel）"与"输出质量（报告/PPT 视觉）"是真实用户最直接感知的两个大缺口**，加 G1 真实模型验证未过。三者不补，产品仍停留在"能力 demo"而非"可交付工具"。建议按上表优先级逐项闭环。
