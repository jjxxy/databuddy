# 工作空间成果目录与安全预览

此目录展示服务端实际存在的版本：工作区文本与代码、Office交付件、业务简报、完整加工结果、已发布/候选业务应用结果、受限Agent成果和文件来源。目录不会用预览行数代替完整行数。业务简报的Markdown、JSON、Word和PPT归为一个条目。

## 接口

所有接口使用现有业务用户鉴权；不会接受远程URL或磁盘路径。

- `GET /api/projects/{pid}/artifacts?kind=&offset=0&limit=40`
- `GET /api/projects/{pid}/artifacts/{id}/preview?offset=0&limit=50&text_limit=12000&sheet=`
- `GET /api/projects/{pid}/artifacts/{id}/download?format=`

`kind`可选`all/code/office/report/result/application/chart/agent/source`。目录提供真实筛选后`total`、`next_offset`和`page_is_complete_catalog`，不可把当前页当全目录。不可读取的实际资源明确列入`unavailable`，不生成假文件。ID绑定项目、资源、格式与版本摘要；旧代码条目的下载读取已冻结字节，不受后续编辑影响。项目进入回收站后读取返回410；来源、所属应用或成果归档同样拒绝继续读取。删除原对话不删除独立成果，已隐藏/删除/内部对话不会成为可打开的来源链接。

目录`sha256_scope`说明摘要对应原文件、完整结果清单、冻结报告清单还是Parquet。下载变体有独立实际文件摘要，通过`X-Artifact-SHA256`返回。每次打开/下载都重新核对完整字节；返回不可变缓存副本，拒绝hash不符的版本。目录只说明已有服务器回执，`integrity_verified=false`；读取成功后`preview.artifact.integrity_verified=true`。

字节完整性和业务含义分开显示。`semantic_status`可为`requires_review/declared_by_workflow/owner_confirmed_version/not_confirmed/not_applicable`；`metric_status=confirmed`仅来自绑定代码版本的真实指标确认记录，不信模型填写的确认字段。外部Agent完整结果强制待口径核对。`financial_verified=false`，不会产生财务审批结论。

## 预览级别

| 内容 | 返回值 | 真实边界 |
|---|---|---|
| 加工结果/Parquet | `render=table`, `columns/rows/schema/total/next_offset` | 真实固定版本全量数据按行分页；Decimal以原字符串返回，超JavaScript安全整数也转字符串 |
| CSV/JSON对象数组 | `render=table` | CSV第一行仅作为展示表头，扫描完整行数；JSON使用十进制解析；不运行公式或确认字段含义 |
| Excel | 原始网格、页签、格式、原始XML数值 | 多sheet可选，合并值仅保留锚点，不推断表头；数值优先保留OOXML十进制文本，公式显示原文不执行，不证明缓存新鲜 |
| Markdown/文本/代码/HTML | `render=markdown/text` | 字符分页，`unit=characters`，使用`text_limit`；HTML/JavaScript只显示文本，前端安全DOM渲染Markdown |
| Word/PPT | `render=document`, `blocks` | 正文和表格提取、按块分页，`layout_fidelity=false`；不承诺分页、字体、图形或版式还原 |
| 图表 | `chart.points`精确数值 | 从完整真实结果按明确的可加语义或冻结求和代码计算；不猜比例可加性，不把缺失补零，超100类明确标示展示范围 |
| PDF | 鉴权同源下载URL | 原始PDF供下载核对，未执行OCR，未核实图形数据 |
| 图片 | 鉴权同源原文件 | 校验支持的图片文件头，未执行OCR或认可图片中的事实 |
| 其他格式 | `render=download_only` | 明确只下载，没有虚假的在线预览 |

表格`unit=rows`、Office`unit=blocks`、文本`unit=characters`；前端必须跟随`next_offset`，不猜分页。工作簿的日期显示与原始数值/格式同时保留，源Excel在保存前已丢失的15位以上数值无法恢复。宏、脚本和生成HTML不执行。数值结果的完整Excel导出使用已有精度保护和行数控制；应用结果导出全量Parquet行，原生Decimal采用精确文本策略。

## 预算与运行成本

目录单页最多100项，但扫描的是实际完整目录，没有固定总数截断。当前通用元数据存储下，目录扫描成本随记录数增长；大量项目/历史运行场景需在部署容量评估中测目录延迟，并规划按项目建立资产索引。该实现不会宣称已通过任意企业规模负载测试。

文件验证/下载预算256MiB，工作区文本按既有256KiB单文件政策读取，文本预览预算4MiB；表格最多200列、行数受`MAX_SOURCE_ROWS`约束，Office提取最多2MiB正文/10000块。超限明确拒绝或转下载，不能把截断内容声称为全量。冻结版本和导出缓存占用额外磁盘，部署需设置容量、备份与保留策略；现有清理不得直接篡改仍被回执引用的字节。

测试：`tests/test_workspace_artifacts.py`覆盖鉴权、跨项目、真实全量分页、精确数值/图表/Excel、实际dbt发布结果、报告多格式、Office提取、原始XML精度、多sheet/合并/公式、篡改与回收站边界。

完整校验后的远端普通报告/Office成果通过`import_artifacts`成为真实本地任务成果，直接出现在项目目录；无需仅在协作详情中查找加密包。它们保留真实本地父任务、原字节SHA256及父交接清单，始终标记`needs_review/requires_review`。目录`all`优先展示报告、Office、完整结果与图表，再展示代码/spec和来源；每类内部按更新时间排列，类型筛选仍可读取全部原始文件。
