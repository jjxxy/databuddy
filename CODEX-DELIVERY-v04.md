# Codex v0.4：财经能力修补与用户亲测交接

日期：2026-10-24。产品版本仍为 Atlas DataBuddy v0.20.0，本轮便携构建为 r7。

当前结论：**工作台已启动，已准备原始月结资料和未发送草稿；G1 尚未通过，不作为财经生产交付放行。** 按用户最新指令，本轮停止向豆包发起评审，交用户亲测。未继续增加收费模型运行。

## 【本轮改动摘要】

- 版本号：v0.4。最终程序：`dist/Atlas-DataBuddy-win32-x64-r7/Atlas DataBuddy.exe`；使用根目录 `打开财经月结亲测.lnk` 可再次打开同一个亲测空间。
- G1／A02：`agent_service/conversation.py`、`execution.py`、`planner.py`、`workspace.py`、`workbook_reader.py`。保留结构性探查错误分类；原件完整性校验后允许 Agent 检查需明确布局的工作簿，不把来源改为 ready。正式执行仍检查范围、合并单元格、单位、百分比和公式；异常时释放 Windows 读取句柄。
- G1／A02–A05：`agent_service/agent.py`、`agent_context.py`、`opencode_runtime.py`、`opencode_plugin.mjs`、新增 `opencode_context.mjs`。区分画像样本与完整计数；保留小表和有界读取回执；分页恢复限定原任务及恢复链；正常 Atlas 原生请求要求工具协议。最新补丁按完整定义摘要统一驻留工具 schema，明确工具名查询不再附送无关定义，保留当前请求中真实存在的完整定义及变版定义。
- G1／A03：`agent_service/data_workflows.py`。生成给模型的列结构明确数值单位要求，与现有运行时校验一致。
- G1／A04：`agent_service/result_delivery.py`、`project_data.py`、`data_api.py`、`workspace_artifacts.py`、`data_tools.py`、`static/data-workbench.html`、`static/data-workbench.js`。新增产品实际完整 CSV 下载、目录与按钮；从冻结交接包生成，按显式数值列类型保留十进制负数，公式样式文本沿用既有文字防护。CSV 使用独立格式缓存键与版本路径；重复下载回读摘要，拒绝篡改。对话采用支持精确绑定运行 ID 与方案摘要。
- G1／验收控制器：新增 `acceptance/depth/common.py`、`verify_agent_depth.py`、`cases/finance_monthly.py`、`cases/finance_negative.py`，及前期五个规模场景控制器。只上传实际输入，标准答案不发给模型，下载实际文件后调用原始独立校验器。负例可单选并明确未运行范围。
- 对应回归：`tests/test_attachment_reading_boundary.py`、`test_workflow_column_schema.py`、`test_bounded_planning_context.py`、`test_finance_delivery_boundaries.py`、`test_finance_depth_harness.py`、`test_finance_negative_harness.py`、`test_data_api.py`、`tests/ui/data-workbench.cjs`、`desktop/tests/opencode-context.test.cjs` 等原生协议回归文件。各套结果单列，不相加为总通过率。
- 已并入此包的前段 UI 修补对应 UI-1 与用户导航要求：中文眉题；取消原生菜单和业务重启入口；保留正常文本编辑快捷键；各子页错误、空态和返回路径检查。相关证据保留在 `verification/ui-*v04*` 及此前 UI 回归记录中，未以 UI 修补替代 G1。

## 【实现说明】

### 根因与实际证据

官方月结正例最初在模型调用前被复杂表头的探查失败挡住。放行只读检查后，工具回执又被截成分页；分页本身再次截断，并错误指向分页步骤，造成递归恢复。修正后仍有“小表被最近两条策略轮番淘汰”的问题。保留数据后，真实运行暴露工具定义重复发送、工具发现附带无关定义及工作流指南叠加导致的上下文与累计输入上限。

最新补丁解决已复现的 schema 双发和精确工具名发现问题，**只完成协议回归，尚未重新取得官方月结真实模型通过证据**。没有提高任务默认预算：18 次模型调用、360000 输入字符、24000 输出 token 预算、300000 ms。

CSV 是另一项真实缺口：原工作流只有 XLSX 和交接包，不能由测试脚本改写 JSONL 凑成 CSV。新增完整 CSV 后，独立复查又发现精度选项缓存冲突，以及冻结数值按字符串存储时负金额误入文本防护。现按已声明列类型处理，未通过字符串相似度猜测数值含义：

```python
def scalar(value, field):
    if value is None:return ''
    # Exact numeric text follows the declared numeric schema.
    if _numeric(field):return format(_decimal(value), 'f')
    if isinstance(value, bool):return 'true' if value else 'false'
    if isinstance(value, Decimal):return format(value, 'f')
    if isinstance(value, (datetime, date)):return value.isoformat()
    if isinstance(value, str):return literal(value)
    if isinstance(value, (int, float)):return str(value)
    raise DeliveryError('CSV只支持标量列，复杂结构请下载完整交接包')

for row in rows:
    _row(row, names)
    values = [scalar(row[field['name']], field) for field in fields]
    writer.writerow(values)
```

### 自测

