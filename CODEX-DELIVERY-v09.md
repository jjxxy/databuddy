# v0.9：Agent 历史恢复、无进展循环与续跑修补

## 【本轮改动摘要】

版本 **v0.9**，产品 Atlas DataBuddy v0.20.0；最终候选 **r16**。用户本轮要求先检查项目、稍后连接豆包；以下为源码、自测和内部交叉复审结论，尚非豆包复审结论。

本轮围绕用户指出的“处理流程重复很多遍”。上一轮验收的 G1 完整财经月结仍未通过，不以技术回归替代财经能力证明。新问题编号均为本轮自查编号，不冒充豆包问题编号。

| 编号 | 真实问题及用户痛点 |
| --- | --- |
| AG-1 | 新澄清轮能看到旧步骤引用，却无法按所属轮次恢复；部分恢复请求错误地返回当前摘要，导致原文永远读不到。 |
| AG-2 | 换包装步骤编号、分页长度、工具发现可延续无进展回读；被拦截调用还可能收到上一条无关回执。 |
| AG-3 | 读过的合法历史页未保留在实际工作上下文；真正压缩后又不能合法恢复，形成“忘记—重读”循环。 |
| AG-4 | 显式续跑没有完整保留计划；同任务已验证加工成果可能被重复排队。 |
| AG-5 | 继承步骤、复用成果、拒绝执行与本轮新操作展示混杂，造成重复执行观感。 |
| AG-6 | 工具描述仍禁止新接口支持的授权历史读取；完成判定按工具名清算失败，读 B 成功竟可抵消读 A 失败。 |

| 改动文件 | 问题映射与实现 |
| --- | --- |
| `agent_service/agent_context.py`、`context_compaction.py`、`opencode_context.mjs` | AG-1/AG-3：显式所属轮次、实时授权和隐私过滤、原页恢复、稳定资源版本与字符游标；保留不同合法页、精确去重；自读快照剔除恢复遍历记录，防止读自己制造新版本。 |
| 新增 `agent_service/agent_progress.py` | AG-2/AG-3：按资源、版本、区间和服务端压缩 epoch 判断新增证据；有效新页继续，重复或无效引用返回真实未执行回执。 |
| `agent_service/agent.py` | AG-2/AG-6：拒绝尝试持久化并绑定本次原生调用编号；重放重新检查读取权限；历史恢复说明与 schema 对齐；只有同工具、规范化后完全相同参数的后续成功才清算失败。 |
| `agent_service/opencode_runtime.py` | AG-3：只接纳当前已鉴权原生会话中真实 completed 压缩记录；去重推进 epoch，在生成和工具回调前同步；模型参数不能自行解锁。 |
| `agent_service/conversation.py` | AG-4/AG-5：明确续跑保留计划、完成索引，提供继承步骤数。 |
| `agent_service/project_data.py`、`data_tools.py` | AG-4：同任务复用前核验当前授权、冻结方案、原件快照、引擎指纹和完整交接包；返回原运行编号与时间，不新排队、不发布。 |
| `agent_service/static/app.js`、`static/index.html`、`tests/ui/work.cjs` | AG-5：继承历史默认折叠但可查看；当前尝试独立计数；明确“复用已有成果”“未重复执行”；更新静态资源版本。 |
| 新增 `tests/test_history_recovery_protocol.py`、`test_recovery_progress.py`、`test_opencode_context_epoch.py`、`test_workflow_continuation_reuse.py`、`test_failure_resolution.py`、`desktop/tests/history-recovery.test.cjs` | AG-1…AG-6：包含跨作用域/撤回、原始分页、真实请求投影、回执重放、压缩恢复、成果复用及失败清算反例。 |
| `tests/test_no_progress_discovery_loop.py`、`test_conversation_reliability.py`、`test_workflow_run_diagnostics.py` | AG-2/AG-4：核对被拒绝尝试而非隐藏它们；隔离测试遗留活动任务，保留失败证据，不跳过后台调度。 |
| `verification/audit-agent-loops-v09.py`、原生/真实模型恢复脚本、包清单及用户状态核验脚本 | AG-1…AG-6：只读原任务聚合审计、隔离合成验收与独立包核验；不自动发送、续跑或改写用户任务。 |

