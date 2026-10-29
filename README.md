> **财务使用警示：r24 为修复前历史包，禁止用于财务生产。** 来源派生和 Agent 交接两处已复现绕过已修复，r25 测试包本体的 7 项定向回归通过；最终干净副本的文件一致性已校验，功能测试未在副本复跑。真实财经业务验收未完成，不能据此认定产品可用于财务生产。

> 当前源码支持图片与 PDF 指定单页 OCR 候选、财务来源复核和语义风险防护。r25 测试包本体以合成资料验证了空 PATH 本地处理链和扫描 PDF OCR；跨 Agent 接收的普通来源暂不能直接正式发布，Windows 长路径导入仍有失败案例，短路径仅是规避。真实财经识别准确率、完整财经月结和云端部署尚未验证。证据见 [r25 验证记录](verification/r25-finance-and-ocr-20261028.json)、[REPOSITORY-STATUS.md](REPOSITORY-STATUS.md) 和 [多模态工程核查](docs/multimodal-capability-audit.md)。

> **v0.20.0 更新**：本包延续 v0.19.0，新增独立工作空间网关/部署工具与委派任务撤权保护。先看 [本版交付与验收边界](RELEASE-v0.20.0.md)。原单实例启动方式仍保留；独立账号隔离需另按 [独立部署指南](deploy/isolated/README.md) 配置，不能把多个账号直接加入原共享空间当作隔离。

# Atlas Personal Agent

**云端工作模式执行底座**：新增独立 gVisor 执行服务，支持后台 Bash / Python / Node 任务、日志与文件检查点、恢复与取消，并将真实文件接入原有成果预览和下载。沿用现有模型目录、OpenCode、项目 Git 与 Skill / MCP。部署、范围和验收见 [WORK-MODE.md](WORK-MODE.md)。执行服务需单独部署；本次环境未运行真实 Docker / gVisor 或真实模型对标，不能据此认定已达到 ChatGPT 的全部能力或财经生产标准。

v0.19.0 部署包提供一个容器的快速启动：填写 `llm.json` 的接口地址、模型名和 API Key，Windows 双击 `start.cmd`，Linux/macOS 执行 `bash start.sh`。需要已启动的 Docker 与 Compose v2，不需要本机 Python 或独立数据库。操作见 [QUICKSTART.md](QUICKSTART.md)；它是单个 owner 工作空间部署，当前环境未实测 Docker 镜像构建与 Windows Docker Desktop。


v0.19.0 默认部署使用实际 OpenCode 2.1.2 会话和受控原生工具循环，后台统一提供多模型，前台只选择名称。旧版的无状态决策接口不是完整OpenCode Agent。本版新增双向 Agent 调用、只读专长 Agent 协同、共享预算及完整结果交接，并简化项目/对话导航，提供回收站和项目成果预览。使用说明见 [Agent 网关](AGENT-GATEWAY.md)、[协作接入](AGENT-COLLABORATION.md)、[内置团队](AGENT-TEAMS.md)、[项目与对话管理](WORKSPACE-LIFECYCLE.md)、[本版复核](docs/v19-review.md)。多附件、本地文件夹、个人/管理员 Skill 与 MCP、可编辑记忆及上下文压缩继续保留，见 [LOCAL-MODE.md](LOCAL-MODE.md)、[EXTENSIONS.md](EXTENSIONS.md)、[MEMORY-AND-CONTEXT.md](MEMORY-AND-CONTEXT.md)。完整业务场景和标准答案见 [财经月报验收包](acceptance/finance_monthly/README.md)。

日常使用：左侧选择项目及其对话，行内“⋯”可改名或移到回收站；右上角“成果”可查看真实文件、精确数据分页、报告正文并继续修改。其他工具集中在左下“工作设置与工具”。预览范围、版本与下载说明见 [WORKSPACE-ARTIFACTS.md](WORKSPACE-ARTIFACTS.md)。

