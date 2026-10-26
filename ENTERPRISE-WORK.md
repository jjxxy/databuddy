# Atlas v0.13：企业部署与对话式工作助手

v0.17 兼容说明：本文件及 Kubernetes 生成器描述旧的分角色企业拓扑，生成清单显式设置 `ATLAS_OPENCODE_RUNTIME=0`，保留 Atlas 工具循环。镜像包含 OpenCode 不代表该拓扑已集成其原生多副本运行服务。当前 OpenCode 原生业务 Agent 使用 `compose.quickstart.yaml` 的 cloud supervisor；集群原生会话归属和故障接管仍需另外实现和验收。

本版继续以“项目 + 聊天/工作模式”为主入口。用户描述目标，Agent 选工具、执行、检查并交付；接入权限和基础设施由所有者/管理员配置。后台管理能力不会要求普通用户先理解数据管道。

## 独立判断与本次范围

“解决所有工作问题”不是可验收目标。即使成熟商业 Agent 仍会出错，也依赖模型、应用连接、权限、资料质量、执行环境与人的判断。“vibe work”在这里指对话驱动的完整工作闭环，不把它当作某个未经定义的产品指标。

本版把偏数据的工具框架扩展为多种成果：调查与分析、结构化报告、可下载的 Word/Excel/PPT、数据应用，以及已连接工作系统的查询与受控写入。模型换新不等于能力自动齐全，MCP 接口也不会自动带来公司账号或业务系统权限。没有真实连接时，必须说明缺少能力，不能假装已经查过邮件、提交过工单或完成业务。

现有 Railway 地址的实际状态与完整企业集群必须区分。单独 PostgreSQL、备份和恢复机制能改善可靠性，但不代表 API、模型、文件系统或数据库主库都已经高可用。`/api/platform` 会报告配置缺口，`high_availability_verified` 不会因为设置了环境变量就变成 true。

## 已实现的架构变更

| 方面 | 本版实现 | 不能据此宣称 |
| --- | --- | --- |
| 元数据 | 支持原 SQLite 到 PostgreSQL 的单向、原子迁移，先复制原库并检查行数/审计链，保留原文件和密钥 | 单个 PostgreSQL 就是 HA |
| API | 独立进程，可在共享状态条件成立后运行多个副本；`/health` 与 `/ready` 分开 | `/health` 返回 200 就代表任务一定成功 |
| Agent | 去掉跨所有项目的全局执行锁；不同项目可并行，同项目严格 FIFO；故障状态仍在持有项目锁时写入 | 同一项目的冲突修改可以任意并行 |
| 互斥与失效处理 | 共享文件锁加 PostgreSQL session advisory lock；连接丢失后拒绝旧进程写入，进程退出交由编排器接管 | 网络分区后仍可无条件继续写 |
| 执行器 | 保留事务队列、去重键、租约、心跳和结果发布检查；API、Agent、Worker 可独立部署 | 副作用绝对恰好执行一次 |
| 存储 | HA 模式要求共享 RWX、固定存储身份、相同密钥与数据库中的密钥证明；错误配置拒绝启动 | 两个本地目录各放一份相同文件就是共享存储 |
| 健康 | 按角色和节点记录心跳，展示实际健康副本，避免最后一个节点覆盖整个服务状态 | 心跳正常就证明模型回答正确 |
| 身份 | 可选 OIDC：固定签发者/JWKS/受众/主体允许列表，验证签名和有效期；OIDC 模式不接受工作空间口令降级登录 | 已接入某公司的 SSO 或已经实现多租户隔离 |
| 成本 | 每轮步骤/时间/输出预算，跨节点原子预留每日模型请求预算，全工作空间排队上限 | 预算 token 数等于上游准确账单 |
| 备份 | 先取得一致性应用快照，再由 Restic 加密、去重并存入 S3，恢复到临时目录做完整文件哈希回读检查 | 同账号异卷就等于跨地域不可变灾备 |

