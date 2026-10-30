import numpy as np
from scipy import stats as scipy_stats


def check_cv(values: np.ndarray, threshold: float = 0.01) -> dict:
    """Check coefficient of variation. CV near 0 in experimental data is suspicious."""
    values = values[~np.isnan(values)]
    if len(values) < 3:
        return {"testable": False, "reason": "insufficient_data", "n": len(values)}

    mean = np.mean(values)
    std = np.std(values, ddof=1)

    if abs(mean) < 1e-12:
        return {"testable": False, "reason": "mean_near_zero"}

    cv = std / abs(mean)
    severity = "high" if cv < 0.001 else "medium" if cv < threshold else "low"

    return {
        "testable": True,
        "mean": float(mean),
        "std": float(std),
        "cv": float(cv),
        "flagged": cv < threshold,
        "severity": severity,
        "n": len(values),
    }


def check_arithmetic_sequence(values: np.ndarray, rel_tol: float = 0.005) -> dict:
    """Check if values form an arithmetic progression."""
    values = values[~np.isnan(values)]
    if len(values) < 3:
        return {"is_arithmetic": False, "reason": "insufficient_data"}

    diffs = np.diff(values)
    mean_diff = np.mean(diffs)

    if abs(mean_diff) < 1e-12:
        is_constant = np.all(np.abs(diffs) < 1e-12)
        return {"is_arithmetic": is_constant, "common_diff": 0.0, "max_relative_deviation": 0.0, "type": "constant", "n": len(values)}

    max_deviation = float(np.max(np.abs(diffs - mean_diff)) / abs(mean_diff))
    is_arith = max_deviation < rel_tol

    return {
        "is_arithmetic": is_arith,
        "common_diff": float(mean_diff),
        "max_relative_deviation": max_deviation,
        "n": len(values),
    }


def check_geometric_sequence(values: np.ndarray, rel_tol: float = 0.005) -> dict:
    """Check if values form a geometric progression."""
    values = values[~np.isnan(values)]
    if len(values) < 3 or np.any(values <= 0):
        return {"is_geometric": False}

    log_values = np.log(values)
    result = check_arithmetic_sequence(log_values, rel_tol)
    return {
        "is_geometric": result.get("is_arithmetic", False),
        "common_ratio": float(np.exp(result.get("common_diff", 0))),
        "max_relative_deviation": result.get("max_relative_deviation"),
        "n": result.get("n"),
    }


def grim_test(reported_mean: float, n: int, precision: int = 2) -> dict:
    """GRIM test: check if a reported mean of N integer values is mathematically possible."""
    if n < 1:
        return {"testable": False}

    product = reported_mean * n
    nearest_int = round(product)
    reconstructed = round(nearest_int / n, precision)
    consistent = abs(reconstructed - reported_mean) < 0.5 * 10 ** (-precision)

    return {
        "consistent": consistent,
        "reported_mean": reported_mean,
        "n": n,
        "nearest_valid_mean": reconstructed,
        "flagged": not consistent,
    }


def benfords_law_test(values: np.ndarray, min_samples: int = 100) -> dict:
    """Test if first significant digits follow Benford's law."""
    values = values[~np.isnan(values)]
    positive = np.abs(values[values != 0])

    if len(positive) < min_samples:
        return {"testable": False, "reason": "insufficient_data", "n": len(positive)}

    first_digits = []
    for v in positive:
        s = f"{v:.10e}"
        for ch in s:
            if ch.isdigit() and ch != "0":
                first_digits.append(int(ch))
                break

    first_digits = np.array(first_digits)
    if len(first_digits) < min_samples:
        return {"testable": False, "reason": "insufficient_nonzero_digits", "n": len(first_digits)}

    expected_freq = np.array([np.log10(1 + 1 / d) for d in range(1, 10)])
    observed_counts = np.array([np.sum(first_digits == d) for d in range(1, 10)])
    expected_counts = expected_freq * len(first_digits)

    chi2, p_value = scipy_stats.chisquare(observed_counts, expected_counts)

    return {
        "testable": True,
        "chi2": float(chi2),
        "p_value": float(p_value),
        "flagged": p_value < 0.05,
        "observed": observed_counts.tolist(),
        "expected": [round(x, 1) for x in expected_counts.tolist()],
        "n": len(first_digits),
    }


