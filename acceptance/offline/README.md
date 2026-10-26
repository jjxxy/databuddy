# 原生 Windows 离线后端验收

在交付包根目录运行（开发与验收工具，不是业务用户启动步骤）：

```powershell
.\runtime\python\python.exe .\acceptance\offline\verify_backend.py --app-root . --evidence-dir .\verification\windows-offline-01
```

`--app-root` 也可指向最终解压包中的 `resources/app`。证据目录必须为新目录或空目录。程序仅使用合成订单资料；状态、数据库、临时模型配置均放在应用目录之外的系统临时目录中，不读取用户的模型配置或业务资料。

检查内容：空状态启动、API/Worker/Agent 就绪、错误令牌拒绝、空和超长输入拒绝、空附件拒绝、项目内本地附件画像、真实 Git/dlt/dbt 加工、发布前不可读取正式结果、人工确认、完整 CSV 精确合计 118.15、审计完整性、停止重启后数据/凭据/端口保留。另用不可达模型和假密钥重复本地加工，验证模型不可用不会阻断工作台；缺少 Git 运行文件的错误路径用独立空目录夹具调用启动校验，不修改真实运行库。

程序以 `--offline`（应用 `local_only` 策略）启动后端，移除继承的开发机 PATH、Python 和模型环境变量。IPC 中的访问口令只保留在内存中，不写入 JSON 证据或诊断日志。

这是原生 Windows 嵌入式运行时和应用离线策略的真实集成验收，**不是**物理断网/操作系统防火墙验收，也**不是**干净 Windows 虚拟机证明。桌面点击、视觉、操作系统通知和真实 AI 推理需分别验收。业务用户无需执行这里的命令。

## 本次实际结果（2026-10-24）

`verification/windows-offline-06/backend-verification.json` 为本机源码组装后的实际运行证据，`ok=true`。空工作空间与不可达模型两种场景均完成真实 Git/dlt/dbt 加工、人工发布和 CSV 导出，得到 2 行、精确合计 `118.150000000000`，两个导出的 SHA256 相同。重启后数据、访问凭据和端口保留，审计验证通过；空/超长输入、空文件及错误令牌均被拒绝。

较早的 `windows-offline-01` 至 `05` 留作失败诊断记录：它们暴露并推动修复了 Windows 临时目录清理顺序、嵌入式 Python 相对路径和 dlt 深层路径长度问题，不是当前通过证据。最终打包产物仍需使用同一命令重新验收，不能把源码组装目录通过等同于完整交付包通过。

## 原生 Windows 本地文件夹模式

```powershell
.\runtime\python\python.exe .\acceptance\offline\verify_local_folder.py --app-root . --evidence-dir .\verification\windows-local-folder-01
```

该夹具创建含中文和空格的临时业务目录，以 `--workspace-dir` 和空的继承 PATH 启动真实服务。检查两文件原始字节快照、摘要和重复请求幂等；拒绝越界、私有配置、NTFS 数据流、设备名及缺失文件；完成离线 Git/dlt/dbt 加工和人工发布；保存两次可版本化文本，再从 HTTP 读取新旧版本；生成两份完整成果目录，逐项核对清单摘要及完整 CSV 的精确合计；修改原始资料后确认旧导入仍可用；重启后核对结果、端口、凭据和已有成果，并比较业务根目录及原始文件 ACL 未改变。

文本写入与检查点由独立进程在主服务停止时调用公开服务方法完成，再通过真实 HTTP 校验；不把它说成 AI 或桌面点击验证。目录联接、硬链接、持有目录时禁止替换、同尺寸修改后恢复 mtime 的竞态、成果发布不覆盖及失败清理不跟随联接，另由 `tests/test_windows_local_workspace.py` 原生测试覆盖。

## 正式来源与候选版本隔离

```powershell
.\runtime\python\python.exe .\acceptance\offline\verify_lineage.py --app-root . --evidence-dir .\verification\windows-lineage-01
```

此夹具通过真实 HTTP 上传三份合成 CSV，执行 Git/dlt/dbt、核对并人工发布原关联来源，再替换候选方案的关联来源并成功构建新候选。检查来源到正式成果的关系仍来自原发布运行的冻结输入，新候选的来源和代码版本保持独立，不自动发布；两份正式明细的精确合计为 30.75，旧关联值为 East/West，新候选为 North/South。还检查未发布候选不出现正式运行 ID、未授权请求拒绝、审计验证与干净退出。

可用 `--app-root` 指向最终交付包 `resources/app`；状态仍在应用目录外的临时目录，继承 PATH 为空，不读取模型配置、不调用 AI。此检查验证图的数据 API 契约，不宣称 React Flow 原生窗口视觉检查、物理断网或干净虚拟机验收。

还会在同一个主状态目录下同时启动第二个已选业务文件夹，确认访问令牌、端口和数据库独立，第二个实例不能看到第一个实例的项目，第一个实例仍能读取已有结果。

Windows 使用无重解析点的原生目录 HANDLE 和相对打开，所有祖先在操作期间禁止改名或删除；写入先刷新文件，再通过同一 HANDLE 原子发布。Windows 没有 POSIX 目录 `fsync` 的对应保证，此验收不宣称两平台断电持久性完全等同。私有 ACL 只用于 Atlas 管理的目录，不重设整个业务文件夹权限。

`verification/windows-local-folder-source-01/local-folder-verification.json` 是 2026-10-24 本机源码组装运行时的实际通过证据，含两个文件夹同时运行的隔离检查。对应范围、测试命令、源码摘要和限制见同目录 `README.md`；最终交付包仍须重新执行夹具。
