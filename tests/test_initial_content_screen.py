from __future__ import annotations

import json
from types import SimpleNamespace
from types import SimpleNamespace

import fitz
import pytest

from modules import initial_content_screen as screen


def test_initial_prompt_distinguishes_serious_typos_from_editorial_errata():
    assert "笔误应保留为勘误意见" in screen.SYSTEM
    assert "严重笔误" in screen.SYSTEM
    assert "引用编号" in screen.SYSTEM
from modules.chinese_report_generator import _compute_overall_risk


def test_production_request_uses_effort_and_falls_back_on_exhausted_output(monkeypatch):
    from utils import llm_client
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        exhausted = len(calls) == 1
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='length' if exhausted else 'stop',
            message=SimpleNamespace(content='' if exhausted else '{"complete":true,"candidates":[]}'))],
            usage=SimpleNamespace(completion_tokens=16384))
    monkeypatch.setattr(llm_client, 'get_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setenv('INITIAL_CONTENT_MODEL', 'primary')
    monkeypatch.setenv('INITIAL_CONTENT_FALLBACK_MODEL', 'fallback')
    monkeypatch.setenv('INITIAL_CONTENT_REASONING_EFFORT', 'low')
    assert screen._parse(screen._request('manuscript'))['complete']
    assert [c['model'] for c in calls] == ['primary', 'fallback']
    assert all(c['max_tokens'] == 32768 for c in calls)
    assert all(c['reasoning_effort'] == 'low' for c in calls)


@pytest.mark.parametrize('content', ['', 'not JSON'])
def test_stop_with_empty_or_invalid_output_is_not_accepted(monkeypatch, content):
    from utils import llm_client
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content=content))], usage=None)
    monkeypatch.setattr(llm_client, 'get_client', lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(screen.ContentScreenIncomplete):
        screen._request('manuscript')
    assert len(calls) == 2


def paper(tmp_path, *, correction=False):
    path = tmp_path / "paper.pdf"
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 540, 750),
            "Table 1. Accuracy on all three test sets.\n"
            "Set A accuracy is 10.00\nSet B accuracy is 20.00\nSet C accuracy is 60.00\n"
            "The reported average is 15.00\n"
            "We report the arithmetic mean over the test sets.\n")
        if correction:
            page = doc.new_page()
            page.insert_text((40, 80), "For Table 1 we exclude Set C from the reported average.")
        doc.save(path)
    return path


def candidate():
    def value(label, n):
        return {"page": 1, "quote": f"Set {label} accuracy is {n}", "value": n}
    return {"id": "c1", "kind": "arithmetic", "location": "PDF第1页Table 1平均值",
            "description": "三项均值与平均行不一致，需要核对。", "scope": "同表三种测试集的算术均值",
            "evidence": [{"page": 1, "quote": "Table 1. Accuracy on all three test sets."},
                         {"page": 1, "quote": "The reported average is 15.00"}],
            "calculation": {"operation": "mean", "operands": [value("A", "10.00"), value("B", "20.00"), value("C", "60.00")],
                            "reported": {"page": 1, "quote": "The reported average is 15.00", "value": "15.00"}}}


def responder(verdict="support", evidence=None, candidates=None):
    calls = []
    def request(prompt):
        calls.append(prompt)
        if "候选:\n" not in prompt:
            return json.dumps({"complete": True, "candidates": [candidate()] if candidates is None else candidates})
        return json.dumps({"complete": True, "decisions": [{"id": "c1", "verdict": verdict,
            "reason": "核对了统计范围和正文说明。", "evidence": evidence or candidate()["evidence"]}]})
    return request, calls


def assessment(issues):
    return _compute_overall_risk({"paper": {}, "image_duplicates": [], "data_anomalies": issues, "reference_issues": []})


def test_missing_component_mean_is_recomputed_and_enters_review(tmp_path):
    request, calls = responder()
    issues, warnings, audit = screen.screen_manuscript(paper(tmp_path), request=request)
    assert len(calls) == 2 and len(issues) == 1
    assert issues[0]["details"]["calculation"]["calculated"] == 30
    assert audit["status"] == "complete" and audit["pages_checked"] == [1]
    assert assessment(issues)["level"] == "高风险"
    assert "Table 1" in calls[0] and "论文材料" in calls[1]
    assert "analysis_warnings" not in calls[0]  # No archived detector findings supplied.


def test_author_exclusion_in_appendix_rejects_arithmetic_candidate(tmp_path):
    evidence = [{"page": 2, "quote": "For Table 1 we exclude Set C from the reported average."}]
    request, calls = responder("reject", evidence)
    issues, _, audit = screen.screen_manuscript(paper(tmp_path, correction=True), request=request)
    assert not issues and audit["pages_checked"] == [1, 2]
    assert "exclude Set C" in calls[1]