v0.16 功能基础：继续建设个人财经业务工作空间。补齐文本金额/百分比及不可加指标的处理约束、来源字节完整性、导出崩溃恢复、过期租约写入保护和凭据错误脱敏；新增可核阅语义包与只读生产资格检查。用户描述目标，Agent 生成并执行可版本化逻辑。该历史版本的产品比较与实现见 [PRODUCTION-REVIEW-v0.16.md](PRODUCTION-REVIEW-v0.16.md)，定位及 23 项覆盖见 [BUSINESS-WORKSPACE.md](BUSINESS-WORKSPACE.md)。完整包包含源码、依赖锁、部署脚本与测试；本次未部署线上实例。

独立验证财经链路可运行 `python scripts/verify_business_workspace.py`：在临时空间启动真实 API 与 Worker，检查完整数值、Excel/Word/PPT、失败保留正式结果和重启复用。使用合成数据及明确方案，不代表真实模型或企业底稿验收。该历史版本记录见 `VALIDATION-v0.16.json`。本版最终检查见 `VALIDATION-v0.19.0.json`。

当前一个部署对应一个共同拥有的工作空间，所有 owner 能访问全部项目；它尚不是隔离个人账户的多租户服务。生产资格检查 `python -m agent_service.production_release --check` 只读取验收证据，缺失真实验收时返回 blocked，不影响开发启动；格式见 [PRODUCTION-RELEASE.md](PRODUCTION-RELEASE.md)。旧来源缺少原件摘要时需要重新上传建立修订，不能把盘上现有字节自动追认为原件。

v0.14 合入数据契约、精确对账、分页资产目录、版本冻结的字段血缘和正式指标登记。历史说明见 [DATA-GOVERNANCE.md](DATA-GOVERNANCE.md)。

v0.13 增加通用工作Agent、可编辑Office交付、MCP只读工具与一次性外部写入确认、OIDC验签、共享状态/失效写入拦截、PostgreSQL迁移及Restic异卷备份。企业部署、实际边界与验收见 [完整说明](ENTERPRISE-WORK.md)。源码具备HA部署机制不代表当前试用地址已完成HA集群验收。

## 已有能力与版本历史

0.10增强 Agent 执行底座：原生工具协议验证、按需发现工具、完整结果按页回读、文件内容搜索与版本校验精确修改、重复操作检测和失败完成拦截。提供18个真实模型验收案例、重复运行及同任务比较。0.10阶段未接真实模型；本次接入与真实验证见 OPENCODE.md 和 VERIFICATION.md，仍不能声称商业智能对标通过。完整独立复审与用法见 [AGENT-COMPARISON.md](AGENT-COMPARISON.md)。

0.9接入本公司内部检索 API：对话搜索与读取原文、权限/版本重新核对、精确引用、模型内部资料授权，以及随代码和运行结果冻结的依据清单。日常仍通过聊天/工作模式使用。真实公司接口尚待提供协议并联调；完整接入、隐私及验证边界见 [COMPANY-SEARCH.md](COMPANY-SEARCH.md)。

0.8补齐 Agent 工作可靠性：服务端任务契约与完成证据、否定指令与应用范围校验、条件问答防误答、统一模型预算/用量、可追溯业务记忆、工作流程和计划；云端对话编排与数据执行分开。新增真实模型评测入口及默认关闭的独立隔离执行接口。实现、验证边界和使用说明见 [AGENT-RELIABILITY.md](AGENT-RELIABILITY.md)。

0.7继续收敛对话工作：会话各自记住应用、目标歧义在对话澄清；停止不影响其他会话；模型临时故障有限重试，暂停后说“继续”承接已有步骤；发布前显示新旧差异与来源/时效；支持每日、工作日、每周和常见整点间隔计划。对话草稿及数据版本下载也已补齐。完整复审及明确边界见 [PRODUCT-REVIEW.md](PRODUCT-REVIEW.md)。

0.6将“数据应用”作为持续使用的成果：描述目标 → 采集与加工 → 生成数据、页面和代码 → 预览确认 → 重跑、对话调整、定时更新和数据问答。默认交付表格/汇总型应用，包含搜索、排序、分页、汇总图、结果与代码导出。应用界面、处理规则、语义和数据版本一致；调整中的草稿不覆盖正式应用。完整生命周期及范围见 [DATA-APPLICATIONS.md](DATA-APPLICATIONS.md)。

