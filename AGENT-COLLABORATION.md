# Atlas Agent 协作（v0.19）

业务用户可以在同一个项目对话中把明确的子任务交给本地专业 Agent 或已授权的远端 Agent，主任务会等待、接回实际状态并继续核对。当前实现侧重可追踪、可恢复、可检查的数据处理协作。多个 Agent 的一致意见、协议完成状态和文件哈希都不能替代财务口径批准或独立控制数核对。

## 业务用法

例如：“按本项目已确认的办事处粒度处理收入明细，请数据检查 Agent 检查期间、单位、漏行和重复；请财务复核 Agent 独立指出口径风险，再由你给我完整结果和经营汇报。”

默认可用三个本地只读协作者：数据检查、财务复核、报告复核。它们使用主任务固定的模型连接、指定的来源/结果范围和共同预算，不能任意改写工作文件、发布结果或递归委派。返回意见需有实际读取证据；指定输入未读全时不记为成功。主任务保留原有工作流和数值验收职责。

远端连接在项目“Agent 协作”中配置，或由管理员后台预置。对外发送资料、提交远端任务及让所选模型读取返回内容均需明确授权。用户只选择已有模型的显示名称，不需要填写模型服务地址、模型密钥或 Agent 引擎。

每次最多4个子任务。委派时分别固定目标、明确的来源/结果标识、完整输入字节及来源版本；默认不把整个项目、工作目录、模型密钥、工作台口令或 MCP 凭据发给远端。独立 Agent 仍可能在它获准的远端项目范围内读到额外资料；远端服务的项目权限需要单独配置。固定连接和声明的模型ID不能证明第三方内部实际使用哪个供应商；远端的模型变更、数据保留和转委托政策仍需要独立治理。

## 配置远端 Agent

支持两个明确的 A2A JSON-RPC 轮询配置和一个 MCP 工具配置。它们不是“所有 Agent 都能直接接入”的承诺。

| 配置 | 提交 / 查询 / 取消 | 边界 |
| --- | --- | --- |
| A2A 1.0 | SendMessage / GetTask / CancelTask | ROLE_USER、TASK_STATE_*，明确 returnImmediately=true；默认用于新连接 |
| A2A 0.3.0 | message/send / tasks/get / tasks/cancel | 兼容旧版 kind、user/agent、小写状态 |
| MCP Agent | 配置的三个独立工具，默认 atlas_submit_task / atlas_get_task / atlas_cancel_task | 实际发现工具、校验输入 schema，冻结工具结构；不是通用任意工具执行器 |

A2A 发现卡只能位于同一站点的 `/.well-known/agent-card.json`，必须声明与配置完全一致的协议与端点。省略版本时可识别明确声明的0.3.0旧卡，保存后钉住所选版本；未知版本不会猜测兼容。发送时始终带明确版本头。当前不声明 A2A 流式、推送、OAuth、签名卡片或完整协议一致性认证；需要补充信息时保留 input_required，用户明确补充后建立新任务，不自动重启远端工作。

MCP 使用 Streamable HTTP，支持协商2025-03-26、2025-06-18或2025-11-25，接收有界 JSON/SSE 回执。每次调用重新发现并核对被冻结的工具结构；结构改变则要求重新连接。MCP 连接自己安装的通用工具能力与这里的“具备任务生命周期的远端 Agent”有不同契约。

使用远端颁发的专用服务令牌。例如另一 Atlas 实例的管理员通过 [AGENT-GATEWAY.md](AGENT-GATEWAY.md) 创建受限调用方；不能把本机 APP_TOKEN、管理员口令或模型 API key 填成远端凭据。

个人配置接口示例，普通业务鉴权沿用已有接口：

~~~json
{
  "name": "经营分析复核方",
  "transport": "a2a",
  "url": "https://remote-atlas.example.com/a2a",
  "credential": "REMOTE_SCOPED_AGENT_TOKEN",
  "remote_project_id": "p_remote_authorized_project",
  "remote_model_id": "finance",
  "data_sharing_approved": true,
  "submit_approved": true,
  "model_approved": true,
  "approved_model_ids": ["finance"]
}
~~~

- GET/POST `/api/projects/{pid}/agent-peers`：列出或配置连接；列表不返回密钥。
- POST `/api/projects/{pid}/agent-peers/probe`：验证配置，不保存；探查不等于授权外发资料。
- PUT/DELETE `/api/projects/{pid}/agent-peers/{peer_id}?expected_revision=...`：编辑或停用；旧版本不能覆盖新配置。相同地址编辑时空凭据保留已有密钥。
- 管理员 POST/PUT/DELETE `/api/operator/projects/{pid}/agent-peers...`：预置连接；使用已有后台鉴权。业务用户不能改写预置连接。

包含公司内部资料的项目只能发给管理员单独批准的目的地（后台 `private_data_approved`）。个人用户不能通过勾选自行取得内部资料外发许可。模型授权绑定实际模型连接及目录修订，后续切换到未批准模型会阻断。项目一旦接收协作内容，就保留该内容的模型目的地限制；停用连接不抹掉已经接收的数据限制。

