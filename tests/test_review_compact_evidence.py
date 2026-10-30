from api.services.review_validation import renderer_module


def test_compact_lowrisk_keeps_correction_and_impact_together():
    renderer = renderer_module()
    text = ('本模块复核结论：建议低风险。\n'
            'Data S7表头写40D，但376个均值对应45日。'
            '这属于表头笔误，不代表采样时间错误。\n其余详细核验记录。')
    output = renderer._compact_section_text(text, '建议低风险')
    assert '376个均值对应45日' in output
    assert '不代表采样时间错误' in output
    assert '其余详细核验记录' not in output


def test_compact_summary_preserves_locations_and_limits():
    renderer = renderer_module()
    text = 'Fig.4a组别与源数据相反。Fig.2d两处p值写错，但更正后仍显著。'
    assert renderer._compact_overall_reason({'result': '高风险'}, text) == text
