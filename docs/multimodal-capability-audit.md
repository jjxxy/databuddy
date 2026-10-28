# 多模态数据处理能力工程核查

核查对象：[多模态专项需求清单](multimodal-data-processing-budget-1100.md)。本文是工程现状核查，不修改预算申请内容；条目状态代表代码覆盖程度，不代表业务验收、生产安全或财务正确性。

**财务使用警示：r24 为修复前历史包，禁止用于财务生产。** 来源派生和 Agent 交接两处已复现绕过已修复，r25 测试包本体 7 项定向测试通过；最终干净副本已核查与测试包文件一致，功能测试未在副本复跑。真实财经准确率及业务验收尚未通过。证据见 [r25 验证记录](../verification/r25-finance-and-ocr-20261028.json)。

状态口径：**已实现**为需求限定范围内存在可运行实现；**部分实现**为仅覆盖部分格式、链路或条件；**未实现**为未发现可完成该需求的端到端实现。无论状态如何，均需按约定真实业务样本和用户验收标准验证。

特别说明：人工视觉转录依赖用户对照原图确认，系统未独立核验像素；OCR API 和工作台界面可请求 PNG、JPEG、WebP 单帧图片或 PDF 的指定单页候选，用户自行选择后再人工录入，系统不会把识别候选自动认定为准确。PDF OCR 绑定原件修订、SHA-256 和页码，识别框仅对应本次渲染图像像素，不能映射为 PDF 原生坐标；表单与注释可能未被渲染，须对照原件。r25 测试包本体已用合成扫描 PDF 测试，不能代表真实财务准确率；缺少组件时提示人工处理。Docling 为可选路径，同样不等于完成图表或扫描表验收。

### 当前财务安全边界

- 文字事实及视觉事实的原文核对，仅证明用户核对过单条提取内容，**不证明不同指标、单位、期间、含税或汇率口径可以相加或关联**。目前没有可验证的逐条业务语义对齐回执，事实或视觉事实派生来源仅形成供分页查看的候选，不能正式采用、发布、新报告和下游数值复用；已复现的来源派生与 Agent 交接绕过已修复并通过定向测试，单条事实也不自动例外。单条已核对事实可以生成原样候选，并对照原件确认后单独导出 JSON；这不改变正式采用状态，也不允许汇总、报告或下游加工。来源确认勾选不能代替业务语义核对。恢复正式加工需要先设计并验证绑定原件修订与摘要、完整事实与业务口径、处理方案和输出摘要的可信人工回执，之后再明确支持范围。
- 未对齐的事实候选保留只读分页和警告；批量 CSV、Excel、完整交接包和基于候选的汇总图表在既有已测路径受到后端门槛限制，工作台及数据应用同步隐藏或禁用相关按钮。r25 包体七项定向回归覆盖已复现的两条绕过，但不能证明所有路径安全。跨 Agent 接收的普通来源暂时也被保守限制，不能直接正式发布；缺少可证明来源的候选不得被视为安全。关联代码：[project_data.py](../agent_service/project_data.py)、[releases.py](../agent_service/releases.py)、[workspace_artifacts.py](../agent_service/workspace_artifacts.py)、[applications.py](../agent_service/applications.py)。
- 已发布历史成果保持原样：可证明属于当前已发布版本且结果完整时继续只读查看和导出；来源或业务语义证据不足时显示待核对风险，不能标记为已确认，也不计算或展示新的应用汇总和图表。无法证明来源或发布身份的其他历史记录不应据此自动放行批量输出。历史警告不会追认旧口径的正确性。
- 一般 CSV 或 Excel 即便通过格式、来源和数值校验，也不能证明内容的业务含义正确；尤其匿名 CSV 可能缺失含税、汇率、期间及预算版本等信息，系统无法仅凭列名判断业务口径。用户应补足数据字典、原始依据和业务核对；当前不保证对未知语义自动识别或阻断所有错误。
- 当前变更的指定回归和 r25 测试包本体已用合成资料验证；最终干净副本的 17,509 个包文件与测试包清单一致、无缺失或额外文件，168 个源码文件独立核查一致；功能测试未在副本复跑。Windows 长路径导入曾失败，短路径仅为规避；真实财经/月结、云端部署和业务验收未验证，不能据此宣称财务准确率已验收。

## 优先顺序