PostgreSQL 会话锁要求连接直达 PostgreSQL 或使用 session pooling，不能使用 PgBouncer transaction pooling。持久文件系统还必须正确支持跨节点 POSIX 锁；共享存储本身的容错能力需要由存储服务提供。

## Agent 的工作闭环

1. 从用户目标明确交付物与完成标准；复杂任务记录计划，并优先执行已有信息能够确定的部分。
2. 检索项目资料、历史、业务约定和已批准的工作系统。摘要不是完整证据，需要时回读原始结果。
3. 通过实际工具执行，不把计划、代码或工具目录当作已经完成的工作。
4. 核对真实文件、结构、哈希、执行回执或数据质量；发现未完成项则修正、暂停或说明阻碍。
5. 在对话中交付文件、应用或业务结果，保留版本与可继续入口。外部写入需要一次性确认。

深度工作模式由 `ATLAS_AGENT_EFFORT=deep` 控制，单轮上限提高到 40 次模型请求、128,000 个预留输出 token、约 10 分钟累计模型时间；仍受每日总预算限制。它增加可处理的步骤，不保证更高正确率。OpenCode 决策接口未提供完整上游用量，因此这些限制不是严格的金额上限，费用应与供应商账单核对。

### 可下载办公成果

`render_office_artifact` 从项目 JSON 规格生成真正可编辑的 `.docx`、`.xlsx`、`.pptx`。规格与交付清单进入项目 Git，二进制产物单独保存，以哈希校验后下载。重复渲染同一规格会复用产物。

这是结构化文档、表格和文字演示文稿能力：不承诺商业产品同等的视觉设计，不自动修改复杂原有 Office 模板；不声称做过视觉验收或 Excel 公式重算。导入字符串保持文本，避免把用户数据当成 Excel 可执行公式。任意生成代码仍需独立隔离执行器，不能因此在应用主机任意运行。

### 工作系统与 MCP

工作系统连接由管理员通过后台 API 提供 HTTPS Streamable HTTP 地址、凭证和明确的只读/写入工具列表，业务页面不提供模型密钥或连接配置入口。配置列表与新建使用 `/api/projects/{project_id}/work-connectors` 的 GET/POST，修改或停用使用 `/api/projects/{project_id}/work-connectors/{connector_id}` 的 PUT/DELETE；这些请求除有效的业务身份外，都需要独立请求头 `X-Atlas-Operator-Token`，与服务端至少 16 字符的 `ATLAS_OPERATOR_TOKEN` 匹配。未设置管理员口令时管理 API 禁用，业务访问口令不授予管理权限。

支持协商的 MCP 2025-06-18 / 2025-03-26、JSON/SSE 响应、会话编号、工具目录与 JSON Schema 校验；不支持本地 stdio、模型驱动的采样/凭证索取或任意工具自动安装。

允许模型处理系统资料需要 `storage_approved: true`、`model_approved: true`，并在后台模型配置启用 `share_data`。可使用 `approved_model_ids` 明确批准目录中的多个模型，配置时冻结各模型连接/运行时指纹及目录修订号；未列出的模型、新增模型或已变更目的接口的模型不能继承批准。省略列表保留单模型批准，只对应配置时的当前/默认模型。目录中无关模型的修改不会撤销未变更连接的批准。

同一项目包含多个工作系统的资料时，完整历史使用各系统许可的交集：一个模型必须同时被这些系统批准，才能接收项目内容。连接第二个系统不会覆盖第一个系统对既有资料的限制。停用连接保留资料的隐私约束；需要重新授权时，由管理员逐项核对，而不是通过换模型或重新连接另一个系统解锁。外部动作的后台执行沿用原任务冻结的模型身份进行权限检查。