def test_correct_mean_and_rounding_do_not_become_high_risk(tmp_path):
    path = paper(tmp_path)
    pages, _ = screen.read_pages(path)
    calc = candidate()["calculation"]
    calc["operands"] = calc["operands"][:2]
    assert not screen.recalculate(calc, pages)["mismatch"]
    request, calls = responder(candidates=[dict(candidate(), calculation=calc)])
    assert not screen.screen_manuscript(path, request=request)[0]
    assert len(calls) == 1  # No model can overrule a valid local arithmetic check.
    pages = [{"page": 1, "tables": [], "text": "First value 1.2; second value 1.3; reported mean 1.2"}]
    calc = {"operation": "mean", "operands": [
        {"value": "1.2", "page": 1, "quote": "First value 1.2"},
        {"value": "1.3", "page": 1, "quote": "second value 1.3"}],
        "reported": {"value": "1.2", "page": 1, "quote": "reported mean 1.2"}}
    assert not screen.recalculate(calc, pages)["mismatch"]


@pytest.mark.parametrize("operation,values,reported,expected", [
    ("relative_change", ["21.95", "34.15"], "35.72", 55.5808656),
    ("difference", ["0.432", "0.539"], "0.107", -.107),
    ("sum", ["50", "60", "38"], "149", 148),
    ("ratio_percent", ["106", "170"], "46.5", 62.3529412),
    ("ratio", ["1.33", "0.33"], "3", 4.03030303),
])
def test_generic_calculations(operation, values, reported, expected):
    entries = [{"value": v, "page": 1, "quote": f"Measurement number {i} is {v}"}
               for i, v in enumerate([*values, reported])]
    if operation == "sum":
        for entry in entries:
            entry["exact"] = True
    pages = [{"page": 1, "tables": [], "text": "\n".join(e["quote"] for e in entries)}]
    result = screen.recalculate({"operation": operation, "operands": entries[:-1], "reported": entries[-1]}, pages)
    assert result["mismatch"] and result["calculated"] == pytest.approx(expected)


@pytest.mark.parametrize("response", ['{}', 'invalid', '{"complete":false}', '{"complete":true}'])
def test_incomplete_output_is_not_a_low_risk_pass(tmp_path, response):
    with pytest.raises(screen.ContentScreenIncomplete):
        screen.screen_manuscript(paper(tmp_path), request=lambda _: response)


def test_malformed_candidate_is_dropped_not_reported(tmp_path):
    # A structurally invalid lead never becomes a finding and no longer fails
    # the whole paper; it is recorded for audit.
    issues, warnings, audit = screen.screen_manuscript(
        paper(tmp_path), request=lambda _: '{"complete":true,"candidates":[{}]}')
    assert issues == [] and len(audit["dropped_leads"]) >= 1 and "未计入" in warnings[-1]


def test_unresolved_candidate_is_initial_high_risk_and_fabricated_quote_is_dropped(tmp_path):
    # An unresolved conflict is recorded for the later review as initial high
    # risk (10.1038/s41467-026-77224-6) instead of withholding the paper.
    path = paper(tmp_path)
    request, _ = responder("uncertain")
    issues, warnings, audit = screen.screen_manuscript(path, request=request)
    assert len(issues) == 1 and issues[0]["severity"] == "high"
    assert issues[0]["description"].startswith("初审未能核清，需复核：")
    assert issues[0]["details"]["verification_status"] == "unresolved_needs_review"
    assert audit["status"] == "needs_review" and audit["confirmed_count"] == 0
    assert "未能核清" in warnings[-1]
    assert assessment(issues)["level"] == "高风险"
    fake = candidate()
    fake["evidence"][0]["quote"] = "This sentence does not appear in the manuscript."
    request, _ = responder(candidates=[fake])
    # A fabricated quote is never reported; it is set aside and recorded.
    issues, warnings, audit = screen.screen_manuscript(path, request=request)
    assert issues == [] and "无法对照" in audit["dropped_leads"][0]["error"]


def test_fabricated_numeric_operand_cannot_pass(tmp_path):
    pages, _ = screen.read_pages(paper(tmp_path))
    calc = candidate()["calculation"]
    calc["operands"][0]["value"] = "999.0"
    with pytest.raises(screen.ContentScreenIncomplete, match="未出现"):
        screen.recalculate(calc, pages)


def test_source_locator_uses_pdf_text_not_model_rephrasing(tmp_path):
    pages, _ = screen.read_pages(paper(tmp_path))
    source_id, text = next(iter(pages[0]['sources'].items()))
    item = {'page': 1, 'source_id': source_id, 'quote': 'model fabricated quote'}
    screen._ground(item, pages)
    assert item['quote'] == text
    with pytest.raises(screen.ContentScreenIncomplete, match='编号不存在'):
        screen._ground({'page': 1, 'source_id': 'p1-b99999'}, pages)


