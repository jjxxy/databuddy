# v0.6：修复“继续”消息的资源不存在（r9）

## 【本轮改动摘要】

版本号 v0.6；产品版本仍为 Atlas DataBuddy v0.20.0，便携构建 r9。对应用户本轮报错，不联系豆包。

| 文件 | 对应问题与改动 |
| --- | --- |
| `agent_service/static/app.js` | U1：“继续”按钮传错任务编号。改用真正插值的模板字符串；对明确拒绝且消息状态确认为未接收的继续请求解除冻结，保留输入、附件、模型。未知发送结果继续沿用原编号，不擅自重发。 |
| `agent_service/conversation.py` | U1：缺失继续目标不再落入通用“资源不存在”，返回可操作的 HTTP 400；原任务权限、资料快照、模型及幂等校验保留。 |
| `tests/ui/resume-message.cjs`、`tests/ui/package.json` | U1：真实 DOM 按钮点击检查实际任务编号；覆盖明确拒绝、安全解除冻结、未知/已接收状态继续冻结等路径。 |
| `tests/test_resume_request_boundary.py` | U1：不存在/模板字面量目标的明确拒绝，以及正确继续沿用原附件、重复请求只创建一个任务。 |
| `agent_service/opencode_runtime.py` | U2：恢复后模型上下文超限，被同时在途的 idle 轮询覆盖为泛化错误。优先保留桥接回调已设置的真实错误或经验证的终态。预算及业务验收门槛均未修改。 |
| `tests/test_opencode_inflight_stop.py` | U2：通过真实本地 HTTP 桥复现预算拒绝/合法完成与轮询竞争；无并发停止的未验证 idle 仍拒绝。 |
| `verification/diagnose-send-resource.py`、`recover-resume-request.py` | U1：只读引用核查；确认原消息未接收后仅更正目标编号，沿用原消息 UUID 恢复，并核对重复提交返回同一任务。 |
| `verification/diagnose-resumed-native-stop.py`、`summarize-resumed-task.py` | U2：原生停止元数据与安全枚举摘要，保留失败证据，不复制提示词和表格内容。 |
| `verification/finalize-r9-staging.py`、`verify-opencode-recovery-v09.py`、`trial-r9-state-check.py` | 打包完成后仅在尚未使用的 r9 staging 补入最后的轮询修补并重算对应清单项；实际随包运行时真实模型诊断；更新前后原项目、资料、任务及草稿一致性核查。 |

根目录 `打开财经月结亲测.lnk` 已指向 r9，沿用原测试工作空间和模型配置。旧 r8 交付目录未改。

## 【实现说明】

### 根因

U1 的根因是单引号 HTML 中写了 `${esc(t.id)}`，它没有执行模板插值。前端把这段字面量当作 `resume_turn_id` 发送，项目、会话和两份附件实际均存在。后端通用 404 又让前端按“发送结果未知”冻结原请求，从而反复发送同一个错误目标。

修补保留“未知结果不能换号重发”的幂等规则。只有服务器明确返回未接收，且仍是原项目、原 pending 对象，才允许恢复编辑；它不会自动创建第二条任务。

U2 是独立缺陷：回调线程因上下文上限设置了停止及 `BudgetExceeded`，主线程却可能已在查询原生会话。查询返回 idle 后，旧代码先抛 `ModelOutputError`，掩盖了真实原因。本轮在查询返回后先检查停止事件，再统一处理真实错误/成功；未经验证的 idle 不因此放行。

关键代码摘录（保留原任务权限和业务验证）：

```python
if value.resume_turn_id:
    try:
        resume_target = get('turn', value.resume_turn_id)
    except KeyError:
        raise ValueError(
            '待继续的任务不存在，请刷新对话后再点原任务的“继续”。输入和附件已保留。'
        ) from None

# A callback may finish or fail while this request is in flight.
messages = _request(value, 'GET', '/api/session/' + session_id[0] + '/context')['data']
if stop.is_set():
    break
if messages and messages[-1].get('type') == 'idle':
    raise provider.ModelOutputError('OpenCode会话已结束，但尚未提交通过验收的结果；已有步骤已保留')
```

### 自测证据

- 后端：`.venv/Scripts/python.exe -X utf8 -m pytest tests/test_resume_request_boundary.py tests/test_multi_file_conversation.py tests/test_conversation_reliability.py -q`，**43 passed in 13.91s**；`verification/resume-http-regression-v01.xml`。
- 界面：整套 **18 个脚本通过**，含新 DOM 点击/恢复回归；`verification/resume-ui-regression.log`、`resume-message-regression.log`。不是人工截图验收。
- 原生运行时：竞争、就绪、桥接及压缩相关 **59 passed in 5.76s**；`verification/opencode-inflight-stop-regression.xml`。竞争测试走真实 HTTP 桥和任务验证器，未调用模型。
- 现场恢复：`verification/resume-request-recovery.json`，原 request UUID 保留、原模型和资料快照保留、重复 POST 返回同一个新任务。先确认 `accepted=false` 后恢复，随后 `accepted=true`。
- 真实月结恢复的实际结果：**7 次模型调用**，完成资料检查、多个页签读取、计划及加工指南读取后因**单次上下文上限**暂停。`verification/resumed-task-outcome.json` 明确 `G1_passed=false`，没有生成已发布月结结果。历史失败文本原样保留，不改写成成功；新版本仅修复未来错误的优先级。
- r9 最终包：`verification/desktop-build-v09.log` 与 `r9-finalization.json`，15238 清单项、运行库 15006 文件／743300599 字节。最终三处源码哈希与包体和 `FILES.sha256` 一致，没有携带 `llm.json` 或用户工作空间。
- r9 实跑：`.venv/Scripts/python.exe -X utf8 verification/verify-opencode-recovery-v09.py`，全新中文目录、空继承 PATH、随包 Python 启动，真实模型返回“运行诊断通过”，`model_calls=1`，重复提交同一编号返回同一任务；`verification/opencode-connected-v09.json` 的 `ok=true`。此项仅诊断消息，不冒充财经验收。
- 部署前活跃任务 **0**。r9 打开后 `verification/trial-r9-after.json` 的 `saved_state_preserved=true`；原项目、两份资料、两条任务、草稿摘要及原请求已接收状态一致。实际服务 `/app.js` 哈希与修补源码一致。
- Windows 原生可访问性树确认 r9 已打开原项目，显示两份资料、继续按钮及“草稿已同步”。本轮没有成功取得可用于视觉验收的截图，没有声称实际鼠标点击通过。

## 【反驳意见】

无反驳。资源不存在不等于资料丢失。完整月结暴露的上下文容量问题仍然存在，不能用简单诊断消息成功代替 G1 放行，也不能靠静默提高预算或删除财经证据掩盖。

## 【自评扫描】

1. 本轮覆盖缺失/错误引用、重复请求、未知发送结果、已接收状态、上下文预算拒绝和并发完成。空输入等无关边界沿用既有回归，不声称全部重新实机操作。
2. r9 空 PATH、随包运行时、全新中文目录启动并真实调用已验；未进行物理断网测试，遵从用户“不用断网”指令。
3. 新缺失目标报错可操作；新轮询保留真实预算错误。历史失败记录不改写，不把密钥、表格或提示词写入公开证据。
4. 没有新增重启入口、自动改配置、自动发表结果或后台重复提交。
5. 本轮修补回归通过；G1 完整财经月结仍未通过，G2–G10 仍未验证，尚不能声明产品可完整交付财经业务用户。

## 【给豆包的下一步请求】

依用户最新指令，本轮不调用豆包；窗口已打开，交用户继续亲测。
