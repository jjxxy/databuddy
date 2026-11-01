# Agent 执行器：配置、适配与运行边界

Atlas 使用执行器驱动模型规划和工具调用；项目、业务资料、工具权限、审批、任务状态、回执与成果仍由 Atlas 管理。Web 页面面向业务工作，执行器配置由部署管理员维护。模型目录与执行器是两个不同的配置：更换模型不会自动更换 Agent 执行器。

本次实现提供的是**最小内部适配器接口，以及对现有两个执行路径的封装**。本指南描述当前仓库已有的实现，不代表任意第三方 Agent 已经接入或通过企业生产验收。

## 已注册的执行器

| 名称 | 实际实现 | 注意事项 |
| --- | --- | --- |
| `opencode` | OpenCode 会话和工具循环，通过 Atlas 受控工具桥完成业务操作 | 需要在执行节点启用并部署对应 OpenCode；集成限定已验证版本，原生工具不全部开放 |
| `atlas_legacy` | Atlas 内置的持久规划与工具执行图，每次由工作进程执行一步并继续调度 | 这是仓库原有的 Atlas 自有实现，不是 Codex、Claude Code 或其他第三方执行器；仍需要配置可用模型 |

OpenCode 模式中 Atlas 插件移除未授权的原生工具，只允许调用由 Atlas 校验的 `atlas_step`；文件、终端、浏览器等能力必须通过当前已配置并授权的 Atlas 工具和运行环境访问。OpenCode 的全部原生功能并未因此开放。代码入口见 [`agent_runtime.py`](agent_service/agent_runtime.py)、[`opencode_runtime.py`](agent_service/opencode_runtime.py)、[`opencode_plugin.mjs`](agent_service/opencode_plugin.mjs) 和 [`runtime_legacy.py`](agent_service/runtime_legacy.py)。

当前没有注册 Codex、Claude Code、DeepSeek Harness 或 Deep Agents 执行器，也没有实现跨引擎迁移、第三方原生事件流或原生审批的通用对接。即使某个外部引擎的功能探针成功，也不代表其隔离和权限已经验收，不能在管理员选项中直接启用。不同产品的私有原生会话不能直接迁入其他执行器。

## 默认值与管理员选择

没有保存管理员选择时，默认值沿用部署配置：`ATLAS_OPENCODE_RUNTIME=1` 时选择 `opencode`，否则选择 `atlas_legacy`。默认 quickstart 在配置中启用 OpenCode。执行节点必须确实配置并启动对应执行器；API 进程的配置展示不能证明其他节点健康或所选模型可用。

管理员可通过 `/api/operator/agent-runtimes` 查看或修改**新任务的默认执行器**。当前没有面向普通用户的执行器选择页面。该 API 沿用管理员鉴权：请求必须通过工作空间身份认证，且提供独立的 `ATLAS_OPERATOR_TOKEN`；Token 模式下是 `Authorization: Bearer <APP_TOKEN>` 和 `X-Atlas-Operator-Token: <ATLAS_OPERATOR_TOKEN>`。后台口令至少 16 个字符，不能与工作空间口令相同；未配置时管理员接口不可用。OIDC 部署应使用该部署的工作空间身份认证，并另行提供后台口令。

以下是 Token 模式下的示例；使用实际部署的 HTTPS 地址，避免把凭据写进脚本或代码仓。示例中的地址和名称需要按部署替换。

```bash
read -rsp '工作空间口令：' atlas_workspace_token; echo
read -rsp '后台管理员口令：' atlas_operator_token; echo
atlas_base_url='https://your-atlas.example'

curl -sS "$atlas_base_url/api/operator/agent-runtimes" \
  -H "Authorization: Bearer $atlas_workspace_token" \
  -H "X-Atlas-Operator-Token: $atlas_operator_token"
```

读取响应中的 `selection.revision`，将它填入下面请求的 `expected_revision`。例如选择 Atlas 自有执行器：

```bash
curl -sS -X PUT "$atlas_base_url/api/operator/agent-runtimes" \
  -H "Authorization: Bearer $atlas_workspace_token" \
  -H "X-Atlas-Operator-Token: $atlas_operator_token" \
  -H 'Content-Type: application/json' \
  -d '{"name":"atlas_legacy","expected_revision":"替换成上一步的revision"}'
```

选择 OpenCode 时将 `name` 改为 `opencode`；设为 `null` 恢复由部署环境决定的默认选择。配置会持久保存。并发修改发生冲突时返回 HTTP 409，应重新读取配置后再修改，不要盲目覆盖另一位管理员的变更。使用后清除当前 shell 中的口令变量：

