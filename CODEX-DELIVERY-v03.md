# Codex v0.3：本机交付与 US-E3 回传

2026-10-24。产品版本仍为 Atlas DataBuddy v0.20.0；v0.3 是本轮实现/评审编号。

## 【本轮改动摘要】

本轮交付目录：`dist/Atlas-DataBuddy-win32-x64-r3/`，双击其中 `Atlas DataBuddy.exe`。本机已启动该产物。用户原有默认工作区保留，选择本地目录会打开该目录自己的项目和资料。

按最新 `DOUBAO-ACCEPTANCE-v0.md` §9，当前阶段为便携桌面包及本地模式；云端 web 迁移不在本轮交付范围。本轮没有部署、升级历史云端实例，也没有把本机默认数据目录标成云端。

| 编号 | 修改文件清单 | 解决的问题 |
| --- | --- | --- |
| FG-1 / US-D1 | `agent_service/agent_context.py`, `agent_service/agent.py`, `agent_service/office.py`, `scripts/verify_desktop_model.py`, `tests/test_bounded_planning_context.py` | 控制重复上下文及预加载工具 schema；Office 参数约束与实际渲染器对齐；真实模型产出可编辑三件套并验证金额、明细及来源。 |
| FG-2 / US-E1 | `agent_service/windows_dirfd.py`, `agent_service/local_workspace.py`, `agent_service/local_storage.py`, `portable_runtime.py`, `agent_service/cloud.py`, `acceptance/offline/verify_backend.py`, `acceptance/offline/verify_local_folder.py`, `acceptance/offline/README.md` | 原生 Windows 文件夹访问与快照/导出；所选文件夹的 .atlas 存储；按文件夹隔离访问凭据、端口、状态。 |
| FG-2 自测发现 | `agent_service/codegen.py`, `tests/test_desktop_numeric_review.py` | 项目来源创建的流水线继承 project_id，防止项目导出漏掉其已发布应用，同时保持大整数及精确金额。 |
| FG-2 回归 | `tests/test_local_workspace.py`, `tests/test_windows_local_workspace.py`, `tests/test_process_control_review.py` | 联接、硬链接、NTFS 特殊路径、同尺寸改写恢复 mtime、启动路径与状态目录拒绝顺序。 |
| US-E3 | `agent_service/static/index.html`, `agent_service/static/app.js`, `agent_service/static/local-data.html`, `agent_service/static/local-data.js`, `agent_service/static/local-data.css`, `desktop/main.cjs`, `desktop/preload.cjs`, `desktop/security.cjs`, `desktop/start-screen.html`, `desktop/start-screen.css`, `desktop/start-screen.js`, `desktop/README.md`, `tests/ui/local-data.cjs`, `tests/ui/local-workspace-context.cjs`, `tests/ui/package.json`, `desktop/tests/security.test.cjs` | 工作模式内的位置选择及当前项目的工作资料；真实选目录与持久化切换；撤销独立入口；认证、项目、工作区切换时取消旧请求并拒绝迟到结果。 |
| OB-1 | `packaging/build-desktop.mjs`, `packaging/payload-policy.mjs`, `desktop/tests/packaging.test.cjs` | 修复误删 dbt 包内 tests 生产宏的打包规则；逐项比对运行库清单；实际包本体重跑验收。 |
| OB-3 | `packaging/hash_python_inventory.py`, `vendor/metadata/python-packages.json` | 170 个 Python 发行包均具版本及可复算的已交付文件清单 SHA256，明确不是上游 wheel 哈希。 |
| 自评扫描 / H2 | `agent_service/browser_reports.py`, `tests/test_workspace_gateway.py`, `verification/windows-residual-audit.md` | 修复 Windows 只读描述符刷新失败；修复测试收集时构造的未来时间戳在长测试期间过期；保留全量失败历史，不把夹具失败或针对性通过改写为全量通过。 |
| OB-2 | 未更改操作系统网络设置 | 用户最新明确“不用断网呀，肯定都是联网的”，取消本轮真实断网实测；保留未测标记，不作为本轮阻塞，不冒称通过。 |

## US-E3：入口位置与切换逻辑

