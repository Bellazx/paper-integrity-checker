import importlib.util
from pathlib import Path
import sys

import pytest

from modules.review_column_roles import is_summary_sample_count_pair
from modules.review_coverage import validate


def pair():
    return {'file': 'MOESM10.xlsx', 'sheet': 'Fig. 3e', 'col_a_header': 'n', 'col_b_header': 'n',
            'group_a': 'Statistics summary | GP values | Damage site',
            'group_b': 'Statistics summary | GP values | Surrounding area', 'cross_group': True,
            'sample_a': [13, 17, 17, 17, 17], 'sample_b': [13, 17, 17, 17, 17]}


def test_summary_counts_do_not_block_but_raw_measurements_still_do():
    item = pair()
    evidence = {'deterministic_findings': {'duplicate_column_pairs': [item]},
                'high_findings': [{'dim': 'data', 'test': 'figure_n_mismatch'}]}
    _, gaps = validate({}, evidence)
    assert not any('跨组重复列对' in gap for gap in gaps)
    assert any('figure_n_mismatch' in gap for gap in gaps)
    item['col_a_header'] = item['col_b_header'] = 'Mean'
    _, gaps = validate({}, evidence)
    assert any('跨组重复列对' in gap for gap in gaps)


@pytest.mark.parametrize('change', ['raw_context', 'one_side', 'fractional', 'missing_sample', 'nan', 'measurement_name'])
def test_ambiguous_or_measurement_columns_are_never_exempt(change):
    item = pair()
    assert is_summary_sample_count_pair(item)
    if change == 'raw_context':
        item['group_a'] = item['group_b'] = 'Raw data'
    elif change == 'one_side':
        item['col_b_header'] = 'Cell 1'
    elif change == 'fractional':
        item['sample_b'] = [13.7, 17]
    elif change == 'missing_sample':
        item['sample_b'] = []
    elif change == 'nan':
        item['sample_b'] = [float('nan')]
    else:
        item['col_a_header'] = item['col_b_header'] = 'Cell count'
    assert not is_summary_sample_count_pair(item)


def test_bundle_keeps_counts_in_audit_metadata_but_not_duplication_manifest(monkeypatch, tmp_path):
    scripts = Path('/home/library/.claude/skills/paper-batch-review/scripts')
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('count_role_bundle', scripts / 'review_evidence.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = tmp_path / 'source.xlsx'
    data.touch()
    monkeypatch.setattr(module, '_analyze_xlsx', lambda p: {'file': str(data), 'sheets': {}})
    monkeypatch.setattr(module, 'scan_xlsx', lambda *a, **k: [pair()])
    for name in ('_cross_sheet_reuse', '_cluster_cross_sheet_reuse', '_decimal_mismatch',
                 '_trailing_digit_findings', '_passage_offset_findings'):
        monkeypatch.setattr(module, name, lambda *a: [])
    monkeypatch.setenv('REVIEW_EVIDENCE_SKIP_SOFT_REBUTTALS', '1')
    bundle = module.build_bundle(str(tmp_path), '', '10.1/test')
    assert not bundle['deterministic_findings']['duplicate_column_pairs']
    assert bundle['deterministic_findings']['sample_count_column_matches'] == [pair()]
    assert not any(x.startswith('dupcol::') for x in bundle['coverage_manifest']['must_address'])
