# Atlas 0.7 · Railway 云端试用部署

0.7进一步补齐会话目标隔离、停止/继续、发布差异、时效提醒和每日/工作日/每周/小时计划，新增ATLAS_EXPERIENCE_ACCEPTANCE与ATLAS_EXPERIENCE_RESTART_VERIFIED。仍沿用现有域名、访问口令和数据卷。

0.6新增可持续使用的数据应用，页面与处理代码一起版本化，支持预览发布、重跑、对话调整、定时更新和数据问答。运行与部署拓扑沿用现有实例及持久化卷，不增加服务。云端增加ATLAS_APPLICATION_ACCEPTANCE与ATLAS_APPLICATION_RESTART_VERIFIED；本次部署的实际结果以deploy/railway-deployment.json为准。详见DATA-APPLICATIONS.md。

部署现有应用的核心服务：一个实例中 API + Worker，真实采集、Git、dlt、dbt、业务发布和 cron。默认元数据使用同一持久化卷中的 SQLite；可配置外部 PostgreSQL。Dagster 管理界面不在此实例中启动，定时功能使用已有 Worker 调度逻辑。原 Docker Compose 方案仍保留。

## Agent与版本更新

0.5新增项目多会话、聊天/工作模式、页内模型连接、LangGraph多步工具循环及项目统一Git版本/回退。模型密钥加密保存在同一持久化卷，可在界面配置，无需改部署变量。源码包和原件仍不含密钥。原 `/data` 卷、访问口令和域名沿用；新工作区位于 `/data/workspaces`。

新增云端 `ATLAS_WORKSPACE_ACCEPTANCE`/`ATLAS_WORKSPACE_RESTART_VERIFIED` 验证聊天只读、跨会话共享版本、回退保留后续版本、代码导出与持久性；真实模型尚未接入，不以此声称模型智能已验证。

## 对话版更新

正常用户入口改为项目和对话，技术管理面板已移出前台。版本0.4的云端验收在同一实例经项目/消息 API 验证采集、退款筛选、结果确认、定时/暂停和问答/导出，报告写入 deploy/railway-deployment.json。源码包可用 `python scripts/prepare_railway_bundle.py OUTPUT_DIR` 重建；输出包含源码和锁定依赖，不包含用户数据或口令。

## 实际状态

2026-10-15 已升级0.7并部署成功： https://atlas-trial.up.railway.app

项目 Atlas Personal Trial / trial / atlas；实例上限为1 GB内存、1 vCPU、500 MB持久化卷，使用当前账号允许的资源。部署达到 SUCCESS，云端运行验收报告和项目标识见 deploy/railway-deployment.json。真实订单样例经 dlt/dbt 生成2条正式对象、118.1500净金额，运行 UID 10001，Git commit 和审计可追溯；重启后正式结果及审计仍可读。

当前命令行下载受到本地网络限制，因此实际采用已连接 Railway 接口部署 python:3.12-slim 公共镜像，并以带 SHA256 校验的源码包安装同一应用。入口为 deploy/railway_bootstrap.py；requirements-cloud.lock 是同版本约束下的核心运行依赖，显式包含 DuckDB/Parquet 所需依赖，不启动 Dagster 管理界面。源码包配置在服务变量中，不包含原始资料和连接凭证；APP_TOKEN 作为单独服务变量保存，交付源码不含其值。

依赖和程序安装在容器 /opt/atlas 中，不占500 MB数据卷；容器重新创建时按锁定版本重新安装。此分发方式用于本次受限环境部署，后续正式发布可改用提供的 Dockerfile.cloud 构建并锁定镜像 digest。Dockerfile.cloud 本身尚未执行构建。

用户 UID 切换后删除继承的 root HOME，由账户目录解析确定 dbt 用户设置路径；云端已经验证这一修复。浏览器导航验收超时，因此没有宣称视觉/点击验收通过。当前未配置真实大模型，规划和建议采用明确标识的规则模式，OCR扩展未启用。

## 由部署操作创建的资源