def test_prompt_compaction_preserves_locators_and_legacy_quotes_without_mutating_audit():
    evidence = [{'page': 1, 'source_id': 'p1-b1', 'quote': 'original block' * 100},
                {'page': 2, 'quote': 'legacy contiguous quotation'}]
    payload = screen._locators_only({'evidence': evidence})
    assert payload['evidence'][0] == {'page': 1, 'source_id': 'p1-b1'}
    assert payload['evidence'][1]['quote'] == 'legacy contiguous quotation'
    assert evidence[0]['quote'] == 'original block' * 100


def test_grouped_integer_is_grounded_without_merging_separate_numbers():
    pages = [{'page': 1, 'text': 'Total 3,098 dialogues; other measurements 3, 098', 'tables': []}]
    assert screen._number({'page': 1, 'quote': 'Total 3,098 dialogues',
                           'value': '3098', 'exact': True}, pages) == (3098, 0)
    assert screen._number({'page': 1, 'quote': 'Total 3,098 dialogues',
                           'value': '3,098', 'exact': True}, pages) == (3098, 0)
    with pytest.raises(screen.ContentScreenIncomplete, match='未出现'):
        screen._number({'page': 1, 'quote': 'other measurements 3, 098',
                        'value': '3098', 'exact': True}, pages)


def test_leading_decimal_keeps_the_precision_of_the_source():
    pages = [{'page': 1, 'text': 'Measured accuracy .76 and reference .80', 'tables': []}]
    assert screen._number({'page': 1, 'quote': 'Measured accuracy .76',
                           'value': '0.76'}, pages) == (.76, .005)
    with pytest.raises(screen.ContentScreenIncomplete, match='未出现'):
        screen._number({'page': 1, 'quote': 'Measured accuracy .76',
                        'value': '76'}, pages)
    pages[0]['text'] = 'Group count 16; average 16.0'
    assert screen._number({'page': 1, 'quote': pages[0]['text'],
                           'value': '16.0'}, pages) == (16, .05)


def test_table_mean_candidates_are_only_leads_and_respect_display_rounding():
    rows = [['Setting', 'Accuracy'], ['A', '10.00'], ['B', '20.00'],
            ['C', '60.00'], ['Average', '15.00']]
    sources = {f'p1-t1-r{i}': ' | '.join(row) for i, row in enumerate(rows, 1)}
    expanded = [{'source_id': sid, 'cells': row} for sid, row in zip(sources, rows)]
    pages = [{'page': 1, 'text': '', 'sources': sources,
              'tables': [{'extraction_id': 'p1-t1', 'expanded_rows': expanded}]}]
    leads = screen._table_mean_candidates(pages)
    assert len(leads) == 1 and 'verification_status' not in leads[0]
    checked = screen._validate_candidates({'candidates': leads}, pages)
    assert checked[0][1]['calculated'] == 30
    rows[-1][1] = '30.00'
    sources['p1-t1-r5'] = 'Average | 30.00'
    assert not screen._table_mean_candidates(pages)


def test_weighted_mean_uses_original_counts_and_propagates_rounding():
    pages = [{'page': 1, 'text': 'Group A: count 2, value 10.00. Group B: count 3, value 20.00. Overall 16.00.', 'tables': []}]
    def item(value, quote, exact=False):
        return {'value': value, 'quote': quote, 'page': 1, 'exact': exact}
    calc = {'operation': 'weighted_mean',
            'operands': [item('10.00', 'Group A: count 2, value 10.00'), item('20.00', 'Group B: count 3, value 20.00')],
            'weights': [item('2', 'Group A: count 2', True), item('3', 'Group B: count 3', True)],
            'reported': item('16.00', 'Overall 16.00')}
    result = screen.recalculate(calc, pages)
    assert result['calculated'] == 16 and not result['mismatch']
    calc['weights'][0]['value'] = '999'
    with pytest.raises(screen.ContentScreenIncomplete, match='未出现'):
        screen.recalculate(calc, pages)


def test_invalid_locator_repair_must_pass_the_same_grounding_check(tmp_path):
    pages, _ = screen.read_pages(paper(tmp_path))
    good = candidate()
    bad = candidate()
    bad['evidence'][0] = {'page': 1, 'source_id': 'p1-b99999'}
    prompts = []
    def request(prompt):
        prompts.append(prompt)
        return json.dumps({'complete': True, 'candidates': [bad if len(prompts) == 1 else good]})
    checked = screen._checked(request, 'manuscript', lambda result: screen._validate_candidates(result, pages))
    assert len(prompts) == 2 and '编号不存在' in prompts[1]
    assert checked[0][1]['calculated'] == 30


def test_invalid_numeric_mapping_is_repaired_by_separate_request(tmp_path):
    pages, _ = screen.read_pages(paper(tmp_path))
    bad = candidate()
    bad['calculation']['operands'][0]['value'] = '999.0'
    prompts = []
    def repair(prompt):
        prompts.append(prompt)
        return json.dumps({'complete': True, 'candidates': [candidate()]})
    result = screen._checked(
        lambda _: json.dumps({'complete': True, 'candidates': [bad]}),
        'manuscript', lambda value: screen._validate_candidates(value, pages), repair_request=repair)
    assert len(prompts) == 1 and '999.0' in prompts[0] and '未出现在' in prompts[0]
    assert result[0][1]['calculated'] == 30