1. **P0 财务安全和可复核**：保持来源版本绑定、未复核来源不得自动发布、历史证据缺失时重新运行；验证漏读、错读、金额精度、失败保留和权限。
2. **P1 核心多模态闭环**：选定并随包提供可离线使用的识别组件和模型，完成扫描 PDF、图片、表格、图表的来源定位、候选校正和明确人工核对，并对关键字段建立真实数据验收。
3. **P2 业务能力纵深**：完善跨来源口径冲突、字段级溯源、结构化导出、性能容量和便于业务用户理解的过程说明。

## 逐项核查

### 一、多模态资料接入

- **多格式资料接收：已实现。** 证据：[api.py:receive_file](../agent_service/api.py)。缺口：按扩展名接受清单中的 CSV、XLSX、PDF、PNG 和 JPEG；需要继续检查伪装类型、损坏及端到端可读性。

- **文件类型识别：部分实现。** 证据：[api.py:receive_file](../agent_service/api.py)、[connectors.py:_file_rows](../agent_service/connectors.py)。缺口：主要由扩展名选解析器，Office 会检查 ZIP；尚无全部格式可靠内容识别与一致性判断。

- **批量资料组织：部分实现。** 证据：[api.py:attachment](../agent_service/api.py)、[agent.py](../agent_service/agent.py)。缺口：项目可多附件并记录请求，但按一批完成及按业务资料分组的完整工作流仍需场景验收。

- **原件版本记录：已实现。** 证据：[sources.py:revision](../agent_service/sources.py)、[api.py:receive_file](../agent_service/api.py)、[project_data.py:source_path](../agent_service/project_data.py)。缺口：来源修订和 SHA-256 可核对；旧资料缺摘要及外部来源时不能追认。

- **输入条件检查：部分实现。** 证据：[api.py:receive_file](../agent_service/api.py)、[document_reader.py:read_document](../agent_service/document_reader.py)、[connectors.py:_file_rows](../agent_service/connectors.py)。缺口：支持大小、部分损坏及编码检查；加密、压缩炸弹与全部格式异常需继续分格式验证。

- **资料范围确认：部分实现。** 证据：[project_data.py:InputBinding](../agent_service/project_data.py)、[workbook_reader.py:read_table](../agent_service/workbook_reader.py)。缺口：支持来源及表格范围绑定，但跨文件业务期间需人工声明，尚无完整自动判断。

### 二、表格与结构化数据解析

- **工作簿读取：部分实现。** 证据：[workbook_reader.py:read_table](../agent_service/workbook_reader.py)。缺口：支持指定页签、范围及多表头；复杂企业工作簿与完整 UI 选择仍需业务样本验收。

- **复杂表头解析：部分实现。** 证据：[workbook_reader.py:inspect_workbook](../agent_service/workbook_reader.py)、[workbook_reader.py:read_table](../agent_service/workbook_reader.py)。缺口：候选和合并锚点有处理；候选不等于确认，跨页或特别复杂布局仍需复核。

- **表格结构核对：部分实现。** 证据：[workbook_reader.py:read_table](../agent_service/workbook_reader.py)。缺口：可配置隐藏行、汇总行策略；需实测多种文件结构与用户确认体验。

- **文本与数字区分：部分实现。** 证据：[workbook_reader.py:_value](../agent_service/workbook_reader.py)、[workbook_reader.py:read_table](../agent_service/workbook_reader.py)。缺口：可保留原数值文本、类型与坐标；Excel 本身已损失的精度无法恢复。

- **公式与缓存值提示：部分实现。** 证据：[workbook_reader.py:_value](../agent_service/workbook_reader.py)、[workspace_artifacts.py](../agent_service/workspace_artifacts.py)。缺口：记录公式和缓存可用性并提示不重算；缓存新鲜度未知，尚无完整 Excel 计算引擎。

- **CSV 规范解析：部分实现。** 证据：[connectors.py:_file_rows](../agent_service/connectors.py)、[tests/test_csv_financial_integrity.py](../tests/test_csv_financial_integrity.py)。缺口：校验逗号分列、重复或空表头、字段数和不合法引号及编码；只支持 UTF-8/GB18030，其他分隔符及编码不自动识别。

### 三、文档、图片与扫描件识别

- **文档文本提取：部分实现。** 证据：[document_reader.py:read_document](../agent_service/document_reader.py)、[connectors.py:_file_rows](../agent_service/connectors.py)。缺口：提取 PDF 文字层和 Office XML 文本、单元格；版式、图形及 PDF 表格结构未全面保证。

