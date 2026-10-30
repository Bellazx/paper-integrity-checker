import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.humanities_checker import check_humanities_integrity
from modules.reference_checker import _compare_titles, _extract_references


def test_chinese_reference_section_and_numbered_citations():
    text = """本文讨论相关研究[1]、[2-3]。\n\n参考文献\n[1] 张三. 文献甲[J]. 2020, 1(1): 1-5.\n[2] 李四. 文献乙[M]. 2021.\n[3] 王五. 文献丙[J]. 2022.\n"""
    refs = _extract_references(text)

    assert [ref["number"] for ref in refs] == [1, 2, 3]
    findings = check_humanities_integrity(text, refs)
    assert not any(x["issue_type"] == "citation_without_reference" for x in findings["reference_issues"])


def test_missing_citation_and_duplicate_reference_are_reported():
    text = """研究结果见[1]。\n\n参考文献\n[1] Zhang A. A study. 2020.\n[2] Zhang A. A study. 2020.\n[3] Li B. Another study. 2021.\n"""
    refs = _extract_references(text)
    findings = check_humanities_integrity(text, refs)
    types = {item["issue_type"] for item in findings["reference_issues"]}

    assert "uncited_references" in types
    assert "duplicate_reference" in types


def test_review_method_check_is_low_severity_and_text_repetition_is_data_evidence():
    repeated = "这是一个需要人工核对的长段落，" * 30
    text = f"文献综述介绍研究进展。\n\n{repeated}\n\n{repeated}\n\n参考文献\n[1] 张三. 文献甲[J]. 2020.\n"
    refs = _extract_references(text)
    findings = check_humanities_integrity(text, refs)

    method = [x for x in findings["reference_issues"] if x["issue_type"] == "review_method_incomplete"]
    repeats = [x for x in findings["text_issues"] if x["test"] == "text_repetition"]
    assert method and method[0]["severity"] == "low"
    assert repeats


def test_unnumbered_chinese_references_and_cjk_title_matching():
    text = """研究表明相关问题值得讨论。\n\n参考文献\n张三，2020，《中文研究方法》，北京：某出版社。\n李四，2021，《另一项研究》，《学术月刊》，(2): 10-20。\n"""
    refs = _extract_references(text)

    assert len(refs) == 2
    assert _compare_titles("张三，《中文研究方法》，某出版社", "中文研究方法") == 1.0


def test_selected_bibliography_extracts_narrative_author_year_entries():
    text = """A conceptual review.\n\nSelected bibliography\nPlease see Smith, A., and Jones, B., 2020. A useful study. Journal A, 1: 1-9. Wang, C., 2021. Another study. Journal B, 2: 10-20.\n"""
    refs = _extract_references(text)

    assert len(refs) == 2
    assert refs[0]["number"] == 1
    assert refs[1]["number"] == 2