- 只读能力来自管理员允许列表，不信任工具自己标注的 readOnlyHint。
- 外部写入先生成固定工具、目标、参数与摘要绑定的确认卡。用户点击后入持久队列，执行前再检查连接版本、工具定义和有效期。
- 进程中断或超时导致结果未知时标记 unknown，不自动重复发信、建工单或付款；需要回原系统核对。MCP 的幂等 metadata 只是给服务端的提示，不保证服务端实现幂等。
- 成功后可沿原任务继续；停止会取消未提交动作，不能撤销外部系统已经发生的变更。
- 凭证加密保存，不提供给模型。工作系统资料进入项目历史后会加密保存；更换模型或停用连接会撤销历史内容的模型授权。

管理员批准的“只读工具”仍可能有命名不实或业务副作用，必须结合真实系统文档和测试账号验收。传回的网页、邮件、文档和工具结果均为不可信资料，不能代替用户授权。

## 企业集群部署

源码包含 `Dockerfile.enterprise`、部署清单生成器、部署脚本与故障验收脚本。

目标拓扑为：OIDC 网关、API、Agent、Worker 各至少 2 个副本；PostgreSQL 由 CloudNativePG 管理 3 个数据节点；共享文件系统使用已有 HA RWX CSI 存储。副本反亲和、可用区分布、PodDisruptionBudget、滚动发布、非 root、只读根文件系统、无 Kubernetes API token 与网关到 API 的网络策略均在生成器中配置。

PostgreSQL 示例要求至少一个同步副本确认提交；同步副本不足时优先保护数据并暂停写入。这是在可用性与持久性之间做出的明确取舍，不能同时承诺任何故障下都可写且零丢失。

部署前必须有真实输入，而不是示例占位符：

- 可部署的 Kubernetes context、足够的跨节点/可用区容量、Ingress、TLS 和 CloudNativePG operator。
- 支持 POSIX 锁、可跨节点挂载且自身具有容错能力的 RWX PVC；单台 NFS 服务不能替代 HA 存储。
- 已构建并上传的 Atlas 镜像摘要、已批准的 OAuth2 Proxy 镜像摘要。
- Secret：`APP_TOKEN`（OIDC 模式下不可用作降级登录）、`DATABASE_URL`、`OIDC_ISSUER`、`OIDC_JWKS_URL`、`OIDC_AUDIENCE`、`OIDC_ALLOWED_SUBJECTS`、`OIDC_CLIENT_SECRET`、`OIDC_COOKIE_SECRET`。若由本清单引导 PostgreSQL，还需 CNPG 所需的 `username=atlas` 与 `password`；Secret 类型及字段须符合所安装 operator 要求。
- OIDC 回调为 `https://站点/oauth2/callback`；主体允许列表要使用 issuer 颁发的稳定 sub，不使用邮箱猜测身份。

从 `deploy/enterprise/config.example.json` 填写实际配置。镜像未固定摘要、共享盘/密钥身份不匹配时，生成器或服务会直接拒绝。部署脚本先做 Kubernetes server-side dry-run，再初始化共享状态并发布各角色。不会自动选集群，不会覆盖所有者管理的 Secret/PVC。

```bash
python scripts/render_enterprise.py enterprise-config.json atlas-enterprise.yaml
bash scripts/deploy_enterprise.sh enterprise-config.json YOUR_KUBECTL_CONTEXT
python scripts/verify_enterprise_ha.py --url https://YOUR_ATLAS_HOST
```

验收凭证通过 `ATLAS_VERIFY_TOKEN` 环境变量提供。`--disrupt-agent --context ... --namespace ...` 才允许删除测试任务所属的 Atlas Agent Pod；脚本会先检查标签和执行归属。单次演练通过不等于长期可用性 SLO 已达标。

## 当前部署与迁移边界

Railway 的挂卷服务不能开副本，且挂卷重部署有短暂中断。故当前地址即便迁移到独立 PostgreSQL，也仍有单应用实例、本地工作卷、单模型决策服务和数据库单主节点等单点。

本轮向 Railway 官方 Postgres HA 模板发起了异步工作流，但在库存检查中未观察到对应 HA 节点或待应用资源，不能算部署成功，也不能据此推断具体套餐限制。成功创建并可观测的是独立 PostgreSQL 和私有恢复桶。完整 HA 仍需要实际可部署的集群/共享存储，或者针对当前平台进一步重构为远端工作区与对象存储；不能仅把环境变量改成 ha。

