# DOUBAO 只读审查报告 · Atlas DataBuddy v0.20.0

- 审查角色：检查者（只读，未修改任何代码/配置，未读取 .env、密钥或私人业务数据）
- 审查对象：`C:\Users\001\Documents\ChatGPT\New project\personal-data-agent`
- 审查日期：2026-10-24
- 审查范围：部署可运行性、主流程、数据正确性、权限与输入边界
- 分级：`[证实]` = 从源码路径可确定复现/必然发生；`[推断]` = 逻辑成立但影响/触发条件依赖环境或运行数据

严重性：**高**（必须修）/ **中**（应修，影响正确性或一致性）/ **低**（健壮性/文档）

---

## 一、结论摘要

项目整体工程质量和安全意识显著高于一般水平。v0.20.0 本轮交付的三个核心模块（独立工作空间网关 `workspace_gateway.py`、机器 Agent 网关 `agent_gateway.py`、委派任务权限围栏 `job_authority.py`）设计严谨，静态扫描未发现 `eval/exec/shell=True/os.system`、危险反序列化、SQL 注入、路径穿越、SSRF 直连等常见高危缺陷。验证证据真实可复现（见第五节）。

本次审查共提出 **4 项实质发现**（无高危），集中在**本机部署配置一致性**与**少数健壮性**问题，均给出文件行号与最小修复建议。

---

## 二、已证实/高价值发现

### 1. [中] `run-local.sh`（无 Docker 的本机开发入口）未设置 `ATLAS_OPENCODE_RUNTIME`，本机默认走旧工具循环，与容器行为及 README 不一致

- **位置**：`run-local.sh:11-15`（仅 `export DATA_DIR`、`SCHEDULER_MODE=worker`）；判定逻辑 `agent_service/opencode_runtime.py:33` → `return os.getenv('ATLAS_OPENCODE_RUNTIME') == '1'`（未设置即 False）。
- **对比**：`compose.quickstart.yaml:15` 与 `compose.local.yaml:21` 均显式 `ATLAS_OPENCODE_RUNTIME: "1"`；`cloud.py:86` 也按 `=='1'` 分支。README:8/133 与 OPENCODE.md:11 明确"v0.19/v0.20 默认使用 OpenCode 原生 Agent"，且旧路径需**显式** `=0`。
- **复现**：在无 Docker 环境执行 `bash run-local.sh`；启动后查看 `/api/health` 的 `agent`/`planner_mode`，或检查 OpenCode 原生服务是否被拉起——本机不会拉起。
- **影响**：你正在做本机（WSL + Python3.12，无 Docker）部署，`run-local.sh` 是唯一不依赖 Docker 的入口；但按当前脚本它会落在旧工具循环（`ATLAS_OPENCODE_RUNTIME=0` 语义），而非 v0.20 README 宣称的 OpenCode 原生运行时。README「开发与 CI」一节（README:204-213）也未提示本机需手动设置，存在文档缺口。
- **最小修复**（二选一）：
  1. `run-local.sh` 增加 `export ATLAS_OPENCODE_RUNTIME=1` 和 `export AGENT_ORCHESTRATION_MODE=separate`（与容器一致）；或
  2. 在 README「开发与 CI」一节明确：无 Docker 的本机开发默认走旧 Atlas 工具循环，如需 OpenCode 原生 Agent 请手动 export 上述两变量或使用容器入口。
- **分类**：`[证实]`（代码路径确定），功能影响为 `[推断]`（取决于你是否期望本机也启用 OpenCode 原生）。

### 2. [中] 所有容器启动入口要求 `llm.json` 先填真实模型，占位值会直接中断启动

- **位置**：`quickstart.py:89-93`（`configuration()` 检测 `your-llm.example.com` / `YOUR_MODEL` / `YOUR_API_KEY` 占位并 `raise ValueError`）；`quickstart.py:111`（生成模板后同样抛错）；`start.ps1:87`、`start-local.sh:126`、`start-local.ps1:120` 均先执行 `setup`，失败即 `exit`，服务不启动。
- **复现**：`llm.json` 保持占位时运行 `start.ps1` / `bash start-local.sh up` → 输出「请在 llm.json 填写 LLM 接口地址、模型名和 API Key」并退出。
- **影响**：这是设计意图（防止占位误上线），但对你当前部署是**直接阻塞点**：你提到 `llm.json` 仍为占位。容器/本地文件夹入口在你填好真实（或 `unused` 假密钥的本地）模型前都无法 `up`。仅 `run-local.sh` 可绕过此检查启动（回退 rules 模式），但见发现 1 的功能差异。
- **最小修复**：无代码改动；部署前置动作——在 `llm.json` 填入可达的 `base_url`/`model`/`api_key`（本地模型可 `api_key: "unused"`），或明确改走 `run-local.sh` 并接受 rules 模式。
- **分类**：`[证实]`（设计如此，非代码缺陷；对你是流程提示）。