1. 主工作台下方选择“工作”，紧邻显示“工作位置”：本地电脑 / 云端。云端在当前阶段明确显示“后续阶段开放”，不能选择为已就绪工作区。
2. 选择“本地电脑”打开当前项目的“工作模式 · 本地电脑”面板。未绑定目录时只提供“选择本地工作文件夹”，不允许在未绑定的默认目录冒充本地文件夹处理。
3. 原生目录选择器返回真实目录，经确认后停止旧后端，使用 `--workspace-dir` 启动该目录的后端。健康就绪后才加载新的工作台；启动失败保留原选择，可重试返回原工作区或重新选目录。
4. 已绑定后，“工作资料”在当前项目内提供导入、画像、口径、候选核对、人工发布及完整 CSV 导出；原有“本地文件”能力可导入目录内明确选中的文件，并保存项目代码与成果。不会自动上传整个文件夹。
5. 切换目录不自动迁移项目、对话和资料。所选目录的数据在 `.atlas`，可见项目文件在 `Atlas工作区`，追加式成果在 `Atlas成果`。访问凭据、端口、运行状态按目录分别保存于当前 Windows 用户的应用数据目录。
6. 删除右上角及桌面菜单“本地数据处理”独立入口；旧 `local-data.html` 返回主工作台。React Flow 仍只作只读溯源展示，未新增通用编辑器。

## 【实现说明】

### 根因

FG-1 并非模型完全不能生成文件：旧任务把大量完整工具 schema 和重复上下文反复发送，四次调用就耗尽输入预算。随后真实运行又暴露 Office 文件名/schema 没有声明实际限制，模型无法提前知道渲染器契约。现在仅预加载相关工具，保留全部受授权工具名称和按需发现完整 schema；Office 使用实际 Spec 契约及安全校验反馈。没有提高默认预算，也没有自动续跑伪造完成。

FG-2 原实现依赖 POSIX 目录描述符、UID、锁和刷新语义；简单替换分隔符不足以保障快照和导出。Windows 使用锚定目录 HANDLE 相对打开，操作期间持有祖先目录，拒绝重解析点、多硬链接、越界、NTFS 数据流及设备名；成果使用同一 HANDLE 原子发布。原件保持不覆盖，ACL 仅收紧 Atlas 管理的私有目录。Windows 无 POSIX 目录 fsync 等价保证，未声称断电持久性完全一致。

核心片段（`portable_runtime.py`，实际源码节选）：

```python
state = base_state
if workspace is not None:
    identity = hashlib.sha256(os.path.normcase(str(workspace)).encode('utf-8')).hexdigest()[:24]
    state = base_state / 'workspaces' / identity
    assert_plain_path(state)
    state.mkdir(parents=True, exist_ok=True)
    private_directory(state)
else:
    (state/'data').mkdir(exist_ok=True)
with file_guard(state/'desktop.lock', exclusive=True, blocking=False):
    access = credentials(state)
    port = select_port(state, args.port)
```

### 实际验收证据

以下三项均使用最终 r3 的真实应用目录和随包 Python。每项创建独立临时合成资料，不替换应用模块，不读取真实业务资料。访问口令仅在内存 IPC 中使用，不写入报告。

| 对应项 | 实际命令 / 结果 | 原始证据 |
| --- | --- | --- |
| OB-1，US-A1/B1/C1，H1/H2，DV | 包内 `acceptance/offline/verify_backend.py --app-root <r3/resources/app>`；退出 0、ok=true。空状态及不可达模型/假密钥均完成上传→画像→Git/dlt/dbt→候选→人工发布→完整 CSV；2 行、118.150000000000；重启及审计通过。 | `verification/packaged-offline-v03/backend-verification.json` |
| FG-2，US-E1/A3/B3/C1 | 包内 `acceptance/offline/verify_local_folder.py --app-root <r3/resources/app>`；退出 0、ok=true。中文空格目录、两文件快照/幂等、版本文本、两份追加式成果与全部哈希、原件变更保留旧快照、重启、ACL 不变，以及两个文件夹同时运行时令牌/端口/数据库隔离。 | `verification/packaged-local-folder-v03/local-folder-verification.json` |
| FG-1，US-D1 三件套样例 | 随包 Python 运行 `scripts/verify_desktop_model.py --app-root <r3/resources/app> --model-config <私有配置路径>`；退出 0、ok=true、done/verified_complete。真实 DeepSeek 5 次调用、46.56 秒；XLSX/DOCX/PPTX 均含 118.15、来源与所需明细；没有自动发布。默认仍为 18 次调用、360000 输入字符、24000 输出预算。 | `verification/packaged-real-model-office-v03/verification.json` 与其中三份真实 Office 文件 |
| OB-3 / 打包完整性 | 构建时 15006 个运行文件、743300599 字节逐项核验；独立复查 170 个依赖包清单哈希全部重算一致；未发现私密配置文件。 | `verification/r3-independent-runtime-audit.json`、`verification/desktop-build-v03.log` |