## 【实现说明】

### 根因与边界

原保存记录的一轮只有 **34 个用户字符**，却有 **145 次模型调用、140 个不同原生调用编号**：45 次历史读取，74 次工具结果读取失败，**业务工具执行为 0**。其中 69 个失败引用实际存在于其他已保存轮次；20 次成功恢复返回了相同的当前摘要；45 次历史分页中，23 次已被先前区间完整覆盖。证据为 `verification/agent-loop-baseline-v09.json`。这是实际新请求造成的恢复空转，不是同一个 HTTP 回调简单重发，也不能据此认定业务加工真的执行了 140 遍。

直接增加模型预算或盲目压缩不能解决这些问题。现在历史引用带明确来源，读取前实时核验权限，页有稳定原资源与版本标识；新证据保留，纯重复不能伪装成进展。真正的原生压缩完成后，新 epoch 允许重新恢复被压缩掉的原页。内部交叉复审曾发现“移除旧页但继续禁止重读”的误拦，已修正并加入三页保留及真实自动压缩验收。

本轮没有给普通主会话增加总调用数、累计字符或固定工具步数上限。无进展检测针对已经覆盖的证据或无效引用；新资源、新版本、新范围和明确续跑仍可继续。停止、授权、人工采用和真实外部子任务额度照常执行。

以下是失败清算的核心代码；参数键先由相应工具 schema 规范化，未知或无效参数不能清算失败：

```python
def _unresolved_failures(trace):
    from .agent_progress import BLOCKED_CODES
    completed=set();unresolved=[]
    for step in reversed(trace):
        key=_tool_request_key(step)
        if step.get('status')=='done':
            if key is not None:completed.add(key)
            continue
        if step.get('status')!='failed':continue
        observation=step.get('observation')
        if (isinstance(observation,dict) and observation.get('executed') is False
                and observation.get('code') in BLOCKED_CODES):continue
        if key is None or key not in completed:unresolved.append(step)
    return list(reversed(unresolved))
```

历史快照不是当前原件的最新版本，因此不会用“恢复历史成功”自动消除“读取当前原件失败”。这会保守保留部分状态；需要有明确、可验证的业务替代证据才能进一步改进，不能为显示绿色而清掉失败。

### 自测证据

最终 r16 的回归、包验收及启动状态以同目录下的最终证据记录为准；r15 候选及失败尝试保留用于对照，不能混写为最终通过。

| 实跑命令 / 操作 | 终端结果 / 证据 |
| --- | --- |
| `.venv/Scripts/python.exe -X utf8 -m pytest <verification/agent-recovery-v09-final-test-modules.json 中38模块> -q --junitxml=verification/agent-recovery-v09-final.xml` | **628 passed in 161.33s**，0 failure/error/skip。最终组合包含 17 项失败清算反例；不是全仓所有测试通过的声明。 |
| 随本机构建 Node 执行 `node --test desktop/tests/*.test.cjs` | **40 pass，0 fail**；`verification/desktop-recovery-v09.log`。 |
| `tests/ui/` 20 个 UI 脚本逐个实跑 | 全部通过，21 条 JSON（lifecycle 输出两条）；`verification/agent-recovery-ui-v09.log`。它们覆盖 DOM 与交互状态，未声称完整实机页面视觉验收。 |
| `verification/verify-native-compaction-recovery-v09.py` | 实际随包 OpenCode 2.1.2，20 次生成、4 次真实自动压缩；历史页在 epoch 0 与 4 均恢复，最终文件 verified_complete。模型是本地替身、付费调用 0；不是财经 LLM 证据。最终 r16 的相关原生压缩与恢复源码与该测试一致。 |
| r16 随包 `runtime/python/python.exe -X utf8 verification/verify-real-history-recovery-v09.py --app-root dist/Atlas-DataBuddy-win32-x64-r16/resources/app --model-config <现有私密配置> --artifact r16 --evidence-stem real-history-recovery-r16-v09` | **真实选定模型、5 次调用、verified_complete**；1 次历史页读取、2 次明确所属轮次的原结果读取、1 次实际文件写入。实际文件含原始合成 case_code 和 13% 税率，非空文件合同经验收，未加工/发布；`verification/real-history-recovery-r16-v09.json`。与原用户 145 次调用不是相同输入，不作性能倍率比较。 |
| `acceptance/offline/verify_backend.py --app-root dist/Atlas-DataBuddy-win32-x64-r16/resources/app --evidence-dir verification/backend-r16-delivery` | **ok=true**：包内运行时、空 PATH、新目录、local_only 导入→加工→人工采用→导出→审计，金额 **118.150000000000**；重启、错误密钥不阻断本地、缺运行文件人话提示均通过。 |
| `verification/audit_delivery_sources.py --artifact r16 --baseline r14` | **15,240/15,240** 清单文件哈希一致，**154** 个一方文件与源码一致，**15,006** 个随包运行时文件一致；无禁入私密配置/用户数据路径。相对 r14 为新增 1、修改 10 个生产文件，依赖未变；`verification/r16-source-inventory.json`。 |
| 打开最终 r16 原工作空间；执行 `verification/trial-r16-state-check.py before/after`；查看真实应用截图 | **ok=true**；原 **8 轮记录、2 个源文件、浏览器草稿与待发送状态**哈希一致，原生服务 ready，未提交/续跑用户任务。工作台实际显示原项目与“继续这项工作”按钮。两个“打开财经月结亲测”快捷方式均指向 r16，保留原 profile。 |

