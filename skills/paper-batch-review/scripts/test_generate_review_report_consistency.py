#!/usr/bin/env python3
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

sys.path.insert(0, "skills/paper-batch-review/scripts")
sys.path.insert(0, ".")

import generate_review_report as report


class DimensionReasonRoutingTest(unittest.TestCase):
    def test_review_report_base_url_uses_server_environment(self):
        self.assertEqual(
            report.REVIEW_REPORT_BASE_URL,
            f"{report.PUBLIC_BASE_URL}/review_reports",
        )

    def test_review_header_uses_bilingual_paper_pecker_title(self):
        header = report._header_html({"doi": "10.1/example", "title": "Example"})
        self.assertIn("Paper Pecker论文预警报告（AI复核版）", header)
        self.assertIn("Paper Pecker Risk Alert Report — AI Review", header)

    def test_high_risk_requires_reader_explainable_detail(self):
        vague = {
            "doi": "10.1/vague",
            "result": "高风险",
            "verdict": "建议高风险",
            "reason": "存在公式不一致，建议高风险。",
        }
        with self.assertRaisesRegex(ValueError, "not reader-explainable"):
            report._validate_explainable_highrisk_detail(vague)

        structured = {
            "doi": "10.1/clear",
            "result": "高风险",
            "verdict": "建议高风险",
            "data_review": (
                "问题位置：论文第5页 Table II 与公式(5)。\n"
                "问题类型：公式计算结果与表格不一致。\n"
                "具体情况：代入DS=2、BT=1得到As=1.29，表中为0.83。\n"
                "需关注原因：该差异会改变模型参数比较。\n"
                "核查依据：按论文公布的公式和参数逐项复算。"
            ),
            "reason": "论文第5页 Table II 的复算值与表格不一致，建议高风险。",
        }
        report._validate_explainable_highrisk_detail(structured)

    def test_legacy_deterministic_excel_finding_is_reader_explainable(self):
        info = {
            "doi": "",
            "_report_slug": "task_20260727-test",
            "result": "高风险",
            "verdict": "建议高风险",
            "trigger": "源数据确定性审计：rectangular_matrix_reuse",
            "data_review": (
                "问题位置：Supplementary Data S2.xlsx / CF AQDI "
                "R4:R8 C1:C136 与 Supplementary Data S8.xlsx / CF ADQI "
                "R4:R8 C1:C136。"
                "发现：两个5x136数值矩阵逐格完全相同，共680个测量值。"
                "为什么重要：不同源数据文件中的整块数据相同，不能由共享横轴解释。"
                "核查依据：示例数值为1、5、5、2、5。"
            ),
            "reason": "源数据确定性审计发现跨文件数值矩阵复用，建议高风险。",
        }
        report._validate_explainable_highrisk_detail(info)

    def test_empty_doi_error_uses_report_slug(self):
        info = {
            "doi": "",
            "_report_slug": "task_20260727-test",
            "result": "高风险",
            "reason": "存在异常，建议高风险。",
        }
        with self.assertRaisesRegex(ValueError, "task_20260727-test"):
            report._validate_explainable_highrisk_detail(info)

    def test_review_report_does_not_render_initial_result(self):
        info = {
            "doi": "10.1/example",
            "result": "高风险",
            "verdict": "建议高风险",
            "data_review": "问题位置：第5页 Table II。问题类型：数值不一致。",
            "reason": "第5页 Table II 数值不一致，建议高风险。",
        }
        overview = report._review_overview_html({"data_anomalies": []}, info)
        analysis = report._analysis_html(info)

        self.assertNotIn("初检结果参考", overview)
        self.assertNotIn("初检综合风险", overview)
        self.assertNotIn("初检结果仅作为复核背景", overview)
        self.assertIn("三、复核详情", analysis)
        self.assertIn("四、综合评估", analysis)

    def test_review_high_without_any_high_module_is_downgraded(self):
        info = {
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "未发现真实图像问题。",
            "data_review": "未发现数据问题。",
            "ref_review": "未发现参考文献问题。",
            "methodology_review": "未发现方法学问题。",
            "reason": "综合评估：未发现独立高风险证据。",
        }
        reconciled = report._reconcile_review_verdict(info)
        self.assertEqual("低风险", reconciled["result"])
        self.assertEqual("建议低风险", reconciled["verdict"])
        self.assertTrue(reconciled["_risk_reconciliation_downgraded"])

    def test_low_risk_summary_does_not_mention_initial_screening(self):
        info = {"result": "低风险", "verdict": "建议低风险"}
        analysis = report._analysis_html(info)
        self.assertNotIn("初筛", analysis)
        self.assertNotIn("初审", analysis)
        self.assertNotIn("初检", analysis)
        self.assertIn("本次复核未发现具体的论文完整性问题", analysis)

    def test_review_disclaimer_follows_review_section_numbering(self):
        self.assertIn("五、免责声明", report.REVIEW_DISCLAIMER_HTML)
        self.assertNotIn("六、免责声明", report.REVIEW_DISCLAIMER_HTML)

    def test_explanation_labels_are_visually_emphasized(self):
        rendered = report._highlight_explanation_labels("问题位置：第5页 Table II。")
        self.assertIn("<b>问题位置：</b>", rendered)

    def test_review_formula_reference_is_not_split(self):
        rendered = report._text_to_html("第4部分公式(\n5)与Table II不一致。")
        self.assertIn("需关注原因", report._text_to_html("为什么需要关注：数值影响结论。"))
        self.assertIn('white-space:nowrap;', rendered)
        self.assertNotIn("公式(\n5)", rendered)

    def test_review_uses_plain_research_design_label(self):
        info = {
            "result": "低风险",
            "verdict": "建议低风险",
            "image_review": "未发现具体图像问题。",
            "data_review": "未发现具体数据问题。",
            "ref_review": "未发现具体参考文献问题。",
            "reason": "本次复核未发现具体的论文完整性问题。",
        }
        rendered = report._analysis_html(info)
        self.assertIn("研究设计与统计核查", rendered)
        self.assertIn("实验或研究设计", rendered)
        self.assertNotIn("方法学与统计核查", rendered)

    def test_only_high_dimension_evidence_is_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "annotation.png"
            image.write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
                b"\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            high = {
                "result": "高风险",
                "verdict": "建议高风险",
                "image_review": "问题位置：第6页 Fig.2A。确认存在图像完整性问题。",
                "data_review": "未发现独立支持高风险的数据问题。",
                "ref_review": "未发现独立支持高风险的参考文献问题。",
                "reason": "图像维度：第6页 Fig.2A 存在图像完整性问题。",
            }
            items = report._review_evidence_images(
                high,
                {"image_duplicates": [{"severity": "high", "page_a": 6, "page_b": 0,
                                       "annotation_path": str(image)}]},
                Path(tmp) / "scratch",
            )
            self.assertEqual(1, len(items))
            html = report._evidence_images_html(items)
            self.assertIn("证据截图", html)
            self.assertIn("原始图文件", html)

            low = dict(high)
            low["result"] = "低风险"
            low["verdict"] = "建议低风险"
            self.assertEqual([], report._review_evidence_images(
                low, {"image_duplicates": [{"severity": "high", "annotation_path": str(image)}]},
                Path(tmp) / "low_scratch",
            ))

    def test_compact_mode_removes_dismissed_block_and_shortens_summary(self):
        info = {
            "result": "高风险",
            "verdict": "建议高风险",
            "data_review": (
                "问题位置：第5页 Table II。\n"
                "问题类型：表格与正文不一致。\n"
                "具体情况：正文为10，表格为5。\n"
                "需关注原因：影响核心参数比较。\n"
                "核查依据：按表中数值复算。\n"
                "【已排除的项】坐标轴模板告警10项均为假阳性，逐项过程很长。"
            ),
            "reason": "第5页 Table II 存在数值不一致，建议高风险。",
        }
        rendered = report._analysis_html(info)
        self.assertIn("问题位置", rendered)
        self.assertNotIn("逐项过程很长", rendered)
        self.assertIn("不等同于已经证明存在学术不端", rendered)

    def test_non_image_page_evidence_uses_only_high_dimension_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            doc = report.fitz.open()
            for page_no in range(4):
                page = doc.new_page()
                page.insert_text((72, 72), f"page {page_no + 1}")
            doc.save(pdf)
            doc.close()
            info = {
                "result": "高风险",
                "verdict": "建议高风险",
                "data_review": "问题位置：第2页 Table I。确认存在数据完整性问题，表格数值不一致。",
                "ref_review": "本模块复核结论：建议低风险。参考文献经核查无实质问题。",
                "reason": "第2页 Table I 数据不一致。",
                "_pdf_path": str(pdf),
            }
            items = report._review_evidence_images(info, {}, Path(tmp) / "scratch")
            self.assertEqual(1, len(items))
            self.assertEqual("data", items[0]["dimension"])
            self.assertIn("第2页", items[0]["caption"])

    def test_excel_evidence_regions_render_from_submitted_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_dir = root / "source data"
            source_dir.mkdir()
            workbook_path = source_dir / "source.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Fig2"
            sheet.append(["group", "A", "B", "C"])
            sheet.append(["x", 4, 7, 3])
            sheet.append(["y", 5, 8, 5])
            workbook.save(workbook_path)

            info = {
                "result": "高风险",
                "verdict": "建议高风险",
                "dimension_verdicts": {"data": "建议高风险"},
                "_input_dir": str(root),
                "excel_evidence_regions": [{
                    "dimension": "data",
                    "caption": "精确Excel区域",
                    "regions": [{
                        "label": "区域A",
                        "file": "source data/source.xlsx",
                        "sheet": "Fig2",
                        "row_range": [2, 3],
                        "col_range": [2, 4],
                    }],
                }],
            }
            scratch = root / "scratch"
            items = report._review_evidence_images(info, {}, scratch)

            self.assertEqual(1, len(items))
            self.assertEqual("data", items[0]["dimension"])
            self.assertEqual("精确Excel区域", items[0]["caption"])
            self.assertTrue(Path(items[0]["path"]).is_file())
            self.assertGreater(Path(items[0]["path"]).stat().st_size, 1_000)

    def test_negated_methodology_note_does_not_raise_data_dimension(self):
        info = {
            "result": "低风险",
            "verdict": "建议低风险",
            "data_review": (
                "主要定量面板与正文数值逐项一致。综上，未见逐值复用、"
                "末位数字集中或跨图标注矛盾等异常。"
            ),
            "reason": (
                "(1) 图像维度，未发现真实重复。"
                "(2) 数据维度，主要定量面板与正文严格匹配，未见典型异常。"
                "(3) 参考文献维度，未见明显不一致。"
                "(4) 方法学维度，未单独列出动物伦理审批编号属可改进的"
                "规范性问题但不构成数据完整性问题。综合判定为低风险。"
            ),
        }

        chunks = report._reason_chunks_for_dimension(info["reason"], "data")

        self.assertTrue(chunks)
        self.assertIn("数据维度", chunks[0])
        self.assertNotIn("动物伦理审批编号", chunks[0])
        self.assertEqual("建议低风险", report._dimension_verdict(info, "data"))

    def test_final_low_risk_caps_all_dimension_labels(self):
        info = {
            "result": "低风险",
            "verdict": "建议低风险",
            "data_review": "初检标记数据复用，经核查为解析假阳性。",
            "reason": "数据维度未发现真实完整性问题，综合判定低风险。",
        }
        self.assertEqual("建议低风险", report._dimension_verdict(info, "data"))

    def test_final_high_risk_uses_labelled_reason_evidence(self):
        info = {
            "result": "高风险",
            "verdict": "建议高风险",
            "data_review": "正文表格与提取结果逐行匹配。",
            "reason": (
                "数据维度（核心证据）：Table 3 的 HR=0.980，但 95%CI 为"
                "(0.981, 0.992)，点估计不在置信区间内，属于数学上不可能的统计异常。"
                "综合判定为高风险。"
            ),
        }
        self.assertEqual("建议高风险", report._dimension_verdict(info, "data"))

    def test_byte_identical_hidden_pdf_images_raise_image_dimension(self):
        info = {
            "result": "高风险",
            "verdict": "建议高风险",
            "image_review": "常规渲染页面未见跨图重复。",
            "reason": (
                "图像重复嵌入：三对图像为不同 XRef 对象但字节级完全一致，"
                "MD5 相同，副本被放在页面边界之外。这属于非标准PDF构造，"
                "构成图像完整性问题。参考文献未发现异常。"
            ),
        }
        self.assertEqual("建议高风险", report._dimension_verdict(info, "image"))

    def test_numbered_data_finding_does_not_raise_low_image_or_reference(self):
        info = {
            "result": "高风险",
            "verdict": "建议高风险",
            "data_review": "统计列存在系统性不一致。",
            "reason": (
                "③数据维度：调整前后 SE 列完全相同，属于数据完整性问题。"
                "图像重复与拼接类未发现高风险证据；"
                "参考文献著录完整、未发现实质不一致。建议高风险。"
            ),
        }
        self.assertEqual("建议高风险", report._dimension_verdict(info, "data"))
        self.assertEqual("建议低风险", report._dimension_verdict(info, "image"))
        self.assertEqual("建议低风险", report._dimension_verdict(info, "ref"))

    def test_not_independently_high_risk_is_a_negated_conclusion(self):
        text = "样本量口径存在差异，作为方法学注意点记录，不单独定为高风险。"
        self.assertFalse(report._has_highrisk_final_conclusion(text))


if __name__ == "__main__":
    unittest.main()