def _is_monotonic_arithmetic(arr: np.ndarray) -> bool:
    """Inline axis test (stats.py can't import data_checker): strictly monotonic, near-perfect
    constant-step sequence with >=4 points = an X-axis / swept parameter, not measurement data."""
    v = arr[np.isfinite(arr)]
    if len(v) < 4:
        return False
    diffs = np.diff(v)
    if not (np.all(diffs > 0) or np.all(diffs < 0)):
        return False
    step = np.median(diffs)
    if step == 0:
        return False
    return bool(np.max(np.abs(diffs - step)) / abs(step) <= 0.02)


def check_cross_group_duplicates(groups: dict[str, np.ndarray]) -> list[dict]:
    """Check for duplicate data values across experimental groups."""
    _SEV_ORDER = {"low": 0, "medium": 1, "high": 2}
    flagged = []
    names = list(groups.keys())

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = groups[names[i]][~np.isnan(groups[names[i]])]
            b = groups[names[j]][~np.isnan(groups[names[j]])]
            if len(a) == 0 or len(b) == 0:
                continue

            if _is_monotonic_arithmetic(a) and _is_monotonic_arithmetic(b):
                continue
            n = min(len(a), len(b))
            aligned = float(np.mean(np.round(a[:n], 8) == np.round(b[:n], 8))) if n else 0.0

            common = np.intersect1d(a, b)
            overlap = len(common) / max(len(a), len(b))

            if aligned >= 0.95:
                severity = "high"
            elif aligned >= 0.8:
                severity = "medium"
            elif aligned > 0.5:
                severity = "low"
            else:
                continue

            high_precision_count = sum(
                1 for v in common
                if '.' in str(v) and len(str(v).split('.')[-1].rstrip('0')) >= 6
            )
            if high_precision_count >= 3 and _SEV_ORDER.get(severity, 0) < _SEV_ORDER["medium"]:
                severity = "medium"

            flagged.append({
                "group_a": names[i],
                "group_b": names[j],
                "overlap_ratio": float(aligned),
                "set_overlap_ratio": float(overlap),
                "common_values": common.tolist(),
                "high_precision_matches": high_precision_count,
                "severity": severity,
            })
    return flagged


def check_linear_dependency(
    values_a: np.ndarray,
    values_b: np.ndarray,
    r2_threshold: float = 0.9999,
    min_samples: int = 50,
) -> dict:
    """Check if two data series have a near-perfect linear relationship (y = ax + b)."""
    mask = ~np.isnan(values_a) & ~np.isnan(values_b)
    a = values_a[mask]
    b = values_b[mask]
    n = len(a)
    if n < min_samples:
        return {"testable": False, "reason": "insufficient_data", "n": n}

    if np.std(a) < 1e-10 or np.std(b) < 1e-10:
        return {"testable": False, "reason": "constant_data"}

    try:
        slope, intercept, r_value, p_value, std_err = scipy_stats.linregress(a, b)
    except ValueError:
        return {"testable": False, "reason": "constant_data"}
    r_squared = r_value ** 2

    is_integer_slope = abs(slope - round(slope)) < 0.001 if abs(slope) > 0.1 else False
    is_integer_intercept = abs(intercept - round(intercept)) < 0.5 if abs(intercept) > 0.5 else False
    is_offset_pattern = (abs(slope - 1.0) < 0.01) and is_integer_intercept and abs(intercept) >= 1.0

    return {
        "testable": True,
        "flagged": r_squared > r2_threshold,
        "slope": float(slope),
        "intercept": float(intercept),
        "r_squared": float(r_squared),
        "p_value": float(p_value),
        "n": n,
        "is_integer_slope": is_integer_slope,
        "is_integer_intercept": is_integer_intercept,
        "is_offset_pattern": is_offset_pattern,
    }


def check_decimal_uniformity(values: np.ndarray) -> dict:
    """Check if decimal patterns are suspiciously uniform (fabrication indicator)."""
    values = values[~np.isnan(values)]
    if len(values) < 5:
        return {"testable": False}

    str_values = [f"{v}" for v in values]
    decimal_places = []
    for s in str_values:
        if "." in s:
            decimal_places.append(len(s.split(".")[1].rstrip("0")))
        else:
            decimal_places.append(0)

    if not decimal_places:
        return {"testable": False}

    unique_places = len(set(decimal_places))
    all_same = unique_places == 1

    return {
        "testable": True,
        "uniform_decimals": all_same,
        "unique_decimal_lengths": unique_places,
        "decimal_places": decimal_places,
        "flagged": all_same and len(values) >= 5,
        "severity": "low",
    }


