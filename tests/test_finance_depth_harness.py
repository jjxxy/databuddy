"""Controller/validator counterexamples; these tests never call a model or author Office files."""
import importlib.util
from decimal import Decimal
from pathlib import Path
import unittest

PATH = Path(__file__).resolve().parents[1] / 'acceptance/depth/cases/finance_monthly.py'
spec = importlib.util.spec_from_file_location('finance_depth_harness', PATH)
case = importlib.util.module_from_spec(spec)
spec.loader.exec_module(case)


class ReportSemantics(unittest.TestCase):
    def test_values_bind_to_labels_including_shortfall_sign(self):
        blocks = ['2026年9月经营分析', '总收入：460,001.30元', '直接成本：239000.50元',
                  '毛利：221000.80元', '收入较预算少19998.70元']
        expected = {'收入元': Decimal('460001.30'), '直接成本元': Decimal('239000.50'),
                    '毛利元': Decimal('221000.80'), '收入预算差额元': Decimal('-19998.70')}
        values = case._metric_values(blocks)
        self.assertTrue(all(values[key] == [value] for key, value in expected.items()))
        swapped = case._metric_values(['总收入239000.50元，直接成本460001.30元，毛利221000.80元，收入预算差额-19998.70元'])
        self.assertNotEqual(swapped['收入元'], [expected['收入元']])
        self.assertNotEqual(swapped['直接成本元'], [expected['直接成本元']])

    def test_period_is_visible_business_period_not_ids_or_source_filename(self):
        self.assertTrue(case._period_is_september(['报告期间：2026年9月', '完整结果编号：run_october1234']))
        self.assertFalse(case._period_is_september(['2026年10月经营小结', '完整结果编号：run_09abc1234', '来源2026-09_业务源表.xlsx']))
        self.assertFalse(case._period_is_september(['来源2026-09_业务源表.xlsx', 'run_09abc1234']))

    def test_version_accepts_exact_id_or_digest_and_rejects_conflicting_ref(self):
        refs = {'run_09approved': 'a' * 64}
        self.assertTrue(case._version_reference('完整结果编号：run_09approved', [], refs))
        self.assertTrue(case._version_reference('交接摘要：' + 'a' * 64, [], refs))
        self.assertFalse(case._version_reference('结果run_09approved，结果run_09wrong', [], refs))
        self.assertFalse(case._version_reference('结果run_09approved 交接摘要：' + 'b' * 64, [], refs))

    def test_office_breakdown_is_not_misread_as_total_income(self):
        values = case._metric_values(['华东办事处收入195000.15元', '总收入460001.30元'])
        self.assertEqual(values['收入元'], [Decimal('460001.30')])

    def test_regional_scope_continues_after_commas(self):
        values = case._metric_values(['华东办事处：收入195000.15元，成本94000.05元，毛利101000.10元。',
                                      '总收入460001.30元，直接成本239000.50元，总毛利221000.80元，收入较预算少19998.70元'])
        self.assertEqual(values['直接成本元'], [Decimal('239000.50')])
        self.assertEqual(values['毛利元'], [Decimal('221000.80')])

    def test_capture_date_cannot_replace_report_period(self):
        self.assertFalse(case._period_is_september(['数据采集时间：2026-09-30', '2026年10月经营小结']))
        self.assertFalse(case._period_is_september(['报告期间：2026年9月', '2026年10月经营小结']))

    def test_actual_plain_hex_run_ids_reject_conflicting_result_reference(self):
        approved = 'a' * 24
        refs = {approved: 'c' * 64}
        self.assertTrue(case._version_reference('结果编号：' + approved, [], refs))
        self.assertFalse(case._version_reference('结果编号：' + approved + '；结果编号：' + 'b' * 24, [], refs))


