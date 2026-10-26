# 开发者本机部署（WSL，无需 Docker）

此文档保留开发者环境的操作方式，**不是业务用户零环境配置交付入口**。
业务用户使用随包 Python/Git/OpenCode 的 Electron 桌面交付包，不需要 WSL、Docker 或系统 PATH。
旧 `run-local.sh` 显式使用 `ATLAS_OPENCODE_RUNTIME=0`；它是联网安装依赖的开发脚本，不满足离线首启验收。

此入口运行 Atlas DataBuddy v0.20.0 的 API、worker 和 agent 三个独立进程。仅监听
`127.0.0.1`，浏览器访问 `http://127.0.0.1:8000`。Windows 通过 WSL localhost
转发访问 Linux 服务。程序不会自动安装依赖或修改 Windows 防火墙。

## 已配置环境及首次安装条件

- WSL2 的 `Ubuntu-24.04`，发行版默认用户须为普通用户。
- Ubuntu Python 3.12、`python3-venv`，及项目完整源码。
- 虚拟环境 `~/.venvs/atlas-databuddy-020` 已安装 `requirements.lock` 中的依赖。
- 使用默认 AI 引擎时，需要 Linux OpenCode **2.1.2**，默认路径为
  `~/.local/bin/opencode-atlas-2.1.2`。其他版本会明确报错。
- `llm.json` 填写真实上游地址、模型和密钥；启动前校验，密钥不作为命令行参数。

如需重建 Python 环境，在 Ubuntu 中运行：

```sh
python3 -m venv ~/.venvs/atlas-databuddy-020
~/.venvs/atlas-databuddy-020/bin/python -m pip install -r requirements.lock
```

将当前目录切换到源码所在位置后执行上述安装。OpenCode 需另外安装受信任的
Linux 2.1.2 发布版本；本入口不联网下载或自动升级运行文件。

## 启动和日常操作

Windows 在本文件所在目录打开 PowerShell：

```powershell
.\start-native.cmd start
.\start-native.cmd status
.\start-native.cmd open
.\start-native.cmd token
.\start-native.cmd logs
.\start-native.cmd stop
```

双击 `start-native.cmd` 也会启动并打开页面。首次进入页面时，使用 `token`
命令显示的工作台访问口令登录。口令仅在明确执行该命令时输出；URL 不含凭证。
另有独立后台管理员口令保存在本机状态文件中，启动脚本不会输出它。
`status` 验证 API 和本次实例的 worker、agent 心跳，不能证明模型实际回答成功。

可选参数（Windows PowerShell 参数格式）：

```powershell
.\start-native.cmd start -Port 8001 -NoOpen
.\start-native.cmd start -ModelConfig 'C:\private\llm.json' -OpenCodeBinary '/home/001/.local/bin/opencode-atlas-2.1.2'
```

`-Distro` 可选择其他 WSL 发行版，`-Python` 可选择 Linux 虚拟环境解释器。
`-StateDir` 可选择 Linux 状态目录，后续 stop/status/token 等操作必须使用同一目录。
更换端口、配置文件或引擎前先 stop 再 start；重复 start 不会偷偷替换运行中的配置。

Linux/WSL 直接调用：

```sh
python3 native_local.py start --no-open
python3 native_local.py status
python3 native_local.py stop
```

## 模型与兼容引擎

存在有效 `llm.json` 或先前已保存的模型配置时，默认使用 OpenCode 2.1.2。缺少运行文件或配置错误会明确
停止启动；上游请求失败不会偷偷切换到模拟模型或其他引擎。启动时会加载并验证
模型目录文件，文件内容变更会验证并更新模型目录；后续模型维护使用应用后台管理员流程。

如果 `llm.json` 不存在且没有已保存的模型配置，则允许启动 UI 与数据后台，输出 `MODEL UNCONFIGURED`
提示，AI 任务需配置真实模型。此时使用 Atlas 原有兼容引擎（`ATLAS_OPENCODE_RUNTIME=0`），
没有模拟模型。存在占位内容或无效内容的文件会报错，不能视作未配置。
要明确使用原有兼容引擎，可执行 `start -Engine compatibility`，或 Linux 参数
`--engine compatibility`。此选项同样使用真实模型，没有上游失败后的隐藏降级。

独立隔离脚本执行器未在此入口中配置；应用会明确提示相应能力尚未可用。
本地单实例 SQLite 部署不提供多实例高可用保证。

## 数据、日志和停止

Linux 默认状态目录是 `~/.local/share/atlas-databuddy-020`，而非 Windows 挂载盘：

- `data/`：SQLite、业务文件及加密所需 `connector.key`。
- `credentials.json`：随机生成且持久保存的两套独立口令，权限 0600。
- `process.json`：实例 PID、内核启动时间与启动标识，避免停止复用该 PID 的无关进程。
- `atlas.log`：服务日志；每次启动时超过 8 MiB 的旧日志保留为 `atlas.previous.log`。

目录权限为 0700。stop 让 supervisor 正常关闭全部子服务，保留数据与口令。
启动超时也会尝试正常停止。停止超时会报错，不会按端口或进程名强杀其他程序。
备份前先 stop，并完整备份状态目录及私有 `llm.json`；不要单独复制数据库而遗漏密钥。
WSL 关闭或 Windows 重启后需重新 start；本入口没有注册开机服务。
