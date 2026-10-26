# Skills 与 MCP 能力管理

面向业务用户的入口是右上方“能力”：选择可用技能、安装自己的技能、连接自己的业务系统，或启用管理员预置的连接。用户不需要选择 Agent 实现，也不需要配置模型密钥。模型目录和服务地址仍由管理员配置。

## 已实现的使用边界

- **个人能力按服务端身份隔离。** OIDC 登录的不同用户分别拥有技能和连接；单一 `APP_TOKEN` 对应同一个 `owner`，共享口令不能提供员工级隔离。客户端不能提交 `owner_id` 冒充身份。
- **项目仍是共享范围。** 个人连接配置、凭据不向其他身份展示；通过连接取得的数据、任务记录和成果会进入项目空间并按项目范围共享。需要私有项目或逐成员成果权限时，应先扩展项目 ACL，不能仅依靠个人连接隔离。
- **安装技能不授予额外权限。** Skill 正文和引用文件只是工作指导，作为不可信内容传入已获准的模型；包内脚本不会自动在宿主机执行。计算仍经过 Atlas 已注册的工作流和执行工具，外部写操作仍需要预览、确认。
- **安装 MCP 不等于批准所有工具。** 用户须明确选择只读工具和需要确认的写工具，确认资料保存以及具体模型处理范围。工具名称、Schema、连接修订号或模型目的地改变后，不会沿用旧的待执行写授权。

默认提供 `finance-monthly-close` 财经月结技能，包含附件完整性、期间/币种/粒度核对、明细先汇总再关联、控制总额、成果核对和下月复用的方法。它提供操作方法，不提供财务事实。

## 用户安装 Skills

上传 UTF-8 的 `SKILL.md` 或 ZIP 包。ZIP 中须只有一个 `SKILL.md`，其他参考文件放在同一包目录下。例如：

```text
finance-close/
  SKILL.md
  references/
    business-rules.md
```

`SKILL.md` 可带 YAML 头部：

```markdown
---
name: revenue-review
description: 逐月收入复核与异常解释
version: '1.0'
---
先核对源表合计、币种、期间和最小分析粒度，再生成可复用加工方案。
参考 references/business-rules.md；不能从证据判断的口径需要核实。
```

限制：上传不超过 5 MB，解压后不超过 10 MB、最多 120 个归档条目，`SKILL.md` 不超过 64 KB。不接受路径穿越、链接、设备文件、隐藏文件、重复路径和加密 ZIP。二进制引用会保留，但不能被当作已解析的数据。

安装包按文件 SHA-256 清单形成不可变版本。Agent 先通过 `discover_skills` 找到能力，再用 `load_skill` 按需读取正文、用 `read_skill_file` 分页读取引用。任务保存实际加载的版本、摘要和文件清单；管理员更新预置包不会替换正在运行的任务版本。已经用过旧版预置技能的用户需要重新启用新版本。停用或删除会阻止后续读取。

本版本不承诺兼容要求直接执行宿主机脚本、动态安装依赖或原生开放任意工具的所有第三方 Skill。

## 用户连接 MCP

远程使用 Streamable HTTP 风格 JSON-RPC：实际执行初始化、工具目录读取和工具调用，不只保存展示配置。当前支持协商 `2025-06-18`、`2025-03-26` 协议。需通过部署的外部地址允许策略；云端仅 HTTPS，本地电脑模式允许已明确放行的本机 HTTP 地址。带 URL 用户名、密码、查询参数或片段的地址会被拒绝。

“检查连接”只读取实际工具目录，不会自动批准工具。保存时须填写允许的只读和需确认写入工具。服务端返回的工具注解不能替代用户授予的访问范围，工具的真实行为仍需要系统提供方保障。每次调用重新核对工具 Schema；发生变化则要求重新验证。

凭据与本地 MCP 环境变量加密存储，GET、模型工具目录和前端不会返回其明文。编辑同一远程地址时，留空凭据会保留原凭据；本地连接更新时，省略 `args`、`env` 会保留旧配置，显式传 `[]`、`{}` 会清空。停用后可重新验证并连接，修订号改变会使旧待确认动作失效。

## 本地 stdio MCP

