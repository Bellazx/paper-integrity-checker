import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from api.services import review
from api.services.review_validation import validate


def test_exact_excel_range_is_preserved_in_renderable_schema():
    from api.services.review_validation import normalize_excel_evidence
    original = {'file': 'source.xlsx', 'sheet': 'Fig3', 'range': '$B$4:D9',
                'issue_index': 2, 'description': '同一实验对应区域'}
    out = normalize_excel_evidence([original])[0]
    assert out['regions'] == [{'file': 'source.xlsx', 'sheet': 'Fig3',
                               'row_range': [4, 9], 'col_range': [2, 4]}]
    assert out['issue_index'] == 2 and out['caption'] == original['description']
    assert 'regions' not in original
    assert normalize_excel_evidence([out]) == [out]


def test_descriptive_excel_location_is_never_guessed():
    from api.services.review_validation import normalize_excel_evidence
    item = {'file': 'source.xlsx', 'sheet': 'Fig3', 'range': 'all statistical rows'}
    assert normalize_excel_evidence([item]) == [item]


def result(risk='高风险'):
    return {'doi': '10.1/test', 'result': risk, 'verdict': '建议' + risk, 'trigger': 'review',
            'image_review': '已渲染 Fig.1 并核对图像与源数据，未见图文矛盾。',
            'data_review': '问题位置：Fig.2。问题类型：跨实验重复。具体情况：两列四个数值逐值相同。'
                           '为什么重要：两列对应独立实验，需要作者解释。核查依据：读取源数据并逐值比对。',
            'ref_review': '已核对参考文献的 DOI 与题名，未见实质不一致。',
            'methodology_review': '已核对实验分组、样本量与统计方法，未见独立异常。',
            'dimension_verdicts': {'image': '建议低风险', 'data': '建议' + risk,
                                   'ref': '建议低风险', 'methodology': '建议低风险'},
            'reason': '数据维度' + ('确认异常，建议高风险。' if risk == '高风险' else '可解释，建议低风险。')}


@pytest.mark.parametrize('precision', ['decimal precision mismatch', 'decimal_precision_mismatch',
                                     'decimal-precision mismatch', '小数精度不一致'])
@pytest.mark.parametrize('row', ['行内值复用', '行内数值复用', 'row-wise value reuse'])
def test_real_review_wording_is_covered_without_rewriting_findings(precision, row):
    evidence = {'deterministic_findings': {'decimal_precision_mismatch': [{}] * 7},
                'high_findings': [{'dim': 'data', 'test': 'rowwise_value_reuse'}]}
    info = result()
    info['data_review'] += (
        f'Fig 4I {row}告警：核验为可解释假阳性。IgG2c [0,25,0,0,25] 与 IgG3 [25,25,25,25,25]'
        '中的0和25为检出限相关数值。'
        f'{precision}告警：col_2 跨 Fig 1F 与 Fig 5G，涉及不同物理量；'
        'col_3 至 col_11 跨 Fig 4I 的整数滴度和 Fig 5G 的百分比，不构成同量纲精度矛盾。'
    )
    checked, gaps = validate(info, evidence)
    assert not gaps
    assert checked['data_review'] == info['data_review']
    assert checked['result'] == info['result']
    _, missing = validate(result(), evidence)
    assert any('小数精度' in g for g in missing)
    assert any('rowwise_value_reuse' in g for g in missing)


def test_fallback_preserves_specific_failure_reason():
    cause = '综合复核仍未覆盖全部维度或高风险线索：data/rowwise_value_reuse'
    error = review._fallback_result('10.1/test', cause)
    assert cause in error['reason']
    assert error['_review_error'] == cause
    assert error['trigger'] == 'review_error'
    assert error['review_status'] == 'review_error'
    assert error['risk_conclusion'] is None
    assert '未获得可解析' not in error['reason']


def test_unknown_screenshot_dimension_is_inferred_from_unique_page():
    info = result('低风险')
    info['image_review'] = '(1) 论文第 3 页 Fig. 1 已渲染核对，未见矛盾。'
    info['evidence_images'] = [{
        'dimension': 'unknown', 'caption': '论文 PDF 第 3 页 Fig. 1',
        'issue_index': None, 'path': '/tmp/page3.png',
    }]
    checked, gaps = validate(info, {'high_findings': []})
    assert not any('issue_index' in gap for gap in gaps)
    assert checked['evidence_images'][0]['dimension'] == 'image'
    assert checked['evidence_images'][0]['issue_index'] == 1


def test_unknown_screenshot_dimension_is_not_guessed_when_page_is_ambiguous():
    info = result('低风险')
    info['image_review'] = '(1) 论文第 3 页 Fig. 1 已渲染核对。'
    info['data_review'] = '(1) 论文第 3 页源数据已核对。'
    info['evidence_images'] = [{
        'dimension': 'unknown', 'caption': '论文 PDF 第 3 页',
        'issue_index': None, 'path': '/tmp/page3.png',
    }]
    checked, gaps = validate(info, {'high_findings': []})
    assert any('invalid dimension' in gap for gap in gaps)
    assert checked['evidence_images'][0]['dimension'] == 'unknown'


@pytest.mark.parametrize('saved_stage', ['judge', 'coverage_repair'])
def test_recovery_revalidates_latest_synthesis_before_first_pass_shortcut(monkeypatch, tmp_path, saved_stage):
    inputs = tmp_path / 'input'; inputs.mkdir()
    outputs = tmp_path / 'output'; outputs.mkdir()
    report = outputs / 'report.json'; report.write_text('{}')
    evidence = tmp_path / 'evidence.json'; evidence.write_text('{}')
    artifacts = review._review_artifacts_dir(str(outputs))
    state_path = review._review_state_path('10.1/test', str(report), str(inputs), artifacts)
    state = review._new_review_state('10.1/test', str(report), str(inputs))
    high = result()
    state['stages'] = {'evidence_path': str(evidence), 'llm_evidence_path': str(evidence),
                       'reviewer1': result('低风险'), saved_stage: high,
                       'final': review._fallback_result('10.1/test', 'old validator false positive')}
    review._save_review_state(state_path, state)
    monkeypatch.setattr(review, '_validate_review_result', lambda *args: {**args[3], '_coverage_gaps': []})
    monkeypatch.setattr(review, '_source_data_audit_highrisk_review_result', lambda *args: None)
    forbidden = AsyncMock(side_effect=AssertionError('must reuse saved synthesis without any new LLM pass'))
    for name in ('_run_with_retry', '_run_lowrisk_verifier_with_retry', '_run_judge'):
        monkeypatch.setattr(review, name, forbidden)
    recovered = asyncio.run(review.run_review_single('10.1/test', str(report), str(inputs), str(outputs), resume=True))
    assert recovered['result'] == '高风险'
    assert recovered['data_review'] == high['data_review']
    assert forbidden.await_count == 0
    assert json.loads(state_path.read_text())['stages']['decision']['source'] == saved_stage