| 实际执行 | 结果与证据 |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest`：附件边界、多资料、工作簿与混合百分比四套 | 93 passed；`verification/finance-layout-inspection.xml` |
| 独立十进制夹具、项目工作流、列结构回归 | 52 passed；`verification/finance-deterministic-regression.xml`；不是模型业务通过 |
| CSV／HTTP／成果目录／结果交接／操作意图回归 | **102 passed**；`verification/finance-delivery-regression-v04.xml`。含真实 215 行负金额工作流完整下载，合计 `-23220.000000000215`，末行 `-1.000000000001` 无文本前缀 |
| 原生上下文、分页、恢复、记忆与 schema 发现最终回归 | **81 passed**；`verification/depth-final-schema-discovery-regression.xml`；11 项 JS 回归通过 |
| 工作流页面 CSV 按钮与无自动发布 | `verification/finance-csv-ui.log`；DOM 交互通过，不称原生像素布局证明 |
| r7 实际随包 Python + 包内 verify_backend，空继承 PATH、独立新目录、local_only | **ok=true**；`verification/packaged-offline-v07/backend-verification.json`。上传→画像→确定性加工→人工发布→完整导出→审计、重启保留、无效模型与缺运行文件均通过；简化金额 118.15 仅作部署回归，不能替代月结 |
| r7 构建与依赖字节核对 | 15238 文件，随包运行库 15006 文件／743300599 字节，未携带模型配置；`verification/desktop-build-v07.log` |
| 原生工作台读取 | `verification/user-finance-trial-v04-window.txt`：亲测项目、DeepSeek V4 Pro、两份已上传资料、完整未发送草稿、本地电脑工作位置实际存在。桌面控制前置曾返回 GetCursorPos 拒绝访问；通过正常应用启动恢复就绪，未把黑色截图当视觉验收 |

未重跑完整 Windows 全量套件；此前全量仍有已记录失败，不能写“全部测试通过”。没有做物理断网，用户已取消该操作。

### G1 的真实结果

- `verification/finance-monthly-v01`：原始入口阻断，零模型调用，未通过。
- `finance-monthly-v02`：真实模型，分页恢复循环，未通过。
- `finance-monthly-v03`：5 次模型调用，207052 输入字符，步骤耗于重复恢复，未通过。独立复查确认预算 E5/E6/E7 公式与缓存算术已有证据，详见该目录 `controller-audit-correction.json`。
- `finance-monthly-v04`：7 次模型调用，348909 输入字符；恢复循环消失，加载完整指南后上下文超限；没有候选或数值产物，A02–A05 整体仍未通过。
- `finance-negative-v01`：N01–N04 均技术暂停；没有正式发布不等于正确业务阻断。
- `finance-negative-v02-n01`：仅 N01 实跑，7 次调用、277689 字符，仍超限；N02–N04 明确未运行。
- 最新去重补丁未继续收费模型重测，交用户亲测；不写成通过。A06–A11、N05–N07 未验证。原始输入、标准答案和校验器未修改；原清单的 README 字节差异见 `verification/finance-g1-coverage-audit.md`。

G2 真实脱敏样本、G3 长期口径复用、G4 母版与差异报告、G5 百万行与容量、G6 OCR、G7 会计业务日历、G8 原生 Excel 计算、G9 企业隔离与恢复、G10 老式／宏／加密 Excel：**均未验证**，不宣传普遍支持。

## 【反驳意见】

无反驳：接受 G1 优先级和独立数值验收要求，不用工具数量、UI 或 118.15 回归替代财经证明。物理断网按用户已取消的直接指令处理，不再要求配合断网。

## 【自评扫描】

1. 空输入、超长输入、空文件、缺运行文件、错误模型配置：包本体已试。并发／目录隔离沿用此前 r5 专项证据，本轮不冒称重新全验；物理断网未验。
2. r7 新目录，无根目录 node_modules、系统 Python／Node／Git／编译工具依赖；包内 Python 以空继承 PATH 完成实际链路。
3. 错误有本机诊断日志，缺文件返回中文操作提示；模型技术暂停仍是未完成，不是成功。
4. 没有新增通用编辑器、云端部署、OCR 或日历等额外功能；CSV 属 A04 明确交付要求。
5. 102 项数值／交付回归和 81 项协议回归通过；旧失败记录保留。**仍有 G1 待解决，不能写“我已无新增改进点”，当前不放行财经正式交付。**

## 【给豆包的下一步请求】

依用户最新指令，本轮暂不调豆包；现在由用户亲测，不发送评审请求。

## 用户亲测场景

工作台项目：**亲测·2026年9月经营月报**。两份资料及一段业务指令已经放在输入区，尚未代用户发送。

原始输入为官方 phase1 的 `2026-09_业务源表.xlsx` 与 `业务口径.txt`，副本位于 `verification/财经月结亲测`，未把标准答案或校验器放入该工作文件夹。用户可自行改指令并点击发送。

仅供用户核对，不发给模型：收入 **460001.30 元**；直接成本 **239000.50 元**；毛利 **221000.80 元**；收入较预算少 **19998.70 元**；收入、成本各 **7 笔**。此简短体验指令的结果不能冒充原 604 字官方用例通过。