def test_failed_evidence_repairs_preserve_responses(tmp_path):
    pages, _ = screen.read_pages(paper(tmp_path))
    bad = candidate()
    bad['calculation']['operands'][0]['value'] = '999.0'
    raw = json.dumps({'complete': True, 'candidates': [bad]})
    with pytest.raises(screen.ContentScreenIncomplete) as error:
        screen._checked(lambda _: raw, 'source material',
                        lambda result: screen._validate_candidates(result, pages))
    failed = error.value.audit['failed_validation']
    assert failed['prompt'] == 'source material'
    assert len(failed['attempts']) == 2
    assert all('999.0' in attempt['response'] for attempt in failed['attempts'])


def test_production_repairs_and_adjudication_use_secondary_model(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, 'MINIMAX_FALLBACK_MODEL', 'evidence-model')
    monkeypatch.delenv('INITIAL_CONTENT_REPAIR_MODEL', raising=False)
    bad = candidate()
    bad['calculation']['operands'][0]['value'] = '999.0'
    calls = []
    def request(prompt, *, model=None):
        calls.append(model)
        if '候选:\n' not in prompt:
            return json.dumps({'complete': True, 'candidates': [bad if model is None else candidate()]})
        return json.dumps({'complete': True, 'decisions': [{
            'id': 'c1', 'verdict': 'uncertain' if model is None else 'support',
            'reason': '核对原文和计算范围', 'evidence': candidate()['evidence']}]})
    monkeypatch.setattr(screen, '_request', request)
    issues, _, _ = screen.screen_manuscript(paper(tmp_path))
    assert len(issues) == 1
    assert calls == [None, 'evidence-model', None, 'evidence-model']


@pytest.mark.parametrize('verdict', ['support', 'reject', 'uncertain'])
def test_uncertain_candidates_get_source_grounded_adjudication(tmp_path, verdict):
    calls = []
    def request(prompt):
        calls.append(prompt)
        if '候选:\n' not in prompt:
            return json.dumps({'complete': True, 'candidates': [candidate()]})
        adjudicating = '所引证据页的原文材料:' in prompt
        return json.dumps({'complete': True, 'decisions': [{
            'id': 'c1', 'verdict': verdict if adjudicating else 'uncertain',
            'reason': '核对平均值的范围与表格数据', 'evidence': candidate()['evidence']}]})
    issues, _, audit = screen.screen_manuscript(paper(tmp_path, correction=True), request=request)
    assert len(issues) == (0 if verdict == 'reject' else 1)
    if verdict == 'uncertain':
        assert audit['unresolved_candidates'] and audit['confirmed_count'] == 0
        assert issues[0]['details']['verification_status'] == 'unresolved_needs_review'
    assert len(calls) == 3
    assert 'Set C accuracy is 60.00' in calls[-1]
    assert 'exclude Set C' in calls[-1]  # Adjacent context, not just cited fragments.
    assert audit['decisions']['c1'][-1]['stage'] == 'arbitration'


def test_different_runs_or_weighted_means_can_be_rejected(tmp_path):
    # Verifier rejects, instead of treating a numerically different value as proof.
    request, _ = responder("reject")
    assert not screen.screen_manuscript(paper(tmp_path), request=request)[0]


def test_all_pages_reach_screening_and_late_counterevidence_wins(tmp_path, monkeypatch):
    path = paper(tmp_path, correction=True)
    pages, _ = screen.read_pages(path)
    monkeypatch.setattr(screen, "_chunks", lambda _: [[pages[0]], [pages[1]]])
    call_number = 0
    def request(prompt):
        nonlocal call_number
        call_number += 1
        if call_number <= 2:
            return json.dumps({"complete": True, "candidates": [candidate()] if call_number == 1 else []})
        reject = call_number >= 4
        evidence = [{"page": 2, "quote": "For Table 1 we exclude Set C from the reported average."}] if reject else candidate()["evidence"]
        return json.dumps({"complete": True, "decisions": [{"id": "c1", "verdict": "reject" if reject else "support",
                                                             "reason": "已核对当前材料", "evidence": evidence}]})
    issues, _, audit = screen.screen_manuscript(path, request=request)
    assert not issues and call_number == 5 and audit["chunks"] == 2
    assert audit['decisions']['c1'][-1]['stage'] == 'arbitration'