- **页面与图像预处理：未实现。** 证据：[offline_ocr.py:recognize_image](../agent_service/offline_ocr.py)。缺口：图片识别保持原始像素坐标，PDF 单页在受控进程内渲染并保留该图的像素坐标；未实现原 PDF 坐标映射、去倾斜或去模糊处理。

- **扫描文字识别：部分实现。** 证据：[offline_ocr.py:recognize_image](../agent_service/offline_ocr.py)、[visual_fact_service.py](../agent_service/visual_fact_service.py)、[data_api.py](../agent_service/data_api.py)、[data-workbench.js](../agent_service/static/data-workbench.js)、[tests/test_offline_ocr.py](../tests/test_offline_ocr.py)。API 与工作台对绑定原件修订及摘要的单帧 PNG/JPEG/WebP 或 PDF 指定单页返回未验证 OCR 候选，由用户自行选择并对照原件核对；PDF 框坐标仅对应渲染图像，不是原 PDF 坐标。缺口：真实财经样本识别准确率及业务验收尚未完成；表单及注释可能漏渲染，人工核对不构成业务语义对齐。

- **版面结构识别：部分实现。** 证据：[document_reader.py](../agent_service/document_reader.py)、[connectors.py:_file_rows](../agent_service/connectors.py)。缺口：Office XML 结构、PDF 页和可选 Docling 文本块可保留部分位置；扫描页面标题、表格与附注的完整识别未验。

- **财经字段提取：部分实现。** 证据：[fact_service.py:propose_facts](../agent_service/fact_service.py)、[visual_fact_service.py:propose](../agent_service/visual_fact_service.py)。缺口：可提交带数值、单位和期间的待核对事实；文字或视觉事实核对后仍须业务语义对齐，现阶段不允许其作为正式数值加工来源。自动识别发票或合同字段及准确率尚未验收。

- **扫描表格还原：未实现。** 证据：[offline_ocr.py:recognize_image](../agent_service/offline_ocr.py)、[visual_fact_service.py](../agent_service/visual_fact_service.py)。缺口：OCR 是按行候选、人工录入是人工转录；尚无扫描表格行列与单元格结构重建。

### 四、图表数据识别

- **图表位置识别：未实现。** 证据：[document_reader.py:read_document](../agent_service/document_reader.py)。缺口：记录是否存在图形，不定位图表范围。

- **图表文字提取：未实现。** 证据：[offline_ocr.py:recognize_image](../agent_service/offline_ocr.py)。缺口：一般图片可输出行文字候选；尚无图表标题、坐标、图例分类抽取。

- **图表单位提示：未实现。** 证据：[visual_fact_service.py](../agent_service/visual_fact_service.py)。缺口：人工录入要求填写单位，尚无图表单位和刻度自动识别提示。

- **可见数值整理：未实现。** 证据：[visual_fact_service.py](../agent_service/visual_fact_service.py)、[offline_ocr.py](../agent_service/offline_ocr.py)。缺口：人工可提交数据且需复核；尚无图表标签到数据点的可靠整理。

- **图表原件回看：部分实现。** 证据：[workspace_artifacts.py:preview](../agent_service/workspace_artifacts.py)、[visual_fact_service.py](../agent_service/visual_fact_service.py)。缺口：可查看原始图像并按页人工核对；尚无图表区域或数据点自动定位关联。

### 五、跨模态关联与语义对齐

- **跨文件字段对齐：部分实现。** 证据：[semantic_alignment.py:suggest_alignment](../agent_service/semantic_alignment.py)、[semantic_service.py](../agent_service/semantic_service.py)。缺口：支持字段语义候选与人工审核，但现有字段对齐不是事实逐行和处理方案的可信语义回执，不能放行文字或视觉事实正式加工；跨表格、文档和图像统一链路未完成验收。

- **业务主体匹配：部分实现。** 证据：[semantic_alignment.py](../agent_service/semantic_alignment.py)、[data_workflows.py](../agent_service/data_workflows.py)。缺口：可定义映射和键关联；合同/单据/表格跨模态主体匹配及歧义策略待实测。

- **期间与版本对齐：部分实现。** 证据：[semantic_alignment.py:compare_fields](../agent_service/semantic_alignment.py)、[sources.py:revision](../agent_service/sources.py)。缺口：支持语义期间校验及来源版本；跨多种资料冲突提示仍需验收。