最终产物为 `dist/Atlas-DataBuddy-win32-x64-r16/Atlas DataBuddy.exe`。运行生成的 `__pycache__` 不属于交付清单；打包下载应只用清单文件，不把运行缓存或原工作空间装入包。

真实模型首次隔离恢复测试 `verification/real-history-recovery-v09.json` 整体 **ok=false、partial**。它确实找回旧回执、保存正确合成编号和税率且没有加工发布，但最初误读已不存在的当前文件；脚本还把文本任务误当成文件合同，空要求的 `task_verified=true` 不足作为交付证据。原记录保留。后续脚本显式指定文件目标、核验非空文件合同，并说明原件仅存历史，不靠修改预期把失败写成通过。

## 【反驳意见】

本轮尚未收到豆包的新编号清单，**无对外反驳**。对“删掉全部控制就能消除循环”的潜在处理方式不采纳：这会放大无证据回读成本，不能修复旧引用、假恢复和成果重复执行。替代方案是默认主会话无限策略加可审计的进展判断，保留授权和真实财务验收。

## 【自评扫描】

1. 空输入、超长输入、空文件、错误配置、缺运行文件、路径与权限边界、同调用重放和并发相关回归已运行。真实模型恢复使用隔离合成空间，没有自动重跑用户的失败月结。
2. 包内随包 Python/Git、新状态目录、空继承 PATH 验收证明不依赖开发目录的 Node 构建或全局运行时。离线策略为 `local_only`；用户已表示不做手动断网，本轮没有声称物理断网或防火墙隔离通过。
3. 无效引用与无进展都有持久回执和人话提示，不复用上一条成功回执；缺运行文件返回重新完整解压说明。原失败步骤与审计记录保留。
4. 没有新增通用编辑器、重写业务核心或自动发布。复用仅限现有成果、现有权限、当前任务恢复链与精确快照。
5. 已检验不同文件/分页/工作流/发布对象不能清算失败，真实压缩后可恢复，撤回后不能读旧缓存，变化/损坏/受限成果不复用，继承历史可见。DOM/状态脚本不是实际浏览器视觉验收。

**交付结论：本轮修补可供人工监督亲测，不能据此放行无人复核的财经生产。** G1 的 A02–A05 完整真实模型月结及独立十进制导出校验仍未通过；G2–G10 不能由本轮技术回归推定通过。尚未满足“没有可改进点”的终止条件。

## 【给豆包的下一步请求】

连接后请重点复审历史恢复的授权与版本边界、真实压缩后的合法重读、成果复用的精确条件、失败清算是否仍可能误报，以及按原 G1 基线的完整财经月结证据；待发送材料为 `DOUBAO-REQUEST-v04.md`。
