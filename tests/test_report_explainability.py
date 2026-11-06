import unittest
from unittest import mock

from modules import chinese_report_generator as report
from modules import report_generator as english_report


class InitialReportExplainabilityTest(unittest.TestCase):
    def test_report_renderer_requires_pinned_runtime_and_fonts(self):
        with mock.patch.object(report.sys, "version_info", (3, 9, 9)):
            with self.assertRaisesRegex(RuntimeError, "Python 3.12.3 required"):
                report.validate_report_render_environment()

    def test_initial_header_uses_bilingual_paper_pecker_title(self):
        findings = {
            "paper": {"title": "Example", "doi": "10.1/example"},
            "image_duplicates": [],
            "image_splicing": [],
            "data_anomalies": [],
            "reference_issues": [],
        }
        html = report._build_full_html(findings, "")
        self.assertIn("论文复核报告", html)
        self.assertIn("Paper Review Report", html)
        self.assertNotIn("Paper Pecker", html)

    def test_high_risk_analysis_requires_structured_explanation(self):
        vague = "<p>公式计算结果与表格不一致。</p>"
        self.assertIn("问题位置", report._analysis_explanation_gaps(vague, "高风险"))

        clear = """
        <p><b>问题位置：</b>论文第5页 Table II 与公式(5)。</p>
        <p><b>问题类型：</b>公式计算结果与表格不一致。</p>
        <p><b>具体情况：</b>代入DS=2、BT=1得到As=1.29，表中为0.83。</p>
        <p><b>需关注原因：</b>该差异会改变模型参数比较。</p>
        <p><b>核查依据：</b>按论文公布的公式和参数逐项复算。</p>
        """
        self.assertEqual([], report._analysis_explanation_gaps(clear, "高风险"))

    def test_initial_label_and_formula_reference_are_normalized(self):
        findings = {
            "paper": {"title": "Example", "doi": "10.1/example"},
            "image_duplicates": [], "image_splicing": [],
            "data_anomalies": [], "reference_issues": [],
        }
        html = report._build_full_html(
            findings,
            '<p>为什么需要关注：第5页公式(\n5)与表格不一致。</p>',
        )
        self.assertIn("需关注原因", html)
        self.assertNotIn("为什么需要关注", html)
        self.assertIn('white-space:nowrap;', html)
        self.assertNotIn("公式(\n5)", html)

    def test_isolated_cross_dimension_alerts_do_not_auto_escalate(self):
        findings = {
            "paper": {"title": "Example", "doi": "10.1/example"},
            "image_duplicates": [{
                "page_a": 2, "page_b": 5, "image_a": "a.png", "image_b": "b.png",
                "severity": "high", "match_type": "partial_region",
            }],
            "image_splicing": [],
            "data_anomalies": [{
                "severity": "high", "test": "formula_mismatch", "details": {},
            }],
            "reference_issues": [],
        }
        overview = report._build_risk_score_html(findings)
        self.assertIn("综合风险等级：低风险", overview)
        self.assertRegex(overview, r"图像问题：</b><span[^>]+>低风险</span>")
        self.assertRegex(overview, r"数据异常检测：</b><span[^>]+>低风险</span>")

    def test_each_high_dimension_requires_its_own_complete_detail(self):
        analysis = """
        <h2>四、主要发现详情</h2>
        <p>（一）图像问题</p>
        <p>问题位置：第4页 Fig.2。</p><p>问题类型：图像重复。</p>
        <p>具体情况：两个区域相似。</p><p>需关注原因：影响图像比较。</p>
        <p>核查依据：逐页比对。</p>
        <p>（二）数据分析</p><p>未发现数据问题。</p>
        <p>（三）参考文献</p><p>未发现引用问题。</p>
        <h2>五、综合风险评估</h2>
        """
        gaps = report._analysis_explanation_gaps(analysis, "高风险", {"image", "data"})
        self.assertNotIn("image高风险维度完整说明", gaps)
        self.assertIn("data高风险维度完整说明", gaps)

    def test_unexplainable_high_signal_is_effectively_low_risk(self):
        findings = {
            "_explainability_downgraded": True,
            "image_duplicates": [{"severity": "high"}],
            "image_splicing": [],
            "data_anomalies": [{"severity": "high"}],
            "reference_issues": [],
        }
        overall = report._compute_overall_risk(findings)
        self.assertEqual("低风险", overall["level"])
        self.assertEqual(0, overall["score"])
        self.assertEqual(set(), overall["high_dimensions"])
        overview = report._build_risk_score_html(findings)
        self.assertIn("综合风险等级：低风险", overview)
        self.assertNotRegex(overview, r"：</b><span[^>]+>高风险</span>")
        fallback = report._build_explainability_downgrade_html()
        self.assertIn("综合判定为<span style=\"color:#080;\">低风险</span>", fallback)

    def test_risk_reconciliation_enforces_both_directions(self):
        high_findings = {
            "image_duplicates": [],
            "image_splicing": [],
            "data_anomalies": [
                {"severity": "high", "test": "formula_mismatch", "details": {}}
                for _ in range(7)
            ],
            "reference_issues": [],
        }
        image = report._compute_image_risk(high_findings)
        data = report._compute_dimension_risk(high_findings["data_anomalies"])
        ref = report._compute_dimension_risk([])
        overall = report._compute_overall_risk(high_findings)
        report._reconcile_risk_levels(high_findings, overall, image, data, ref)
        self.assertEqual("高风险", overall["level"])
        self.assertEqual("高风险", data["level"])
        self.assertEqual("低风险", image["level"])
        self.assertEqual("低风险", ref["level"])

        forced_low = dict(high_findings, _force_risk_level="低风险")
        image = report._compute_image_risk(forced_low)
        data = report._compute_dimension_risk(forced_low["data_anomalies"])
        ref = report._compute_dimension_risk([])
        overall = report._compute_overall_risk(forced_low)
        report._reconcile_risk_levels(forced_low, overall, image, data, ref)
        self.assertEqual("低风险", overall["level"])
        self.assertEqual("低风险", data["level"])

    def test_missing_detail_after_retry_sets_downgrade_flag(self):
        findings = {
            "paper": {"title": "Example", "doi": "10.1/example"},
            "image_duplicates": [], "image_splicing": [],
            "data_anomalies": [
                {
                    "severity": "high", "test": "formula_mismatch",
                    "location": f"第{i}页 Table {i}", "description": "数值不一致",
                    "details": {},
                }
                for i in range(1, 8)
            ],
            "reference_issues": [],
        }
        incomplete = "<h2>三、检测结果总览</h2><p>综合风险为高风险。</p>"
        with mock.patch.object(report, "chat", side_effect=[incomplete, incomplete]):
            _, analysis = report._generate_analysis_html(findings, "")
        self.assertTrue(findings["_explainability_downgraded"])
        self.assertIn("综合判定为<span style=\"color:#080;\">低风险</span>", analysis)

    def test_english_prompt_requires_paper_pecker_and_plain_language_fields(self):
        self.assertIn("# Paper Pecker Risk Alert Report", english_report.REPORT_TEMPLATE)
        self.assertIn("**Location:**", english_report.REPORT_TEMPLATE)
        self.assertIn("**Why it matters:**", english_report.REPORT_TEMPLATE)
        self.assertIn("**Verification basis:**", english_report.REPORT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