只有本地电脑模式启用 stdio：`ATLAS_LOCAL_MODE=1` 且 `ATLAS_LOCAL_WORKSPACE` 指向存在的工作文件夹。默认内置 `atlas-local-clock`，可在“能力”里启用；它启动真实 Python 子进程，通过 `get_local_time` 返回本机当前时间，不访问网络和业务文件。本机时间取决于系统时钟，不能冒充外部授时依据。

联网本地模式可由本机管理员配置 `ATLAS_STDIO_TEMPLATES`，用户选择已安装的准确程序模板，而非提交任意 shell 命令。示例：

```json
{
  "approved-finance-system": {
    "command": "/opt/atlas-mcp/bin/python",
    "args": ["/opt/atlas-mcp/finance_server.py"],
    "allow_extra_args": false
  }
}
```

程序、Node/Python 运行时和依赖需要管理员实际安装，绝对路径必须在运行 Atlas 的环境内有效；不会隐式下载或运行任意 `npx`/`uvx` 软件包。在 Docker 中选择的是容器内路径。`command` API 字段填写模板别名 `approved-finance-system`。模型不能新增模板。

启动使用 `shell=False`、工作文件夹 cwd 和最小环境，凭据仅以用户明确配置的环境项注入。禁止覆盖 `PATH`、`HOME`、Python/Node 注入项、模型和 Atlas 密钥等系统环境。模板与可识别的固定程序文件摘要发生变化，需重新连接。每次工具会话启动独立进程，采用 stdin/stdout 换行 JSON-RPC，限制消息大小和超时，会话结束终止进程组。stdout 必须只输出协议消息；stderr 不传给模型或用户。

**stdio 进程不是操作系统沙箱。** 联网模式的可信模板可用 Atlas 服务进程的系统权限访问文件和网络，选择工作文件夹不是文件系统隔离。目录、进程与网络隔离需要额外的 OS/容器策略。`ATLAS_NETWORK_MODE=local_only` 时仅允许第一方本机时间模板，拒绝全部自定义模板；这不是对任意第三方进程断网能力的承诺。

## 管理员预置

默认财经技能随部署包内置，无需联网。可选环境变量 `ATLAS_EXTENSION_PRESETS_FILE` 指向管理员只读 JSON 文件。启动时先读取普通文件，完成存储和模型目录初始化后注册预置；文件最多 8 MB、最多 60 个项目。见 `admin-presets.example.json`。敏感文件应由管理员创建并限制读权限，不提交到源码库。

Docker Quickstart 和本地模式提供可选 `compose.presets.yaml`，将宿主机文件只读挂载到 `/run/atlas/presets.json`。先完成正常 bootstrap 并准备真实配置，然后使用：

```bash
export ATLAS_EXTENSION_PRESETS_HOST=/absolute/private/admin-presets.json
docker compose -f compose.quickstart.yaml -f compose.presets.yaml up -d --build
```

本地模式改用 `-f compose.local.yaml -f compose.presets.yaml`，并保留正常本地启动流程设置的 `ATLAS_LOCAL_WORKSPACE_HOST` 等环境。不要把该 overlay 直接用于旧版多服务 `compose.yaml`；后者服务名和启动方式不同，需要自己的管理配置流程。默认不开启此 overlay 时，仍可使用内置财经技能以及 operator API 发布预置。

```json
{
  "skills": [
    {
      "id": "department-close",
      "content": "---\nname: department-close\ndescription: 部门月结口径\nversion: '1'\n---\n核对来源与控制总额。"
    }
  ],
  "mcp": [
    {
      "id": "department-data",
      "configuration": {
        "name": "部门数据查询",
        "transport": "http",
        "url": "https://mcp.example.com/mcp",
        "credential": "Bearer REPLACE_WITH_REAL_CREDENTIAL",
        "read_tools": ["query_report"],
        "write_tools": [],
        "storage_approved": true,
        "model_approved": false,
        "approved_model_ids": []
      }
    }
  ]
}
```

