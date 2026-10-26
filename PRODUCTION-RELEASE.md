# 正式部署资格闸门

`python -m agent_service.production_release --check` 检查当前部署是否有资格进入正式发布流程。未通过返回非零退出码。它不改变应用启动、开发模式或现有 `single` / `ha` preflight，也不自动启用部署。

默认运行（不加 `--check`）仅输出 JSON 状态，退出码为 0；自动化发布必须使用 `--check`。`--fingerprint` 输出实际运行目录的 build 指纹；`--case-manifest` 输出固定验收任务、观测值和当前 case 集 hash。没有创建证据或人工勾选通过的命令。

## 检查与边界

- 实际 build：直接读取 `agent_service` 的源码、静态资源、实际存在的依赖清单/锁文件、部署入口，结合 Python 版本与实际安装包版本清单求 hash。至少存在一个依赖 lock。`BUILD-INFO.json`、文档、测试结果、历史验收文件及运维证据不参与指纹；这是本机测量，不是容器镜像或供应链远程认证。
- 实际健康：数据库/存储 roundtrip，加 `api`、`agent`、`worker` 三角色新鲜心跳；每个活跃节点必须与当前 build 一致。`single` 每角色至少一个，`ha` 每角色至少两个不同实例。心跳在进程首次测量后固定 build，修改运行文件必须重启角色进程。无法测量指纹不影响普通心跳，但无法取得正式资格。
- 身份架构：明确为 `owner_workspace`。被允许的 owner 身份共享整个工作空间，系统未实现多租户隔离。SSO 配置检查与实际 SSO 验收是两项不同要求。
- 业务验收：固定财经、浏览器报告、备份恢复 case 集必须完整；部署要求、SSO、HA、已启用的独立 Python runner 会增加必需项。不能用低层连通性、测试夹具、旧 build 或旧 case 集代替真实验收。
- 所有失败、缺失、模拟、未执行、未知字段/状态、过期或 hash 不匹配都会 `blocked`。证据最长有效七天；任何运行源码、已安装依赖、相关部署配置或 case 定义变化都要求重新验收。

## 运维证据存放

固定入口为 **`DATA_DIR/operator-evidence`**，不是 Agent 可写的项目文件路径。运维必须将独立证据卷**只读挂载到该目录**，应用进程不能获得卸载/重挂载权限。验证器同时检查 Linux mountinfo 和实际文件系统只读标志；普通可写目录或仅 `chmod` 不被视为信任边界，其他平台若无法确认也会阻断正式资格。

这是运维供给的信任域，不等于独立第三方认证。验证器校验格式、完整性、同 build/配置/case 集和部署侧模型调用回执；无法独立证明远端模型身份、浏览器测试的全部过程、运维执行器诚实性或平台隔离部署质量。不得把 `eligible` 描述为外部安全认证、金融业务准确性保证或 uptime SLO 证明。

应用没有上传、生成、编辑或批准该目录证据的 API/Agent 工具。请由受信任的外部验收 runner 在独立目录执行并保存记录，然后由运维将完整证据集只读挂载。不要把本文示例、单元测试夹具、历史 `VALIDATION*.json` 或模拟协议结果复制成正式证据。

目录结构：

```text
operator-evidence/
  policy.json
  acceptance.json
  artifacts/
    <每个case独立执行记录>.json
```

不接受符号链接或硬链接工件。单个 JSON 限制 2MB，重复字段及非有限 JSON 数值会被拒绝。原始浏览器 trace、下载文件、office 文件、备份恢复日志、负载报告和脱敏网关结果还应由运维按保留策略保存；case 记录中的标识用于定位这些材料。

## 严格输入格式

以下仅说明格式；`<...>` 必须由实际运行测量替换，不是可通过的证据。

环境必须明确 `ATLAS_DEPLOYMENT_ID`、`ATLAS_PUBLIC_ORIGIN`（无凭证的 HTTPS origin）、部署 profile 与认证配置。`policy.json` 只能声明要求，不能声明验收完成：

