# v0.8：模型预算、上下文与财经输入可靠性修补（r14）

## 【本轮改动摘要】

版本 **v0.8**；产品仍为 Atlas DataBuddy v0.20.0，最终便携构建 **r14**。本轮没有联系豆包。当前交付形态沿用已经对齐的便携本地工作空间；云端 web 迁移仍是后续阶段。

对 PRD 的理解保持为：服务财经及经营分析用户；原件与口径可追溯；本地导入、加工、人工核对采用和导出可用；云端模型不能阻断本地服务；财务口径不能通过模糊匹配或隐式单位转换猜出来。

本轮定位编号：**U5**＝用户报告累计模型预算耗尽；**U6**＝两句输入触发八万字符及压缩衔接；**G1-I**＝完整月结实测中的授权误判；**G1-E**＝加工失败无法定位或恢复；**G1-M**＝Excel 混合金额无法进入显式规范化；**G1-L**＝重复工具发现导致的空转。G1 仍沿用验收人的完整财经月结要求，以下修补不等于 G1 已通过。

| 改动文件 | 对应问题及实现 |
| --- | --- |
| `agent_service/task_budget.py`、`tasks.py`、`agent.py` | U5：普通主会话默认总调用数、累计输入、累计输出、总时长为无限策略；旧的标准/深度默认合同在执行视图迁移，原合同保留。去掉普通主会话固定工具步数门槛。停止指令、验证、确认发布与授权检查仍执行。 |
| `agent_service/provider.py`、`opencode_runtime.py` | U5/U6：移除默认八万字符拦截与默认累计额度；按实际响应额度做授权预留/释放。原生历史读取允许处理超过旧 2 MiB 的结果，保留有明确目的的内存保护。 |
| `agent_service/provider_compatibility.py`、`opencode_plugin.mjs` | U6：给当前 DeepSeek V4 Pro 设置经核实的真实窗口和单次输出能力；压缩摘要额度独立于业务生成额度；有明确范围的子任务额度应用到实际请求。启动不查外网。 |
| `agent_service/agent_context.py`、`opencode_context.mjs` | U6/G1-L：保留完整小表和仍需使用的规范说明；仅对严格相同的布局重复项引用去重；修复“写了笔记就丢掉加工规范”的误判。相同失败不能通过重复发现同一工具绕过无进展检测；有实际新证据或修复操作可以继续。 |
| `agent_service/intents.py` | G1-I：区分“用户无需写代码”和“禁止 Agent 写文件”；修正把“分别汇总”里的“别汇总”误识别为禁止分析。真实的“别运行”“不要发布”等限制继续生效。 |
| `agent_service/agent.py` | G1-E：结构校验返回字段位置、类型要求和受控修复提示，不回显原始参数、密钥或堆栈，减少反复发现同一工具。 |
| `agent_service/project_data.py`、`data_tools.py` | G1-E/G1-M：返回与冻结方案一致的失败运行诊断，区分失败运行和有效结果；识别草稿是否已变化。新增显式 `text_columns` 输入投影，把精确数字无损表示为文本，以便混合金额进入后续明确的金额解析步骤。单位、类型事实、坐标和原文件保留；空选项保持历史方案摘要兼容。 |
| `agent_service/data_workflows.py` | G1-E：类型错误带输入别名、列、相对行号、期望类型和实际类型；不输出原始单元格值。严格类型校验没有放宽。 |
| `agent_service/collaboration_budget.py`、`agent_team.py`、`agent_mesh.py`、`quotas.py` | U5：主会话无限策略贯穿共享计量和协作调度；真实子任务、外部网关及显式管理员授权仍按各自范围执行。默认不注入日额度，显式零额度仍表示禁止。 |
| `scripts/render_enterprise.py`、`deploy/enterprise/config.example.json`、`scripts/verify_shared_budget.py` | U5：部署示例和渲染器不再悄悄恢复默认日额度；协作验收支持无限主会话，并继续严格检验计量、取消、撤销和委派额度。未进行云端部署。 |
| `acceptance/depth/common.py`、相关回归测试、`verification/` 复现脚本 | U5/U6/G1：验收普通默认合同使用无限策略，无测试专用提额；保存真实失败、修复重放、压缩和最终包证据。完整清单见 `verification/v08-implementation-files.json`。 |

相对上次交付 r10，最终包共有 **17 个业务源码文件变更**，见 `verification/r14-production-changes-since-r10.json`。辅助文件清单使用文件时间审计，已注明不是 Git diff。

## 【实现说明】

### 为什么两句话会有八万字符

报错那轮的业务原文实际 **165 字符**，后续“继续”为 **2 字符**。八万是旧程序对**拼装后的模型请求**设置的字符阈值。原始诊断中，最后一次已经放行的请求为 **77,250 字符**：

| 组成 | 字符数 |
| --- | ---: |
| 工具结果、表格及规范回执 | 47,104 |
| 用户消息及附加的项目、资料、合同上下文 | 23,446 |
| 模型历史消息 | 3,532 |
| 系统提示 | 1,704 |
| 工具定义 | 1,267 |
| JSON 封装差额 | 197 |

