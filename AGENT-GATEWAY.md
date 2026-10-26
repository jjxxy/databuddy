# Atlas Agent 调用网关（v0.19）

本模块让外部 Agent 调用真实的 Atlas 项目任务。请求进入原有持久会话、模型、工具和结果验收链路；不是把请求原样交给聊天模型后直接宣称完成。返回“已接收”与返回“已完成”有不同含义。

## 先配置独立调用身份

业务用户继续使用项目与对话页面。管理员通过后台接口创建调用方，不把工作空间口令或管理员口令交给其他 Agent。

- 创建：POST /api/operator/agent-clients
- 查看：GET /api/operator/agent-clients
- 修改：PUT /api/operator/agent-clients/{id}，提交 expected_revision
- 轮换：POST /api/operator/agent-clients/{id}/rotate，提交 expected_revision
- 停用：DELETE /api/operator/agent-clients/{id}?expected_revision=...

这些后台操作使用既有的业务认证及独立 X-Atlas-Operator-Token。创建与轮换只返回一次新令牌；持久记录仅保存高熵令牌的 SHA-256，列表和模型上下文不会返回令牌或摘要。新令牌不是普通网页或管理员凭据。

创建请求示例：

~~~json
{
  "name": "经营分析协作方",
  "project_ids": ["p_actual_project"],
  "approved_model_ids": ["finance"],
  "interaction_mode": "chat",
  "max_active_tasks": 4,
  "max_total_tasks": 1000,
  "max_model_calls_per_task": 12,
  "max_total_model_calls": 10000,
  "max_input_characters": 360000,
  "max_output_tokens": 24000,
  "task_timeout_seconds": 300,
  "ttl_seconds": 2592000
}
~~~

项目和模型必须实际存在。chat 默认为只读分析；work 允许项目文本、受控数据工作流和报告生成。work 仍不允许调用方直接发布、确认正式指标、改调度、删除文件、执行任意 Python、通过外部系统写工具操作，或继续递归委派其他 Agent。服务端限制不依赖模型自觉遵守。

授权是**项目级**：调用方能在获准项目的范围内分析现有与后续资料，不是只对某份附件开放。如果项目内存在不能共享给它的资料，应建立独立项目并限定授权。调用方使用独立服务身份，不继承管理员/个人的私人记忆或个人 MCP；项目约定和项目资料仍属于该项目授权范围。当前实例不因此成为已验证的多租户平台。

修改授权或轮换令牌使旧修订任务失效，并请求取消旧活动任务。停用和到期也会在后续模型请求及工具执行前重新检查。无法撤回已经发送到模型或另一系统的内容，取消也不会自动回滚已经完成的工作。

## REST 任务接口

使用 Authorization: Bearer <单独颁发的Agent令牌>。

- POST /api/agent/v1/tasks：提交任务，HTTP 202 表示接收。
- GET /api/agent/v1/tasks/{task_id}：查看此调用方自己的状态。
- POST /api/agent/v1/tasks/{task_id}/cancel：取消并请求停止活动数据运行。
- GET /api/agent/v1/tasks/{task_id}/artifacts/{artifact_id}?offset=0&limit=65536：分页读取校验过的产物字节。

~~~json
{
  "project_id": "p_actual_project",
  "request_id": "finance-monthly-request-001",
  "goal": "核对9月收入和预算，按已明确的口径生成完整结果，缺少信息时具体说明。",
  "source_ids": ["source_actual_id"],
  "model_id": "finance"
}
~~~

request_id 至少8字符。相同调用方、项目和请求编号固定一个任务；重试原请求不会新增第二个任务。相同编号更换目标、输入、模型或模式返回409，不会静默替换。更换有效授权后应使用新请求编号明确发起。

状态包括 submitted、working、input-required、completed、failed、cancelled。内部会话 done 可能代表需要补充信息、暂停或部分完成，因此不能直接等同于 completed。失败/部分完成保留实际已产生的成果；模型文字不成为数值或执行证明。

任务接口只允许查询自己发起的任务，不能通过猜任务编号绕过接口鉴权。授权项目中的共享资料、历史和成果仍可被该项目的数据工具使用；这不是项目内按调用方隔离。严格隔离需要独立项目或实例。业务口令不能用于这些机器接口，机器令牌也不能访问普通业务或管理员接口。

## A2A 协议配置

端点为 POST /a2a；受保护的发现卡为 GET /.well-known/agent-card.json。对外站点由管理员设置 ATLAS_AGENT_PUBLIC_URL，例如 https://atlas.example.com，不能依赖不可信 Host 或转发头生成对外地址。未配置时发现卡使用本机回环地址，适用于同机接入，不代表远程可达。

提供两个明确的 JSON-RPC 轮询配置：

