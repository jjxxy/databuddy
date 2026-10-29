# Atlas 云端工作模式

本次继续使用现有 OpenCode / Atlas Agent、模型目录、后台编排、项目 Git、Skill / MCP、任务恢复和成果面板，补上可独立部署的执行平面。目标是让用户描述目标后，Agent 能编写代码、真实运行、检查结果、修复并交付文件，而不是只返回代码或计划。

这不是 ChatGPT 内部实现的复制，也不代表已经通过商业产品能力对标、真实财经验收或生产高可用验收。

## 五个组成如何对应源码

| 组成 | 实现 | 本次补齐 |
| --- | --- | --- |
| 大模型 | `provider.py`、`model_catalog.py`，沿用管理员模型目录与任务冻结模型 | 工作环境能力、执行约束与交付说明进入 Agent 上下文 |
| Agent 执行框架 | `agent.py`、`conversation.py`、`orchestrator.py`，沿用原生 OpenCode 或兼容循环 | 命令开始、后台等待、回执回读、失败修复和取消；等待执行时不占一次长模型调用 |
| 隔离工作环境 | 新增 `work_runner/` 独立服务，Docker Engine + gVisor `runsc` | Bash、Python、Node、办公库、离线 Chromium；非 root、无网络、只读根目录、资源限制 |
| 工具与插件 | 原有 Skill / MCP；新增 `work_runtime.py` 工具 | 执行命令、查看文件清单、分页读文本、保存可下载成果；联网工作仍经已批准 MCP |
| 状态与文件 | 原有数据库和项目 Git；执行器独立持久卷 | 稳定执行编号、不可变文件检查点、输入摘要、日志、成果摘要；运行中断后回读同一执行 |

执行与编排分开部署。Atlas API、数据 Worker 和 OpenCode 均不需要 Docker socket；只有专用执行服务持有自己的执行宿主机 Docker socket。任务容器不接收该 socket、模型密钥、数据库连接、应用访问口令或执行器令牌。

## 用户怎么使用

日常入口继续是项目与对话。比如上传资料后说：“帮我处理这几份文件，生成图表和可下载报告，并检查结果。”模型按需发现工具，先把可复用的代码写入项目，再显式传入要执行的文件与本轮附件。运行期间可以关闭页面，任务由后台继续推进；再次打开可查看原来的对话、进度和成果。

“工作设置与工具 → 工作进度”显示执行状态与有界输出，“成果 → 工作成果”显示已保存文件。用户不需要在页面维护命令或基础设施。停止当前工作沿用对话停止按钮，取消请求由后台送到执行器。执行器不可达时取消会等待恢复；不能把本地显示停止理解为远端已经确认停止。

Agent 新增工具：

- `work_environment_status`：查看配置和执行目录；传入页面选中成果的 `artifact_id` 可定位完整执行编号、文件路径与输入版本，支持继续读取和修改。不把配置存在当作部署验收。
- `run_work_command`：在 `/workspace` 执行命令，显式携带 `project_files` 与当前附件 `source_ids`。附件位于 `inputs/<source_id>/<原文件名>`，不隐式发送整个项目。
- `list_work_files` / `read_work_file`：按执行编号回读不可变文件清单或 UTF-8 文本。文本分页单位是字符，二进制不经过模型转录。
- `publish_work_file`：校验版本、大小和 SHA256 后持久保存成果，进入原有成果面板。Office 文件还需结构验证；普通文本不能靠改成 `.xlsx` 冒充办公产物。

每个账号和项目对应一个执行工作区。新命令从上一个完整文件检查点恢复；每条命令使用新的临时容器，命令结束后终止残留进程，再回读和校验文件后保存，容器随后销毁。文件持续存在不等于进程、内存或后台服务器永久运行。项目 Git 回退与执行检查点是不同时间线，执行时应显式传入当前版本代码，不能把旧运行目录当作当前 Git 状态。

## 部署

### 1. 准备独立执行宿主机

