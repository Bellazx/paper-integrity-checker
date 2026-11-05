"""One publication gate for review repair and PDF rendering.

No findings or acknowledgements are synthesized here. The renderer applies the
public wording policy only after validation; internal decisions stay intact.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from functools import lru_cache
from pathlib import Path

PUBLICATION_POLICY_VERSION = '2026-10-23-public-wording-v2-inline-evidence'


def normalize_excel_evidence(items):
    """Translate explicit A1 ranges to the renderer schema without guessing cells."""
    from openpyxl.utils.cell import range_boundaries
    normalized = []
    for raw in items or []:
        item = dict(raw) if isinstance(raw, dict) else raw
        if isinstance(item, dict) and not item.get('regions'):
            address = str(item.get('range') or '').strip()
            if (item.get('file') and item.get('sheet') and
                    re.fullmatch(r'\$?[A-Za-z]+\$?[1-9][0-9]*(?::\$?[A-Za-z]+\$?[1-9][0-9]*)?', address)):
                c1, r1, c2, r2 = range_boundaries(address)
                item['regions'] = [{'file': item['file'], 'sheet': item['sheet'],
                                    'row_range': [r1, r2], 'col_range': [c1, c2]}]
                item.setdefault('dimension', 'data')
                if not item.get('caption') and item.get('description'):
                    item['caption'] = item['description']
        normalized.append(item)
    return normalized


def renderer_path() -> Path:
    for root in (Path('/opt/.claude'), Path('/home/library/.claude'), Path.home() / '.claude'):
        path = root / 'skills/paper-batch-review/scripts/generate_review_report.py'
        if path.is_file():
            return path
    raise FileNotFoundError('paper-batch-review renderer not found')


@lru_cache(maxsize=1)
def renderer_module():
    path = renderer_path()
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('shared_review_renderer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from modules.review_report_layout import evidence_images_html
    module._evidence_images_html = evidence_images_html
    from modules.review_public_wording import install_renderer_policy
    install_renderer_policy(module)
    if hasattr(module, 'DB_CONFIG'):
        module.DB_CONFIG.update(login_timeout=10, timeout=60)
    return module


def validate(result: dict, evidence: dict) -> tuple[dict, list[str]]:
    if not isinstance(result, dict):
        gaps = ['Review must be a JSON object']
        return {'_coverage_status': 'warning', '_coverage_gaps': gaps}, gaps
    schema_gaps = []
    for field in ('result', 'verdict', 'image_review', 'data_review', 'ref_review',
                  'methodology_review', 'reason'):
        if field in result and not isinstance(result[field], str):
            schema_gaps.append(f'{field} must be a string')
    dimensions = result.get('dimension_verdicts')
    if dimensions is not None and (not isinstance(dimensions, dict)
                                  or any(not isinstance(v, str) for v in dimensions.values())):
        schema_gaps.append('dimension_verdicts must map dimensions to risk strings')
    if schema_gaps:
        return {**result, '_coverage_status': 'warning', '_coverage_gaps': schema_gaps}, schema_gaps
    renderer = renderer_module()
    from modules.review_coverage import validate as validate_coverage

    # Canonicalize explicit equivalent labels, never infer missing dimensions
    # from the paper's overall verdict.
    result = dict(result)
    aliases = {'低风险': '建议低风险', '高风险': '建议高风险',
               '未发现风险': '建议低风险', '有风险': '建议高风险'}
    result['dimension_verdicts'] = {
        k: aliases.get(v, v) for k, v in (result.get('dimension_verdicts') or {}).items()
    }
    if 'excel_evidence_regions' in result:
        result['excel_evidence_regions'] = normalize_excel_evidence(result['excel_evidence_regions'])

    # Model-generated evidence occasionally labels a screenshot dimension as
    # unknown even though its caption names a page that appears in exactly one
    # numbered finding. Resolve only that unique case; ambiguous or unlocated
    # evidence remains a publication gap.
    from modules.review_report_layout import split_review_issues
    fields = {
        'image': 'image_review',
        'data': 'data_review',
        'ref': 'ref_review',
        'methodology': 'methodology_review',
    }
    page_issues = {}
    for dimension, field in fields.items():
        try:
            _, issues = split_review_issues(str(result.get(field) or ''))
        except ValueError:
            issues = []
        for issue_index, body in issues:
            for match in re.finditer(r'第\s*(\d+)\s*页|\bpage\s*(\d+)\b', body, re.IGNORECASE):
                page = next((value for value in match.groups() if value), None)
                if page:
                    page_issues.setdefault(page, set()).add((dimension, issue_index))
    for field in ('evidence_images', 'excel_evidence_regions'):
        normalized = []
        for raw in result.get(field) or []:
            item = dict(raw) if isinstance(raw, dict) else raw
            if isinstance(item, dict) and str(item.get('dimension') or '').strip().lower() in {'', 'unknown'}:
                caption = str(item.get('caption') or '')
                page_match = re.search(r'第\s*(\d+)\s*页|\bpage\s*(\d+)\b', caption, re.IGNORECASE)
                page = next((value for value in page_match.groups() if value), None) if page_match else None
                candidates = page_issues.get(page, set()) if page else set()
                if len(candidates) == 1:
                    dimension, issue_index = next(iter(candidates))
                    item['dimension'] = dimension
                    if item.get('issue_index') is None:
                        item['issue_index'] = issue_index
            normalized.append(item)
        if field in result or normalized:
            result[field] = normalized

    gaps = []
    for field in ('evidence_images', 'excel_evidence_regions'):
        for item in result.get(field) or []:
            if not isinstance(item, dict):
                gaps.append(f'{field} entries must be objects')
                continue
            dimension = item.get('dimension') or ('data' if field == 'excel_evidence_regions' else '')
            if dimension not in fields:
                gaps.append(f'Evidence has invalid dimension: {dimension or "missing"}')
    if result.get('trigger') == 'review_error':
        gaps.append('A process error is not a substantive review')
    if result.get('result') not in {'高风险', '低风险'}:
        gaps.append('Result must contain an explicit high/low risk conclusion')
    elif result.get('verdict') != '建议' + result['result']:
        gaps.append('Verdict must agree with the explicit risk conclusion')
    if not isinstance(evidence, dict) or not evidence:
        gaps.append('A nonempty evidence bundle is required for publication')
    _, coverage_gaps = validate_coverage(result, evidence if isinstance(evidence, dict) else {})
    gaps.extend(coverage_gaps)
    from modules.review_report_layout import bind_issue_evidence
    for dimension, field in [('image', 'image_review'), ('data', 'data_review'),
                             ('ref', 'ref_review'), ('methodology', 'methodology_review')]:
        images = [item for item in (result.get('evidence_images') or [])
                  if isinstance(item, dict) and item.get('dimension') == dimension]
        images += [item for item in (result.get('excel_evidence_regions') or [])
                   if isinstance(item, dict) and item.get('dimension', 'data') == dimension]
        if images:
            try:
                bind_issue_evidence(result.get(field, ''), images)
            except ValueError as exc:
                gaps.append(str(exc))
    try:
        renderer._validate_review_info_consistency(result)
    except ValueError as exc:
        gaps.append(str(exc))
    out = dict(result)
    # These fields describe an older validation attempt, not scientific facts.
    # Always recompute them against the current full evidence and review.
    out.pop('_coverage_error', None)
    out['_coverage_gaps'] = gaps
    out['_coverage_status'] = 'warning' if gaps else 'ok'
    return out, gaps
