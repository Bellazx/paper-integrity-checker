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


def check_cross_group_duplicates(groups: dict[str, np.ndarray]) -> list[dict]:
    """Check for duplicate data values across experimental groups."""
    flagged = []
    names = list(groups.keys())

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = groups[names[i]][~np.isnan(groups[names[i]])]
            b = groups[names[j]][~np.isnan(groups[names[j]])]
            if len(a) == 0 or len(b) == 0:
                continue

            common = np.intersect1d(a, b)
            overlap = len(common) / max(len(a), len(b))
            if overlap > 0.5:
                flagged.append({
                    "group_a": names[i],
                    "group_b": names[j],
                    "overlap_ratio": float(overlap),
                    "common_values": common.tolist(),
                    "severity": "low",
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

    return {
        "testable": True,
        "flagged": r_squared > r2_threshold,
        "slope": float(slope),
        "intercept": float(intercept),
        "r_squared": float(r_squared),
        "p_value": float(p_value),
        "n": n,
        "is_integer_slope": is_integer_slope,
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