def test_conflicting_chunk_verdicts_are_adjudicated_not_silently_cleared(tmp_path, monkeypatch):
    path = paper(tmp_path)
    pages, _ = screen.read_pages(path)
    monkeypatch.setattr(screen, '_chunks', lambda _: [pages, pages])
    calls = []
    def request(prompt):
        calls.append(prompt)
        n = len(calls)
        if n <= 2:
            return json.dumps({'complete': True, 'candidates': [candidate()] if n == 1 else []})
        return json.dumps({'complete': True, 'decisions': [{'id': 'c1',
            'verdict': 'reject' if n == 4 else 'support',
            'reason': '核对了原文中相同范围的数值与平均定义',
            'description': '分项与汇总值不一致，需核对汇总口径。',
            'evidence': candidate()['evidence']}]})
    issues, _, audit = screen.screen_manuscript(path, request=request)
    assert len(calls) == 5 and len(issues) == 1
    assert audit['decisions']['c1'][-1]['stage'] == 'arbitration'
    assert '分项与汇总值' in issues[0]['description']


def test_different_arithmetic_columns_sharing_evidence_are_not_deduplicated(tmp_path):
    first = candidate()
    second = candidate()
    second['location'] = 'same source, a different calculation'
    second['calculation']['operands'] = second['calculation']['operands'][:2]
    second['calculation']['reported'] = {
        'value': '60.00', 'page': 1, 'quote': 'Set C accuracy is 60.00'}
    def request(prompt):
        if '候选:\n' not in prompt:
            return json.dumps({'complete': True, 'candidates': [first, second]})
        return json.dumps({'complete': True, 'decisions': [
            {'id': cid, 'verdict': 'support', 'reason': '核对原文及计算口径',
             'evidence': candidate()['evidence']} for cid in ('c1', 'c2')]})
    issues, _, _ = screen.screen_manuscript(paper(tmp_path), request=request)
    assert len(issues) == 2


def test_oversized_page_is_not_silently_truncated():
    with pytest.raises(screen.ContentScreenIncomplete, match="未截断"):
        screen._chunks([{"page": 1, "text": "a" * 100}], limit=50)


def test_runtime_chunk_budget_preserves_all_pages(monkeypatch):
    pages = [{"page": i, "text": "a" * 80} for i in range(1, 5)]
    monkeypatch.setenv("INITIAL_CONTENT_CHUNK_CHARS", "130")
    chunks = screen._chunks(pages)
    assert len(chunks) == 4
    assert [p["page"] for chunk in chunks for p in chunk] == [1, 2, 3, 4]


@pytest.mark.parametrize("timeout_override,expected_timeout", [(None, 300), ("360", 360)])
def test_runtime_output_budget_and_timeout_reach_model(monkeypatch, timeout_override, expected_timeout):
    import utils.llm_client

    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop", message=SimpleNamespace(content='{"complete":true,"candidates":[]}'))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(utils.llm_client, "get_client", lambda: client)
    monkeypatch.setattr(utils.llm_client, "LLM_TIMEOUT", 300)
    monkeypatch.setenv("INITIAL_CONTENT_MAX_TOKENS", "32768")
    if timeout_override is None:
        monkeypatch.delenv("INITIAL_CONTENT_TIMEOUT", raising=False)
    else:
        monkeypatch.setenv("INITIAL_CONTENT_TIMEOUT", timeout_override)
    assert json.loads(screen._request("paper"))["complete"] is True
    assert requests[0]["max_tokens"] == 32768
    assert requests[0]["timeout"] == expected_timeout


def test_single_unverified_or_medium_candidate_is_not_decisive():
    issue = {"test": "verified_manuscript_content_conflict", "severity": "high", "details": {}}
    assert assessment([issue])["level"] == "低风险"
    issue["details"]["verification_status"] = "source_grounded_and_counterchecked"
    assert assessment([issue])["level"] == "高风险"
    issue["severity"] = "medium"
    assert assessment([issue])["level"] == "低风险"


def test_editorial_typo_candidate_is_retained_without_high_risk_gate():
    issue = {"test": "verified_manuscript_content_conflict", "severity": "low",
             "details": {"verification_status": "source_grounded_and_counterchecked"}}
    result = assessment([issue])
    assert result["level"] == "低风险"