### 3. [低] `agent_gateway.py` 预算记账字段 `input_characters` / `output_tokens_budgeted` 无配额上限

- **位置**：`agent_service/agent_gateway.py:169-176`（`admit_model_request`）。`model_requests` 有 `>= max_total_model_calls` 上限（行 173），但 `input_characters`（行 175）与 `output_tokens_budgeted`（行 176）仅做 `+= int(...)` 累加，**无任何上限判定**。
- **复现**：单个 client 在授权内持续提交任务（受 `max_total_model_calls`=10000 与每任务 `max_model_calls_per_task`=12 兜底），累计输入字符/预算输出 token 可随调用单调增长，远超任何单任务上限，`task_status` 会把这些大数回显。
- **影响**：非安全漏洞（有调用次数兜底，不会无限放大调用），但「预算」语义对 input/output 未真正封顶，长期运行下记账失真、`task_status` 返回体膨胀，可能误导运维判断用量。
- **最小修复**：在 `admit_model_request` 中对 `input_characters`、`output_tokens_budgeted` 增加与 `model_requests` 一致的上限判定（例如分别对 `max_total_model_calls * max_input_characters`、`max_total_model_calls * max_output_tokens` 封顶并 `raise 429`）。
- **分类**：`[证实]`（代码路径明确），影响为 `[推断]`（低，健壮性）。

### 4. [低] `workspace_gateway.py` 并发快速失败检查与 acquire 非原子，503 保护可能被排队请求绕过

- **位置**：`agent_service/workspace_gateway.py:353-355` —— `if semaphore.locked(): raise 503` 随后 `await semaphore.acquire()` 是"检查-再获取"，非原子；并发突增时多个请求可同时通过 `locked()` 检查后进入 `acquire()` 排队。
- **复现**：并发请求数超过 `max_concurrent_requests`（默认 64）时，部分请求不会立刻 503，而是排队等待 semaphore 释放，实际挂起时长由上游超时兜底。
- **影响**：`max_concurrent_requests` 的"快速拒绝"语义不完全生效；排队请求依赖上游 `upstream_timeout_seconds` 收敛，极端情况下客户端感知到的是超时而非明确的 503。属健壮性，非安全漏洞。
- **最小修复**：改为在 `acquire` 上加超时（`asyncio.wait_for(semaphore.acquire(), timeout=1)` 失败即 503），或将 `locked()` 检查并入获取逻辑（如 `asyncio.Semaphore` 本身不提供非阻塞获取，可用 `try_acquire` 辅助实现）。
- **分类**：`[证实]`（代码逻辑），触发条件 `[推断]`（需并发压力）。

---

## 三、其他观察（低优先级，建议知悉）

- **`quickstart.py:133-137`（`info`）把 `APP_TOKEN` 打印到 stdout**。本机助手设计如此（供启动者读取），但若 stdout 被重定向/CI 捕获，访问令牌会落盘。建议在非交互场景提示，或在输出时标注"仅终端可见，勿写入日志"。`[证实]`，低。
- **`agent_gateway.py:142-147`（`authenticate`）对 `all_items('agent_client')` 全表线性扫描 + `hmac.compare_digest`**。数据量小（默认 client 数受 operator 创建），非关键；若未来 client 数量增长，可加索引/缓存。`[推断]`，低。
- **本机 `run-local.sh` 依赖重依赖安装**（`requirements.lock` 含 `dbt-duckdb`/`dagster`/`dlt`/`duckdb`，见 `pyproject.toml:8-9`）。在 WSL Python3.12 纯本机安装耗时且可能受网络/编译影响；若部署受阻，优先考虑容器入口（`start-local.sh`）而非本机 venv。`[推断]`，环境相关。

---

## 四、经检查确认的高质量项（未发现问题的部分）

