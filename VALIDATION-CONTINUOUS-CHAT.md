# 连续对话与上下文恢复验证

验证日期：2026-10-29。

普通项目对话不再继承旧任务的轮次、模型调用数、累计输入输出或时长预算；已有交互任务按当前策略读取，历史审计记录与使用量保留。内部只读协作者继承主任务策略。显式外部调用授权、管理员配置的每日费用额度、取消操作仍然有效；没有新增默认每日额度。

模型上下文与任务预算分别处理。没有实际窗口限制时，规划请求保留完整可见历史，不再按固定六轮或一万字符裁掉最近内容。明确配置的单次上限和上游真实超限会触发压缩；正常容量内不提前压缩。原始消息、附件版本、执行回执不删除。

压缩使用可回查检查点：保留当前需求、关键否定约束、任务范围、记忆和执行状态；省略的历史、工具结果及参数有固定引用。检查点按用户、项目、来源轮次及记忆版本校验，不能当成财务证据。原生摘要失败或桥接层拒绝超长请求时，关闭旧桥接后重建模型会话，继续同一任务；同一无进展边界最多进行三次递进恢复，避免服务持续拒绝时无休止循环。这不是任务总调用额度。当前不可压缩的必要约束仍超出模型容量时，会保留任务并说明实际问题。

生成的 Office、工作环境文件、图片、音频及业务报告根据真实工具回执关联到对应对话，显示查看与下载卡片；“成果”仍提供集中管理。打开时沿用原有项目访问校验、固定版本与文件完整性检查。下载保留实际文件名和扩展名。

## 验证结果

- 267 项相关 Python 测试通过，涵盖真实 HTTP 超限响应、自动重试、检查点原文恢复、未撤回约束、记忆变更、预算迁移、协作授权边界、真实 Office 文件及 API 内成果关联。
- 25 个前端 DOM 回归脚本通过；下载格式调整后复核对话成果、成果面板及滚动三个脚本。
- 21 项原生插件 JavaScript 检查通过。
- Chromium 153 验证桌面和手机端对话成果入口、固定版本预览、PNG 页图与分页、无横向溢出。使用受控 API，未连接真实业务系统。
- 实际 OpenCode 2.1.2 进程加本地模拟模型：先执行一次文件写入，之后模型返回 HTTP 400 `context_length_exceeded`；原生摘要同样被模拟接口拒绝，Atlas 自动恢复并完成原任务。恢复前最大请求 125660 字符，恢复后 46864 字符；文件写入次数为 1，未要求用户继续，旧原生消息仍可回查。
- 实际 OpenCode 原生压缩与记忆撤回回归通过：摘要完成回执被核对，撤回后的旧偏好未进入新会话。

这些检查不代表已验证付费模型质量、Windows 安装包或生产部署；未运行全仓全部测试。

## 重现

```bash
node tests/ui/conversation-outputs.cjs
node --test desktop/tests/opencode-budget.test.cjs desktop/tests/opencode-context.test.cjs desktop/tests/opencode-working-set.test.cjs
ATLAS_OPENCODE_BINARY=/path/to/verified/opencode .venv/bin/python scripts/verify_context_overflow.py
ATLAS_OPENCODE_BINARY=/path/to/verified/opencode .venv/bin/python scripts/verify_context_runtime.py
ATLAS_BROWSER_EXECUTABLE=/path/to/chromium node tests/ui/browser-render.mjs
```

`verify_context_overflow.py` 在临时独立数据目录内运行，只访问本地模拟模型，不需要外部模型密钥。
