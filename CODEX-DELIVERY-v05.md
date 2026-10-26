# v0.5：AI 运行服务就绪检查修补（r8）

## 【本轮改动摘要】

- 版本号 v0.5，产品版本仍为 v0.20.0，便携构建 r8。
- 对应用户问题：亲测任务提示“OpenCode运行服务未就绪，已有工作已保存”。未联系豆包。
- `agent_service/opencode_runtime.py`：拆出脱敏就绪诊断，正常任务入口对本机连接/超时及 HTTP 429/502/503/504 最多短重试一次；完整性、插件、版本、身份和配置不一致继续立即阻止执行。JSON 文件显式使用 UTF-8，错误提示给出继续操作。
- `tests/test_opencode_readiness.py`：新增 18 项有意义的暂态恢复、重试上限、拒绝配置漂移及敏感信息不入日志测试。
- `verification/diagnose-trial-opencode.py`、`probe-opencode-process-context.py`、`probe-live-model-readiness.py`、`verify-opencode-recovery-v08.py`、`trial-r8-state-check.py`：本机专项诊断及部署前后状态验证。根目录亲测快捷方式已指向 r8。

## 【实现说明】

已确认这次失败发生在请求模型前，用户月结任务没有模型调用记录。旧入口只检查一次；任一版本、插件、配置或本机 HTTP 检查失败都被吞成 False，再用同一条消息暂停任务。旧日志没有保存具体检查项，因此**不能确认历史那次是超时、临时状态或其他检查失败，不能归因于密钥或进程退出**。

随后对同一实例的本机版本、插件、源路径、文件摘要与生效配置逐项检查均正常；同一后台独立诊断项目成功完成一次真实模型调用。修补针对已确认的“一次检查失败即暂停、缺少细项诊断”缺陷，不以重写配置或静默切换引擎放行。

核心逻辑（完整权限与配置检查仍由 readiness_diagnostic 执行）：

```python
def require_ready():
    for attempt in range(2):
        diagnostic = readiness_diagnostic()
        if diagnostic['ready']:
            return diagnostic
        log.warning(
            'OpenCode readiness failed code=%s stage=%s retryable=%s',
            diagnostic['code'], diagnostic['stage'], diagnostic['retryable'])
        if attempt == 0 and diagnostic['retryable']:
            time.sleep(.2)
            continue
        # No provider configuration or exception message enters this error.
        raise ValueError('AI运行服务暂不可用，资料和已有工作已保留。')
```

上面为核心摘录；实际日志还包含安全枚举形式的错误类型、HTTP 状态和尝试次数，实际用户文案区分暂态连接故障与配置校验失败。

实际自测：

- 新 18 项及原生运行时/桥接/压缩回归共 **56 passed**，`verification/opencode-readiness-regression-v01.xml`。
- 原 r7 同一现场一次真实模型诊断成功，`verification/user-opencode-live-model-probe.json`。
- r8 实际随包 Python，以空继承 PATH 启动新服务，在全新**中文路径工作区**完成一次真实模型调用，回答“运行诊断通过”，`model_calls=1`、`ok=true`；`verification/opencode-connected-v08.json`。未发送业务文件，未提高预算。
- r8 构建核对 15238 文件，运行库 15006 文件／743300599 字节，未携带模型配置；`verification/desktop-build-v08.log`。
- 更新前确认活跃任务为 0；更新后原项目、原任务 ID/状态、两份附件 ID、草稿摘要一致，服务就绪；`verification/trial-r8-before.json`、`trial-r8-after.json`，后者 `saved_state_preserved=true`。

## 【反驳意见】

无反驳。历史单次失败原因尚不能准确还原，保留未知；本轮没有宣称完成月结 G1 验收。

## 【自评扫描】

1. 超时/连接故障/HTTP 暂态、401/403/408/500、版本与插件/配置漂移、重试次数和日志敏感信息已针对性验证；未重复无关输入边界测试。
2. r8 空 PATH、随包运行时、全新中文目录启动及真实调用已验，无业务机工具链依赖。
3. 就绪失败现在有阶段和安全枚举诊断，不输出密钥、配置、URL 或原始异常消息。
4. 没新增重启菜单、自动修改配置、自动启停服务或其他业务功能。
5. 本轮就绪与调用回归通过；完整财经 G1 及原报告中的未测项仍不放行。旧失败记录保留，用户点“继续这项工作”会沿用原资料与步骤。

## 【给豆包的下一步请求】

依用户指令，本轮不调豆包，继续由用户亲测。
