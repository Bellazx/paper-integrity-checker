from modules.review_coverage import validate


def test_manuscript_conflict_accepts_specific_public_wording_without_rule_id():
    result = {
        'result': '高风险', 'verdict': '建议高风险',
        'image_review': '主图图像与对应源文件已核对，未确认跨实验图像复制。',
        'data_review': '图中两个组的数值与对应源数据相反，存在组别对应问题。',
        'ref_review': '参考文献著录与引用语境已核对，未确认无关引用。',
        'methodology_review': '第8页建库段存在正文表述不一致，同时写链特异与非链特异，需统一实际方法。',
        'dimension_verdicts': {k: '建议低风险' for k in ['image', 'data', 'ref', 'methodology']},
    }
    evidence = {'high_findings': [{'dim': 'data',
        'test': 'verified_manuscript_content_conflict', 'location': '第8页建库段'}]}
    _, gaps = validate(result, evidence)
    assert not any('verified_manuscript_content_conflict' in gap for gap in gaps)
    result['methodology_review'] = '已查看文中研究方法及样本安排，未发现其他独立问题。'
    _, gaps = validate(result, evidence)
    assert any('verified_manuscript_content_conflict' in gap for gap in gaps)
