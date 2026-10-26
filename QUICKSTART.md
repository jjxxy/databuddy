> **v0.20.0 更新**：本包延续 v0.19.0，新增独立工作空间网关/部署工具与委派任务撤权保护。先看 [本版交付与验收边界](RELEASE-v0.20.0.md)。原单实例启动方式仍保留；独立账号隔离需另按 [独立部署指南](deploy/isolated/README.md) 配置，不能把多个账号直接加入原共享空间当作隔离。

# Atlas DataBuddy v0.19.0 部署与验收

用于一个人或共同授权团队的独立业务空间。默认共享口令代表同一位使用者；成员共享实例内项目。配置 OIDC 时个人 Skill/MCP/偏好按身份隔离，项目资料与结果仍共享，不是集中多租户服务。默认只监听本机。

## 启动

1. 安装并启动 Docker Desktop（Windows 使用 Linux 容器），或 Docker Engine 与 Compose v2。
2. 解压部署包，用文本编辑器打开已经预置的 `llm.json`。将模型的 `base_url`、`model`、`api_key` 三项占位值替换为实际值。
3. Windows 双击 `start.cmd`；Linux/macOS 在解压目录执行 `bash start.sh`。
4. 健康检查通过后，终端显示地址和工作空间访问口令，浏览器自动打开。默认 [http://localhost:8000](http://localhost:8000)。

无需本机 Python、独立 OpenCode 安装或业务用户配置模型。首次下载、镜像构建和模型访问需要网络，调用模型可能收费。需要支持原生函数调用的 OpenAI 兼容接口；普通聊天网页地址不能作为接口。

```json
{
  "default_model_id": "finance",
  "models": [
    {
      "id": "finance",
      "name": "财经分析",
      "base_url": "https://your-llm.example.com/v1",
      "model": "YOUR_MODEL",
      "api_key": "YOUR_API_KEY",
      "share_data": true
    }
  ]
}
```

`name` 是用户看到的名称，`id` 是稳定标识。增加模型时参考 `llm.multi.example.json`，在 `models` 中添加条目，最多20个；每个模型均须通过连接与原生工具调用验证后，整份目录才生效。默认模型必须存在于目录。不需要提供引擎选项。

`share_data: true` 表示管理员允许任务需要的业务内容发送给该模型服务。省略时默认 `false`，会限制模型读取内容；对话文本和必要上下文仍然发送，并非离线。模型数据保留政策、地区、密钥权限与费用由所选服务决定。

本地模型无需密钥时填写 `unused`。`localhost` 在容器中自动改写为 `host.docker.internal`，但服务本身仍须允许容器访问；Linux 上仅监听宿主机 `127.0.0.1` 的服务可能不可达。完整接口地址末尾的 `/chat/completions` 会自动移除。

## 本地电脑模式与扩展能力

需要在自己的电脑选择业务文件夹时，Windows 双击 `start-local.cmd`；Linux/macOS 执行 `bash start-local.sh up --choose-folder`，也可用 `--folder "/你的业务目录"`。文件夹中保留原件，`Atlas工作区` 保存持续工作的代码，`Atlas成果` 保存核对后的导出快照，`.atlas` 保存运行状态。操作和数据流见 [LOCAL-MODE.md](LOCAL-MODE.md)。

聊天附件支持一次选择或拖入多份文件（最多20份），逐份显示状态；有失败时先重试或移除，再提交。

“能力与记忆”可安装个人 Skill、配置 MCP、启用管理员预置能力，以及查看、修改、忘记记忆和压缩上下文。记忆的范围与压缩方式见 [MEMORY-AND-CONTEXT.md](MEMORY-AND-CONTEXT.md)。管理员预置与权限说明见 [EXTENSIONS.md](EXTENSIONS.md)；新增验收见 [工作空间能力验收](acceptance/finance_monthly/V18-WORKSPACE-ACCEPTANCE.md)。

本地运行仍可调用云端模型。本地模型、镜像和依赖均已准备好时才具备断网运行条件；`--offline` 仅启用应用的“仅本地连接”策略，不是操作系统防火墙，不能把第三方 MCP 当作受隔离程序。

## 项目、对话与成果

左侧只显示项目及所属对话，行内“⋯”支持重命名、移到回收站；左下“工作设置与工具 → 回收站”可恢复。恢复不会重新执行已取消的任务或自动启用原调度。右上“成果”查看表格的完整分页、报告正文、办公文件内容，下载原件或带入对话继续修改；详情见[成果说明](WORKSPACE-ARTIFACTS.md)。

可在对话中要求内置专长Agent协同复核。外部Agent需先由管理员或用户建立获准连接；Atlas被调用时使用管理员签发的独立机器凭据，不能提供工作台口令或模型密钥。说明见[内置团队](AGENT-TEAMS.md)、[外部协作](AGENT-COLLABORATION.md)、[入站网关](AGENT-GATEWAY.md)。

## 管理员与业务用户

业务用户只有模型名称选择器，没有公司发文、接口、密钥或引擎配置。每个任务在提交时冻结模型和配置版本，重试和“继续”沿用原任务；切换下拉选项只影响新任务，不会切换正在运行的任务。后台停用的模型不会被偷偷替换为其他模型。

默认目录配置由管理员在 `llm.json` 维护。修改后运行 `down` 再运行 `up`，新配置经验证后发布为新版本；验证失败不会替换已保存的目录，但本次启动会失败，需要修正文件后启动。旧目录和旧任务的冻结配置在持久存储中加密保留。

旧版单模型 JSON 仍支持首次初始化，但保留原先“不覆盖已保存连接”的迁移行为。需要通过文件持续管理时，改为本文的 `models` 目录格式。旧模型/API配置入口不能隐式覆盖托管目录。

`.env` 自动生成两个独立凭据：`APP_TOKEN` 为工作空间口令；`ATLAS_OPERATOR_TOKEN` 仅供管理员调用后台配置接口。网页登录不需要、也不显示后台口令。后台配置 API 同时要求 `Authorization: Bearer <APP_TOKEN>` 和 `X-Atlas-Operator-Token: <ATLAS_OPERATOR_TOKEN>`。不配置后台口令时配置接口关闭；不要把两种口令设为相同值。

公司发文、工作系统等接入由管理员按 [COMPANY-SEARCH.md](COMPANY-SEARCH.md) 和 [ENTERPRISE-WORK.md](ENTERPRISE-WORK.md) 配置。用户仍可通过对话检索已接入资料、查看引用和核对操作。对内部资料的模型发送授权单独核对，不因新加模型而自动扩大。

## 自行验证

本轮新增[C01–C27协作与界面验收](acceptance/finance_monthly/V19-AGENT-ACCEPTANCE.md)，与原财经验收共同使用。

打开 [完整财经场景验收手册](acceptance/finance_monthly/README.md)。先只上传其 `inputs` 内指定资料；标准答案留给人工核对，不要上传给 Agent。

按手册依次完成多页签底稿识别、业务口径确认、办事处汇总、预算差异、Excel/Word/PPT交付、下月刷新、调整口径、异常阻断及重启复用。需要核对下载文件的真实数值，不能只看对话里说“完成”。自动校验器及人工标准答案均在该目录内。

本包的合成数据和程序测试不能替代你所用模型、公司底稿、授权网站与部署环境的实际验收。具体运行记录见 `VALIDATION-v0.19.0.json`；生产资格检查说明见 [PRODUCTION-RELEASE.md](PRODUCTION-RELEASE.md)。

## 日常操作

Windows：`.\start.cmd status`、`.\start.cmd logs`、`.\start.cmd down`、`.\start.cmd up`、`.\start.cmd open`。

Linux/macOS：`bash start.sh status`、`bash start.sh logs`、`bash start.sh down`、`bash start.sh up`、`bash start.sh open`。追加 `--no-open` 可禁止自动打开浏览器。

不带动作默认 `up`。`down` 保留数据卷。不要使用 `down -v` 或删除数据卷；升级保留 `.env`、填好的 `llm.json` 与数据卷，沿用原目录及同一 Compose 项目。换目录启动不能视为自动数据迁移。

端口占用时在 `.env` 改 `ATLAS_HTTP_PORT`，保留口令。构建结束后健康等待最多180秒，镜像下载和构建另计；超时用 `status`、`logs` 核对。配置格式通过不等于模型连通，也不等于业务正确。

需要备份时见 [ENTERPRISE-WORK.md](ENTERPRISE-WORK.md)。原件、结果、模型配置、加密密钥与数据库需要一致备份；Git版本不能代替备份。不要分享填好后的 `llm.json`、`.env` 或未经脱敏的运行目录。

## 默认范围

默认提供受控 SQL 和类型明确的数据工作流。任意 Python 代码运行需要另外配置独立隔离执行服务；默认不会放开 OpenCode 的原生宿主机 shell。扫描件/OCR、特定网站浏览器导出、Excel公式重算和公司专用连接不是仅填一个 LLM 就自动可用的能力。默认上传25MB、来源100000行，可按资源调整，但不等于无限数据处理。

旧多容器 `compose.yaml` 与 quickstart 是两套部署，数据不会自动迁移。旧 Compose 和企业 Kubernetes 清单覆盖各角色启动命令，显式保留 `ATLAS_OPENCODE_RUNTIME=0` 的 Atlas 工具循环兼容路径，尚未集成 OpenCode 原生多副本服务；本版 OpenCode Agent 使用默认 quickstart。默认包不公开 Dagster 或 OpenCode 管理端口。尚未验证的 Docker Desktop/Windows/真实模型限制以本版验证记录为准。