def check_trailing_digit_uniformity(
    values: np.ndarray,
    n_digits: int = 4,
    ratio_threshold: float = 0.8,
    min_samples: int = 10,
) -> dict:
    """Detect synthetic-data '末位填充' (trailing-digit padding) pattern.

    A genuine measurement column has approximately uniformly random trailing
    digits. If >= ratio_threshold of the values share the same last N decimal
    digits (e.g. all 30 values end in "1246"), the column is almost certainly
    fabricated — probability ~10^-(n_digits * ratio * n) for genuine data.

    Distinct from `check_decimal_uniformity`, which only checks the *count* of
    decimal places, not the digit *content* of the trailing portion.

    Parameters
    ----------
    values : np.ndarray
        Numeric column to check.
    n_digits : int
        How many trailing decimal digits to consider. 4 by default; values
        with fewer significant decimals are counted with their actual length.
    ratio_threshold : float
        Minimum fraction of values that must share the most-common suffix
        to flag as high. Default 0.8.
    min_samples : int
        Minimum non-NaN samples required to test. Default 10.

    Returns
    -------
    dict
        ``flagged``, ``severity`` ("high" or "medium"), ``most_common_suffix``,
        ``match_ratio``, ``match_count``, ``total``, ``n``.
    """
    clean = values[~np.isnan(values)]
    if len(clean) < min_samples:
        return {"testable": False, "reason": "insufficient_data", "n": int(len(clean))}

    # Integer columns are covered by terminal_digit_test (which is unbiased for ints).
    if np.all(clean == np.floor(clean)):
        return {"testable": False, "reason": "integer_data_use_terminal_digit", "n": int(len(clean))}

    suffixes: list[str] = []
    for v in clean:
        s = f"{abs(float(v)):.15f}"
        if "." in s:
            decimal_part = s.split(".", 1)[1].rstrip("0")
            if len(decimal_part) >= n_digits:
                suffixes.append(decimal_part[-n_digits:])
            elif decimal_part:
                # Shorter than requested but has content — record actual digits
                # (handles padding-style cases where trailing zeros were stripped
                # by float parsing).
                suffixes.append(decimal_part)
            else:
                suffixes.append("__int__")  # value has no decimal portion
        else:
            suffixes.append("__int__")
    if not suffixes:
        return {"testable": False, "reason": "no_decimals", "n": int(len(clean))}

    from collections import Counter
    counter = Counter(suffixes)
    most_common_suffix, most_common_count = counter.most_common(1)[0]
    match_ratio = most_common_count / len(suffixes)

    # Skip the "all values are integer-like" case — that's a decimal_uniformity
    # concern, not a synthetic-padding marker. We want patterns like "1246" or
    # "47", not "".
    if most_common_suffix == "__int__":
        return {
            "testable": True,
            "flagged": False,
            "match_ratio": float(match_ratio),
            "most_common_suffix": "",
            "n": int(len(clean)),
        }

    # Severity ladder:
    #   high   — last-4 suffix uniform at >= 60%  OR  last-3 at >= 80%
    #            (very strong synthetic marker; 4-digit pattern at 60% match over
    #             15 values has chance probability ~10^-32 — essentially impossible
    #             by chance)
    #   medium — last-4 suffix uniform at >= 50%  OR  last-3 at >= 60%
    #            (still very rare by chance; ~10^-12 over 15 values for 4-digit at 50%)
    if n_digits >= 4 and match_ratio >= 0.6:
        severity = "high"
    elif n_digits >= 3 and match_ratio >= 0.8:
        severity = "high"
    elif n_digits >= 4 and match_ratio >= 0.5:
        severity = "medium"
    elif n_digits >= 3 and match_ratio >= max(0.6, ratio_threshold - 0.2):
        severity = "medium"
    else:
        return {
            "testable": True,
            "flagged": False,
            "match_ratio": float(match_ratio),
            "most_common_suffix": most_common_suffix,
            "n": int(len(clean)),
        }

    return {
        "testable": True,
        "flagged": True,
        "severity": severity,
        "n_digits": n_digits,
        "most_common_suffix": most_common_suffix,
        "match_ratio": float(match_ratio),
        "match_count": int(most_common_count),
        "total": int(len(suffixes)),
        "n": int(len(clean)),
    }