- **不确定匹配处理：部分实现。** 证据：[semantic_alignment.py:suggest_alignment](../agent_service/semantic_alignment.py)、[semantic_service.py](../agent_service/semantic_service.py)。缺口：候选匹配有阻断和人工确认；跨模态多候选交互需验证。

- **跨来源冲突比较：部分实现。** 证据：[semantic_alignment.py:compare_fields](../agent_service/semantic_alignment.py)、[fact_service.py:align_facts](../agent_service/fact_service.py)。缺口：可比较语义字段；缺少完整多来源数据冲突逐条对照和选择体验。

### 六、财经精度与数据加工

- **金额与单位解析：部分实现。** 证据：[workbook_reader.py:parse_localized_value](../agent_service/workbook_reader.py)、[data_workflows.py](../agent_service/data_workflows.py)。缺口：有显式定点数、单位策略；扫描结果及混合格式真实样本尚需复核。

- **百分比口径解析：部分实现。** 证据：[workbook_reader.py:_normalize_percentage_cell](../agent_service/workbook_reader.py)。缺口：区分明确百分号、Excel 格式与裸数歧义；跨格式场景待实测。

- **日期与会计期间：部分实现。** 证据：[workbook_reader.py:parse_localized_value](../agent_service/workbook_reader.py)、[semantic_alignment.py:_concrete_period](../agent_service/semantic_alignment.py)。缺口：支持明确格式及固定期间；企业会计期间、节假日和变更规则仍需定义。

- **财务口径冲突提示：部分实现。** 证据：[semantic_alignment.py:compare_fields](../agent_service/semantic_alignment.py)、[data_workflows.py](../agent_service/data_workflows.py)。缺口：可检查部分已声明的单位、期间和汇总语义；匿名 CSV 未声明的口径不可凭表头推断，含税、汇率、预算版本等长期口径未全面闭环。

- **精确计算与舍入：部分实现。** 证据：[data_workflows.py](../agent_service/data_workflows.py)、[data_contracts.py](../agent_service/data_contracts.py)、[tests/test_data_contract_numeric_evidence.py](../tests/test_data_contract_numeric_evidence.py)。缺口：使用定点数并阻止已知不安全计算；公式缓存、输入真实性与所有业务规则不能由数值精度保证。

- **表格数据关联：部分实现。** 证据：[data_workflows.py](../agent_service/data_workflows.py)、[codegen.py](../agent_service/codegen.py)。缺口：显式关联及部分唯一性检查已具备；跨来源复杂口径及放大检查需真实任务验收。

### 七、质量检查与人工复核

- **必填与完整性检查：部分实现。** 证据：[data_contracts.py:validate](../agent_service/data_contracts.py)、[data_contracts.py:evaluate](../agent_service/data_contracts.py)。缺口：按配置检查字段、空值及行数；规则未配置不等于完整。

- **金额对账：部分实现。** 证据：[data_contracts.py:evaluate](../agent_service/data_contracts.py)、[tests/test_data_contract_numeric_evidence.py](../tests/test_data_contract_numeric_evidence.py)。缺口：对配置的汇总目标使用精确十进制；单据、明细和合计的多源对账仍需业务配置与验证。

- **重复与异常检查：部分实现。** 证据：[data_contracts.py:evaluate](../agent_service/data_contracts.py)、[api.py:upload](../agent_service/api.py)。缺口：支持唯一性、范围和部分上传重复检测；文件级及跨来源业务重复判断不完整。

- **识别结果人工校正：部分实现。** 证据：[visual_fact_service.py:propose](../agent_service/visual_fact_service.py)、[visual_fact_service.py:review](../agent_service/visual_fact_service.py)、[data-workbench.js](../agent_service/static/data-workbench.js)。工作台支持原图或 PDF 页人工转录、复核和修订提案，图片或单页 PDF OCR 行可由用户选择追加并自行校正；系统不独立保证原文准确，也不把事实核对当成业务语义对齐。缺口：区域级纠错和大表格纠错尚需完善。

- **高风险结果阻断：部分实现。** 证据：[project_data.py:require_safe_numeric_lineage](../agent_service/project_data.py)、[releases.py](../agent_service/releases.py)、[applications.py](../agent_service/applications.py)、[tests/test_candidate_bulk_gate.py](../tests/test_candidate_bulk_gate.py)。此前测试覆盖了对未对齐事实候选发布、批量导出及汇总图表的部分阻断；已发现其他路径存在来源派生及 Agent 交接绕过，不能声称已全面阻断；工作台保留分页预览及警告。已发布历史按可核验身份保留查看与导出并显示风险。缺口：事实语义可信回执尚未实现，不能由一次来源勾选替代；修复前 r24 曾执行有限合成资料包体验证，不能证明新增绕过已修复，全仓回归与真实业务样本仍待完成。

