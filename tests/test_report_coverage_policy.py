from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "project_generate_review_report",
    Path(__file__).parents[1] / "scripts" / "generate_review_report.py",
)
assert _SPEC and _SPEC.loader
report_entrypoint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(report_entrypoint)
sys.path.insert(0, "/home/library/.claude/skills/paper-batch-review/scripts")
import coverage_validator


class _FakeRenderer:
    def _coverage_check(self, info):
        return {**info, "_coverage_error": "coverage timed out after 240s"}


class _GapRenderer:
    def _coverage_check(self, info):
        return {**info, "_coverage_gaps": ["未说明小数精度不一致类别（共1项）"]}


def test_resource_coverage_timeout_is_soft_warning(monkeypatch):
    monkeypatch.setenv("REVIEW_COVERAGE_FAILURE_POLICY", "warn")
    renderer = _FakeRenderer()
    report_entrypoint._patch_coverage_entrypoint(renderer)

    result = renderer._coverage_check({"doi": "10.1/example"})

    assert "_coverage_error" not in result
    assert result["_coverage_status"] == "resource_warning"
    assert "timed out" in result["_coverage_warning"]


def test_strict_policy_keeps_nonfatal_handling_opt_out(monkeypatch):
    monkeypatch.setenv("REVIEW_COVERAGE_FAILURE_POLICY", "strict")
    renderer = _FakeRenderer()
    report_entrypoint._patch_coverage_entrypoint(renderer)

    result = renderer._coverage_check({"doi": "10.1/example"})

    assert result["_coverage_error"] == "coverage timed out after 240s"


def test_semantic_coverage_gaps_remain_blocking_even_if_legacy_override_is_set(monkeypatch):
    monkeypatch.setenv("REVIEW_COVERAGE_GAP_POLICY", "warn")
    renderer = _GapRenderer()
    report_entrypoint._patch_coverage_entrypoint(renderer)

    result = renderer._coverage_check({"doi": "10.1/example"})

    assert result["_coverage_gaps"] == ["未说明小数精度不一致类别（共1项）"]
    assert result.get("_coverage_status") != "warning"


def test_highrisk_report_must_not_invent_omitted_rowwise_acknowledgement():
    info = {
        "doi": "10.7554/eLife.97621",
        "result": "高风险",
        "verdict": "建议高风险",
        "image_review": "已核查图像，未见独立问题。",
        "data_review": "已核查数据并确认存在高风险异常。",
        "ref_review": "已核查参考文献，未见独立问题。",
        "methodology_review": "已核查研究设计和统计事实。",
        "reason": "数据异常支持高风险结论。",
        "dimension_verdicts": {
            "image": "建议低风险", "data": "建议高风险",
            "ref": "建议低风险", "methodology": "建议低风险",
        },
    }
    evidence = {
        "high_findings": [{
            "dim": "data", "test": "rowwise_value_reuse",
            "location": "elife-97621-fig4-data3-v1.xlsx / Figure 4I",
        }],
        "deterministic_findings": {},
        "main_figures": [],
    }
    from api.services.review_validation import validate
    checked, gaps = validate(info, evidence)
    assert any("rowwise_value_reuse" in gap for gap in gaps)
    assert checked['data_review'] == info['data_review']