def check_value_recycling(values: np.ndarray, min_samples: int = 10) -> dict:
    """Detect value recycling: few unique values filling many data points."""
    clean = values[~np.isnan(values)]
    if len(clean) < min_samples:
        return {"testable": False}

    unique_count = len(np.unique(clean))
    total_count = len(clean)
    ratio = unique_count / total_count

    all_integer = np.all(clean == np.floor(clean))
    if all_integer and unique_count <= 20:
        return {"testable": True, "flagged": False, "ratio": float(ratio)}

    flagged = ratio < 0.3
    if not flagged:
        return {"testable": True, "flagged": False, "ratio": float(ratio)}

    # Dense curves/spectra (thousands of sampled points) naturally repeat rounded values —
    # a low unique/total ratio there is NOT fabrication. For large columns, demand a much
    # lower ratio before calling it HIGH; small tables keep the sensitive 0.15 threshold
    # (that's where fabricated small datasets actually show up).
    high_ratio_cutoff = 0.05 if total_count >= 500 else 0.15
    severity = "high" if ratio < high_ratio_cutoff else "medium"
    return {
        "testable": True,
        "flagged": True,
        "ratio": float(ratio),
        "unique_count": int(unique_count),
        "total_count": int(total_count),
        "severity": severity,
    }


def _last_significant_digit(v) -> int or None:
    """Return the last significant digit of a number (trailing zeros stripped)."""
    if not np.isfinite(v):
        return None
    s = f"{abs(float(v)):.10f}".rstrip("0").rstrip(".").replace(".", "")
    if s and s[-1].isdigit():
        return int(s[-1])
    return None


def terminal_digit_test(values: np.ndarray, min_samples: int = 50) -> dict:
    """Terminal-digit test for INTEGER measurement data (e.g. counts): the last digit
    (v % 10) of genuine counts is distributed ~uniformly over 0-9, so a strong
    chi-square deviation can indicate manually constructed numbers.

    Restricted to integer-valued columns on purpose: source spreadsheets are parsed as
    floats, which lose trailing zeros, so the "last significant digit" of decimal data
    systematically undercounts 0 and would false-positive. Decimal-precision fabrication
    is covered separately by decimal_uniformity / cross-sheet precision checks. Never
    rated above 'medium' on its own."""
    values = values[~np.isnan(values)]
    if len(values) < min_samples:
        return {"testable": False, "reason": "insufficient_data", "n": len(values)}

    # Integer-only: last digit is unbiased and 0 is a legitimate outcome.
    if not np.all(values == np.floor(values)):
        return {"testable": False, "reason": "non_integer_data", "n": len(values)}
    # Need values large enough that the last digit can plausibly be uniform.
    if np.median(np.abs(values)) < 10:
        return {"testable": False, "reason": "values_too_small", "n": len(values)}
    # Avoid recycled / low-cardinality columns (IDs, small code sets).
    if len(np.unique(values)) < 10:
        return {"testable": False, "reason": "low_cardinality", "n": len(values)}

    digits = (np.abs(values).astype(np.int64) % 10)
    counts = np.array([int(np.sum(digits == d)) for d in range(10)])
    n = int(counts.sum())
    expected = np.full(10, n / 10.0)
    chi2, p_value = scipy_stats.chisquare(counts, expected)

    if p_value < 0.001:
        severity = "medium"
    elif p_value < 0.01:
        severity = "low"
    else:
        severity = None

    return {
        "testable": True,
        "n": n,
        "chi2": float(chi2),
        "p_value": float(p_value),
        "digit_counts": counts.tolist(),
        "flagged": severity is not None,
        "severity": severity,
    }


def sd_regularity_test(values: np.ndarray) -> dict:
    """Detect suspiciously regular dispersion (SD/SE) columns: all-integer values,
    identical decimal precision, or all half-steps (multiples of 0.5) — patterns that
    are unusual for genuine standard deviations/errors. Rated 'low' (agent decides)."""
    values = values[~np.isnan(values)]
    if len(values) < 4:
        return {"testable": False, "n": len(values)}
    if np.all(values == 0):
        return {"testable": False, "reason": "all_zero"}

    all_integer = bool(np.all(values == np.floor(values)))
    all_half = bool(np.all(np.abs(values * 2 - np.round(values * 2)) < 1e-9))
    dec = check_decimal_uniformity(values)
    uniform_dec = dec.get("uniform_decimals", False) and dec.get("unique_decimal_lengths", 0) == 1

    if all_integer:
        pattern = "all_integer"
    elif all_half:
        pattern = "all_half_step"
    elif uniform_dec:
        pattern = "uniform_decimals"
    else:
        pattern = None

    return {
        "testable": True,
        "n": len(values),
        "flagged": pattern is not None,
        "pattern": pattern,
        "severity": "low" if pattern else None,
    }