@pytest.mark.parametrize("fail", [False, True])
def test_shared_pipeline_persists_verified_risk_or_stops_before_report(tmp_path, monkeypatch, fail):
    from core import pipeline
    from utils import pdf_utils, pdf_table_extractor, docx_extractor, safe_data_checker
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source = paper(input_dir)
    request, _ = responder()
    result = screen.screen_manuscript(source, request=request)
    monkeypatch.setenv("PAPER_CLEAN_JUNK", "0")
    monkeypatch.setattr(pipeline, "extract_metadata", lambda p: {"title": "Test"})
    for name in ("extract_images", "check_image_duplicates", "check_splicing"):
        monkeypatch.setattr(pipeline, name, lambda *a, **k: [])
    monkeypatch.setattr(pdf_utils, "extract_standalone_images", lambda *a, **k: [])
    monkeypatch.setattr(docx_extractor, "extract_docx_images_in_dir", lambda *a, **k: [])
    monkeypatch.setattr(pdf_table_extractor, "extract_tables_from_pdf", lambda *a, **k: [])
    monkeypatch.setattr(safe_data_checker, "check_data_anomalies_safe", lambda *a: ([], []))
    reports = []
    def generate(*args):
        reports.append(args)
        return "test.pdf", {}
    def content(*args):
        if fail:
            raise screen.ContentScreenIncomplete("test timeout", audit={
                'unresolved_candidates': [candidate()], 'decisions': {'c1': [{'verdict': 'uncertain'}]}})
        return result
    monkeypatch.setattr(pipeline, "generate_chinese_pdf", generate)
    monkeypatch.setattr(pipeline, "screen_manuscript", content)
    output = tmp_path / "out"
    if fail:
        with pytest.raises(RuntimeError, match="未生成放行结论"):
            pipeline.analyze_paper(str(input_dir), str(output), skip_refs=True)
        assert not reports and not (output / "report.json").exists()
        saved = json.loads((output / "initial_content_screen.json").read_text())
        assert saved['status'] == 'incomplete'
        assert saved['unresolved_candidates'][0]['id'] == 'c1'
        assert saved['decisions']['c1'][0]['verdict'] == 'uncertain'
    else:
        findings = pipeline.analyze_paper(str(input_dir), str(output), skip_refs=True)
        assert findings["initial_risk_level"] == "高风险"
        saved = json.loads((output / "report.json").read_text())
        assert saved["content_screening"]["status"] == "complete"
        assert saved["data_anomalies"][0]["test"] == "verified_manuscript_content_conflict"
        assert reports


def test_report_writer_failure_keeps_verified_content_evidence(tmp_path, monkeypatch):
    from modules import chinese_report_generator as report
    request, _ = responder()
    issues, _, _ = screen.screen_manuscript(paper(tmp_path), request=request)
    findings = {"paper": {}, "image_duplicates": [], "data_anomalies": issues, "reference_issues": []}
    monkeypatch.setattr(report, "chat", lambda *a, **k: "bad output")
    monkeypatch.setattr(report, "_parse_combined_report_response", lambda s: ({"authors_full": [], "affiliations": []}, "<p>不完整说明</p>"))
    _, html = report._generate_analysis_html(findings, "")
    assert "程序复算为30" in html and not findings.get("_explainability_downgraded")
    assert assessment(issues)["level"] == "高风险"


def test_spelled_out_multiple_is_grounded_and_compared_as_a_ratio():
    # Nat. Commun. 10.1038/s41467-026-77281-x Fig. 4c caption: the batch failed
    # because "three times" was not accepted as evidence for the value 3.
    caption = ("the neutral squirmer swims three times faster than the pusher; "
               "linear fits U = mP give m = 1.33, 1.16 and 0.33")
    pages = [{'page': 1, 'text': caption, 'tables': []}]
    item = lambda v: {'value': v, 'page': 1, 'quote': caption}
    calc = {'operation': 'ratio', 'operands': [item('1.33'), item('0.33')], 'reported': item('3')}
    result = screen.recalculate(calc, pages)
    assert result['mismatch'] and result['calculated'] == pytest.approx(4.0303, abs=1e-4)
    assert result['ambiguous_multiple']  # 4.03 = "three times larger" read as +3
    pages[0]['text'] = caption = caption.replace('three', 'four')
    calc['operands'] = [item('1.33'), item('0.33')]
    calc['reported'] = item('4')
    assert not screen.recalculate(calc, pages)['mismatch']


@pytest.mark.parametrize('text,value,grounded', [
    ('a threefold increase', '3', True),
    ('twice as fast', '2', True),
    ('in twenty-one samples', '21', True),
    ('in twenty-one samples', '20', False),
    ('in twenty-one samples', '1', False),
    ('one of the particles moved', '1', False),
    ('the other three-dimensional particles', '3', True),
])
def test_spelled_numbers(text, value, grounded):
    pages = [{'page': 1, 'text': text, 'tables': []}]
    item = {'value': value, 'page': 1, 'quote': text}
    if grounded:
        assert screen._number(item, pages)[0] == float(value)
    else:
        with pytest.raises(screen.ContentScreenIncomplete, match='未出现'):
            screen._number(item, pages)


def multiple_paper(tmp_path, phrase):
    claim = "The neutral squirmer reaches velocity values " + phrase + " than the pusher."
    fits = "Fits U = mP give m = 1.33 for the neutral squirmer and m = 0.33 for the pusher."
    caption = claim + "\n" + fits
    path = tmp_path / "multiple.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_textbox(fitz.Rect(40, 40, 540, 750), caption)
        doc.save(path)
    item = lambda v, quote=fits: {"page": 1, "quote": quote, "value": v}
    reported = {"three times larger": "3", "two times larger": "2", "three times as fast as": "3"}[phrase]
    found = {"id": "c1", "kind": "arithmetic", "location": "PDF第1页图注",
             "description": "斜率之比约为4.03，并非正文所称3倍。", "scope": "同一功率下两种模式的速度比",
             "evidence": [{"page": 1, "quote": claim}, {"page": 1, "quote": fits}],
             "calculation": {"operation": "ratio", "operands": [item("1.33"), item("0.33")],
                             "reported": item(reported, claim)}}
    return path, found


