# 财经报告业务旅程：当前能力与缺口

本清单依据代码能力梳理，**不是生产验收结论**。部分功能需要模型、OIDC 或其他服务配置；能调用工具或导出文件不代表真实数据、财务口径或页面效果已经验收。状态含义：**部分**为已有能力但与需求不完全等同；**待建设**为当前未发现对应完整工作闭环。

| 业务环节 | 用户诉求 | 当前状态与边界 | 主要依据 |
| --- | --- | --- | --- |
| 数据源接入 | 多文件、数据库、API、网页获取数据 | 部分：支持多种文件、API 和数据库读取；有网页采集与浏览器工作能力。并非所有企业系统、认证方式和规模都经过验证。 | `agent_service/connectors.py`、`agent_service/project_reports.py`、`agent_service/browser_reports.py` |
| 数据加工 | 清洗、转换、拼接、过滤 | 部分：数据工作流支持过滤、列处理、归一化、聚合、关联、SQL 和联合；具体规则与数据类型受工作流校验约束。 | `agent_service/data_workflows.py` |
| 数据加工 | 维度和枚举映射 | 部分：支持重新分类、查找和生效期映射，拒绝未映射或不唯一的情形；自动语义匹配及组织级业务规则仍需实际场景验收。 | `agent_service/data_workflows.py`、`agent_service/data_api.py` |
| 数据加工 | 指标计算、指标口径定义 | 部分：支持数值计算、聚合、单位/期间/可加性校验和指标声明、确认；模型生成的口径不会自动等于业务确认。 | `agent_service/data_workflows.py`、`agent_service/metric_registry.py` |
| 数据加工 | 加工可追溯 | 部分：保留方案、输入证据、版本及结果信息；外部交接资料仍需受控核对，不能据此证明远端来源真实。 | `agent_service/project_data.py`、`agent_service/business_explanation.py`、`agent_service/external_alignment.py` |
| 数据加工 | 数据变化后的增量更新 | 待建设：可重跑或计划更新，但当前数据应用描述为全量快照，不能将定时重跑等同于增量计算或 CDC。 | `agent_service/codegen.py`、`agent_service/execution.py` |
| 数据分析 | 数据问答 | 部分：对已发布数据可执行受约束的只读 SQL 或规则问答；限制返回量、时间和类型，复杂问题需模型及真实资料。 | `agent_service/analysis.py` |
| 数据分析 | 透视、下钻、筛选和切月 | 部分：工作流和报告方案支持筛选及期间字段；未发现完整的交互式透视、跨层下钻及切月分析工作台。 | `agent_service/data_workflows.py`、`agent_service/business_reports.py` |
| 数据分析 | GAP、归因和双因素分析 | 部分：报告支持基于指定字段的差异和比率计算；缺少完整的可配置归因及双因素分解工作台，不能由数值差异自动推断因果。 | `agent_service/business_reports.py` |
| 数据分析 | 分析结论提炼、解释和验证 | 部分：报告可记录完整结果、公式、筛选与假设；业务原因不能自动视为事实，仍需真实依据与复核。 | `agent_service/business_reports.py`、`agent_service/business_explanation.py` |
| 模板 | 上传、检查、选用 PPT 模板 | 部分：可检查项目中固定版本的 PPTX，占位符、文本框及组合内文本可按明确位置填充，并检查整个替换后文本；不承诺复杂图表和版式保真。 | `agent_service/ppt_template.py` |
| 模板 | 组织级和个人级模板管理、切换、调试 | 待建设：现有项目文件与 PPTX 填充不等于完整的组织/个人模板库、授权、检索、切换和调试闭环。 | `agent_service/ppt_template.py` |
| 模板 | 从 PPT、截图、设计稿提取并复用风格 | 待建设：有限 PPTX 文本检查不等于模板设计结构、截图和设计稿的准确提取及风格重建。 | `agent_service/ppt_template.py` |
| 报告生成 | 报告、HTML、Word、Excel、PPT | 部分：已有报告和 Office 文档生成及成果查看入口；复杂内容的布局、数据准确性及企业级视觉质量仍需真实材料验收。 | `agent_service/business_reports.py`、`agent_service/office.py`、`agent_service/office_presentation.py`、`agent_service/workspace_artifacts.py` |
| 报告生成 | PPT 逐页生成、逐页展示与进度 | 部分：交互式 Agent 的标准 PPT 生成会在实际构建每页后展示文字和表格内容；最终文件通过结构检查后另行交付。这不是逐页图片渲染或可视化编辑；模板填充、计划任务和缓存命中不提供同等逐页回放，业务和视觉准确性仍需核验。 | `agent_service/office_presentation.py`、`agent_service/office_slide_progress.py`、`agent_service/office.py` |
| 报告生成 | 数据与结论匹配图表、数据和结论核对 | 部分：可生成图表及依据完整结果计算的简报，保留结果和公式；验证内容结构或计算不等于自动验证业务叙述和整页视觉效果。 | `agent_service/office.py`、`agent_service/business_reports.py` |
| 报告生成 | 补充数据生成、数据替换和重新计算 | 部分：支持变更来源/规则后生成新工作流或报告结果；复杂报告局部刷新、自动增量更新及无副作用复用尚需完善。 | `agent_service/project_data.py`、`agent_service/business_reports.py` |
| 编辑调整 | 结构、内容、结论的灵活调整 | 部分：可修改已保存的规格和工作文件并重生成；并非任意上传文件的完整保真编辑。 | `agent_service/office.py`、`agent_service/agent.py` |
| 编辑调整 | 局部选中编辑、对话编辑、可视化编辑器 | 待建设：现有文件/规格调整和有限 PPT 文本替换不构成通用所见即所得、按页面元素选择的报告编辑器。 | `agent_service/office.py`、`agent_service/ppt_template.py` |
| 编辑调整 | 报告预览、多端预览 | 部分：存在多种文件预览；Office 内容提取预览和只读 Office 集成不能保证与所有原件、字体和终端一致。 | `agent_service/workspace_artifacts.py`、`agent_service/onlyoffice_readonly.py` |
| 编辑调整 | 历史版本、对比、回退及零污染 | 部分：项目工作文件有版本、回退和固定版本的文本差异对比；二进制及大文件没有逐页视觉差异。结果记录固定摘要；“零污染”还依赖业务输入、隔离、安全回退和部署配置的真实验证。 | `agent_service/workspace.py`、`agent_service/project_data.py` |
| 编辑调整 | 多人协作编辑 | 待建设：只读 Office 预览不等于多人同时编辑、冲突解决或协作留痕。 | `agent_service/onlyoffice_readonly.py` |
| 发布分享 | 发布及版本级、文件级控制 | 部分：数据结果有候选和采用流程；不能等同于覆盖所有报告、文件和页面的统一发布中心。 | `agent_service/data_api.py`、`agent_service/project_data.py` |
| 发布分享 | 权限配置、链接分享、有效期、撤销 | 部分：已有针对当前已采用数据结果的 OIDC 指定主体只读分享，可设置期限、下载权限并撤销；结果版本替换后旧链接失效，访问中的撤销/回退并发仍需加强。不等同于通用 PPT/HTML 分享和完整组织权限管理。 | `agent_service/data_sharing.py`、`agent_service/data_api.py` |
| 发布分享 | 消息通知与分享通知 | 部分：已有工作完成提醒和动态；不代表对指定收件人自动发送分享通知。 | `agent_service/static/index.html`、`agent_service/data_sharing.py` |
| 发布物查看 | 分享链接查看、列表、下载 | 部分：已有数据结果分享和下载；完整的统一报告发布物列表、各类成果的访客访问体验需继续建设与验证。 | `agent_service/data_api.py`、`agent_service/workspace_artifacts.py` |
| 发布物查看 | 评论、批注、反馈与二次编辑 | 待建设：未发现覆盖已发布成果的完整评论、批注、反馈及受控二次编辑闭环。 | `agent_service/data_sharing.py`、`agent_service/onlyoffice_readonly.py` |
| 发布物查看 | 术语解析、指标解读留痕 | 部分：存在指标登记、确认、定义及报告依据；没有因此证明跨报告的完整术语解析与阅读交互留痕。 | `agent_service/metric_registry.py`、`agent_service/business_explanation.py` |

## 生产验证边界

- 容量限制存在于读取、计算、报告和模板处理环节；应以部署配置、实际数据规模、执行耗时、并发和恢复演练评估，不能仅凭小样例宣称满足超大型企业规模。
- 报告数字需要固定来源版本、完整计算结果、口径与审批记录共同验证。资料能回读、图表能渲染或模型能给出结论，均不单独构成财务验收。
- 共享访问、身份隔离、审计、备份恢复和高可用需要真实企业身份、存储、网络与故障注入验收。现有 owner workspace 中多个授权主体共享工作空间，不能据此声称具备完整租户隔离。
- PPT 或报告的内容正确、视觉质量、兼容性与使用体验应在目标 Office 版本和终端上分别验收。