示例域名不是可用连接，须替换真实地址及工具名称；启动注册可用预置不会联系可选远程服务器，用户首次启用时才验证实际目录。上述 `model_approved:false` 明确不允许模型读取该系统资料，须由管理员核对模型目的地后设置 `model_approved:true` 并按需要填写后台模型目录中的 `approved_model_ids`。批准的是当时模型配置的指纹；后台换地址、换模型后需重新核对预置授权。未给出模型 ID 时，批准针对当时默认模型。

相同配置重启不产生新版本。修改预置内容会生成新摘要；已经启用的用户连接保留旧版，用户核对可见的工具范围、模型和存储许可后显式启用新版本。用户可停用预置，不能改写管理员内容或读取托管凭据。预置文件删除条目不会自动删除旧能力或撤销正在执行的任务；停用、轮换凭据或撤销服务端访问仍应由管理员明确完成。管理员也可使用受独立 operator 认证保护的 API 发布技能包、更新 MCP 预置。

## API 与 Agent 接口

所有用户 API 均要求正常登录，operator API 还要求独立管理员认证。请求不接受自选身份。

| 接口 | 用途 |
| --- | --- |
| `GET /api/extensions?project_id=...` | 当前身份的技能、项目连接、管理员预置和本地策略 |
| `POST /api/extensions/skills` | multipart `file`、可选 `name` 安装 |
| `PUT /api/extensions/skills/{id}` | JSON `enabled`、`expected_digest` 启停 |
| `DELETE /api/extensions/skills/{id}?expected_digest=...` | 删除自己的技能 |
| `POST /api/extensions/mcp/probe` | 检查 MCP 目录，不自动授权 |
| `POST /api/projects/{pid}/extensions/mcp` | 创建个人连接 |
| `PUT /api/projects/{pid}/extensions/mcp/{cid}` | 同 ID 编辑或重新连接 |
| `DELETE /api/projects/{pid}/extensions/mcp/{cid}` | 停用并使旧写动作失效 |
| `POST /api/projects/{pid}/extensions/presets/{id}/enable` | 以 `enabled`、`expected_digest` 选择预置版本 |
| `POST /api/operator/extensions/skills` | 管理员上传，可选 `skill_id` 更新 |
| `POST /api/operator/extensions/mcp` | 发布管理员 MCP 预置 |
| `PUT /api/operator/extensions/mcp/{id}` | 更新管理员 MCP 预置 |

MCP 创建/检查请求字段：`name`、`transport`、HTTP 的 `url`/`credential` 或 stdio 的 `command`/`args`/`env`，以及 `read_tools`、`write_tools`、`storage_approved`、`model_approved`、`approved_model_ids`。本地进程还需 `command_approved:true`。

Agent 通过已有 `list_work_connectors`、`list_work_tools`、`read_work_tool` 读取，通过 `prepare_work_action` 产生等待用户确认的写动作。新技能接口为 `discover_skills({query})`、`load_skill({skill_id})`、`read_skill_file({skill_id,path,offset,limit})`。技能加载和 MCP 结果传给模型都受当前模型数据授权约束。

## 部署后验证

1. 上传带 `references/business-rules.md` 的技能 ZIP；让 Agent 发现、加载并引用文件，核对任务记录中的包摘要。
2. 管理员更新已用预置技能：确认旧任务继续固定版本、新任务需要重新启用；停用后不再允许后续读取。
3. 连接真实 MCP，选择一个只读和一个写工具；核对只读结果、写操作确认前无外部变化、确认后只执行一次。
4. 准备写动作后修改或停用连接；确认旧动作无法继续执行。换模型目的地后核对原授权失效。
5. 两个独立 OIDC 身份不能发现、读取、修改对方的个人能力；另行确认项目成果共享符合组织期望。共享 `APP_TOKEN` 不能用来验证员工隔离。
6. 本地启用 `atlas-local-clock`，取得真实系统时间；云模式拒绝 stdio，`local_only` 拒绝自定义模板。可信第三方 MCP 需要在目标电脑验证其真实依赖、权限和业务行为。

自动化用例在 `tests/test_extensions.py`、`tests/test_mcp_stdio.py`，覆盖真实 HTTP 测试服务、真实 stdio 子进程、包文件摘要、身份权限、凭据不回显、写确认、版本变化及超时清理；测试服务不代表已联通实际企业系统。