def grimmer_test(reported_mean: float, reported_sd: float, n: int,
                 mean_precision: int = 2, sd_precision: int = 2) -> dict:
    """GRIMMER test: check whether a reported SD is consistent with an integer-valued
    sample of size n having the reported mean. Returns flagged=True if no valid integer
    sum-of-squares exists in the SD's rounding interval (i.e. the SD is impossible)."""
    if n < 2:
        return {"testable": False}

    S = round(reported_mean * n)            # integer sum implied by GRIM
    half = 0.5 * 10 ** (-sd_precision)
    bounds = []
    for sd_b in (max(0.0, reported_sd - half), reported_sd + half):
        var = sd_b ** 2
        bounds.append(var * (n - 1) + S * S / n)   # SS = var*(n-1) + S^2/n  (ddof=1)
    lo, hi = min(bounds), max(bounds)
    floor_ss = S * S / n                     # variance >= 0 => SS >= S^2/n

    found = False
    for ss in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
        if ss < lo or ss > hi:
            continue
        if ss + 1e-9 < floor_ss:
            continue
        if ss % 2 == S % 2:                  # parity: sum(x^2) == sum(x) (mod 2)
            found = True
            break

    return {
        "testable": True,
        "consistent": found,
        "flagged": not found,
        "reported_mean": reported_mean,
        "reported_sd": reported_sd,
        "n": n,
    }


def recompute_p(stat_type: str, stat_value: float, df1: float,
                df2: float = None, tail: str = "two") -> dict:
    """Recompute a p-value from a reported test statistic and its degrees of freedom
    (statcheck-style). Supports t, F, chi2, and r. Used to detect reported p-values
    that contradict the statistic they were supposedly derived from."""
    st = str(stat_type).lower()
    x = abs(float(stat_value))
    try:
        if st == "t":
            p = scipy_stats.t.sf(x, df1) * (2 if tail == "two" else 1)
        elif st == "f":
            p = scipy_stats.f.sf(x, df1, df2)
        elif st in ("chi2", "chisq", "x2"):
            p = scipy_stats.chi2.sf(x, df1)
        elif st == "r":
            if x >= 1:
                return {"testable": False, "reason": "invalid_r"}
            t = x * np.sqrt(df1 / (1 - x * x))
            p = scipy_stats.t.sf(t, df1) * (2 if tail == "two" else 1)
        else:
            return {"testable": False, "reason": "unknown_stat_type"}
    except (ValueError, ZeroDivisionError, TypeError):
        return {"testable": False, "reason": "compute_error"}
    return {"testable": True, "recomputed_p": float(min(p, 1.0))}