```bash
unset atlas_workspace_token atlas_operator_token
```

`adapters` 是 API 所在进程看到的注册与可用配置，`agent_nodes` 用于查看节点上报信息；这不是集群或模型健康保证。部署应确保 API、Agent/Worker 和相关服务使用相同代码修订、共享必要配置与状态，并在实际执行节点核对 OpenCode、模型和工具连接。不要为了切换执行器清除业务数据库。

## 任务版本、继续与恢复

- 新提交任务会保存所选执行器的 `name` 和 `version`；修改默认值只影响之后创建的新任务。
- 继续现有任务和内部协作子任务沿用原任务的执行器绑定。暂停后继续并不等同于把任务切给新默认执行器。
- 旧任务没有绑定信息时，系统仅在存在 OpenCode 会话、版本、回执或对应调用标识等明确线索时按 OpenCode 处理；普通 Atlas 工具记录本身不能证明过去使用的是哪种执行器。缺少明确线索时，兼容策略会按当前默认值推断，并记录 `historical_unknown_default`，这不是对历史执行器身份的确定证明。既有业务操作记录不因推断而变更。
- 若固定的执行器未启用、不可用、版本不匹配或与已有原生会话冲突，任务保留并提示恢复原执行器/版本；不会静默改用另一个执行器。恢复已开始的工具调用前同样校验绑定。
- 通过新的对话轮次继续任务时，如果旧任务存在 `started` 且结果未确认的写操作或未知工具，系统会阻止继续并提示先核对工作记录与外部状态；原任务的继续标记保留。只读步骤可重新读取。同一任务原有恢复机制与此检查不同，不能据此认定所有外部操作均可恰好执行一次。
- 当前没有自动把执行中的任务、原生会话或隐藏上下文从一个执行器迁移到另一个执行器的功能。将来实现跨引擎继续时，需要单独设计检查点、操作回执、权限核对和未确认外部操作的处理。

普通对话没有固定总模型调用次数限制；单次上下文窗口、模型服务额度、部署策略及实际资源约束仍然存在。不同执行器的上下文机制不保证相同；长期对话和异常恢复需要按实际模型与部署环境验证。

## 开发新的执行器适配器

当前内部协议定义在 [`agent_runtime.py`](agent_service/agent_runtime.py)：适配器提供 `name`、`version`、`label`、`available()`、`capabilities()` 和 `run(turn_id)`。`capabilities()` 描述当前实现的工具控制、审批、进度和继续方式等；声明不代表已经完成生产验收。其中 `run` 驱动 Atlas 已持久化的任务；Atlas 的工作进程负责调度和外层错误处理。两个实现的执行方式不同：OpenCode 驱动其会话循环，Atlas 自有执行器每次执行一个图步骤。新增适配器必须清楚说明这种生命周期差异，不能把一次调用的返回等同于任务成功完成。

接入新执行器时，至少需要：

1. 明确其官方 SDK、服务协议或结构化事件协议和固定版本；不能把未经约束的终端输出当作可验证的工具结果。
2. 在注册表及管理员允许的配置中添加适配器，并定义版本兼容与升级策略；不能覆盖已有任务固定的执行器版本。
3. 把获准的 Atlas 工具、安全校验、数据访问、审批、停止、失败处理和持久回执接入该引擎。MCP 是可选连接方式，不能替代 Atlas 授权与审批。
4. 验证隔离和执行权限。引擎如果可以绕过 Atlas 直接使用宿主机文件、命令或网络，必须有等效的隔离、授权和审计；仅靠提示词不足以建立安全边界。
5. 不假设原生会话、压缩摘要、Skill 格式或隐藏状态可跨引擎无损迁移。未知外部操作结果不能盲目重试。
6. 按许可、服务条款、模型认证、费用、数据处理与网络要求核查实际使用方式；适配成功不等于商业授权或生产验收通过。

现有的关键回归入口包括 `tests/test_runtime_legacy_adapter.py`、`tests/test_runtime_adapter_conformance.py`，以及现有 OpenCode、工具权限、任务恢复与上下文测试。示例：

```bash
.venv/bin/python -m pytest -q tests/test_runtime_legacy_adapter.py tests/test_runtime_adapter_conformance.py tests/test_opencode_agent_bridge.py
```

自动化测试使用受控模型或模拟服务时，只能证明测试范围内的行为。接入真实引擎前，还应在目标环境测试文件与数据库权限、审批、长任务中断和恢复、重复副作用、上下文压缩、成果校验、计费及服务不可用等场景。
