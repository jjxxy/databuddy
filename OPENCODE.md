# OpenCode 业务 Agent 运行方式

核验日期：2026-10-21。官方更新日志、npm `@opencode/cli/latest` 和实际下载运行的 Linux amd64 二进制均为 **2.1.2**。镜像锁定这个已核验版本，构建时校验官方 npm 包的 SHA512；不在启动时追随浮动 `latest`。2.1 对会话和插件协议有不兼容变更，后续升级必须重新执行协议回归。`opencode-ai` 属于旧的 1.x 包，不可混用。

## 本版实际架构

此前交付只有 OpenCode 的无状态文本生成接口，工具循环仍由 Atlas 规划。这与“底层就是 OpenCode Agent”有差距，已纠正。

默认 Docker 镜像现包含 OpenCode 2.1.2。cloud supervisor 先启动并检查本机 OpenCode 服务，再启动业务 API、Agent 执行器和数据 Worker。每项业务任务创建持久 OpenCode session，由 OpenCode 调用选定模型并运行原生多步工具循环。业务用户只选择管理员提供的模型名称；地址、密钥和运行引擎不在前台配置。

以上默认启动方式对应 `compose.quickstart.yaml` 和镜像的 cloud supervisor 命令。旧 `compose.yaml`、其 multimodal 覆盖文件和企业 Kubernetes 生成器按角色覆盖启动命令，显式采用 `ATLAS_OPENCODE_RUNTIME=0` 保留原 Atlas 工具循环；它们没有启动或验收 OpenCode 原生服务。不要在这些旧清单中单独改成 `1`：多个副本需要各自的运行服务、连接发现、会话归属和恢复方案，当前原生路径尚未完成这项集成。需要本版 OpenCode 业务 Agent 时使用默认 quickstart 部署。

OpenCode 通过本项目的 `atlas.business-tools` 插件调用唯一原生工具 `atlas_step`。Atlas 检查每次请求的项目、工作/聊天模式、参数、用户意图、业务口径、文件前提及验收证据，再运行相应业务工具。Atlas 的完整数据结果、精确十进制计算、Excel 源单元格证据、版本记录、候选发布、外部写入确认和恢复机制继续生效。OpenCode 的自由文本结束不能被当成业务验收通过。

这是一套针对业务数据处理的 OpenCode Agent，不等于开放 OpenCode 的所有开发工具。原生 shell、文件系统、浏览器、子 Agent、任意 MCP 和 Code Mode 没有授权给模型。只有已注册、通过 Atlas 检查的业务工具可用。任意 Python 执行仍要求单独配置隔离执行器；安装 OpenCode 不会替代它。

## 模型由管理员提供

管理员在 `llm.json` 配置一个或多个 OpenAI 兼容模型。启动时验证连接和真实函数调用能力；只通过普通文本连通性检查的模型不足以运行原生工具 Agent。模型选择随任务冻结到加密保存的配置版本，继续任务沿用原版本。后台停用的模型不能再运行，任务不会静默切换上游。旧的待续任务所引用的配置，在启动 OpenCode 时一并加载。

模型路由标识由地址、模型和密钥共同派生，仅内部使用。轮换密钥或修改同名模型不会把旧任务指向另一条连接。公司资料的外发批准继续绑定具体模型和配置版本；允许连接某个模型不等于批准向其发送内部资料。

## 部署和权限边界

默认服务只监听 `127.0.0.1`，使用独立随机密码。OpenCode 子进程使用私有 HOME/XDG 目录和最小环境，不继承工作空间口令、数据库连接串、公司检索凭据、宿主机模型账户或 OpenCode 配置变量。禁用免费模型回退。业务数据目录和运行目录属于同一服务账户，因此这些限制属于经过校验的应用权限边界，**不是操作系统级沙箱**。

插件首先关闭默认插件选择，仅启用经核验的最小内置模型/Agent 配置和 Atlas 插件；上游两个必选的策略/Console 插件仍由 OpenCode 保留。模型可见工具、执行前钩子和权限规则三处均限制为 `atlas_step`。就绪检查核对精确版本、插件集合、插件源码哈希、唯一配置文档和模型/权限/插件/指令/MCP 配置；发现漂移即停止业务调用。

每次运行建立临时认证的 loopback 工具桥，服务端绑定真实任务与 session，项目不能由模型参数更换。工具调用以 session、message 和 call ID 共同标识；已记录的调用恢复时读取其持久回执。回调在继承执行锁和模型版本的上下文中串行执行，停止后拒绝排队的调用，退出前等待正在执行的回调结束。

OpenCode 会话内容保存在持久数据目录中，可能包含发送给模型的业务资料和工具观察。它们不是匿名日志。部署方应按业务数据设置磁盘访问和备份保留策略；本项目没有宣称 OpenCode 原生会话数据库逐字段加密。

## 预算与准确性

每次 OpenCode 发起模型 HTTP 请求前，Atlas 核对实际请求地址和模型与该任务冻结的配置一致，禁止重定向，再检查资料外发批准、模型调用次数、输入量和任务时间，并预留单次最多 3200 输出 token 的额度。已报告的会话输出量可释放前一次预留；没有报告的用量保守保留。额度用完会暂停并保存步骤。服务端预留不是供应商账单金额保证，兼容服务的实际输出限制、供应商内部重试和费用仍需模型网关控制与对账。

OpenCode 负责选择和调用工具，并不保证业务答案正确。数据加工和报告必须通过服务端真实回执验证；公式缓存、语义歧义、OCR 错误、单位和期间、重复映射、汇总行混入、来源权限及缺失资料仍可能要求人工澄清。对话是统一工作入口，预览、核对和必要确认仍有实际作用。

## 验证和未完成事项

- `scripts/verify_quickstart.py --opencode-binary /absolute/path/opencode --evidence-dir /tmp/atlas-check` 使用真实 OpenCode 进程和本项目真实 API/Agent/Worker，模型由显式标记的确定性协议夹具替代。它验证原生多步工具、唯一工具目录、管理员模型选择、数据加工、完整 Excel/Word/PPT 交付和重启恢复，不能证明真实模型智能或任意任务成功率。
- `tests/test_opencode_runtime.py` 验证环境隔离、版本配置保留以及插件/配置漂移拒绝。服务端回调另有模式、参数、重复调用和恢复回归。
- 部署后的真实模型业务验收见 `acceptance/`；必须使用实际提供的模型运行并按标准答案核对，不能用协议夹具成绩代替。
- 当前环境没有 Docker 引擎，未实际构建/运行最终镜像，也未实际运行 arm64 二进制；amd64 二进制与原生进程集成已经执行。浏览器站点接入、真实公司接口、OCR 生产容量、多用户租户隔离、高可用和灾备演练另需部署验收。
- `agent_service/opencode.py` 仍保留旧的无状态生成适配作兼容代码；`deploy/opencode_bootstrap.py` 是旧的独立服务脚本。默认部署不使用它们充当底层 Agent，也不在前台提供“混合引擎”选择。

## 官方来源

- https://opencode.ai/changelog
- https://registry.npmjs.org/@opencode/cli/latest
- https://registry.npmjs.org/@opencode/cli-linux-x64/2.1.2
- https://registry.npmjs.org/@opencode/cli-linux-arm64/2.1.2
- https://opencode.ai/v2/docs/api
- https://opencode.ai/v2/docs/build/plugins/
- https://opencode.ai/v2/docs/permissions/
- https://opencode.ai/v2/docs/plugins/