def test_ambiguous_multiple_is_a_low_severity_clarification(tmp_path):
    # 10.1038/s41467-026-77281-x: "three times larger" with slopes 1.33/0.33.
    # Reviewer support must not turn the wording ambiguity into high risk.
    path, found = multiple_paper(tmp_path, "three times larger")
    request, _ = responder(candidates=[found], evidence=found["evidence"])
    issues, _, _ = screen.screen_manuscript(path, request=request)
    assert len(issues) == 1 and issues[0]["severity"] == "low"
    assert "两种理解" in issues[0]["description"] and "并非" not in issues[0]["description"]
    assert assessment(issues)["level"] != "高风险"


@pytest.mark.parametrize("phrase", ["two times larger", "three times as fast as"])
def test_unambiguous_or_unmatched_multiple_stays_a_conflict(tmp_path, phrase):
    # "two times larger" fits neither reading of 4.03; "three times as fast
    # as" can only mean 3-fold, so 4.03 is a real conflict.
    path, found = multiple_paper(tmp_path, phrase)
    request, _ = responder(candidates=[found], evidence=found["evidence"])
    issues, _, _ = screen.screen_manuscript(path, request=request)
    assert issues[0]["severity"] == "high"
    assert not issues[0]["details"]["calculation"]["ambiguous_multiple"]
    assert assessment(issues)["level"] == "高风险"


def test_ambiguous_multiple_repeated_in_caption_and_text_is_one_issue(tmp_path):
    # 77281-x repeats "three times" in the Fig. 4c caption (p5) and the main
    # text (p6); both candidates must merge into one clarification.
    fits = "Fits U = mP give m = 1.33 for the neutral squirmer and m = 0.33 for the pusher."
    caption = "The neutral squirmer reaches velocity values three times larger than the pusher."
    text = "At P = 4.7 mW the neutral squirmer reaches 6 um/s, three times faster than the pusher."
    path = tmp_path / "repeated.pdf"
    with fitz.open() as doc:
        doc.new_page().insert_textbox(fitz.Rect(40, 40, 540, 750), caption + "\n" + fits)
        doc.new_page().insert_textbox(fitz.Rect(40, 40, 540, 750), text)
        doc.save(path)
    def found(location, page, claim):
        item = lambda v, quote, pg: {"page": pg, "quote": quote, "value": v}
        return {"kind": "arithmetic", "location": location, "description": "并非3倍。",
                "scope": "同一功率下两种模式的速度比",
                "evidence": [{"page": page, "quote": claim}, {"page": 1, "quote": fits}],
                "calculation": {"operation": "ratio", "operands": [item("1.33", fits, 1), item("0.33", fits, 1)],
                                "reported": item("3", claim, page)}}
    candidates = [found("PDF第1页图注", 1, caption), found("PDF第2页正文", 2, text)]
    def request(prompt):
        if "候选:\n" not in prompt:
            return json.dumps({"complete": True, "candidates": candidates})
        return json.dumps({"complete": True, "decisions": [
            {"id": i, "verdict": "support", "reason": "核对了拟合斜率与倍数表述。",
             "evidence": candidates[0]["evidence"]} for i in ("c1", "c2")]})
    issues, _, audit = screen.screen_manuscript(path, request=request)
    assert len(issues) == 1 and issues[0]["severity"] == "low"
    assert issues[0]["location"] == "PDF第1页图注；PDF第2页正文"
    assert {e["page"] for e in issues[0]["details"]["evidence"]} == {1, 2}
    assert len(audit["candidates"][0]["evidence"]) == 2  # audit record left untouched


@pytest.mark.parametrize("text,value,expected", [
    ("swims three times faster than the pusher", "3", True),
    ("a threefold higher yield than", "3", True),
    ("3-fold more active than", "3", True),
    ("A比B高3倍", "3", True),
    ("提高了3倍", "3", True),
    ("three times as fast as the pusher", "3", False),
    ("A是B的3倍", "3", False),
    ("three times faster than the pusher", "4", False),
])
def test_only_comparative_multiples_are_ambiguous(text, value, expected):
    assert screen._comparative_multiple(text, value) is expected


def test_unresolved_erratum_and_wording_ambiguity_stay_low(tmp_path):
    erratum = dict(candidate(), kind="contradiction", calculation=None, severity="low")
    request, _ = responder("uncertain", candidates=[erratum])
    issues, _, _ = screen.screen_manuscript(paper(tmp_path), request=request)
    assert issues[0]["severity"] == "low" and assessment(issues)["level"] != "高风险"
    path, found = multiple_paper(tmp_path, "three times larger")
    request, _ = responder("uncertain", candidates=[found], evidence=found["evidence"])
    issues, _, _ = screen.screen_manuscript(path, request=request)
    assert issues[0]["severity"] == "low" and assessment(issues)["level"] != "高风险"


