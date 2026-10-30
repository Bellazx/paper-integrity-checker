"""Conservative column-role checks shared by evidence generation and coverage."""
import math


def is_summary_sample_count_pair(pair):
    """Both columns must explicitly be sample counts inside summary blocks.

    Never exclude means, SD/SEM, raw count measurements, or a column named N
    without its summary context. This only classifies duplicate-column alerts;
    checks of sample size versus study design remain independent.
    """
    count_headers = {'n', 'sample size', 'sample count', 'number of samples', '样本量', '样本数'}
    summary_labels = {'statistics summary', 'statistical summary', 'summary statistics', '统计汇总', '统计摘要'}
    for side in ('a', 'b'):
        header = str(pair.get(f'col_{side}_header') or '').strip().casefold()
        groups = {part.strip().casefold() for part in str(pair.get(f'group_{side}') or '').split('|')}
        samples = pair.get(f'sample_{side}') or []
        if header not in count_headers or not groups.intersection(summary_labels) or not samples:
            return False
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                   and math.isfinite(value) and value >= 0 and float(value).is_integer()
                   for value in samples):
            return False
    return True