需要 Linux、Docker Engine 和 gVisor，按 [gVisor 官方 Docker 指南](https://gvisor.dev/docs/user_guide/quick_start/docker/) 安装 `runsc`。执行器会检查 Docker 的 runtime 和实际容器配置；缺少 `runsc` 时拒绝执行，不降级为普通宿主机子进程。

宿主机的 Docker socket具有高权限。执行服务应放在专门的执行主机或相应隔离基础设施，管理入口只允许 Atlas 控制平面访问。普通容器并不等于微虚拟机，也不能把单项配置检查当作隔离安全审计。

### 2. 构建并固定工作镜像

在仓库根目录执行，将镜像推入你自己的镜像仓：

```bash
docker build -f deploy/work-runner/Dockerfile.workspace -t YOUR_REGISTRY/atlas-workspace:work-v1 .
docker push YOUR_REGISTRY/atlas-workspace:work-v1
```

从镜像仓或 `docker image inspect` 取得真实 RepoDigest，配置 `ATLAS_SANDBOX_IMAGE=YOUR_REGISTRY/atlas-workspace@sha256:<64位摘要>`。执行器只接受固定摘要；不会运行模型指定的镜像、Docker 参数或主机挂载。

镜像提供 Python、Node、Chromium、LibreOffice、中文字体与数据/办公库。依赖安装发生在构建阶段；运行时默认没有网络。定制依赖应重新构建和批准镜像，不能让模型在应用主机安装软件。

### 3. 启动执行服务

在执行主机准备仅管理员可读的环境文件，包含单独生成的至少 32 字符随机令牌和镜像摘要：

```dotenv
ATLAS_WORK_RUNNER_TOKEN=<独立随机令牌>
ATLAS_SANDBOX_IMAGE=YOUR_REGISTRY/atlas-workspace@sha256:<真实摘要>
ATLAS_RUNNER_STORAGE_MB=4096
```

```bash
docker compose --env-file /secure/runner.env -f deploy/work-runner/compose.yaml up -d --build
```

服务只绑定执行宿主机回环端口 8090。通过你管理的 HTTPS 反向代理或安全隧道提供给 Atlas；禁止直接公开 Docker socket 或未加密执行接口。反向代理需允许不超过 92 MiB 的请求体。工作卷应独立备份并按组织规则加密；Atlas 原有数据卷备份不包含执行主机的检查点。

### 4. 连接 Atlas

在 Atlas 部署 `.env` 中加入以下配置，沿用原有 `llm.json` 模型目录，再重启后台：

```dotenv
ATLAS_WORK_RUNNER_URL=https://executor.example.com
ATLAS_WORK_RUNNER_TOKEN=<与执行器一致的独立令牌>
```

私网地址还需在现有 `API_ALLOWED_HOSTS` 允许列表中显式批准。仅专用私网调试才设置 `ATLAS_WORK_RUNNER_ALLOW_HTTP=1`；此例外不允许明文公网地址。模型需获准读取项目内容，且原有公司资料授权仍有效，才能发送和读取执行文件。

本次补上了源码仓库原先缺失的 `Dockerfile.quickstart`，可沿用 `compose.quickstart.yaml` 与原启动脚本。新增执行服务不是启动原应用时自动创建的；需要单独部署，不能仅填写三个环境变量就认为机器已经就绪。

## 执行与恢复边界

- 单命令最长 900 秒，默认 120 秒；每容器 1 GiB 内存、1 CPU、128 个 PID，无额外 swap，工作目录与临时目录各 96 MiB tmpfs。工作检查点最多 64 MiB、单文件 25 MiB、1000 个文件。真实宿主机容量和 gVisor 兼容性需实际验收。
- 最多 4 个运行容器、16 个活动/排队请求；同一执行工作区串行。默认检查点磁盘配额 4096 MiB，达到后拒绝新任务，需管理员维护或调整；不自动删除历史证据。
- 模型请求提交前先保存执行编号与请求摘要。丢失 HTTP 响应后回读同一编号；同编号不同请求被拒绝。执行启动结果不确定时记录 `unknown`，不会自动重放。
- 执行器重启后按 Docker exec 编号恢复检查；准备/启动期间存在无法确认的窗口，会保守停止容器并标记 `unknown`。崩溃恢复不承诺所有命令恰好执行一次。
- 文件通过容器内固定传输程序与 Docker exec 双向流传送，不使用无法读取 gVisor tmpfs 的宿主机 `docker cp`。输出只接受有预算的普通文件，拒绝链接、设备、FIFO、越界路径和冲突路径；不在主机解压客体归档。检查点失败保留前一完整版本。不同账号/项目不能读取彼此的执行编号或成果。
- 基础应用仍沿用共同 owner 工作空间权限，不因此变成完整多租户 SaaS；不同账号的执行环境隔离不等于项目数据访问已经全面隔离。独立账号部署仍参考 `deploy/isolated/README.md`。

## 浏览器与业务系统

工作镜像的 Chromium 可对本地生成 HTML 做截图或离线检查，例如：

```bash
chromium --headless --no-sandbox --disable-dev-shm-usage --screenshot=/workspace/report.png --window-size=1440,1000 file:///workspace/report.html
```

Chromium 内层 sandbox 标志不取代外层 gVisor；容器仍无网络。公开检索与正文读取可按 [GENERAL-WORK.md](GENERAL-WORK.md) 开启原生研究工具；登录网站或操作业务系统时，复用已批准的 MCP 连接和原有外部写入确认。此版本没有内置任意网站登录或通用云桌面服务，不能声称已经拥有用户电脑的浏览器会话。

成果面板对网页提供安全文本视图，图片提供实际预览，Office 提供现有内容提取视图。运行中的 Web 端口、交互式 Notebook 和任意脚本网页预览不在本版开放范围；可以在隔离环境生成截图再交付。

后续增强现已接入 `render_work_preview`：可为 HTML / PDF / Office 成果生成按版本绑定的页图，并直接在原成果中查看；启用视觉模型后可 `inspect_visual_artifact` 检查真实图像。公开网页的原生搜索、正文读取、引用、二进制成果续作和配置见 [GENERAL-WORK.md](GENERAL-WORK.md)。登录网站和通用浏览器操作仍需获准的外部工具，不能用正文读取替代。

## 财经可靠性

隔离执行成功只证明程序运行和文件检查点成功，不能证明计算口径、来源完整性、识别准确率或数字正确。通用脚本成果始终标记 `needs_review` / `financial_approval=false`，不注册为已批准指标，不替代现有来源复核、语义、数据契约与正式发布流程。原有财经保护不能通过“生成了一个 Excel”绕过。

## 验证

本次回归结果、基线已有失败与尚未验收项见 [VALIDATION-WORK-MODE.md](VALIDATION-WORK-MODE.md)。

自动回归：

```bash
python -m pytest tests/test_work_runtime.py -q
node tests/ui/work-runtime.cjs
```

这些测试使用明确标记的 Docker 协议夹具，覆盖后台等待、丢失响应、重启回读、取消、失败修复、摘要、账号范围、归档恶意条目和 Office 伪造；不证明真实 gVisor 隔离。

部署真实执行服务后，再运行：

```bash
python scripts/verify_work_mode.py --output work-mode-verification.json
```

此脚本需要通过环境变量提供真实执行服务地址和令牌，实际运行非 root、只读根目录、禁止外连、无凭据/无 socket、精确 Excel 文本、文件摘要、检查点续作和取消探针。不调用大模型，也不提供模型能力评分。

本次开发环境没有 Docker daemon / gVisor，不能把协议测试写成镜像构建或真实隔离验收通过；也未接入真实模型、公司业务系统或生产 HA 集群。实际运行此验证脚本、真实模型任务集和业务验收后，才能进一步评估是否适合正式工作。
