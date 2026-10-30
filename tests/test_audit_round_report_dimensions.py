"""Regression checks for per-dimension conclusions in the 20260910 reports."""
import importlib.util
from pathlib import Path

script = Path(__file__).resolve().parents[1] / 'scripts/apply_yujing_audit_20260910_highrisk_round.py'
spec = importlib.util.spec_from_file_location('audit_round_apply', script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_reference_only_issue_does_not_promote_other_dimensions():
    verdicts = module.dimension_verdicts_for({'doi': '10.1016/j.apmt.2020.100724', 'result': '高风险'})
    assert verdicts == {'image': '建议低风险', 'data': '建议低风险',
                        'ref': '建议高风险', 'methodology': '建议低风险'}


def test_cleared_image_and_reference_remain_low():
    verdicts = module.dimension_verdicts_for({'doi': '10.1007/s00299-023-03006-9', 'result': '高风险'})
    assert verdicts['image'] == verdicts['ref'] == '建议低风险'
    assert verdicts['data'] == verdicts['methodology'] == '建议高风险'


def test_every_verified_high_paper_has_a_supported_dimension():
    assert len(module.VERIFIED_HIGH_DIMENSIONS) == 21
    assert all(dims for dims in module.VERIFIED_HIGH_DIMENSIONS.values())


def test_coverage_index_does_not_confirm_dismissed_findings():
    item = {'image_review': '图像告警均为假阳性，无真实重复或拼接。',
            'data_review': '核心发现：存在可定位数据异常。'}
    evidence = {'high_findings': [
        {'dim': 'image_splicing', 'test': 'unknown_splicing', 'location': 'Fig.2'},
        {'dim': 'data', 'test': 'arithmetic_sequence', 'location': 'Table_1'},
    ]}
    module.add_coverage_notes(item, evidence, '高风险',
                              {'image': '建议低风险', 'data': '建议高风险'})
    assert '非已确认问题' in item['image_review']
    assert '不表示每项均已确认异常' in item['data_review']
    assert '经复核已纳入总体风险判断' not in str(item)