class SourceAndPublicationScope(unittest.TestCase):
    def test_business_scope_confirmation_is_separate_and_contains_no_oracle_numbers(self):
        used = set()
        turn = {'outcome': 'needs_input', 'answer': '口径和异常清单已列出，未发现重复或冲突。请确认以上口径和预览，之后生成候选文件。'}
        reply = case.business_scope_confirmation(turn, used)
        self.assertIsNotNone(reply)
        self.assertIn('不确认当前预览数值', reply)
        self.assertIn('不授权正式采用、发布', reply)
        self.assertIsNone(__import__('re').search(r'\d', reply))
        self.assertIsNone(case.business_scope_confirmation(turn, used))

    def test_missing_mapping_or_conflicting_duplicates_cannot_be_scope_confirmed(self):
        for answer in ['缺少火星省有效映射，请确认规则和预览后按华北处理。',
                       '存在重复单据ID且两笔金额冲突，请确认处理方案。',
                       '日期存在歧义，请确认以上口径并默认美式日期。']:
            self.assertIsNone(case.business_scope_confirmation({'outcome': 'needs_input', 'answer': answer}, set()))

    def test_paused_turn_cannot_get_an_automatic_business_confirmation(self):
        self.assertIsNone(case.business_scope_confirmation({'outcome': 'paused', 'answer': '请确认以上口径和预览后继续。'}, set()))

    def test_budget_cache_requires_three_distinct_original_coordinates(self):
        source = {'id': 's_book', 'content_hash': 'fixture-hash'}
        def step(offset, row):
            return {'tool': 'read_source_table', 'status': 'done', 'observation': {
                'offset': offset, 'rows': [row], 'evidence': {'source_id': source['id'], 'content_hash': source['content_hash'],
                    'selection': {'sheet': '预算'}, 'source_rows': [5, 6, 7]}}}
        first = step(0, {'c_2': '10', 'c_3': '6', 'c_5': '4'})
        ctx = type('Context', (), {'turns': [{'agent_trace': [first, first, first]}]})()
        audit = case.audit_sources(ctx, [], source)
        self.assertFalse(audit['budget_cache_arithmetic_checked'])
        self.assertEqual(audit['budget_cache_distinct_source_rows'], [5])
        ctx.turns[0]['agent_trace'] += [step(1, {'c_2': '20', 'c_3': '7', 'c_5': '13'}), step(2, {'c_2': '30', 'c_3': '8', 'c_5': '22'})]
        self.assertTrue(case.audit_sources(ctx, [], source)['budget_cache_arithmetic_checked'])

    def test_publications_include_legacy_and_typed_with_project_scope(self):
        class Context:
            project_id = 'current'
            def api(self, method, path):
                if path.endswith('/data-workflows'):
                    return {'items': [{'published_run': 'r_typed', 'active_sha256': 'typed-plan'}]}
                return [{'project_id': 'current', 'published_run': 'r_legacy', 'active_commit': 'legacy-plan'},
                        {'project_id': 'other', 'published_run': 'r_other', 'active_commit': 'other-plan'}]
        self.assertEqual(case.publications(Context()), [
            {'kind': 'data_workflow', 'run_id': 'r_typed', 'plan_sha256': 'typed-plan'},
            {'kind': 'pipeline', 'run_id': 'r_legacy', 'plan_sha256': 'legacy-plan'}])

    def test_preview_uses_reader_file_sha256_when_content_hash_is_not_repeated(self):
        source = {'id': 's_book', 'content_hash': 'fixture-hash'}
        proof = {'source_id': source['id'], 'selection': {'sheet': '预算', 'file_sha256': source['content_hash']},
                 'source_rows': [5, 6, 7]}
        observation = {'offset': 0, 'evidence': proof, 'rows': [{'c_2': '10', 'c_3': '6', 'c_5': '4'}] * 3}
        ctx = type('Context', (), {'turns': [{'agent_trace': [{'tool': 'read_source_table', 'status': 'done', 'observation': observation}]}]})()
        self.assertTrue(case.audit_sources(ctx, [], source)['budget_cache_arithmetic_checked'])


if __name__ == '__main__':
    unittest.main()