- 一项云容器服务；本次采用源码包启动方式，标准源码构建方案可用 Dockerfile.cloud。
- 一个挂载 `/data` 的持久化卷，保存原件、加密 key、metadata.sqlite、任务 Git 和运行产物。
- 一个 Railway 自动域名，通过平台 HTTPS 访问。
- 一个实例；不可将 API 与 Worker 分到不能共享卷的实例，也不要复制多个工作空间写入同一 SQLite/产物目录。

挂载的卷在启动时可用，不能把元数据/密钥初始化放入 pre-deploy；缺少挂载时启动器会拒绝 Railway 环境启动。挂载默认由 root 所有，镜像仅在启动时整理 `/data` 归属，然后降为 UID/GID 10001，再初始化数据库和启动服务。不做 root 权限的业务执行。必须实测容器支持 chown/setuid/setgid；失败直接退出，不静默降级成 root。

## 必要设置

| 配置 | 值/用途 |
|---|---|
| APP_TOKEN | 随机访问口令，至少16字符；通过服务变量设置，不写入源码 |
| DATA_DIR | `/data` |
| APP_TIMEZONE | `Asia/Shanghai` |
| PORT | 使用平台提供的值，入口动态绑定；生成域名时端口匹配 |
| 持久化卷 | 创建并挂载 `/data`，由平台提供 RAILWAY_VOLUME_MOUNT_PATH |
| SCHEDULER_MODE | 启动器设置为 worker |
| ATLAS_DROP_PRIVILEGES | 云镜像设置为1 |
| LLM_BASE_URL / LLM_API_KEY / LLM_MODEL | 可选；未配置时明确采用规则模式，不能说已经接入真实模型 |
| API_ALLOWED_HOSTS | 可选，明确允许的私网 API 域名 |

`railway.toml` 指定 Docker 构建、启动命令、健康检查和失败重启。卷、域名、运行变量及资源限制需要在实际账号中创建；这个文件本身不会创建它们。云端资源和额度需由已连接账号的实际状态确定，未承诺免费长期托管。

## 发布验收顺序

1. 确认账号连接、可用项目及资源额度，创建上述服务、卷和变量。
2. 上传现有源码部署；等待镜像构建和服务健康检查通过。
3. 核对运行 UID 10001、数据路径 `/data` 可写、Worker 心跳健康。
4. 生成 HTTPS 域名并测试鉴权、上传和画像；使用 `orders-with-refunds.csv` 验证筛选、计算、候选预览及发布。
5. 查询正式对象、质量和审计；重启实例后再验证同一结果存在。
6. 验证 cron 入队、失败旧结果保留和导出，返回真实访问 URL 及访问方法。

## 本地复现进程验收

```bash
python scripts/verify_service.py --cloud
```

Linux root 且文件系统支持 chown/setuid 的环境可追加 `--drop-privileges`。本地工作环境不支持 chown UID 10001；实际云端已验证运行 UID 10001。

如需要扫描 PDF/图片/Office，启用 WITH_DOCLING 构建参数和 USE_DOCLING=1，再按真实资料验证模型下载和解析效果。可通过服务变量将 HF_HOME/TORCH_HOME 缓存设置到卷中相应目录。此扩展尚未实际验收。

## 官方部署约定

- Dockerfile：https://docs.railway.com/builds/dockerfiles
- 本地源码部署：https://docs.railway.com/cli/up
- 持久化卷与权限：https://docs.railway.com/volumes
- 构建/启动/健康检查配置：https://docs.railway.com/config-as-code/reference


## 公司内部检索接入

0.9在项目连接窗口配置本人检索 API，不写入源码或部署样例。当前公网实例未连接公司内网和真实模型。公司 API 需要可达路由、允许的运行环境及当前模型授权；可选企业 CA/mTLS 挂载方式与适配协议见 [COMPANY-SEARCH.md](COMPANY-SEARCH.md)。不要通过关闭 TLS 验证或使用高权限公共账号绕过限制。