SQLite → PostgreSQL 是单向切换。原 SQLite 与迁移前副本留在卷上供审计/恢复；上线后的写入只进新 PostgreSQL，不能把连接串改回旧 SQLite 冒充回滚。迁移脚本拒绝覆盖非空数据库，按迁移标记幂等重启。

## 备份与真实恢复

Restic 的仓库密码使用独立随机密钥，不写进备份仓或源码；必须由所有者在独立的凭据系统保存。丢失该密码将无法恢复。S3 采用私有桶，所有者通过服务变量引用授予访问，不在源码中复制凭证。备份进程只取得备份所需环境，不继承模型、应用访问口令或数据库密码。

当前兼容部署每天备份一次，失败延后重试；企业清单可配置独立 CronJob。上传后恢复到临时目录并比较完整快照哈希。恢复安装仍只允许空工作空间，避免覆盖现有资料。

`ATLAS_BACKUP_RETENTION_DAYS=0` 默认不自动删备份。设置正整数才启用 Restic 的保留/清理策略，须先确定企业留存政策。异卷备份仍与应用处于同一账号/地域；Railway 桶文档列明不支持 Object Lock、对象版本和桶生命周期配置，因此不能宣称具备不可变备份或完整跨账号灾备。正式灾备应接入独立权限/账号的存储并做空环境恢复演练。

备份保留的时间间隔决定灾难恢复可能丢失的工作量；每日快照不是实时 RPO=0。备份还会使用临时盘、CPU、存储与服务网络流量，应纳入预算。

## 验收口径与未验证项

代码测试、模型任务成功率、系统可用性是三个不同指标。实现测试通过，不代表商业产品能力大差不差。本版验证清单包括真实 PostgreSQL 的跨进程锁、进程退出接管、终止锁连接后的失效写入拦截、原 SQLite 迁移、加密保存、精确一次性确认、未知结果不自动重发、OIDC 签名/受众/主体拒绝、Office 产物与哈希、预算并发上限、Restic 实际加密仓恢复与错误密码拒绝，以及浏览器恢复/通知/确认卡 DOM。

真实模型只安排小范围办公交付冒烟，不把它作为能力评分。完整生产集群、真实公司 OIDC、真实 MCP 系统、跨地域灾难、原生系统通知与专业文档视觉质量尚需各自验收。需用你实际的办公任务集衡量完成率、返工次数、人工接管率、错误外部操作率、耗时与费用；没有任务样本和对照产品评测，不能给出可靠的能力百分比。

## 核实的官方依据（2026-10-19）

- [Railway 卷限制](https://docs.railway.com/volumes/reference)：挂卷服务不支持副本，挂卷发布存在中断。
- [Railway PostgreSQL HA](https://docs.railway.com/databases/postgresql-ha)：Patroni、共识节点与代理的故障切换；平台未发布 uptime SLA，应用需处理重连并自行配置告警。
- [CloudNativePG 同步复制](https://cloudnative-pg.io/docs/1.28/replication/)：明确数据安全与可用性的取舍。
- [OAuth2 Proxy 配置](https://oauth2-proxy.github.io/oauth2-proxy/configuration/overview/) 与 [PyJWT](https://pyjwt.readthedocs.io/en/stable/usage.html)：网关转发身份，后端独立验签，不信任任意转发头。
- [MCP 传输](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) 与 [工具](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)：协议、工具结构与人的确认；工具注解不能提供信任证明。
- [ChatGPT agent 官方介绍](https://openai.com/index/introducing-chatgpt-agent/)：工具组合、可中断协作与有后果操作的确认；官方同时说明仍会出错，不能拿来证明“所有工作问题都能解决”。
- [Restic](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)、[恢复](https://restic.readthedocs.io/en/stable/050_restore.html) 与 [Railway 桶](https://docs.railway.com/storage-buckets)：加密备份、仓库密码与对象存储限制。
