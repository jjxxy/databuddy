# v07 评审回应：事实来源和 Agent 交接

- 接受来源祖先风险：隔离合成反例证明，已核对事实资料经文件替换及缩进结构派生后，普通来源复核曾允许将净收入 123.45 与预算 200 相加并发布。当前代码对固定版本的祖先链进行核对；来源链缺失、冲突、跨项目或成环时按风险处理。
- 接受 Agent 来源交接风险：合成反例证明，事实资料被交接为普通来源后，错误的 323.45 曾可经普通复核发布。当前代码阻断事实来源作为普通资料交接，并对新旧外部交接来源要求独立语义核对。这一保守策略也限制普通跨 Agent 资料的正式复用。
- 对 v07 的导入结果疑点：结果绑定使用 `require_safe_numeric_lineage`，不走报告中假设的普通来源判定。`tests/test_agent_handoff_fact_guard.py::test_imported_result_remains_readable_but_not_reusable_even_with_historical_status` 验证，合成导入结果可读，但无论处于 validated 还是模拟 succeeded 状态，作为下游输入、检查报告或以认证用户确认发布都会被拒绝；该模拟不是对所有真实历史数据库的迁移验收。普通来源解释复核并不等同于可加性审批，因此不采用把所有 `requires_semantic_review` 一律作为事实标记的建议。
- 对历史已发布错误结果：`tests/test_fact_semantic_gate.py::test_historical_mixed_report_remains_readable_but_not_reused_or_refreshed` 使用合成的错误历史结果验证保留查看、阻断新报告与下游输入，并且自动刷新不会替换原发布。证据不足的历史记录按未核对处理；匿名导出的文件重传可能丢失原有来源，不能据此认定全部历史输入均可追溯。
- 测试证据和限制见 [财经来源血缘修复验证](verification/finance-lineage-review-20261028.json)。这不证明真实财经数据、OCR、完整月结或报告准确率。r24 是发现上述问题前的测试包，不能用于财务生产。
