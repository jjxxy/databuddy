# v1.2：当前修补验证与提交（2026-10-27）

状态：本轮修补的完整代码回归与 r20 包本体验证通过；G1/G4 等完整财经交付门槛仍未通过，不声明生产全量放行。

## 【本轮改动摘要】

版本 **v1.2**，候选 **Atlas DataBuddy v0.20.0 / r20**。本轮恢复的是现有工作稿验证与必要修复，没有实施新的报告设计器、离线模型或云端迁移；没有启动新的豆包评审。

| 对应问题 | 改动文件 | 用户可见行为 |
| --- | --- | --- |
| CAP-1 财经精度 | `analysis.py`、`data_workflows.py`、`rules.py`、`codegen.py`、`runner.py`、`project_data.py` | 统一受控 SQL 精度规则，拒绝除法/AVG/浮点 CAST 等中间误差；堵历史 FLOAT 的 CAST、CTE、STRUCT、Bracket 和汇总旁路；嵌套 Decimal、大整数无损传给浏览器；筛选阈值生成固定小数；新旧 legacy divide 均明确阻断并引导精确工作流。 |
| CAP-2/3 Office 数值和缓存 | `office.py` | 从 JSON token 保留 Decimal；非有限值拒绝，超 Excel 安全精度按 text_exact 保留并披露；已登记文件重核原摘要/规格/结构，损坏文件不能重签；大表精度说明无损区间编码并在发布前校验清单预算。 |
| CAP-4 停止与竞态 | `conversation.py`、`data_tools.py`、`job_authority.py`、`jobs.py`、`project_reports.py` | 按可信来源取消本任务异步作业，借用其他会话任务不误停；入队、停止、最终提交与状态回填有相同任务边界，取消后不能复活或生成新候选。 |
| WIN-1 本地报告长路径导出 | `windows_dirfd.py` | 验证中的真实集成场景暴露报告目标超过 Windows 传统路径长度后无法复制；采用已有持有目录句柄的扩展路径，保持摘要和原子发布校验。 |
| SEC-1 预置配置文件边界 | `preset_configuration.py` | 复用已存在的系统安全文件句柄，在 Windows 拒绝链接/重解析路径及硬链接，保留有界读取和错误脱敏。 |
| CAP-6 默认 Windows 编码 | `business_reports.py`、`applications.py`、`execution.py`、`releases.py`、`tasks.py`、`api.py`、`maintenance.py`；`codegen.py`/`runner.py` 同时处理 | 已知应用生成物统一 UTF-8 写读，Git/CLI 输出显式编码，默认 CP936 下中文流程和备份恢复不依赖用户配置；未强制改变用户导入文件编码。 |

以上为相对最后已验证 r17 的 **21 个业务源文件**。回归包含 SQL 数值中间值、三个执行入口、Office 完整性、停止竞态、默认编码与恢复；原失败证据保留，不能把多个重叠测试组相加当总覆盖数。

## 【实现说明】

根因不是最终结果缺一次 CAST。DuckDB 某些除法、AVG 或隐式浮点提升先丢精度，再转 DECIMAL 已无法恢复；旧生成器的科学计数阈值也会改变比较结果。现在在执行前拒绝危险路径，并验证历史列/汇总来源；现有 Decimal `compute`/`aggregate` 是真实可用替代。

**兼容性边界**：旧式加工入口的 divide 已明确停用，新建和旧冻结版本重跑都会给出修复指引；不会伪称该入口仍保留原能力。历史含浮点来源的查询/汇总可能被保守拒绝，需要从原件重算并核对采用，旧正式成果不被自动改写。精确 SQL、普通 COUNT 和精确列仍可用。

停止缺口来自把 turn 标成 cancelled 却没有取消/约束其异步 job；另有入队后回填 waiting 能复活已停止 turn 的竞态。修补用实际 turn origin 及精确关联，不按相同 workflow target 扫描取消，避免误杀其他会话。

查询出口核心代码：

```python
def _exact_query_value(value):
    if isinstance(value,Decimal):return str(value)
    if type(value) is int and abs(value)>2**53-1:return str(value)
    if isinstance(value,dict):
        return {key:_exact_query_value(item) for key,item in value.items()}
    if isinstance(value,(list,tuple)):
        return [_exact_query_value(item) for item in value]
    return value

# 所有查询结果单元格使用同一转换，包含 DuckDB 整行 STRUCT。
# 普通 int/count 与 bool 保留，避免把所有值无区别变成字符串。
```

### 实跑证据

冻结后的最终命令：

```text
.venv\Scripts\python.exe -X utf8 -m pytest tests -q --junitxml=verification/full-suite-20261027-final.xml --durations=8
1911 passed, 31 skipped, 2 warnings in 1097.73s (0:18:17)
```

`full-suite-20261027-final.xml`：1,942 项，**1,911 passed、31 skipped、0 failures、0 errors**。338 个应用/构建输入/测试/验收脚本 SHA256 在运行前冻结，最终核对一致；不是合并多次选择性回归的计数。两条 warning 均为故意构造重复 ZIP 条目的负例。