- **独立工作空间网关 `workspace_gateway.py`**：JWT 验签覆盖 kid/alg/iss/aud/exp/iat/leeway 与 `exp-iat ≤ max_token_lifetime`（行 192-211）；拒绝 crit/jku/jwk/x5u 头（行 198）；仅接受公钥且 RS256 要求 ≥2048 位、ES256 要求 P-256（行 150-164）；明文 HTTP 后端限定私网/环回（行 81-88）；路径规范化与查询编码校验（行 242-261）；body/response 双上限与流式超限报错（行 276-282、360-369）；`_NoCookies` 策略禁止跨工作空间会话状态持久化（行 294-300）；Origin/`sec-fetch-site` 校验（行 340-341）；统一安全响应头（行 214-231）。**未发现可利用缺陷。**
- **机器 Agent 网关 `agent_gateway.py`**：令牌仅存 SHA-256 哈希（行 109、138）；幂等键含 client+project+request_id（行 203）；任务/模型/项目授权逐项校验（行 206-211）；`assert_turn_allowed` 在每次模型请求前复查授权修订、截止时间、只读边界（行 149-162）；任务取消覆盖已入队未写回窗口（`_cancel_turn` 行 291-309）；Origin 与本地环回来源白名单（行 437-445）；JSON 重复键/非常量拒绝（行 427-435）。**未发现可利用缺陷。**
- **委派任务权限围栏 `job_authority.py`**：授权修订固定、取消窗口、`client_guard` 锁复用防护（`assert_owned`）、未标记旧任务保守兼容（行 41-51、65-70）。**未发现可利用缺陷。**
- **SSRF 防护 `connectors.py`**：阻断云元数据地址（`169.254.169.254` 等，行 84-85）；私网 API 须 `API_ALLOWED_HOSTS` 显式允许（行 93-94）；DNS 解析后固定 IP、二次解析防重绑定（行 87-95）；拒绝 link-local（行 92）；`follow_redirects=False`、`trust_env=False`（行 265）。**未发现可利用缺陷。**
- **数据正确性 `data_workflows.py`**：金额统一 `DECIMAL(38,12)`，禁止二进制 float 强转（行 305-320）；除零拒绝发布（行 466）；舍入 `ROUND_HALF_EVEN`（行 471）；单位换算 元/千元/万元/亿元（行 218-224）；聚合 `sum(Decimal)`（行 539）。**未发现正确性缺陷。**
- **文件解析边界 `workbook_reader.py` / `document_reader.py`**：zip 解压上限 200MB、条目数 1 万、sheet 100、单元格扫描 500 万、公式只读缓存不重算并标注 freshness（行 32-38、58、286-287、368）；OOXML ZIP 拒绝重复/不安全路径（document_reader.py:94）；PDF 加密检测与预算拒绝（行 435-459）。**未发现缺陷。**
- **验证证据真实性**：`pytest.xml` 根元素 `testsuites/testsuite` 统计 `tests=1381, errors=0, failures=0, skipped=8`（= 1373 passed + 8 skipped），时间戳 2026-10-24T11:16+08:00 与 SUMMARY「Completed 03:22:59 UTC」吻合；`pytest.exit`/`ui-tests.exit` 均为 0；`requirements.lock` 存在。**SUMMARY 与证据一致，测试通过真实。**
- **配置卫生**：`.gitignore:1-2` 排除 `.env`/`llm.json`；`bootstrap.py:8-11` 生成高熵随机口令并 `chmod 600`；`quickstart.py` 的 `.env` 解析器不执行、拒绝重复键与占位模板。**未见凭据泄露路径（除第三节 info 提示）。**

---

## 五、验证方法与证据

- 本报告基于对 `agent_service` 下约 20 个核心模块的逐行阅读（workspace_gateway / agent_gateway / job_authority / connectors / data_workflows / workbook_reader / document_reader / config / cloud / opencode_runtime 等），以及全部部署脚本、Dockerfile、compose、pyproject、bootstrap、quickstart。
- 静态扫描（Grep）未命中 `TODO/FIXME/NotImplemented`、`eval/exec`、`shell=True`、`os.system`、危险反序列化等模式。
- 验证证据（`verification/current-final/`）已与源码交叉核对。
- **未执行**：真实 pytest 重跑（约 6.5 分钟）、镜像构建、容器启动、真实模型连接、浏览器视觉测试——均超出只读审查范围，且与项目自身记录的边界一致。

---

## 六、后续待你确认/处理

1. 本机部署入口：`run-local.sh` 是否按发现 1 增加 `ATLAS_OPENCODE_RUNTIME=1`？还是接受旧工具循环并在文档注明？
2. `llm.json` 占位是否先填真实/本地模型，以便容器入口可 `up`？
3. 发现 3、4 是否纳入下一轮（均为低危，可择机修）。

我作为检查者，在你修复并给出回归结果后，会再复核上述各项（重点复看发现 1 的环境变量注入与文档一致性）。