def test_numbers_are_read_through_pdf_ligatures_and_hyphenation():
    from modules.initial_content_screen import _number, _comparative_multiple
    text = ("resulting in a ﬁvefold reduction in laser threshold; a ﬁfteen-\nfold gain; "
            "２.５ mg; ten­fold")
    pages = [{"page": 1, "text": text, "tables": [], "sources": {"p1-b1": text}}]
    for value in ("5", "15", "2.5", "10"):
        assert _number({"value": value, "page": 1, "source_id": "p1-b1", "exact": False}, pages)[0] == float(value)
    assert _comparative_multiple("a ﬁve times faster response", 5)


def test_word_form_values_and_partial_replies_do_not_fail_the_paper():
    from modules.initial_content_screen import _literal, _validate_candidates, _validate_checks
    assert [_literal(v) for v in ("three times", "3倍", "ﬁvefold", "五倍", "2.5-fold", "abc")] == \
        ["3", "3", "5", "5", "2.5", "abc"]
    text = "pusher 1.33 and puller 0.33; the pusher needs three times more power"
    pages = [{"page": 1, "text": text, "tables": [], "sources": {"p1-b1": text}}]
    good = {"id": "c1", "kind": "arithmetic", "severity": "high", "location": "p1", "description": "d", "scope": "s",
            "evidence": [{"page": 1, "source_id": "p1-b1"}],
            "calculation": {"operation": "ratio",
                            "operands": [{"value": "1.33", "page": 1, "source_id": "p1-b1"},
                                         {"value": "0.33", "page": 1, "source_id": "p1-b1"}],
                            "reported": {"value": "three times", "page": 1, "source_id": "p1-b1"}}}
    bad = {**good, "id": "c2", "evidence": [{"page": 1, "source_id": "p9-x"}]}
    with pytest.raises(screen.ContentScreenIncomplete, match="c2"):
        _validate_candidates({"candidates": [good, bad]}, pages)
    kept = _validate_candidates({"candidates": [good, bad]}, pages, final=True)
    assert [c["id"] for c, _ in kept] == ["c1"] and kept.dropped[0]["candidate"] == "c2"
    reply = {"decisions": [{"id": "c1", "verdict": "support", "reason": "成立"},
                           {"id": "c2", "verdict": "reject", "reason": "排除", "evidence": [{"page": 1, "source_id": "p1-b1"}]}]}
    with pytest.raises(screen.ContentScreenIncomplete, match="c1: support 必须给 evidence"):
        _validate_checks(reply, {"c1", "c2"}, pages)
    checks = {c["id"]: c["verdict"] for c in _validate_checks(reply, {"c1", "c2"}, pages, final=True)}
    assert checks == {"c1": "irrelevant", "c2": "reject"}


def test_unusable_reply_on_part_of_the_paper_does_not_fail_it(tmp_path):
    # One chunk only ever gets replies that are not JSON: it is retried in
    # smaller pieces, then disclosed as unchecked; the rest is screened and
    # the paper gets a result instead of an error.
    path = paper(tmp_path)
    good, _ = responder("support")
    pages, _ = screen.read_pages(path)
    broken = {"page": 2, "text": "", "tables": [], "sources": {"p2-b1": "BROKEN-CHUNK marker text"}}
    original = screen._chunks
    try:
        screen._chunks = lambda _pages, limit=None: [[broken], pages]
        issues, warnings, audit = screen.screen_manuscript(
            path, request=lambda prompt, **kw: "not json" if "BROKEN-CHUNK" in prompt else good(prompt))
    finally:
        screen._chunks = original
    assert [u["stage"] for u in audit["unchecked"]][:1] == ["discovery"] and audit["unchecked"][0]["pages"] == [2]
    assert issues and "未能完成" in " ".join(warnings)


def test_unusable_reply_is_recovered_by_splitting(tmp_path):
    path = paper(tmp_path)
    good, _ = responder("support")
    state = {"calls": 0}

    def flaky(prompt, **kw):
        state["calls"] += 1
        return "not json" if state["calls"] <= 3 else good(prompt)
    issues, _, audit = screen.screen_manuscript(path, request=flaky)
    assert issues and not audit["unchecked"]


def test_oversized_page_is_split_by_source_blocks_not_rejected():
    page = {"page": 3, "text": "", "tables": [], "sources": {f"p3-b{i}": "x" * 900 for i in range(10)}}
    chunks = screen._chunks([page], limit=3000)
    assert len(chunks) > 1
    assert {k for chunk in chunks for p in chunk for k in p["sources"]} == set(page["sources"])
    assert all(p["page"] == 3 for chunk in chunks for p in chunk)
