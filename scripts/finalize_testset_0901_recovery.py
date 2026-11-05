#!/usr/bin/env python3
"""Merge this recovery's checkpoints, validate, and publish completed papers."""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from api.services.review_validation import validate
from api.services.review import _review_state_path

ROOT = BASE / 'data/output/yujing_testset_0901_0907_nature_nc'
SOURCE = ROOT / 'review_results_0901-0907-Nature_NC_highrisk_20261031.json'


def resolve_evidence(item, artifacts):
    """Recover a bundle by its paper identity, not a mutable timestamp hash."""
    doi = item['doi']
    explicit = item.get('_evidence_path') or item.get('_coverage_evidence_path')
    if not explicit or not Path(explicit).is_file():
        state = _review_state_path(doi, item['_report_json'], item['_input_dir'], artifacts)
        if state.is_file():
            explicit = json.loads(state.read_text()).get('stages', {}).get('evidence_path')
    candidates = []
    if explicit and Path(explicit).is_file():
        candidates = [Path(explicit)]
    else:
        candidates = sorted(artifacts.glob(doi.replace('/', '_') + '_*_evidence.json'))
    matches = []
    for path in candidates:
        bundle = json.loads(path.read_text())
        if str(bundle.get('doi', '')).lower() != doi.lower():
            continue
        if any(not bundle.get(key) or Path(bundle[key]).resolve() != Path(item[field]).resolve()
               for key, field in [('input_dir', '_input_dir'), ('report_json', '_report_json')]):
            continue
        matches.append((path, bundle))
    if not matches:
        raise ValueError(f'No evidence with matching DOI, input directory and report for {doi}')
    if len(matches) > 1 and any(bundle != matches[0][1] for _, bundle in matches[1:]):
        raise ValueError(f'Ambiguous evidence bundles for {doi}; select the verified bundle explicitly')
    return matches[0]


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def verify_publication(ready):
    """Verify the exact batch after the shared PDF/database writer finishes."""
    import fitz
    from api.services.review_validation import renderer_module
    renderer = renderer_module()
    dois = [item['doi'] for item in ready]
    conn = renderer.pymssql.connect(**renderer.DB_CONFIG)
    try:
        cursor = conn.cursor(as_dict=True)
        cursor.execute('SELECT doi,review_result,review_report_url,status '
                       'FROM yujing_testset_0901_0907_nature_nc WHERE doi IN ('
                       + ','.join(['%s'] * len(dois)) + ')', tuple(dois))
        rows = cursor.fetchall()
    finally:
        conn.close()
    if len(rows) != len(dois) or len({row['doi'] for row in rows}) != len(dois):
        raise ValueError('Publication verification: missing or duplicate database records')
    by_doi = {row['doi']: row for row in rows}
    manifest = []
    for item in ready:
        row = by_doi[item['doi']]
        filename = 'review_' + item['doi'].replace('/', '_') + '.pdf'
        path = ROOT / 'review_v2' / filename
        expected_url = renderer.REVIEW_REPORT_BASE_URL + '/yujing_testset_0901_0907_nature_nc/' + filename
        if (row['review_result'] != item['result'] or row['status'] != 2
                or row['review_report_url'] != expected_url):
            raise ValueError(f'Database result/URL/status mismatch: {item["doi"]}')
        expected = '有风险' if item['result'] == '高风险' else '未发现风险'
        with fitz.open(path) as document:
            text = ''.join(''.join(page.get_text() for page in document).split())
            conclusion = re.search(r'复核结论[：:](未发现风险|有风险)', text)
            if not conclusion or conclusion.group(1) != expected:
                raise ValueError(f'PDF conclusion mismatch: {item["doi"]}')
            if item['result'] == '高风险' and not any(page.get_images() for page in document):
                raise ValueError(f'Risk report has no embedded evidence: {item["doi"]}')
            pages = len(document)
        manifest.append({'doi': item['doi'], 'result': item['result'], 'pdf': str(path),
                         'review_report_url': expected_url, 'pages': pages,
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'verified_at': datetime.now(timezone.utc).isoformat(), 'total': len(ready),
            'pdf_verified': len(manifest), 'database_verified': len(rows),
            'results': dict(Counter(item['result'] for item in ready)), 'papers': manifest}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--verify-only', action='store_true',
                        help='Verify existing PDFs and database without rendering again')
    args = parser.parse_args()
    original = json.loads(SOURCE.read_text())
    scope = {x['doi']: x for x in original}
    merged = {doi: dict(x) for doi, x in scope.items() if x.get('trigger') != 'review_error'}
    sources = [ROOT / 'failed_review_recovery_20261101.json',
               ROOT / 'failed_review_recovery_20261101_current.json',
               ROOT / 'failed_review_recovery_20261102_retry.json',
               ROOT / 'failed_review_recovery_20261102_fresh_current.json']
    for path in sources:
        if path.is_file():
            for item in json.loads(path.read_text()):
                if item.get('doi') in scope and item.get('trigger') != 'review_error':
                    merged[item['doi']] = dict(item)
    for path in sorted((ROOT / 'convergence_20261102').glob('10.*.json')):
        item = json.loads(path.read_text())
        if (isinstance(item, dict) and item.get('doi') in scope
                and path.name == item['doi'].replace('/', '_') + '.json'
                and item.get('trigger') != 'review_error'):
            merged[item['doi']] = dict(item)
    ready, pending = [], []
    for doi, source in sorted(scope.items()):
        item = merged.get(doi)
        if not item:
            pending.append({'doi': doi, 'gaps': ['No completed review']})
            continue
        slug = doi.replace('/', '_')
        item['_input_dir'] = (item.get('_input_dir') or source.get('_input_dir') or source.get('input_dir')
                              or str(BASE / 'data/testset/0901-0907-Nature&NC' / slug))
        item['_report_json'] = (item.get('_report_json') or source.get('_report_json') or source.get('report_json')
                                or str(ROOT / 'init' / slug / 'report.json'))
        try:
            ep, evidence = resolve_evidence(item, ROOT / 'review_v2/review_artifacts')
            checked, gaps = validate(item, evidence)
            if gaps:
                pending.append({'doi': doi, 'gaps': gaps})
                continue
            checked['_evidence_path'] = str(ep)
            ready.append(checked)
        except Exception as exc:
            pending.append({'doi': doi, 'gaps': [str(exc)]})
    target = ROOT / 'review_results_converged_20261102.json'
    save(target, ready)
    save(ROOT / 'convergence_pending_20261102.json', pending)
    summary = {'total': len(scope), 'validated': len(ready), 'pending': len(pending),
               'results': dict(Counter(x['result'] for x in ready))}
    save(ROOT / 'convergence_status_20261102.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    for x in pending:
        print(x['doi'], str(x['gaps'])[:500], flush=True)
    if args.publish or args.verify_only:
        if pending or len(ready) != len(scope):
            print('Publication deferred: this convergence batch still has unresolved papers', file=sys.stderr)
            return 2
        if not args.verify_only:
            from scripts import run_scholar_batch as batch
            batch.TABLE = 'yujing_testset_0901_0907_nature_nc'
            # This table is keyed by DOI and has no scholar column.
            rc = batch.render_and_update_reviews(ready, ROOT, '', result_path=target)
            if rc:
                return rc
        publication = verify_publication(ready)
        save(ROOT / 'publication_verified_20261102.json', publication)
        # Keep the historical attempts for audit, but let ordinary progress
        # readers see the completed batch at its established result path.
        backup = ROOT / 'audit_backups' / 'review_results_before_convergence_20261102.json'
        if not backup.exists():
            save(backup, original)
        save(SOURCE, ready)
        summary.update(published=len(ready), database_verified=publication['database_verified'],
                       pdf_verified=publication['pdf_verified'], status='complete')
        save(ROOT / 'convergence_status_20261102.json', summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
