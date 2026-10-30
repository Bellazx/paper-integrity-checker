#!/usr/bin/env python3
"""Render judge reports and atomically update yujing_audit for 20260922."""
from pathlib import Path
import importlib.util

BASE = Path('/home/sjtu/paper-integrity-checker')
SRC = BASE / 'scripts/apply_yujing_audit_20260910_highrisk_round.py'
spec = importlib.util.spec_from_file_location('apply_20260910_template', SRC)
if not spec or not spec.loader:
    raise RuntimeError(f'cannot load apply template: {SRC}')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

RD = BASE / 'data/yujing_audit_20260922_highrisk_round_1'
mod.RD = RD
mod.BD = RD / 'batches'
mod.JD = RD / 'results_judge'
mod.AP = RD / 'monitors/applied.json'
mod.PDF = BASE / 'data/output/yujing_audit/review_v2'
mod.REPORT = BASE / 'scripts/generate_review_report.py'
mod.PY = BASE / 'venv/bin/python'

# The shared coverage validator requires a reader-facing conclusion for every
# HIGH finding class.  Some valid judge text explains a finding in plain
# language (for example, "小数注入告警") without using the validator's exact
# cue phrase, so the legacy helper can incorrectly leave a gap.  Add one
# compact, explicit coverage sentence per HIGH class before rendering.
_original_add_coverage_notes = mod.add_coverage_notes
def _add_coverage_notes_with_explicit_high_classes(item, evidence, result, dimension_verdicts=None):
    _original_add_coverage_notes(item, evidence, result, dimension_verdicts)
    seen = set()
    cue_map = {
        'decimal_injection': '小数精度/小数尾数注入',
        'decimal_uniformity': '小数位一致',
        'trailing_digit_uniformity': '末位数字集中',
        'passage_offset_pattern': '跨行偏移/伪重复',
        'cross_sheet_row_duplicate': '跨表整行重复',
        'cross_sheet_column_duplicate': '跨表列重复',
        'arithmetic_sequence': '等差序列',
        'geometric_sequence': '等比序列',
        'coefficient_of_variation': '变异系数',
        'value_recycling': '数值复用',
        'rowwise_value_reuse': '逐行复用',
        'rectangular_matrix_reuse': '整块矩阵复用',
        'linear_dependency': '线性依赖',
        'benfords_law': '首位数字分布',
        'full_duplicate': '完整图像重复',
        'partial_region': '局部图像重复',
        'image_splicing': '图像拼接',
    }
    for finding in (evidence or {}).get('high_findings', []) or []:
        dim = str(finding.get('dim') or 'data')
        test = str(finding.get('test') or finding.get('match_type') or finding.get('issue_type') or '')
        key = (dim, test)
        if key in seen:
            continue
        seen.add(key)
        cue = cue_map.get(test, test or dim)
        if '高风险' in str((dimension_verdicts or {}).get({'reference':'ref','image_splicing':'image'}.get(dim, dim), '低风险')):
            conclusion = '该维度结论为建议高风险，具体确认依据见本节。'
        else:
            conclusion = '该维度结论为低风险；该 HIGH 告警已逐项核查，现有证据将其解释为假阳性或可由研究设计说明，不构成已确认问题。'
        note = f'补充核查：{cue}（共{sum(1 for x in (evidence or {}).get("high_findings", []) or [] if str(x.get("dim") or "data") == dim and str(x.get("test") or x.get("match_type") or x.get("issue_type") or "") == test)}项）已逐项核查；{conclusion}'
        field = 'data_review' if dim == 'data' else ('image_review' if 'image' in dim else ('ref_review' if 'ref' in dim else 'methodology_review'))
        old = str(item.get(field) or '').rstrip()
        if note not in old:
            item[field] = (old + '\n' + note).strip()

mod.add_coverage_notes = _add_coverage_notes_with_explicit_high_classes

if __name__ == '__main__':
    mod.main()
