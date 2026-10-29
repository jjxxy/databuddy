# 工作模式验证记录

日期：2026-10-29。基线：`173281dca7aea8d64a06d44f89c54e326f9dd699`。

## 已执行检查

| 检查 | 结果与范围 |
| --- | --- |
| Python 全量回归 | 2100 passed、7 failed、34 skipped，640.94 秒 |
| 原始基线对照 | 在独立、未修改的基线工作树复跑相关失败项，以上 7 项均仍失败 |
| 最终相关回归 | 142 passed，21.38 秒；包含最后增加的历史成果执行版本定位 |
| 新增工作模式测试 | 包含于上述相关回归，共 33 项；覆盖持久状态、恢复、取消、输入版本、文件校验和执行协议 |
| 前端 DOM 回归 | `npm test --prefix tests/ui` 的 21 个脚本通过；最后调整的执行失败展示另行复跑 `work.cjs`、`work-runtime.cjs` 通过 |
| 构建与依赖检查 | Python wheel 构建、包含执行器与静态资源的打包检查通过；工作镜像 Python 依赖解析通过；部署 YAML 可解析 |

全量回归后仅补充了历史成果到执行版本的只读定位及相关测试，随后运行最终相关回归。没有为本次功能放宽原有财经复核或来源校验。

新增测试实际运行 Python 客体程序及文件传输 helper，并对 XLSX、DOCX、PPTX、PNG 做生成、校验、保存和读取。Docker 控制面使用明确的协议夹具；双向流测试通过本机 TCP 适配器测试真实 HTTP 升级和 Docker 输出帧，生产连接仍是 Unix socket。这些结果不能替代真实 Docker / gVisor 隔离验收。

## 基线已有失败

以下用例均在修改前的独立工作树复现：

- `tests/test_agent_collaboration_verifier.py::test_real_two_process_bidirectional_http_collaboration_with_explicit_model_fixture`
- `tests/test_financial_workflow_integrity.py::test_cached_formula_unknown_freshness_is_preserved_through_downstream_result`
- `tests/test_financial_workflow_integrity.py::test_excel_precision_history_risk_keeps_exact_stored_value_and_blocks_unadopted_brief`
- `tests/test_governance_boundaries.py::test_quote_contains_exact_code_and_does_not_inherit_model_status`
- `tests/test_isolated_deployment.py::test_committed_templates_match_renderer`
- `tests/test_native_launcher.py::test_native_lifecycle_uses_loopback_credentials_and_preserves_state`
- `tests/test_ocr_supply_chain.py::test_ocr_wheel_lock_exactly_matches_official_inventory`

其中部署模板用例缺少基线原有文件，原生启动用例等待就绪超时，OCR 清单断言预期 21 项而基线库存为 22 项。其他失败涉及原有协作或财经契约，需要独立排查；当前不能宣称全仓测试全部通过。

## 尚未验收

本次环境没有可用 Docker daemon / gVisor，未进行真实镜像构建和部署隔离验收，也未接入真实模型、业务系统或生产高可用集群。浏览器下载未完成，界面验证为 DOM 测试，没有实际浏览器布局验收。

按 [WORK-MODE.md](WORK-MODE.md) 部署独立执行服务后，运行 `python scripts/verify_work_mode.py --output work-mode-verification.json` 检查真实执行、隔离、文件检查点与取消。该脚本不调用模型；模型自主完成任务仍需单独使用真实模型验收。

## 重跑相关回归

```bash
python -m pytest tests/test_work_runtime.py tests/test_bounded_planning_context.py tests/test_extensions.py tests/test_context_working_set.py tests/test_agent_harness.py tests/test_workspace_artifacts.py tests/test_owner_stop_async_jobs.py tests/test_workflow_continuation_reuse.py::test_same_build_fingerprint_is_cached_and_matches_worker_process -q
npm ci --prefix tests/ui
npm test --prefix tests/ui
```
