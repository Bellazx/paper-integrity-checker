"""One publication gate for review repair and PDF rendering.

No findings or acknowledgements are synthesized here. The renderer applies the
public wording policy only after validation; internal decisions stay intact.
"""
from __future__ import annotations

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

PUBLICATION_POLICY_VERSION = '2026-10-23-public-wording-v2-inline-evidence'


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

    gaps = []
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