0.5新增聊天/工作模式、真实模型连接、多步 Agent、项目多会话共享上下文和统一 Git 版本/回退。完整用法、恢复边界与验证见 [AGENT-WORKSPACE.md](AGENT-WORKSPACE.md)。

前台以项目和对话为中心，用户只需打开项目并与 Agent 对话。上传资料、说明分析口径、查看结果、确认发布及安排更新都在对话中完成。用法、实现及边界见 [CONVERSATION.md](CONVERSATION.md)。

一个面向单个用户长期使用的数据应用：接入 → 理解 → 生成版本化处理代码 → 验证 → 预览发布 → 业务对象及持续分析。企业级质量建设及复审结果见 PRODUCT-REVIEW.md；实际验证边界见 VERIFICATION.md。

## 云端试用部署

已提供 `Dockerfile.cloud`、`railway.toml` 和 `agent_service.cloud`。单个持久化实例运行 API、Agent 编排和数据 Worker。历史试用部署记录见 RAILWAY-DEPLOY.md；本次没有更新或核验线上地址、账号资源和模型配置，不能把历史试用实例当成本版已部署完成。

## 快速启动

**v0.19.0 交付包 `Atlas-DataBuddy-v0.19.0-deploy.zip` 用于单个人或共同授权团队的独立实例，不是集中多租户服务或 HA 集群。共同授权的成员共享实例内的项目与数据。**

默认只需 Docker，无需安装本机 Python。Windows 安装并启动 Docker Desktop（Linux 容器模式）；Linux/macOS 使用 Docker Engine 或 Docker Desktop，并确认 Compose v2 可用。

部署 ZIP 已预置不含真实密钥的 `llm.json` 安全模板。解压后直接填写其中的 `base_url`、`model`、`api_key`，保留其余默认项，无需复制或改名。

填好后，Windows 双击 `start.cmd`，或执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

Linux/macOS：

```bash
bash start.sh
```