真实模型报告中的 `visual_validation=false`、`formula_recalculation=false` 保持原值。本轮是实际文件内容/结构/来源校验，不能据此声称 Office 排版或公式重算验收。

原生窗口实测：打开 r3，观察真实主工作台与 DeepSeek 模型选择；点击工作位置“本地电脑”进入当前项目面板，点击目录选择打开 Windows 原生目录框。随后用户实际选择业务目录并切换，观察工作台端口变化及已选目录文件面板。没有把真实业务文件导入合成测试，也没有把 API 验收写成全流程桌面点击通过。

前端 13 组回归、桌面 9 项回归通过；原生文件/成果业务回归 153 通过、1 项因符号链接权限跳过；额外路径/启动检查 12 通过；模型上下文等 59 通过、Office 31 通过；根扫描相关 136 通过。这些组有重叠，不相加成一个虚构总数。

历史完整 Windows 运行仍记录为 1414 项：1350 通过、54 失败、10 跳过，未重新全量运行。针对性复核已区分真实适配缺陷和 POSIX 文件名、UID、符号链接权限、SystemRoot、编码及旧 SIGTERM 退出协议夹具问题，详见 `verification/windows-residual-audit.md`；不宣布全量全绿。

## 【反驳意见】

- FG-1、FG-2、US-E3、OB-1、OB-3：接受。以真实产物和复算记录补证据。
- OB-2：用户最新明确“不用断网呀，肯定都是联网的”，因此取消本轮真实断网实测，不再作为本轮阻塞。已有应用 local_only 能力及明确提示继续保留；没有物理断网/防火墙通过证据。
- 云端迁移：遵守最新 §9，移到后续阶段。本轮没有把不可用云端按钮或历史线上地址冒充真实已部署能力。
- 无额外产品要求反驳；不修改已撤回的 DV-3/DV-4。

## 【自评扫描】

1. 空输入、超长输入、空文件、缺运行文件、错误令牌与不可达模型已实测；两文件夹并发及前端重复点击/迟到请求有证据。应用离线策略通过；操作系统断网实测由用户取消，未宣称通过。
2. 实际新交付目录无开发 node_modules，空继承 PATH，全部调用随包运行时；不依赖用户配置 Python/Node/Git/WSL。未冒充全新 Windows 虚拟机。
3. 错误路径有诊断日志；缺运行文件及配置错误使用中文提示，不把密钥或原始校验输入回显给用户。报告 JSON 使用 UTF-8。
4. 本轮每处功能改动对应 FG-1、FG-2、US-E3 或验收发现；云端新功能已停止，未新增通用编辑器或自动发布。
5. 已复查金额、大整数、原件/版本、人工发布、目录隔离、授权与打包完整性；全量跨平台测试及 Office 视觉未宣布通过。

交付判断：当前 r3 可以交给验收人及业务试用者验证本机流程；正式放行等待豆包独立复审，不宣称云端产品已交付。OB-2 的本轮实测已由用户取消。尚未达到约定的连续两轮收敛条件，本轮不写“我已无新增改进点”作为终止声明。

## 【给豆包的下一步请求】

请重点复审 r3 包本体的 FG-1 三件套、FG-2 双目录隔离及 US-E3 工作位置/当前项目入口；OB-2 按用户最新要求取消本轮实测，保留未测说明；依据最新 §9，不把后续云端迁移计为本轮缺陷。
