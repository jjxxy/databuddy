# 资料读取与任务恢复：修复和验证记录

本记录说明当前仓库中的代码变更及验证范围，供部署、排查和业务验收参考。示例工作簿与 JSON 均为测试构造，**不是用户提供的 `quarterly_budget.xlsx`、`store_performance.xlsx` 或 `marketing_campaigns.json` 原件**；尚不能据此断言三个真实文件或用户实际部署已经通过验收。

## 修复范围

1. **等待资料就绪**：明确附加的多份资料会逐一检查，待全部可读取后再进入 Agent；项目当前来源处于 `queued`、`processing` 等处理状态时进入 `waiting_source` 并触发探查，不再因此进入旧规划器。失败、取消或未知状态给出需要处理的提示，不会把未读资料当成成功。原来支持人工确认读取边界的 Excel 仍可进入受控检查流程。隐式项目来源在等待时固定 ID 和版本，期间切换项目来源或修改原文件不能悄悄替换本次输入。
2. **旧决策器的 429**：仅对上游实际 HTTP 429 采用持久化调度重试，不让工作线程睡眠；第 1、2 次失败后分别等待约 2 秒和 4 秒，第三次仍失败则暂停并保留任务。其他 HTTP 错误不会被误判成限流；请求期间用户取消后不会继续重试。模型供应商配额和限速仍需按实际服务处理。
3. **任务调度和异常退出**：依靠现有每项目锁和任务状态控制并发；不以 `opencode_session_id` 判断任务是否正在执行，也不对长任务统一设置 120 秒强制重置。OpenCode 循环异常返回、任务仍处于执行状态且未请求上下文重建时，会暂停并保留已有步骤，避免轮询反复启动；正常长任务和合法上下文重建不受这个异常退出守卫影响。未知结果的非幂等写操作仍需核查，不会因重试假定恰好执行一次。
4. **Python 字节码缓存**：`Dockerfile.quickstart` 使用新的容器入口脚本，在导入服务前为每次容器启动选择新的字节码缓存路径并禁写字节码，避免直接复用旧的包内缓存。它不删除用户文件，也不替代重建镜像、重建容器或重启已运行进程；运行中的 Python 进程已经加载的模块不会因清理缓存而自动更新。此机制只覆盖使用该 Quickstart 镜像且未覆盖入口的容器；其他构建文件、桌面包和自定义启动方式需分别核实。测试复现了同时间戳、同大小的旧字节码，但未实际构建 Docker 镜像，也未实测非 root 容器映射。
5. **Excel 标题和表头**：只有在未提供明确表头或区域、第一行横向合并标题覆盖唯一候选表格、候选未截断且没有内部表头歧义，并覆盖工作表使用范围的相关边界时，才尝试使用候选位置读取。自动识别结果携带候选 ID、范围、警告和语义复核要求；多表、尾注、模糊候选或用户已指定范围时不自动套用。候选可用于继续检查，不代表财经口径或结果已被批准。
6. **JSON 对象**：单个非空对象可作为一条记录；嵌套结构保留为 JSON 字段并根据情况要求语义复核。顶层唯一的常见非空对象数组，且其余字段中不存在其他数组时，可识别为记录候选并用于预览；额外上下文需核对，旧数据执行路径须明确记录路径后才正式加工。如果存在多个或不明确的记录数组，则保留完整文档并要求明确记录路径，不会静默丢弃其他数组。空数组、非对象数组与复杂文件也不能一概当作有确定表结构的对象数组。原始文件和其版本仍是完整依据，预览不能替代完整数据或业务复核。
7. **调试文件与语法**：生产代码不依赖 `/tmp/handle_trace.log` 等调试文件作为运行前提；检查部署版本时仍需确保运行进程来自最新镜像。Python 语法验证可使用 `python -m compileall -q agent_service`。

## 本地验证入口

以下测试使用临时目录、构造资料或本地模拟模型，不发送真实业务数据：

```bash
.venv/bin/python -m pytest -q tests/test_pending_source_routing.py tests/test_multi_file_conversation.py tests/test_attachment_reading_boundary.py
.venv/bin/python -m pytest -q tests/test_legacy_decision_rate_limit.py tests/test_tick_execution_ownership.py
.venv/bin/python -m pytest -q tests/test_workbook_connector.py tests/test_workbook_reader.py tests/test_exact_json_source.py tests/test_json_source_preview_pages.py
.venv/bin/python -m pytest -q tests/test_container_python_cache.py
python -m compileall -q agent_service
```

以上命令是重现入口，不代表每次提交都已运行所有测试。正式使用前，请用脱敏后的真实三个文件核对工作表范围、字段、合并单元格、记录数、数字精度与业务口径，并在实际部署的容器、模型和网关上验证上传、等待、执行、重启和成果读取。检查页面和服务运行版本一致后再判断部署是否已更新；不要为了更新代码清空现有业务数据。