源码目录若缺少 `llm.json`，首次启动会自动生成模板并提示停止，填写后再运行。脚本使用 `compose.quickstart.yaml` 启动服务，就绪后在本机终端显示地址、访问口令并打开浏览器，默认地址为 [http://localhost:8000](http://localhost:8000)。

预置模型模板显式允许任务内容发送给所选模型服务；具体范围见下方“模型配置与隐私”。首次下载与构建需要联网，模型调用可能收费。保留 `.env`、`llm.json` 和数据卷，不要分享配置或密钥。

示例流程：新建项目，把 `examples/orders-with-refunds.csv` 放进对话，说“按客户汇总，仅统计已付款订单，金额扣除退款”，查看预览后回复“确认发布”。然后说“有多少条记录”“导出结果”或“每天上午9点更新”。

停止、日志、端口冲突、升级及高级／旧多服务部署见 [QUICKSTART.md](QUICKSTART.md)。快速启动默认不提供 Dagster 管理端口；本次未在真实 Docker Desktop 环境完成验收。

升级需保留配置和数据卷，沿用原部署目录及 Compose 项目；解压到任意新路径启动不等于数据迁移。旧 `compose.yaml` 多容器数据不会自动迁入 quickstart 数据卷。

## 常用功能

- 文件：CSV（UTF-8/GB18030）、多页签 XLSX、JSON/JSONL、静态 HTML、文本及文档文字层。图片与 PDF 指定单页可用本地 OCR 获取待核对文字候选，也可选用 Docling 扩展；识别结果仍需人工对照原件及业务语义核对，不能视为自动核实。
- 来源：PostgreSQL/MySQL 只读表；REST API 对象数组、记录路径、页码/offset/cursor 分页、来源总数校验、请求头和额外参数。配置加密保存，保留来源版本和历史原件；支持替换文件、归档及恢复。
- 画像：最多 500 条样本的字段、缺失、重复、数值和候选主键；模式变化提示。
- 任务：模型/规则建议，实际 Git 仓库和 commit；同一任务调整产生新 commit，已发布结果可查看对应历史代码。
- 处理：真实 dlt 加载与 dbt build/test；多对一左关联、文本/数值/日期筛选、字段加减乘除、分组金额汇总、空白整理、必填/非负/最少行数检查。禁止关联右键重复导致金额放大。
- 发布：候选、正式结果与正式代码分开；候选预览、人工发布、正式版本调度及历史回滚。
- 运行：事务队列、周期唯一键、活动目标去重、有界重试、租约、心跳、取消和运行质量报告；任务版本记录总时长、最大尝试次数和采集时效要求。
- 业务：中文字段、对象列表、搜索排序、备注标签、来源/质量、CSV 导出、金额汇总与保存分析。
- 分析：受约束的自然语言 SQL（需模型配置）、手工只读 SQL、保存分析复用，标注结果与代码版本。
- 维护：健康页、HMAC 链审计、备份/空空间恢复、保留期清理；锁定依赖和 CI 配置。

## 业务口径与发布

指标分“候选”和“已确认”，确认时填写计算说明及单位/币种。说明是业务解释；实际执行逻辑由对话规划生成受约束的筛选与计算。例如状态等于 paid、订单日期不早于 2026-10-01、净金额 = 金额 - 退款，选择净金额作为汇总字段。模型可根据明确目标建议规则，用户在对话内查看方案并提出修改；缺失的退款、税费和成本仍不能靠文字补出来。结果记录实际数据版本；代码和 SQL 可经鉴权 API 导出审阅。

可上传 `examples/orders-with-refunds.csv` 验证规则：筛选状态等于“已付款”、订单日期不早于 2026-10-01；计算“金额 - 退款”，按客户汇总该计算字段。候选应有 2 条对象、合计 118.1500；原始采集仍保留 4 条。

多来源关联要求右侧键唯一。可以要求所有记录必须匹配，或者允许未匹配并从质量报告 API 读取数量。关联设置当前属于底层 API 能力。当前只支持主来源与最多四个来源的多对一左关联。

设置定时后，执行的是正式发布版本。调整中的候选代码不会影响现有调度。回滚也切换未来调度的代码。错过多个周期只合并补跑当前一次；同目标已有活动任务时不继续堆积。

## 完整性、空数据与运行预算

分页支持 none、page、offset、cursor。记录路径和下一游标/总数路径使用点分 JSON 路径，例如 `data.records`、`paging.next_cursor`、`paging.total`。cursor 允许空的中间页，只要仍有下一游标就继续。正式采集发现重复页、循环游标、总数变化或到达页数上限但未结束会失败；探查仅为样本，不代表完整采集成功。

默认空来源失败并保留正式结果。业务允许零记录时勾选“允许空来源”并设最少行数为 0；使用已有来源结构，空快照也反映来源删除。初次接入仍需要可探查的数据和字段。

运行预算保存到 Git：总时长默认 240 秒（10～3600）、最大尝试次数默认 3（1～5）、采集时效默认 1440 分钟。部署级 MAX_RUN_TIMEOUT_SECONDS 可进一步降低运行上限。多阶段共享一个执行截止时间；暂时故障有界重试，质量和模式错误直接失败。时效根据正式结果中最早来源的采集时间计算，回滚不会刷新这一时间；它不能证明上游业务数据本身已更新。

筛选目前为 AND 组合，集合条件可用 in/not_in；计算按顺序引用已有数值字段，支持加减乘除。数值运算使用十进制输入，除法按 DuckDB 的数值语义执行；新生成版本金额汇总统一采用 DECIMAL(38,12)，历史快照保持原精度。除零或空分母阻止发布。此能力不替代完整财务账务、汇率换算或复杂指标语言。

## 模型配置与隐私

默认快速启动使用部署包已预置的 `llm.json` 安全模板，由管理员填写模型目录中的连接三项，用户只选择显示名称：

```json
{
  "default_model_id": "finance",
  "models": [
    {
      "id": "finance",
      "name": "业务分析",
      "base_url": "https://your-llm.example.com/v1",
      "model": "YOUR_MODEL",
      "api_key": "YOUR_API_KEY",
      "share_data": true
    }
  ]
}
```

三个占位值必须替换为实际接口地址、模型标识和密钥。模型地址支持 HTTP/HTTPS、云服务或本地服务；粘贴完整地址时会自动移除末尾的 `/chat/completions`。Docker 中会将模型地址 `localhost` 改写为宿主机地址，但不会改变服务监听或防火墙设置；模型服务仍须能从容器访问，Linux 上仅监听宿主机 `127.0.0.1` 的服务可能仍不可达。`api_key` 必须非空，本地模型若不要求密钥可填写 `unused`。首次填好后运行启动脚本；操作步骤见 [QUICKSTART.md](QUICKSTART.md)。

模板显式设置 `share_data: true`：任务中读取的业务内容、行值和文档可以按需要发送给所选模型服务。删除或省略该项时，加载器默认采用 `false`；由旧 `.env` 迁移生成的模型配置也采用 `false`。`false` 会限制读取业务内容的模型工具，但对话文本和必要上下文仍会发送，不能视为完全离线。不要分享 `llm.json`、`.env` 或密钥。

v0.19.0 默认使用管理员维护的 `models` 目录。改 `llm.json` 后停止并重新启动，新目录全部通过验证才生效；任务固定模型版本。业务用户不配置接口、密钥、公司检索或引擎。旧单模型JSON仍保留首次初始化行为；迁移及独立后台口令见 [QUICKSTART.md](QUICKSTART.md)。

高级／旧 `compose.yaml` 与企业 Kubernetes 清单显式保留 Atlas 工具循环兼容路径（`ATLAS_OPENCODE_RUNTIME=0`），未集成 OpenCode 原生多副本服务。需要本版 OpenCode Agent 请使用默认 quickstart。旧 Compose 仍可在 `.env` 中配置以下三项，再运行 `docker compose --env-file .env -f compose.yaml up -d`：

```dotenv
LLM_BASE_URL=https://你的兼容接口/v1
LLM_API_KEY=你的密钥
LLM_MODEL=你的模型名
```

快速启动要求先填好模型连接；旧部署未配置模型时使用内置对话模式，超出其能力会说明限制。模型生成的处理方案和 SQL 仍需通过服务端校验及相应核对流程；模型连接可用并不代表真实业务分析已经验收。

## 采集范围

默认上传最多 25 MB，单次最多 100000 行，整次尝试总时长默认 240 秒，暂时故障默认最多 3 次尝试。可按实际资源设置 MAX_UPLOAD_MB、MAX_SOURCE_ROWS 和部署级 MAX_RUN_TIMEOUT_SECONDS；任务级运行预算可通过 API 配置。

页码/offset 分页以空数组或已达到声明总数结束；cursor 以无下一游标结束，空中间页不会提前结束。分页上限未结束、循环或总数不一致拒绝正式采集。支持常见 cursor 协议，未实现通用 OAuth、增量水位和 CDC。网络地址先验证再固定 IP，保留原始 Host/TLS SNI，不跟随重定向。私网 API 必须在 API_ALLOWED_HOSTS 中明确允许；禁止云元数据地址。

数据库字符串：`postgresql+psycopg2://readonly:password@host:5432/db` 或 `mysql+pymysql://readonly:password@host:3306/db`。密码特殊字符需 URL 编码，源账号必须只读。服务启用只读事务，实际网络和权限仍需部署时验证。

PDF 基础解析提取文字层与页码；扫描 PDF 的 OCR 读取用户指定的一页，绑定原件版本、摘要和页码。识别框仅对应该次渲染图像像素，不能直接用于 PDF 原生坐标高亮；表单与注释可能未渲染，必须人工对照原件。新包已用合成资料进行有限验证，真实财务识别准确率和完整生产验收未完成。

## 多模态扩展

```bash
docker compose -f compose.yaml -f compose.multimodal.yaml up -d --build
```

该扩展需更多内存、磁盘及模型下载。本次未运行 Docling 扩展，不将其写成已验证。扫描 PDF OCR 的渲染在有时限和输出限制的独立进程内完成；这不是操作系统安全沙箱。旧的人工视觉核对路径仍在服务进程检查 PDF 结构，不应将其视为已隔离。

## 安全与部署

默认端口绑定本机，使用高熵 APP_TOKEN。服务包含 CSP、禁止 API 缓存、请求 ID、请求预算和加密配置；Worker 不挂 Docker socket，使用受约束的生成模板。

Linux 下采集/dbt 子进程会在 Worker 消失时终止。一个工作空间仅运行一个 Worker，数据库租约保护恢复和发布。当前基础部署采用共同 owner 权限，完整多租户、团队 RBAC 与 Cube 服务未实现。可选 OIDC、受控外部写入和独立脚本执行需按各自指南配置与验收。

部署到服务器可先用 SSH 隧道：`ssh -L 8000:127.0.0.1:8000 user@server`。正式公网服务需 HTTPS 反向代理、目标网络权限、容量测试与镜像验证。不要直接公开 Dagster 管理端口。底层镜像当前为版本标签；在实际生产发布中由你的 CI/镜像仓锁定批准的 digest。

任务代码位于数据卷 `/data/repositories`；默认本地 Git，未自动推送到 GitHub/GitLab。原件、加密配置、运行产物均与代码分开。可导出任务代码 ZIP。原始技术日志留在数据卷，分享前检查是否含来源 URL。

## 升级旧版

先备份原数据卷和 PostgreSQL，再替换应用源码，执行 `docker compose up -d --build`，保留原 `.env` 和卷。新版本新增 jobs 和审计表，旧 JSON 运行记录幂等迁移；旧已发布结果继续可读，旧任务沿用自动发布行为。新建任务默认预览发布。

迁移不会为历史运行编造来源快照。旧排队任务在恢复时读取当时可用来源配置，并记录此次实际输入。请先在复制的数据环境执行升级验收。

## 备份、恢复与清理

维护工具以共享读写锁阻止应用写操作及运行产物更新，备份期间新写请求会返回维护提示。备份包含原件、Git、产物、元数据、审计与加密 key；不包含可重建的 Dagster 运行历史。

容器内备份到 `/tmp` 后复制出来：

```bash
docker compose exec api python -m agent_service.maintenance backup /tmp/workspace.atlas
docker compose cp api:/tmp/workspace.atlas ./workspace.atlas
```

备份整体未加密，包含密钥，需安全保存；文件默认仅所有者可读写。完整性 hash 用于检查损坏，不等于可信来源认证。

恢复只允许空工作空间。停止服务，使用新的数据库和应用数据卷，将备份复制到容器可见路径，然后执行 `python -m agent_service.maintenance restore /path/workspace.atlas`。恢复先在临时目录校验条目、大小、路径及审计链，再复制产物并事务写入元数据；校验失败不写入目标空间。支持最多 4 GiB 解压体积，部署者可用 MAX_BACKUP_UNCOMPRESSED_MB 调整。不得在已有数据的空间强行覆盖。

清理默认只列出候选，不删除：

```bash
docker compose exec api python -m agent_service.maintenance cleanup --days 30 --keep-success 20
```

确认保留策略后增加 `--apply`。正式、候选、活动任务与近期成功/验证结果受保护；清理产物后历史记录和审计保留，已清理的版本不能回滚。原件及 Git 不自动删除。

停止服务用 `docker compose stop`；不要 `docker compose down -v` 删除数据卷。

## 开发与 CI

Python 3.12/3.13、Git：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . --no-deps
.venv/bin/pip install pytest
python3 bootstrap.py
bash run-local.sh
```

默认快速启动使用 SQLite 元数据和内置周期扫描；高级旧版 compose.yaml 使用 PostgreSQL + Dagster。运行 `python -m pytest tests -q` 与 `node --check agent_service/static/app.js`。独立服务验收运行 `python scripts/verify_service.py`：使用临时空间启动真实 Uvicorn 和 Worker，验证接入、规则、Git/dlt/dbt、预览发布、心跳和审计，不修改部署数据。

`.github/workflows/verify.yml` 提供 SQLite/PostgreSQL 测试及 Docker 构建检查，但当前没有在外部 Git 仓库运行。已测、未测及限制以 VERIFICATION.md 为准。