31 项跳过分开记录：17 个 POSIX shell 执行、5 个符号链接创建权限不足、4 个未配置 Python Playwright 的真实浏览器夹具、3 个需要真实 PostgreSQL、1 个部署镜像 Restic、1 个平台模块在收集阶段跳过。跳过不是通过，具体路径和原因保留在 JUnit。


已完成的独立验收：

- `acceptance/offline/verify_backend.py --app-root dist/Atlas-DataBuddy-win32-x64-r20/resources/app --evidence-dir verification/backend-r20-20261027`：包内 Python/Git、空 PATH、新目录、导入→加工→人工发布→导出/审计、重启保留数据与错误模型配置均 `ok=true`；精确总额 `118.150000000000`，只是确定性链路，`real_ai_evaluated=false`、物理断网未测。
- `verification/r20-source-inventory.json`：15,240 个包清单文件、154 个一方输入、15,006 个运行时文件摘要全部匹配；无遗漏/额外业务文件、配置密钥路径或个人工作数据。清单 SHA256：`8a96804bbfa016ed1f303e217a7e72016d973ff35daab91f6c63a78dc6ac90cf`。
- `verification/node-validation-20261027.json`：40 项桌面测试和 21 个 UI DOM/交互脚本退出码全部为 0；各条命令与日志摘要保留。不是完整真实浏览器视觉验收。
- `verification/windows-report-long-stage-regression-v15.xml`：86 项回归通过；独立重叠 9 项复核见 `windows-longpath-independent-review-v15.xml`。真正运行超过 260 字符的报告四格式导出，并校验哈希、篡改拒绝、同句柄发布和 junction 清理；UNC 仅测路径转换，未连真实 SMB。
- 完整 quickstart 与双进程协作使用本地模拟模型：前者包括本地资料、多源关联、Excel/Word/PPT、技能/MCP、记忆/压缩、完整导出及重启；后者 40 行精确合计 `300.000000000002`。不是 G1 真实模型财经月结证据。最终单独证据见 `quickstart-isolation-actual-v17.xml` 和 `final-collaboration-harness-fixed-v15.json/.xml`。


第一遍全仓保留 `verification/full-suite-20261027.xml`：1889 passed、32 failed、11 skipped。平台/编码前置已分别修补；符号链接权限与 POSIX shell 条件不满足的用例明确跳过，不算通过。两个恢复超时以同规模 9,384 条目、167MB 备份独立定位：正常恢复 72.094 秒并完整核验通过，篡改包 45.875 秒正确拒绝；只有这两个测试的预算从 30 秒改为 180 秒，业务断言与生产恢复逻辑不变。详见 `verification/restore-baseline-scale-benchmark-v13.json`。

曾有一次最终回归在早期暴露了验收脚本共享环境兼容回归：quickstart 的 stdin 停止开关被其他协作脚本继承，导致服务遇 EOF 即退出。已将开关限定到拥有 PIPE 的调用方，并增加共享接口断言；中断运行与源码快照保留为 `full-suite-20261027-pre-harness-fix.log` / `source-freeze-20261027-pre-harness-fix.json`，未冒充完整 JUnit。独立双进程真实复跑通过后才重新冻结和跑最终全仓。

定向证据包括 CAP-1 的真实 HTTP→探查→dlt/dbt→发布→查询及旧冻结版本阻断、CAP-4 的 148 项取消/授权回归、Office 的 119 项完整性回归、默认 CP936 的 46 项 Office 回归与真实中文流程。它们只解释定位与修补过程，最终结论以冻结源码后的全仓和包验收为准。

## 【反驳意见】

不通过放宽精度、重签损坏文件、删除失败记录或将 `visual_validation` 常量改 true 取得绿色结果。保守拒绝不精确 legacy SQL 是已披露的能力回退；后续若恢复该入口，需要端到端精确实现与独立结果校验。

## 【自评扫描】

1. 覆盖空/超长/缺文件、大整数/精细小数/非有限值、损坏缓存、并发、取消与回填竞态；没有自动执行用户原任务。
2. 随包运行时、新目录、空 PATH 与 local_only 单独验收；物理断网未执行，不能推定通过。
3. 新错误带可操作指引，原失败和历史成果保留；记录与回执不冒充执行成功。
4. 不新增用户环境配置或全局依赖；没有加入探索中未使用的 NumPy。
5. 全仓结果、包内闭环与构建哈希分别核验。当前修补通过也不等于完整财经生产验收通过。

## 【给豆包的下一步请求】

本轮未发起新外评。后续若用户要求继续，优先核对当前完整证据、G1 真实模型 A02–A05、G4 真实报告/PPT 渲染，以及 CAP-5 非法协议恢复等未完成项。既存 `quickstart_model` 在 Windows 的配置文件链接边界未在本轮扩展修补，不能将 preset 修补推广成所有配置入口均已验证。**G1/G4 仍未放行**，不写“我已无新增改进点”。