证据：`verification/budget-diagnostic-v08.json`。这不是用户写了八万字，也不是附件压缩包大小。累计输入字符还会把同一历史在不同请求中的重复发送多次计入，不能当成单次窗口大小或计费 token 数。

旧程序会在真实 token 窗口耗尽前被自设字符阈值截停；原生运行时对当前模型又使用了不匹配的容量信息。现在移除普通任务的这些默认门槛，按模型真实容量衔接原生自动压缩。DeepSeek 官方模型接口文档给当前 `deepseek-v4-pro` 的窗口为 **1,048,576 tokens**，单次最大输出 **393,216 tokens**；前者包含输入与输出。已保存模型接口快照，并核对正在运行的原生模型配置。[DeepSeek 模型元数据说明](https://api-docs.deepseek.com/api/list-models/)

压缩接近真实 token 窗口时触发；不会因为用户只写了两句话而把工具读取的内容忽略。摘要是工作索引，不能代替财务原始证据；完整来源和工具结果可回取。精确去重也不合并内容相近但版本、顺序或数值不同的结果。

核心代码摘录（`task_budget.py`，执行视图，不改写历史合同）：

```python
if _scoped(turn):
    limits={key:_positive(raw.get(key) if raw.get(key) is not None else default)
            for key,default in LEGACY_DEFAULTS.items()}
    gateway=turn.get('gateway_limits') or {}
    if not isinstance(gateway,dict):raise ValueError('外部Agent任务预算无效')
    for key in KEYS:
        if key in gateway:limits[key]=min(limits[key],_positive(gateway[key]))
    return limits
if policy=='unlimited' or policy is None and (
        not raw or raw==LEGACY_DEFAULTS or raw==LEGACY_DEEP):
    return dict.fromkeys(KEYS,None)
return {key:None if raw.get(key) is None else _positive(raw[key])
        for key in KEYS}
```

### 完整月结暴露的实际根因

真实模型运行暴露了三类程序问题，不能全部归咎于模型：权限正则误读“分别汇总”；校验失败丢失可修复的位置及类型信息；混合数字和货币文本的 Excel 金额列，在进入金额规范化之前就被严格输入类型拒绝。最后一项通过**显式无损文本投影**修补，后续仍须明确币种、单位和解析规则，没有自动猜测口径。

投影只允许明确指定的 XLSX 原始列，有限 Decimal/整数精确转换，字符串和空值保持。拒绝 float、bool、未知列和百分比语义投影；在分配前检查极端指数与累计文本扩张。原件、单元格证据、单位和历史版本不改变。队列失败不能分页冒充有效结果，也不能发布。

### 自测证据

- 最终源码合并回归：按 `verification/budget-context-r14-tests.json` 的 **29 个模块**运行 `.venv/Scripts/python.exe -X utf8 -m pytest ... -q`，**463 passed in 120.17s**。记录：`budget-context-r14-final-v02.log` / `.xml`。不是全仓所有测试通过的声明。
- 初轮合并有一项旧测试仍要求输入失败 `stage_id=None`；按本轮有意增加的 `input:orders` 精确定位更新断言，重复、错误码及正式成果保留断言均继续执行，随后完整重跑通过。旧失败日志保留。此前连接探测测试也改用独立真实空库，避免其他测试的未结束任务污染，不绕过生产配置保护。
- 原生上下文及桥接专项 **21 个 Node 测试通过**；`native-context-budget-final-v08.log`。包含真实请求输出额度、受限委派及摘要额度、完整小表和规范保留。
- 随包 OpenCode **2.1.2** 自动压缩实测：用隔离本地 SSE 模型替身收紧到 8192-token 测试窗口，原生记录出现 `reason=auto,status=completed`，之后继续完成文件产出与校验；手动压缩接口调用 **0**，付费模型调用 **0**。`native-autocompaction-v08.json` / `native-autocompaction-final-v08.log`。这证明原生压缩链路，不能当作真实财经模型验收。
- 显式混合金额：真实 XLSX → 原始类型拒绝 → 明确投影 → 排队规范化验证成功；未发布；原文件字节、单位、原始类型事实和坐标保留。空选项的旧文档 dump/digest 兼容；极端指数、累计分配、非 XLSX 和百分比拒绝均覆盖。`explicit-source-text-projection-v13.xml` / `.log`，**44 项目标测试通过**，已包含于最终合并回归。
- 授权与循环：官方原请求不改写即可排队加工，禁止发布仍生效；原 v09 快照误判及重复发现循环只读重放定位。`separate-aggregation-real-turn-replay.json`、`no-progress-discovery-real-v10.json`。真实执行节点验证 70 步有进展后仍能写文件，重复查阅未变化工具说明不能掩盖相同失败。
- 共享额度、委派及撤销专项已跑；两进程本机 HTTP 协作验收通过，`collaboration-verifier-unlimited-regression.xml` / `.log`（13 项）。模型使用替身，独立于财经验收。企业配置专项 `enterprise-optional-quota-example-v08.xml` 为 **20 passed, 2 skipped**，跳过项缺 PostgreSQL 环境，未冒称通过。
- 最终 r14 构建：**15,239 个文件**；运行库 **15,006 文件 / 743,300,599 字节**与 vendor 清单一致；未携带模型私有配置。`desktop-build-v14.log`。包内 **133 个首方文件**与当前源码及哈希一致，`r14-source-inventory.json`。`FILES.sha256` 摘要：`859176f137d4251495aeae9ac3adfe663b8251596cfd24ea6558581ce775d9fc`。
- 最终包本体：`acceptance/offline/verify_backend.py --app-root dist/Atlas-DataBuddy-win32-x64-r14/resources/app --evidence-dir verification/backend-r14-delivery`，使用随包 Python、空继承 PATH、新目录、local_only；导入→加工→人工发布→导出精确 **118.15**，重启恢复、鉴权、空输入、超长输入、空文件、错误模型配置及缺运行文件的启动校验通过。`backend-r14-delivery/backend-verification.json`。这是启动/本地闭环验收，不替代完整 G1。
- r14 已打开原亲测工作空间。更新前活跃任务为 0；更新前后项目、全部历史任务、两份附件、草稿、完整 pending 摘要相同，`saved_state_preserved=true`。`trial-r14-before.json`、`trial-r14-after.json`。运行中的六份主会话合同有效额度均为无限，原合同保留；真实原生模型窗口和自动压缩启用状态已核对，`live-budget-policy-v08.json`。此核对触发模型生成 **0 次**。
- `r14-window-verification.json` 记录最终程序窗口；`打开财经月结亲测.lnk` 指向 r14。没有声称通过自动鼠标点击或截图视觉验收，也未自动提交用户的继续请求。

### 真实财经验收结论

完整官方 A02–A05 已真实调用模型，最新独立记录仍为 **未通过**：`verification/finance-monthly-v10/verification.json`、`finance-monthly-acceptance.json`。该轮第一段 55 次调用，最大单次请求 **1,248,681 字符**，已经跨越旧门槛；继续段在确认混合金额输入缺口后经标准停止接口取消，避免无效付费重试。失败、已读取内容和诊断均保留，没有人工填入标准答案或手工修完方案后冒充模型成功。

新混合输入修补通过引擎与最终包回归；**修补后尚未重新获得完整真实模型月结通过记录**。A06 的月更复用、后续更正/改口径及负面案例没有因本轮修补转为已通过。G2–G10 仍未完成独立验收：真实脱敏样本、长期财务口径、模板及变更对照、百万行与并发、OCR、会计期间/工作日、真实 Excel 计算引擎、多租户/共享/备份、旧格式/宏/密码文件。

## 【反驳意见】

- 针对 U5/U6“不要有限制”：**部分接受**。普通主会话的默认累计额度和八万字符阈值已移除。供应商真实 token 窗口仍是有限容量，不能用改本地常量消除；摘要可能损失财务细节，不能用它代替原件核对。替代方案是按真实模型窗口自动压缩、精确去重、保留原始证据，并让有进展的任务继续。
- 外部网关和委派授权、用户停止/撤销、确认发布以及数据内存保护继续执行。它们分别解决越权、重复副作用和资源耗尽问题，不能把主会话无限策略扩成无范围的授权。
- “工具多就代表财经业务足够可靠”不能作为交付结论。本轮真实月结仍失败，决定后续优先级的应是可复现的业务正确性，不能只看工具数量或界面完整度。

## 【自评扫描】

1. 空/长输入、空文件、缺文件、错误密钥、版本与项目边界、取消、重复与并发路径已有相应回归；极端数字、混合金额、百分比与单位不静默转换。物理断网/防火墙隔离依用户指令不执行，OB-2 不冒称通过。
2. 最终包在新目录、空 PATH、local_only、随包运行时实跑，无业务机 Node 构建、全局依赖或工具链要求。不能把当前 Windows 机器上的隔离目录测试称为全新虚拟机验收。
3. 模型和队列错误保留日志及可操作诊断；金额原值、密钥、任意对象内容与堆栈不进入类型错误回执。失败运行不输出伪结果，旧正式成果继续保留。
4. 本轮没有新增导航、通用编辑器、云端迁移或另一个产品。显式金额文本投影直接对应真实 G1 输入缺口；语义口径仍待用户确认。
5. 最终 **463 项相关回归、包本地闭环及保存状态核对通过**；完整 G1 未通过，G2–G10 未验。当前可供人工监督亲测，**不能据此作为无人复核的财经生产工具放行**。仍有明确改进及验收事项，尚未达到“我已无新增改进点”的终止条件。

## 【给豆包的下一步请求】

依用户指令，本轮不联系豆包；原项目已在 r14 打开，用户可查看原资料、沿用已保存工作继续亲测，无需重复上传。