def check_passage_offset_pattern(
    df,
    min_pairs: int = 2,
    n_first_cols: int or None = None,
) -> list[dict]:
    """Detect 'synthetic pseudo-replicate' pattern across rows in a sub-table.

    Pattern: in a passage / timepoint × replicate layout, two rows R_i and R_j
    share *byte-equal* values in the first K columns (replicate 1..K) and differ
    in the last column by either an exact match (0.0) or an integer / half-step
    offset (e.g. +1.5, +2.0, +0.5). Real biological replicates have stochastic
    variability across all replicates; matching N columns exactly + integer
    offset on the (N+1)th is a hallmark of human-edited fabricated data
    (e.g. scitranslmed.aba3571 Fig.S11 Section B Passage 8↔14 / Passage 10↔11).

    Parameters
    ----------
    df : pandas.DataFrame
        Numeric DataFrame (one row per passage/timepoint, one column per replicate).
    min_pairs : int
        Minimum number of matching row-pairs to flag a sub-table. Default 2.
    n_first_cols : int or None
        Number of leading columns to require exact-match. None = use
        df.shape[1] - 1 (i.e. last column is the offset-bearing one).

    Returns
    -------
    list[dict]
        Each result has ``flagged``, ``severity``, ``n_pairs``,
        ``n_exact_matches``, ``n_integer_offsets``, ``n_first_cols``, ``last_col``,
        ``pairs`` (truncated to first 10 row-pairs).
    """
    import pandas as pd  # local import — keeps this module importable without pandas

    results: list[dict] = []
    if not isinstance(df, pd.DataFrame) or df.shape[0] < 3 or df.shape[1] < 2:
        return results
    num_df = df.select_dtypes(include=[np.number])
    if num_df.shape[1] < 2:
        return results
    K = n_first_cols if n_first_cols is not None else num_df.shape[1] - 1
    K = min(K, num_df.shape[1] - 1)
    if K < 1:
        return results

    last_col_idx = num_df.shape[1] - 1
    last_col_name = str(num_df.columns[last_col_idx])
    rows = num_df.to_numpy(dtype=float)
    pair_samples: list[tuple[int, int, float]] = []
    n_exact_matches = 0
    n_integer_offsets = 0

    # Only rows with matching leading values can satisfy the pattern. Grouping
    # candidates first avoids an O(n^2) full-table scan on large measurement
    # sheets while retaining the final allclose check below.
    candidate_groups: dict[tuple[float, ...], list[int]] = {}
    for i, row in enumerate(rows):
        first = row[:K]
        if np.any(~np.isfinite(first)) or not np.isfinite(row[last_col_idx]):
            continue
        key = tuple(np.round(first, decimals=12))
        candidate_groups.setdefault(key, []).append(i)

    # Count qualifying pairs without materializing every pair. A large source
    # table can contain tens of thousands of rows with identical leading
    # fields. The former nested loop then became O(n^2) and appended millions
    # of tuples even though the caller only retained ten examples.
    from bisect import bisect_left
    from collections import defaultdict

    for indices in candidate_groups.values():
        if len(indices) < 2:
            continue

        exact_groups: dict[float, list[int]] = defaultdict(list)
        residual_groups: dict[float, dict[int, list[int]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for i in indices:
            value = float(rows[i, last_col_idx])
            exact_groups[round(value, 12)].append(i)

            # Two values differ by an exact 0.1 step iff they have the same
            # residual after subtracting their nearest tenth. Grouping by that
            # residual lets us count all >=0.5 offsets with a sorted sweep.
            tenth = int(round(value * 10))
            residual = round(value - tenth / 10.0, 9)
            residual_groups[residual][tenth].append(i)

        for same_value_indices in exact_groups.values():
            count = len(same_value_indices)
            if count < 2:
                continue
            n_exact_matches += count * (count - 1) // 2
            if len(pair_samples) < 10:
                pair_samples.append(
                    (same_value_indices[0], same_value_indices[1], 0.0)
                )

        for tenth_groups in residual_groups.values():
            tenths = sorted(tenth_groups)
            if len(tenths) < 2:
                continue
            counts = [len(tenth_groups[tenth]) for tenth in tenths]
            suffix_counts = [0] * (len(counts) + 1)
            for pos in range(len(counts) - 1, -1, -1):
                suffix_counts[pos] = suffix_counts[pos + 1] + counts[pos]

            for pos, tenth in enumerate(tenths):
                other_pos = bisect_left(tenths, tenth + 5, lo=pos + 1)
                if other_pos >= len(tenths):
                    continue
                n_integer_offsets += counts[pos] * suffix_counts[other_pos]

                if len(pair_samples) < 10:
                    for other_tenth in tenths[other_pos:]:
                        i = tenth_groups[tenth][0]
                        j = tenth_groups[other_tenth][0]
                        if i > j:
                            i, j = j, i
                        offset = float(rows[j, last_col_idx] - rows[i, last_col_idx])
                        pair_samples.append((i, j, offset))
                        if len(pair_samples) >= 10:
                            break

    n_pairs = n_exact_matches + n_integer_offsets
    if n_pairs < min_pairs:
        return results

    # "Strong" means >= 2 exact copies OR >= 2 integer-offset copies OR one of each.
    if (
        n_exact_matches >= 2
        or n_integer_offsets >= 2
        or (n_exact_matches and n_integer_offsets)
    ):
        severity = "high"
    else:
        severity = "medium"

    pair_records = [{"row_a": int(p[0]), "row_b": int(p[1]), "offset": float(p[2])}
                    for p in pair_samples]
    results.append({
        "testable": True,
        "flagged": True,
        "severity": severity,
        "n_pairs": int(n_pairs),
        "n_exact_matches": int(n_exact_matches),
        "n_integer_offsets": int(n_integer_offsets),
        "n_first_cols": int(K),
        "last_col": last_col_name,
        "pairs": pair_records,
        "pairs_truncated": n_pairs > len(pair_records),
    })
    return results
