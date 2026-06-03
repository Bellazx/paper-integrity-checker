import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import io
import contextlib

sys.path.insert(0, "/opt/.claude/skills/paper-batch-review/scripts")

import coverage_validator
from modules.chinese_report_generator import _build_risk_score_html


def _load_generate_review_report():
    path = Path("/opt/.claude/skills/paper-batch-review/scripts/generate_review_report.py")
    spec = importlib.util.spec_from_file_location("generate_review_report_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoverageAuditTests(unittest.TestCase):
    def test_validator_warning_does_not_change_low_risk_by_default(self):
        result = {
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "已查看图像，未发现异常。",
            "data_review": "已查看数据，未发现异常。",
            "ref_review": "",
            "reason": "综合判断低风险。",
        }
        evidence = {
            "main_figures": ["Fig 1.png"],
            "high_findings": [],
            "deterministic_findings": {
                "duplicate_column_pairs": [],
                "cross_sheet_reuse": [{"locations": ["a.xlsx::Sheet1::value"]}],
                "decimal_precision_mismatch": [],
            },
        }

        validated, gaps = coverage_validator.validate(result, evidence)

        self.assertTrue(gaps)
        self.assertEqual(validated["result"], "低风险")
        self.assertEqual(validated["verdict"], "建议低风险")
        self.assertEqual(validated["_coverage_status"], "warning")
        self.assertNotIn("_coverage_downgraded", validated)

    def test_validator_checks_grouped_cross_sheet_reuse_not_every_raw_member(self):
        result = {
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "未见图像完整性问题。",
            "data_review": (
                "已核查跨表复用组1的代表样例，Sheet1 与 Sheet2 的 Area 列来自同一"
                "归一化源数据导出；该类126条跨表复用均属于同一处理链路，未见独立完整性问题。"
            ),
            "ref_review": "参考文献未见异常。",
            "reason": "跨表复用按分组核验后可解释，综合建议低风险。",
        }
        raw = [
            {"locations": [f"a.xlsx::Sheet{i}::Area", f"b.xlsx::Sheet{i}::Area"]}
            for i in range(1, 4)
        ]
        evidence = {
            "main_figures": [],
            "high_findings": [],
            "deterministic_findings": {
                "duplicate_column_pairs": [],
                "cross_sheet_reuse": raw,
                "cross_sheet_reuse_groups": [{
                    "group_id": "reuse_group_1",
                    "count": 126,
                    "headers_sample": ["Area"],
                    "locations_sample": ["a.xlsx::Sheet1::Area"],
                    "representatives": [{"locations": ["a.xlsx::Sheet1::Area", "b.xlsx::Sheet2::Area"]}],
                    "requires_expansion": True,
                }],
                "decimal_precision_mismatch": [],
            },
        }

        validated, gaps = coverage_validator.validate(result, evidence)

        self.assertFalse(gaps)
        self.assertEqual(validated["_coverage_status"], "ok")
        self.assertEqual(validated["result"], "低风险")

    def test_report_coverage_check_uses_cached_evidence_and_keeps_low_risk(self):
        grr = _load_generate_review_report()
        review = {
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "已查看图像。",
            "data_review": "已查看数据。",
            "ref_review": "",
            "reason": "低风险。",
        }
        evidence = {
            "main_figures": ["Fig 1.png"],
            "high_findings": [],
            "deterministic_findings": {
                "duplicate_column_pairs": [],
                "cross_sheet_reuse": [{"locations": ["a.xlsx::Sheet1::value"]}],
                "decimal_precision_mismatch": [],
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cached = cache_dir / "10.1_x_evidence.json"
            cached.write_text(json.dumps(evidence), encoding="utf-8")
            with mock.patch.object(grr, "REVIEW_V2_DIR", cache_dir), \
                 mock.patch("review_evidence.build_bundle") as build_bundle:
                checked = grr._coverage_check(review)

        build_bundle.assert_not_called()
        self.assertEqual(checked["result"], "低风险")
        self.assertEqual(checked["verdict"], "建议低风险")
        self.assertEqual(checked["_coverage_status"], "warning")

    def test_coverage_timeout_is_unavailable_not_high_risk(self):
        grr = _load_generate_review_report()
        review = {
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "图像未发现异常。",
            "data_review": "数据未发现异常。",
            "ref_review": "参考文献未发现异常。",
            "reason": "低风险。",
        }
        with mock.patch.object(grr, "_run_coverage_in_child", side_effect=TimeoutError("coverage timed out after 1s")):
            checked = grr._coverage_check(review)

        self.assertEqual(checked["result"], "低风险")
        self.assertEqual(checked["verdict"], "建议低风险")
        self.assertEqual(checked["_coverage_status"], "unavailable")
        self.assertIn("timed out", checked["_coverage_error"])

    def test_skip_coverage_flag_is_not_supported(self):
        grr = _load_generate_review_report()
        review = {
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "图像未发现异常。",
            "data_review": "数据未发现异常。",
            "ref_review": "参考文献未发现异常。",
            "reason": "低风险。",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            results = tmp_path / "results.json"
            results.write_text(json.dumps([review], ensure_ascii=False), encoding="utf-8")
            output = tmp_path / "out"
            old_argv = sys.argv
            sys.argv = [
                "generate_review_report.py",
                "--results", str(results),
                "--output", str(output),
                "--no-db",
                "--skip-coverage",
            ]
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        grr.main()
            finally:
                sys.argv = old_argv

    def test_same_output_and_nginx_dir_does_not_copy_pdf_onto_itself(self):
        grr = _load_generate_review_report()
        review = {
            "doi": "10.3389/fcimb.2021.649067",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "图像未发现异常。",
            "data_review": "数据未发现异常。",
            "ref_review": "参考文献未发现异常。",
            "reason": "低风险。",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            results = tmp_path / "results.json"
            results.write_text(json.dumps([review], ensure_ascii=False), encoding="utf-8")
            output = tmp_path / "review_v2"
            old_argv = sys.argv
            sys.argv = [
                "generate_review_report.py",
                "--results", str(results),
                "--output", str(output),
                "--no-db",
            ]

            def fake_render(_html, output_path):
                Path(output_path).write_bytes(b"%PDF-1.4\n")

            try:
                with mock.patch.object(grr, "_coverage_check", side_effect=lambda x: x), \
                     mock.patch.object(grr, "_find_report_json", return_value=None), \
                     mock.patch.object(grr, "_render_pdf", side_effect=fake_render), \
                     mock.patch.object(grr, "NGINX_REVIEW_DIR", output), \
                     mock.patch.object(grr.shutil, "copy2", side_effect=AssertionError("copy2 should be skipped")), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    grr.main()
            finally:
                sys.argv = old_argv

            self.assertTrue((output / "review_10.3389_fcimb.2021.649067.pdf").exists())

    def test_review_detail_sections_start_with_module_verdict(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "图像标记均为假阳性，\\n\\n不构成完整性问题。",
            "data_review": "数据异常均可解释。",
            "ref_review": "参考文献未发现异常。",
            "reason": "综合建议低风险。",
        })

        self.assertIn("本模块复核结论：建议低风险。", html)
        self.assertEqual(html.count("本模块复核结论：建议低风险。"), 3)
        self.assertNotIn("\\n", html)

    def test_module_verdict_detects_real_numeric_inconsistency(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "图像标记均为假阳性，未发现图像完整性问题。",
            "data_review": "独立确认以下4处真实数值不一致，平均值数学上不可能由所列原始数据得出。",
            "ref_review": "参考文献未发现异常。",
            "methodology_review": "另发现一组对照三联在不同条件标签下完全相同，属数据归属矛盾。",
            "reason": "数据维度存在确切且可复算的完整性问题，建议高风险。",
        })

        self.assertIn("本模块复核结论：建议高风险。", html)
        self.assertGreaterEqual(html.count("本模块复核结论：建议高风险。"), 2)
        self.assertIn("图像标记均为假阳性", html)
        self.assertIn("复核结论：</b><span style=\"color:#c00; font-weight:bold;\">疑似高风险", html)
        self.assertIn("本次复核认为，该论文的主要问题集中在数据、方法学/统计维度。图像和参考文献维度未发现支持该结论的独立问题。", html)
        self.assertIn("（四）方法学与统计核查", html)

    def test_module_verdict_respects_final_lowrisk_conclusion(self):
        grr = _load_generate_review_report()
        verdict = grr._section_verdict(
            "已核验11条图像重复告警，均为同图自比或页眉误匹配。"
            "部分来源图逐对像素尺寸完全一致，属同一图的两种来源。"
            "结论:图像维度未发现完整性问题。",
            "建议高风险",
        )

        self.assertEqual(verdict, "建议低风险")

    def test_reference_verdict_handles_real_reference_lowrisk(self):
        grr = _load_generate_review_report()
        verdict = grr._section_verdict(
            "参考文献1为IPCC AR5报告，匹配置信度偏低系报告类文献题录所致，"
            "标题相似度0.917且能对应到该报告DOI，属真实存在文献，无完整性问题。",
            "建议高风险",
        )

        self.assertEqual(verdict, "建议低风险")

    def test_module_verdict_detects_shifted_decimal_reuse(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "图像标记均为假阳性，未发现图像完整性问题。",
            "data_review": (
                "在 source data 文件中发现系统性、跨实验的整数位平移复用完整性问题："
                "多组本应彼此独立的实测数值共享 6-9 位完全相同的小数尾数，"
                "且无法用正常归一化解释。"
            ),
            "ref_review": "参考文献未发现异常。",
            "reason": "数据维度存在确切且可复算的完整性问题，建议高风险。",
        })

        self.assertIn("本模块复核结论：建议高风险。", html)
        self.assertIn("整数位平移复用", html)

    def test_module_verdict_detects_block_level_phenotype_mismatch(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "图像未发现异常。",
            "data_review": (
                "问题位置：Table S2 反向分析中 Ovarian cyst 第202-207行与 POF 第214-219行。"
                "发现：两块数据逐格 100% 一致，同样 6 个 SNP 及全部统计量完全相同。"
                "为什么重要：两者是不同 FinnGen 表型，不应使用同一组工具变量。"
                "核查依据：F=(Beta/SE)^2 自洽，指向表型归属错配。"
            ),
            "ref_review": "参考文献未发现异常。",
            "reason": "数据维度存在工具变量整块错配，建议高风险。",
        })

        self.assertIn("本模块复核结论：建议高风险。", html)
        self.assertIn("Table S2", html)

    def test_module_verdict_detects_figure_source_data_mismatch_even_with_false_positive_notes(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "图像检测结果均为假阳性，未发现图像完整性问题。",
            "data_review": (
                "经独立验证17个源数据xlsx文件，发现以下问题："
                "【关键问题】Fig. 2L与Fig. 2M的源数据完全一致，4列×3行共12个高精度浮点数值逐一相同。"
                "从生物学定义上，正确数值绝不可能等于Fig. 2L。"
                "对照发表图片，证实发表图与上传的Fig 2M源数据不匹配——源数据系Fig 2L的复制粘贴错误。"
                "【假阳性排除】其他跨sheet行重叠均为稀疏整数计数数据的自然重叠，属假阳性。"
            ),
            "ref_review": "参考文献标记均为假阳性。",
            "reason": "源数据存在可验证的完整性问题，建议高风险。",
        })

        self.assertIn("数据异常检测复核", html)
        self.assertIn("本模块复核结论：建议高风险。", html)
        self.assertIn("图像检测结果均为假阳性", html)
        self.assertIn("参考文献标记均为假阳性", html)
        self.assertIn("本次复核认为，该论文的主要问题集中在数据维度。", html)

    def test_module_verdict_keeps_reference_false_positive_low_risk(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "图像未发现异常。",
            "data_review": "数据存在整块错配。",
            "ref_review": "两条参考文献告警经核验均为提取碎片化所致的假阳性，非真实文献问题。",
            "reason": "数据维度存在工具变量整块错配，建议高风险。",
        })

        self.assertIn("本模块复核结论：建议低风险。", html)
        self.assertIn("提取碎片化所致的假阳性", html)
        self.assertIn("本次复核认为，该论文的主要问题集中在数据维度。", html)
        self.assertNotIn("数据、参考文献维度", html)

    def test_normalize_review_info_keeps_result_and_verdict_consistent(self):
        grr = _load_generate_review_report()
        info = grr._normalize_review_info({
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议高风险",
        })

        self.assertEqual(info["result"], "低风险")
        self.assertEqual(info["verdict"], "建议低风险")

    def test_internal_rule_names_are_humanized_in_review_text(self):
        grr = _load_generate_review_report()
        html = grr._analysis_html({
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "image_duplicates 均为假阳性。",
            "data_review": "cross_sheet_row_duplicate 为真；decimal_uniformity、Benford 和 value_recycling 为假阳性。",
            "ref_review": "参考文献未发现异常。",
            "reason": "数据维度存在 cross_sheet_row_duplicate，建议高风险。Benford 为假阳性。",
        })

        self.assertIn("图像重复告警", html)
        self.assertIn("跨表整行重复告警", html)
        self.assertIn("小数位一致告警", html)
        self.assertIn("首位数字分布", html)
        self.assertIn("数值重复使用告警", html)
        self.assertNotIn("cross_sheet_row_duplicate", html)

    def test_reason_text_is_split_by_issue_type(self):
        grr = _load_generate_review_report()
        text = (
            "确认高风险，触发维度为数据。"
            "数据维度存在 Table S2 整块错配。"
            "图像维度未发现拼接或像素重复；"
            "参考文献告警为文本抽取碎片化假阳性。"
            "最终判定为数据维度高风险。"
        )

        formatted = grr._format_reason_text(text)

        self.assertIn("。\n数据维度存在", formatted)
        self.assertIn("。\n图像维度未发现", formatted)
        self.assertIn("；\n参考文献告警", formatted)
        self.assertIn("。\n最终判定", formatted)

    def test_risk_overview_separates_duplicate_and_splicing_labels(self):
        html = _build_risk_score_html({
            "image_duplicates": [
                {"severity": "high"},
                {"severity": "medium"},
            ],
            "image_splicing": [
                {"page": 6, "details": "疑似拼接"},
                {"page": 10, "details": "疑似拼接"},
            ],
            "data_anomalies": [],
            "reference_issues": [],
            "summary": {},
        })

        self.assertIn("二、初筛风险概览", html)
        self.assertIn("以下为代码确定性规则的初筛结果，最终以 AI 复核结论为准。", html)
        self.assertIn("图像重复检测：", html)
        self.assertIn("图像拼接检测：", html)
        self.assertNotIn("图像检测（重复/拼接）", html)
        self.assertLess(html.index("图像重复检测："), html.index("图像拼接检测："))
        self.assertLess(html.index("图像拼接检测："), html.index("数据异常检测："))

    def test_risk_overview_always_shows_splicing_dimension(self):
        html = _build_risk_score_html({
            "image_duplicates": [],
            "image_splicing": [],
            "data_anomalies": [],
            "reference_issues": [],
            "summary": {},
        })

        self.assertIn("图像拼接检测：", html)
        self.assertIn("未检出疑似拼接图像", html)

    def test_review_overview_prioritizes_review_result_over_initial_result(self):
        grr = _load_generate_review_report()
        html = grr._review_overview_html({
            "image_duplicates": [{"severity": "high"}],
            "image_splicing": [{"severity": "high"}, {"severity": "high"}],
            "data_anomalies": [{"severity": "high"}],
            "reference_issues": [{"severity": "medium"}],
            "summary": {},
        }, {
            "doi": "10.1/x",
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "图像标记均为假阳性，未发现异常。",
            "data_review": "数据异常均可解释。",
            "ref_review": "参考文献未发现异常。",
            "reason": "综合建议低风险。",
        })

        self.assertIn("二、复核风险结论", html)
        self.assertIn("复核结论：建议低风险", html)
        self.assertIn("复核后维度结论", html)
        self.assertIn("三、初检结果参考", html)
        self.assertIn("初检综合风险", html)
        self.assertNotIn("复核结果总览", html)
        self.assertNotIn("综合风险等级：", html)
        self.assertLess(html.index("图像重复："), html.index("图像拼接："))
        self.assertLess(html.index("图像拼接："), html.index("数据异常："))

    def test_review_overview_omits_trigger_dimensions_in_main_verdict(self):
        grr = _load_generate_review_report()
        html = grr._review_overview_html(None, {
            "doi": "10.1/x",
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "图像标记均为假阳性，未发现异常。",
            "data_review": "发现真实数值不一致，数学上不可能。",
            "ref_review": "参考文献未发现异常。",
            "reason": "综合建议高风险。",
        })

        self.assertIn("复核结论：疑似高风险", html)
        self.assertNotIn("触发维度", html)

    def test_reason_normalization_removes_legacy_trigger_wording(self):
        grr = _load_generate_review_report()
        text = grr._normalize_verdict_phrases(
            "维持疑似高风险，触发维度为数据。核心问题明确。"
        )

        self.assertEqual(text, "疑似高风险。核心问题明确。")


if __name__ == "__main__":
    unittest.main()