生产端点必须 HTTPS，连接使用固定解析IP、原始Host及TLS SNI，不跟随重定向，不继承环境代理，不下载对方提供的任意URL。本机HTTP只在明确本地模式与受允许的回环地址使用。私网目的地仍受现有出站网络策略控制。

## 状态、重启与取消

任务和去重批次在同一数据库事务中接收。输入、远端凭据与连接修订固定并加密；真正发请求前先持久化提交状态。相同工具操作编号重试返回已有任务，改变原请求内容会拒绝，不能静默替换已发送工作。

`queued → submitting → submitted/working → succeeded/failed/input_required/auth_required/rejected/cancelled` 是正常流程。`unknown` 表示无法判断远端实际执行情况，尤其是“远端可能已接收但提交响应丢失”或进程中途停止。系统不承诺跨系统 exactly-once，也不会为了恢复界面进度盲目再次提交。没有远端编号时须人工核对；有编号时只查询已绑定任务，不能换成另一个任务的回执。

取消是请求，取得真实取消回执后才显示 cancelled；取消不回滚已完成的远端写入。unknown 但已有远端编号时，用户明确请求取消可重新查询这个已有任务，不会重新提交。过期会发送一次取消并有限查询，仍不明确则 unknown。停用或修改连接会停止使用旧凭据。删除项目/会话会停止本地工作，对已知远端任务仍有限度尝试取消及查询；它不会恢复已删除的主任务、继续接收内容或重新提交。

本地专业 Agent 使用独立有界执行通道，主任务等待时不会占住所有子任务执行槽。主任务在所有子任务返回状态后恢复；未解决的失败、需要补充或 unknown 会阻止它把整项工作宣称为完成。

## 完整成果与财务核对

状态接口：GET `/api/projects/{pid}/agent-tasks`、GET `/api/projects/{pid}/agent-tasks/{id}`；取消 POST `/api/projects/{pid}/agent-tasks/{id}/cancel`。结果区分：

| 字段 | 含义 |
| --- | --- |
| execution_status | 对方任务的执行终态 |
| verification_status=advisory_unverified | 已返回复核意见；不作为数值或文件交付的完整性证明 |
| verification_status=integrity_verified | 已收到并核对完整成果字节、清单与哈希 |
| financial_verified=false | 传输或字节核对没有批准财务事实 |

委派参数 `expected_delivery=advisory` 允许有来源的独立意见；`artifacts` 必须提供非空的实际结果或产物字节，不能只回传输入原件、预览或自报完成。对方协议状态 completed 但业务结果是 partial/needs_input/blocked/paused 时，本地不会记为完整成功。

完整交接格式见 [AGENT-HANDOFF.md](AGENT-HANDOFF.md)。默认解码后最多2MB、总计20项；当前出站客户端报文最大4MB。超过限制会明确拒绝或保留 input_required，不能悄悄截断。大成果可由远端网关的鉴权分页接口人工获取；本版本出站协作尚未自动拉取所有大成果分页，不能把该边界当作已经实现的无限大数据传输。

通过完整结构与字节核验的数值结果会以 `received_result_ids` 接入本项目，保留原子值、类型、语义及全行，既有 `read_result` 等工具可继续实际读取。所有外部结果强制待语义核对，不自动转为已批准指标。普通报告/Office文件经完整字节核对后幂等接入统一成果目录，可安全预览和下载原件，并以 `received_artifact_ids` 关联；`output_artifacts` 保留远端清单。它们不会自动冒充数据来源或已批准结果。

GET `/api/projects/{pid}/agent-tasks/{task_id}/artifacts/{artifact_id}?offset=0&limit=65536` 或只读工具 `read_agent_task_artifact` 可读取已保存的完整内联产物分页。响应是带 Base64 内容、页SHA256、全文件SHA256、下一偏移及eof的JSON，不是任意文件系统路径或远端下载链接。仅当前用户与项目可以读取。修改内容需沿用既有结果生成、核对和发布流程。

## 成本与上线验收

本地协调者和专业协作者共用持久化的模型调用、输入、预留输出和时间预算；预留预算不是实际账单。外部 Agent 内部消耗的模型调用不可由本系统精确计量，需要远端自己的预算和计费控制。多个复核 Agent 增加延迟与模型费用，也可能共享同一种模型偏差；关键数值仍应使用确定性计算、完整来源及独立控制数。

运行针对性检查：

~~~sh
python -m pytest tests/test_agent_mesh.py tests/test_agent_team.py tests/test_collaboration_budget.py tests/test_agent_handoff.py -q
python scripts/verify_agent_collaboration.py --help
~~~

独立两实例检查使用真实HTTP、OpenCode原生工具循环和确定性模拟模型，检查双向A2A/MCP调用、40行完整数值及高精度Decimal、子任务等待恢复、取消、重启未知和撤权。模拟模型验证执行链与数据合同，不证明商业模型在所有业务任务上的规划质量。正式启用仍需在目标身份系统、网络代理、数据库、备份恢复与真实商业模型上进行实际业务验收。

协议设计依据：[A2A 1.0规范](https://a2a-protocol.org/latest/specification/)、[A2A 0.3.0规范](https://a2a-protocol.org/v0.3.0/specification/)、[MCP工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。本项目声明的是上表所列配置能力，不以文档链接代替协议认证或生产负载证明。