### 八、过程说明与数据溯源

- **来源与版本说明：部分实现。** 证据：[business_explanation.py:explain_artifact](../agent_service/business_explanation.py)、[sources.py:revision](../agent_service/sources.py)、[applications.py](../agent_service/applications.py)。业务简报展示当次冻结来源，历史语义或证据不足显示待核对；缺口：任意报告和全部多模态结果尚未覆盖。

- **原文原图定位：部分实现。** 证据：[document_reader.py](../agent_service/document_reader.py)、[fact_service.py](../agent_service/fact_service.py)、[visual_fact_service.py](../agent_service/visual_fact_service.py)。缺口：文字偏移、Excel 坐标、图片或 PDF 页码有记录；PDF OCR 框仅绑定单页渲染图像像素，不能在原 PDF 精确高亮。图像单词或财务单元格端到端定位未完整实现。

- **数据处理过程说明：部分实现。** 证据：[business_explanation.py:explain_artifact](../agent_service/business_explanation.py)、[agent_service/static/workspace-shell.js](../agent_service/static/workspace-shell.js)。缺口：业务简报按冻结回执解释主要加工；OCR、图表及所有生成内容尚无统一过程说明。

- **检查结果说明：部分实现。** 证据：[business_explanation.py:_contract_checks](../agent_service/business_explanation.py)。缺口：可区分已检查、未检查和需复核；依赖可核验执行回执及实际配置。

- **复核与修订留痕：部分实现。** 证据：[visual_fact_service.py:review](../agent_service/visual_fact_service.py)、[fact_service.py:review_facts](../agent_service/fact_service.py)、[semantic_service.py](../agent_service/semantic_service.py)。缺口：可保存提案、摘要及审核人记录；多模态字段级变更对比及全面审批流程仍待验。

### 九、数据输出与专项验收

- **结构化结果导出：部分实现。** 证据：[result_delivery.py:export_xlsx](../agent_service/result_delivery.py)、[result_delivery.py:export_csv](../agent_service/result_delivery.py)、[project_data.py](../agent_service/project_data.py)、[releases.py](../agent_service/releases.py)。安全来源的结构化结果可导出；未语义对齐的文字或视觉事实候选不得批量导出，可核验已发布历史保留导出并提示风险。缺口：自动扫描表与图表结果尚无完整交付链。

- **依据清单导出：部分实现。** 证据：[result_delivery.py:write_handoff](../agent_service/result_delivery.py)、[business_explanation.py](../agent_service/business_explanation.py)。完整交接包保存运行上下文和部分来源依据；未语义对齐候选不得下载完整交接包。缺口：用户可读的多模态单独依据清单需核验。

- **脱敏样本与标准答案：部分实现。** 证据：[acceptance/finance_monthly/README.md](../acceptance/finance_monthly/README.md)、[tests/test_offline_ocr.py](../tests/test_offline_ocr.py)。缺口：已有合成财经样例和孤立 OCR 测试；真实授权脱敏跨模态样本及独立标准答案未完成。

- **识别准确性评估：未实现。** 证据：[tests/test_offline_ocr.py](../tests/test_offline_ocr.py)。缺口：个别合成图片验证不是字段级准确率评估；缺真实分层样本、错误率及验收阈值。

- **财经业务验收：未实现。** 证据：[acceptance/finance_monthly/README.md](../acceptance/finance_monthly/README.md)、[docs/finance-trust.md](../docs/finance-trust.md)。缺口：真实模型完整月结、跨模态真实业务样本和业务用户签收均无通过证据。

## 验收条件

- 每种格式需约定授权样本、页面或表格的复杂度、关键字段和独立标准答案；金额、正负号、单位及期间须分别统计错误，不能只使用总体平均识别率。
- 完整链路需要覆盖输入、来源版本、识别候选、人工复核、业务口径、处理结果、来源回看及导出，异常、漏读和模型不可用必须可见。
- 真实样本、可离线交付、运行容量、生产安全及业务用户签收均需独立证据；当前工程测试或合成图片不构成上述验收通过。
