from api.services import review


def _evidence(*, strong=False, figures=0, high=0):
    det = {"source_data_audit": {"high_findings": [{}] * (1 if strong else 0)}}
    return {
        "coverage_manifest": {"counts": {
            "main_figures": figures,
            "high_findings": high,
            "source_data_audit_high": 1 if strong else 0,
        }},
        "deterministic_findings": det,
    }


def test_strong_data_evidence_uses_read_only_targeted_plan():
    plan = review._adaptive_review_plan(_evidence(strong=True, high=4))
    assert plan["name"] == "deterministic_data_focus"
    assert plan["allowed_tools"] == ("Read",)
    assert plan["max_turns"] == review.REVIEW_MAX_TURNS


def test_figure_signal_keeps_targeted_tools():
    evidence = _evidence(figures=2, high=1)
    evidence["deterministic_findings"]["high_findings"] = [{"dim": "image"}]
    plan = review._adaptive_review_plan(evidence)
    assert plan["name"] == "targeted_evidence_review"
    assert plan["allowed_tools"] == ("Bash", "Read")
    assert plan["max_turns"] == review.REVIEW_MAX_TURNS


def test_clean_evidence_uses_read_only_plan():
    plan = review._adaptive_review_plan(_evidence())
    assert plan["name"] == "clean_coverage_review"
    assert plan["allowed_tools"] == ("Read",)
    assert plan["max_turns"] == review.REVIEW_MAX_TURNS
