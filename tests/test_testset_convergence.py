import json

import pytest

from scripts.finalize_testset_0901_recovery import resolve_evidence


def evidence_case(tmp_path):
    item = {'doi': '10.1038/paper', '_input_dir': str(tmp_path / 'input'),
            '_report_json': str(tmp_path / 'report.json')}
    bundle = {'doi': item['doi'], 'input_dir': item['_input_dir'],
              'report_json': item['_report_json'], 'high_findings': []}
    return item, bundle


def test_resolve_existing_bundle_after_timestamp_hash_changed(tmp_path):
    item, bundle = evidence_case(tmp_path)
    item['_evidence_path'] = str(tmp_path / 'missing_new_hash_evidence.json')
    old = tmp_path / '10.1038_paper_old_hash_evidence.json'
    old.write_text(json.dumps(bundle))
    path, result = resolve_evidence(item, tmp_path)
    assert path == old and result == bundle


@pytest.mark.parametrize('key', ['doi', 'input_dir', 'report_json'])
def test_resolver_rejects_other_paper_or_input_revision(tmp_path, key):
    item, bundle = evidence_case(tmp_path)
    bundle[key] = 'different'
    path = tmp_path / '10.1038_paper_old_hash_evidence.json'
    path.write_text(json.dumps(bundle))
    item['_evidence_path'] = str(path)
    with pytest.raises(ValueError, match='No evidence with matching'):
        resolve_evidence(item, tmp_path)


def test_distinct_matching_bundles_require_explicit_selection(tmp_path):
    item, bundle = evidence_case(tmp_path)
    first = tmp_path / '10.1038_paper_first_evidence.json'
    second = tmp_path / '10.1038_paper_second_evidence.json'
    first.write_text(json.dumps(bundle))
    second.write_text(json.dumps({**bundle, 'high_findings': [{'test': 'new'}]}))
    with pytest.raises(ValueError, match='Ambiguous'):
        resolve_evidence(item, tmp_path)
    item['_evidence_path'] = str(second)
    assert resolve_evidence(item, tmp_path)[0] == second