```json
{
  "schema_version": 1,
  "deployment_id": "<与ATLAS_DEPLOYMENT_ID一致>",
  "origin": "https://<正式部署域名>",
  "identity_architecture": "owner_workspace",
  "profile": "single",
  "require_capacity": true,
  "require_sso": true,
  "capacity_concurrency": 20,
  "capacity_duration_seconds": 300
}
```

布尔值必须是真正 JSON 布尔，容量参数必须为正整数。HA 即使填写 `require_capacity: false` 也必须验收容量与实际进程故障恢复；已启用 OIDC 即使填写 `require_sso: false` 也必须验收 SSO。SSO 需求不能由 token 模式满足。

`acceptance.json` 精确字段如下；时间必须含时区，case 执行区间必须位于 campaign 区间，完成时间不能在未来超过五秒，到期时间必须晚于完成且不超过七天：

```json
{
  "schema_version": 1,
  "deployment_id": "<部署ID>",
  "campaign_id": "<本次完整验收ID>",
  "build_fingerprint": "<--fingerprint的sha256>",
  "configuration_fingerprint": "<当前资格检查返回的配置sha256>",
  "case_set_sha256": "<--case-manifest的case_set_sha256>",
  "started_at": "<ISO8601 UTC>",
  "completed_at": "<ISO8601 UTC>",
  "expires_at": "<ISO8601 UTC>",
  "execution_mode": "real",
  "simulated": false,
  "operator": "<运维身份标识，不是认证凭据>",
  "runner": "<实际验收执行器及版本>",
  "cases": [
    {"id": "model_finance/decimal_money", "artifact": "artifacts/decimal-money.json", "sha256": "<该文件字节sha256>"}
  ]
}
```

`cases` 必须恰好包含当前部署所需每个 case 一次。上方仅展示一个 entry，并不完整。每个工件也是严格 JSON：

```json
{
  "schema_version": 1,
  "id": "model_finance/decimal_money",
  "run_id": "<真实执行ID>",
  "deployment_id": "<同manifest>",
  "campaign_id": "<同manifest>",
  "build_fingerprint": "<同manifest>",
  "configuration_fingerprint": "<同manifest>",
  "case_set_sha256": "<同manifest>",
  "execution_mode": "real",
  "simulated": false,
  "started_at": "<本case开始时间>",
  "completed_at": "<本case完成时间>",
  "status": "passed",
  "observations": {"net_amount": "118.15", "record_count": 2},
  "measurements": {"model": "<当前配置模型>", "model_calls": 1, "task_id": "<部署内实际turn ID>"}
}
```

`observations` 的字段、类型和值必须逐项符合 `--case-manifest`，不能增加自行判断的 `verified` 标记。金额用字符串，不接受浮点或数值替代。每个财经 case 的 `task_id` 还必须对应当前数据库内 `done` / `verified_complete` 且有实际模型调用计数的任务，并且具有同模型、case 时间范围内的成功 `model_event`；仅配置模型或填写正数不够。

各 suite 的 `measurements` 精确字段：

| suite | 必需测量 |
| --- | --- |
| `model_finance` | `model`、正整数 `model_calls`、部署侧 `task_id` |
| `browser_reports` | `browser_engine`（chromium/firefox/webkit）、`browser_version`、正整数 `navigation_count` |
| `backup_restore` | `backup_id`、`restore_id`、相等的64位小写 `source_snapshot_sha256` 与 `restored_snapshot_sha256` |
| `capacity` | 正整数 `observed_concurrency`、`duration_seconds`、`requests`；并发/持续时间必须达到 policy 要求 |
| `sso` | `issuer`、`audience` 必须匹配当前配置，另有 `gateway_run_id`；HTTP 状态见固定观测值 |
| `ha_recovery` | `disruption_id`、`recovered_task_id`、`recovery_seconds`（大于0且不超过300） |
| `python_isolation` | `execution_id`、匹配配置的 `isolation`（microvm/gvisor）、`network: "none"` |

代码测试只验证闸门行为和失败关闭逻辑，不是正式模型、浏览器、备份恢复、负载、SSO 或 HA 业务验收。当前环境没有运维只读证据挂载或完整真实业务证据时，正确结果是 `blocked`，同时应用仍可按既有能力运行。