| 配置 | 请求方法 | 数据形态 |
| --- | --- | --- |
| A2A 1.0 | SendMessage、GetTask、CancelTask | ROLE_USER/ROLE_AGENT、TASK_STATE_*、片段不含旧版 kind；SendMessage 返回 task 包装 |
| A2A 0.3 | message/send、tasks/get、tasks/cancel | user/agent、submitted 等小写状态及 kind；返回 Task |

请求设置 A2A-Version: 1.0 或 0.3。共享 /a2a 路径缺失版本头时按0.3解释；发现卡默认返回1.0卡片，可用版本头读取0.3卡片。

1.0的 SendMessage **必须明确 configuration.returnImmediately=true**。本配置只实现异步提交和轮询，未请求这个选项会明确拒绝，不把尚未完成的任务冒充默认阻塞调用的完成响应。没有声明流式、推送、通用任务续写、gRPC、OAuth、发现卡签名或完整协议一致性认证。需要补充信息时可以通过新任务明确提交补充后的完整目标与输入。

~~~json
{
  "jsonrpc": "2.0",
  "id": "rpc-001",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "finance-monthly-request-001",
      "role": "ROLE_USER",
      "parts": [
        {"text": "按已明确的口径核对本月经营数据。"},
        {"data": {"atlas": {
          "project_id": "p_actual_project",
          "request_id": "finance-monthly-request-001",
          "goal": "按已明确的口径核对本月经营数据。",
          "model_id": "finance",
          "source_ids": ["source_actual_id"]
        }}}
      ]
    },
    "configuration": {"returnImmediately": true}
  }
}
~~~

数据片段必须明确 Atlas 项目及参数。不会根据远程消息下载任意文件 URL。文本、文件内容和远端描述都属于不可信任务资料，不能扩大授权、冒充用户批准或选择任意模型连接。

## MCP 接入

POST /mcp/atlas 提供无服务器会话状态的 Streamable HTTP JSON-RPC 工具端点。支持协商2025-11-25、2025-06-18、2025-03-26，提供 initialize、notifications/initialized、ping、tools/list、tools/call。

暴露四个受限工具：atlas_submit_task、atlas_get_task、atlas_cancel_task、atlas_read_artifact。工具说明和 annotations 只是能力描述；服务端仍独立检查项目、模型、调用方、参数和任务归属。当前不提供 MCP resources、prompts、SSE事件流或实验性tasks协议。

含 Origin 的请求必须来自 ATLAS_AGENT_PUBLIC_URL、ATLAS_AGENT_ALLOWED_ORIGINS 中明确的来源，或与 Host 一致的本机回环来源；不会任意放行跨站网页。机器调用通常不带 Origin。请求体最大3MB，并在应用中逐块限制，不仅依赖代理 Content-Length。JSON重复字段与非标准常量拒绝。生产仍应在TLS反向代理配置请求大小、连接和来源限流，防止授权令牌滥用。

## 完整数据与结果交接

可在任务中提供 input_bundle，使用 [AGENT-HANDOFF.md](AGENT-HANDOFF.md) 的不可变交接格式。普通来源保留原始文件字节、版本及SHA256；完整结果保留类型、粒度、期间、单位和全量交接清单，不将前几行预览当作全部输入。默认最多20项、解码后2MB；超出时明确拒绝，不悄悄裁剪。导入原件只走确定性探查，不把新资料自动交给另一个默认模型做规划。

任务状态中的 artifacts 与 output_bundle 来自本任务真实执行产物；大成果通过受鉴权的分页字节接口读取并核对完整哈希。交接完整性不等于财务业务正确，财务口径仍需既有的核对、正式指标确认和独立控制数。

## 预算与恢复

任务、输入摘要、服务身份、模型修订、来源版本、调用预算和交接引用持久保存，服务重启后仍可查询原任务。客户端应先重试同一已授权请求、查询其任务状态，不在超时后盲目生成新编号。多个调用方的任务身份分别派生。

每任务预算沿用原有模型预算门槛，并按调用方上限收紧。调用方模型请求总量和已预算输入/输出量另外持久计数；这些是 admission 预算，不是模型账单。原生调用会先预留单次输出上限（当前3200 tokens）；把总输出预算设得低于单次上限会明确阻断，系统不会偷偷提高授权预算。即使模型不报告实际token用量，也不能把它记为零成本。默认不允许入站任务再向其他 Agent 委派，避免循环、扩散权限与无限费用。

接口与状态验证运行：

~~~sh
python -m pytest tests/test_agent_gateway.py -q
~~~

这验证应用权限、状态和幂等逻辑；真实企业身份、反向代理、第三方 Agent 行为、商业模型质量、并发负载和灾难恢复仍需在部署环境验收。

官方设计依据：[A2A 1.0规范](https://a2a-protocol.org/latest/specification/)、[A2A 0.3规范](https://a2a-protocol.org/v0.3.0/specification/)、[MCP工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。接口明确声明本次实现的配置范围，未把协议文档等同于已经通过全协议一致性测试。

