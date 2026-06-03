import logging
import hashlib
import re
import time
from pathlib import Path

import numpy as np
import pandas as pd

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import (
    CV_THRESHOLD, ARITHMETIC_SEQ_TOLERANCE, BENFORD_MIN_SAMPLES,
    BENFORD_P_THRESHOLD, CROSS_GROUP_OVERLAP_THRESHOLD,
    LINEAR_DEP_R2_THRESHOLD, LINEAR_DEP_MIN_SAMPLES,
    CROSS_SHEET_MIN_MATCHING_ROWS, CROSS_SHEET_COL_MATCH_RATIO,
    VALUE_RECYCLING_MIN_SAMPLES,
)
from utils.stats import (
    check_cv, check_arithmetic_sequence, check_geometric_sequence,
    grim_test, benfords_law_test, check_cross_group_duplicates,
    check_decimal_uniformity, check_linear_dependency,
    check_value_recycling, terminal_digit_test, sd_regularity_test,
)

log = logging.getLogger(__name__)

MAX_LOAD_RETRIES = 3

# Crawler-generated metadata files (resource manifests / figure download logs) that live
# alongside the paper but are NOT experimental data. Analyzing them produces meaningless
# anomalies (e.g. decimal_uniformity on a 'figure_no' column) and masks real source-data
# absence, so they must be skipped during data-anomaly detection.
_METADATA_FILENAMES = {"resources.csv", "figures.csv", "manifest.csv", "resources.tsv"}


def is_metadata_data_file(path: str | Path) -> bool:
    """Return True for crawler metadata files that are not experimental source data."""
    return Path(path).name.lower() in _METADATA_FILENAMES

_IV_KEYWORDS = {
    'day', 'days', 'time', 'hour', 'hours', 'minute', 'minutes', 'min',
    'second', 'seconds', 'sec', 'week', 'weeks', 'month', 'months',
    'year', 'years', 'cycle', 'cycles',
    'concentration', 'conc', 'dose', 'dosage',
    'wavelength', 'wavenumber', 'frequency', 'freq',
    'position', 'pos', 'distance', 'dist', 'depth',
    'start', 'end', 'locus', 'coordinate',
    'temperature', 'temp', 'pressure', 'voltage', 'current',
    'angle', 'theta', 'phi',
    'origin', 'raman', 'shift',
    'x', 'x1', 'x2',
    'number', 'no', 'num', 'id', 'index', 'order',
    'patient', 'patients', 'donor', 'donors',
    'sample', 'samples', 'subject', 'subjects', 'case', 'cases',
    'injection', 'injections', 'run', 'runs',
    'replicate', 'replicates', 'rep', 'reps',
    'experiment', 'experiments',
    'cell', 'cells', 'foci',
    'fraction', 'fractions', 'scan',
    'rank', 'ranking', 'ranked',
}

_STAT_COL_KEYWORDS = {
    'p_val', 'p_value', 'pvalue', 'pval',
    'p_val_adj', 'padj', 'p_adjust', 'p_adjusted', 'padjust',
    'fdr', 'q_value', 'qvalue', 'qval',
    'log2fc', 'log2foldchange', 'logfc',
    'stat', 'statistic', 'zscore', 'z_score',
    'average', 'avg', 'mean',
    'score', 'rank', 'ratio', 'percent', 'percentage',
    'count', 'counts', 'frequency', 'freq',
    'estimate', 'coefficient', 'coef',
    'auc', 'or', 'hr', 'ci',
}

_STAT_COL_PATTERNS = [
    r'\bp[\s_.-]*(?:adj|adjusted|value|val)\b',
    r'\b(?:fdr|q[\s_.-]*(?:value|val)|e[\s_.-]*(?:value|val))\b',
    r'\b(?:log2?[\s_.-]*fc|fold[\s_.-]*change|z[\s_.-]*score)\b',
    r'\b(?:score|rank|ratio|percent(?:age)?|frequency|freq|count|counts)\b',
    r'\b(?:estimate|coefficient|coef|odds|hazard|auc|ci|r2|correlation|corr)\b',
]

_NON_MEASUREMENT_COL_PATTERNS = [
    r'\b(?:latitude|longitude|longtitude|lat|lon)\b',
    r'\b(?:chr|chrom|chromosome|genome|genomic|locus|coordinate|bp|orf|snp)\b',
    r'\b(?:start|end|position|pos|index|idx|number|no|id)\b',
    r'\b(?:taxonomy|taxon|phylum|class|order|family|genus|species)\b',
    r'\b(?:gene|protein|transcript|motif|domain|annotation|pathway|kegg|go)\b',
    r'\b(?:cluster|module|node|edge|tree|phylogeny|isolate|accession)\b',
    r'\b(?:length|size|year|date)\b',
]

_NON_MEASUREMENT_CONTEXT_PATTERNS = [
    r'\b(?:snp|snps|tree|phylogeny|phylogenetic|isolate|isolates)\b',
    r'\b(?:taxonomy|taxon|phylum|class|order|family|genus|species)\b',
    r'\b(?:annotation|pathway|kegg|go|orthogroup|cluster|module)\b',
]


def _is_stat_column(col_name: str) -> bool:
    name = str(col_name).lower().strip()
    name_clean = re.sub(r'[()（）\s\-]', '_', name).strip('_')
    if name_clean in _STAT_COL_KEYWORDS:
        return True
    name_words = re.sub(r'[()（）_\-./]+', ' ', name).strip()
    return any(re.search(pattern, name_words) for pattern in _STAT_COL_PATTERNS)


def _is_unnamed_column(col_name: str) -> bool:
    return bool(re.match(r'^col_\d+$', str(col_name).strip()))


_SD_COL_PATTERN = re.compile(
    r'(^|[\s_\-(（])(sd|s\.d|se|s\.e|sem|std|stdev|stderr|error)([\s_\-)）.]|$)'
    r'|±|标准差|标准误',
    re.IGNORECASE,
)


def _is_sd_column(col_name: str) -> bool:
    """Check if a column name suggests it holds standard deviations / standard errors."""
    return bool(_SD_COL_PATTERN.search(str(col_name)))


def _is_non_measurement_name(col_name: str) -> bool:
    name = str(col_name).lower().strip()
    name_words = re.sub(r'[()（）_\-./]+', ' ', name).strip()
    return any(re.search(pattern, name_words) for pattern in _NON_MEASUREMENT_COL_PATTERNS)


def _is_numeric_column_name(col_name: str) -> bool:
    try:
        float(str(col_name).strip())
    except (TypeError, ValueError):
        return False
    return True


def _is_non_measurement_context(*parts: str) -> bool:
    text = " ".join(str(p).lower() for p in parts)
    text_words = re.sub(r'[()（）_\-./]+', ' ', text).strip()
    return any(re.search(pattern, text_words) for pattern in _NON_MEASUREMENT_CONTEXT_PATTERNS)


def _is_independent_variable(col_name: str) -> bool:
    """Check if a column name suggests it's an independent variable, not measurement data."""
    name = str(col_name).lower().strip()
    name_clean = re.sub(r'[()（）\s]', ' ', name).strip()
    tokens = set(name_clean.split())
    if tokens & _IV_KEYWORDS:
        return True
    for kw in _IV_KEYWORDS:
        if re.search(r'\b' + re.escape(kw) + r'\b', name_clean):
            return True
    # Match camelCase suffixes like "allStUnwRank", "topStUnwRank", "nodeScore"
    name_parts = set(re.sub(r'([A-Z])', r' \1', col_name).lower().split())
    if name_parts & _IV_KEYWORDS:
        return True
    if name.endswith('rank') or name.endswith('score'):
        return True
    return False


def _row_numeric_ratio(row) -> float:
    """Fraction of a row's non-empty cells that parse as numbers."""
    vals = [v for v in row if pd.notna(v) and str(v).strip() != ""]
    if not vals:
        return 0.0
    num = sum(1 for v in vals if pd.notna(pd.to_numeric(v, errors="coerce")))
    return num / len(vals)


def _find_header_row(df: pd.DataFrame, start_row: int, end_row: int) -> int:
    """Locate the REAL header row of a sub-table.

    Sub-tables can have a multi-row header: <label> / blank / group-labels / real-header / data.
    The real header is the last mostly-text row immediately above the first numeric data row.
    Falls back to start_row+1 if no numeric data is found.
    """
    first_data = None
    for r in range(start_row + 1, end_row):
        if _row_numeric_ratio(df.iloc[r]) >= 0.5:   # this row is mostly numbers => data
            first_data = r
            break
    if first_data is None or first_data == start_row + 1:
        return start_row + 1
    # the text row directly above the first data row is the header
    return first_data - 1


def _split_sub_tables(df: pd.DataFrame, filename: str, sheet_name: str) -> dict[str, pd.DataFrame]:
    """Split a sheet with multiple sub-tables (e.g., Fig.3b, Fig.3c) into separate DataFrames."""
    label_rows = []
    for i in range(len(df)):
        first_val = df.iloc[i, 0]
        if isinstance(first_val, str) and first_val.strip():
            non_null = df.iloc[i].dropna()
            if len(non_null) == 1:
                label_rows.append((i, first_val.strip()))

    if not label_rows:
        if len(df) > 0:
            raw_headers = df.iloc[0].tolist()
            new_cols = []
            seen = {}
            for j, h in enumerate(raw_headers):
                name = str(h) if pd.notna(h) else f"col_{j}"
                if name in seen:
                    seen[name] += 1
                    name = f"{name}_{seen[name]}"
                else:
                    seen[name] = 0
                new_cols.append(name)
            df_with_header = pd.DataFrame(df.iloc[1:].values, columns=new_cols)
        else:
            df_with_header = df.copy()
        for col in df_with_header.columns:
            converted = pd.to_numeric(df_with_header[col], errors='coerce')
            if converted.notna().any():
                df_with_header[col] = converted
        return {sheet_name: df_with_header}

    tables = {}
    for idx, (start_row, label) in enumerate(label_rows):
        end_row = label_rows[idx + 1][0] if idx + 1 < len(label_rows) else len(df)
        header_row = _find_header_row(df, start_row, end_row)
        data_start = header_row + 1
        if data_start >= end_row:
            continue

        sub_df = df.iloc[data_start:end_row].copy()
        if header_row < len(df):
            headers = df.iloc[header_row].tolist()
            new_cols = []
            seen = {}
            for j, h in enumerate(headers):
                name = str(h) if pd.notna(h) else f"col_{j}"
                if name in seen:
                    seen[name] += 1
                    name = f"{name}_{seen[name]}"
                else:
                    seen[name] = 0
                new_cols.append(name)
            sub_df.columns = new_cols

        non_null_cols = [c for c in sub_df.columns if sub_df[c].notna().any()]
        sub_df = sub_df[non_null_cols]
        sub_df = sub_df.reset_index(drop=True)
        sub_df = sub_df.dropna(how="all")

        for col in sub_df.columns:
            converted = pd.to_numeric(sub_df[col], errors='coerce')
            if converted.notna().any():
                sub_df[col] = converted

        if len(sub_df) > 0:
            table_name = f"{sheet_name} / {label}"
            tables[table_name] = sub_df
            log.info("  Sub-table '%s': %d rows x %d cols", label, len(sub_df), len(sub_df.columns))

    if not tables:
        return {sheet_name: df}

    return tables


def _load_data_files(data_dir: str) -> tuple[dict[str, dict[str, pd.DataFrame]], list[str]]:
    """Load all Excel/CSV files with retry logic.
    Returns (loaded_data, failed_file_names)."""
    data_dir = Path(data_dir)
    result = {}
    failed_files = []
    seen_fingerprints: set[tuple[int, str]] = set()

    for f in sorted(data_dir.rglob("*")):
        if is_metadata_data_file(f):
            continue  # crawler metadata, not experimental data
        if f.is_file() and f.suffix.lower() in (".xlsx", ".xls", ".csv", ".docx", ".fcs", ".sav"):
            try:
                fp = (f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest())
            except OSError as e:
                log.warning("Failed to fingerprint %s: %s", f.name, e)
                failed_files.append(f.name)
                continue
            if fp in seen_fingerprints:
                log.info("Skipping duplicate source data file by content: %s", f.name)
                continue
            seen_fingerprints.add(fp)
        if f.suffix.lower() in (".xlsx", ".xls"):
            loaded = False
            engine = "xlrd" if f.suffix.lower() == ".xls" else "openpyxl"
            for attempt in range(MAX_LOAD_RETRIES):
                try:
                    raw = pd.read_excel(f, sheet_name=None, engine=engine, header=None)
                    all_tables = {}
                    for sheet_name, df in raw.items():
                        sub_tables = _split_sub_tables(df, f.name, sheet_name)
                        all_tables.update(sub_tables)
                    result[f.name] = all_tables
                    log.info("Loaded %s: %d sub-tables", f.name, len(all_tables))
                    loaded = True
                    break
                except Exception as e:
                    if attempt < MAX_LOAD_RETRIES - 1:
                        log.warning("Failed to load %s (attempt %d/%d): %s", f.name, attempt + 1, MAX_LOAD_RETRIES, e)
                        time.sleep(2 * (attempt + 1))
                    else:
                        log.error("Failed to load %s after %d attempts: %s", f.name, MAX_LOAD_RETRIES, e)
                        failed_files.append(f.name)
        elif f.suffix.lower() == ".csv":
            loaded = False
            for attempt in range(MAX_LOAD_RETRIES):
                try:
                    df = pd.read_csv(f)
                    result[f.name] = {"Sheet1": df}
                    log.info("Loaded %s", f.name)
                    loaded = True
                    break
                except Exception as e:
                    if attempt < MAX_LOAD_RETRIES - 1:
                        log.warning("Failed to load %s (attempt %d/%d): %s", f.name, attempt + 1, MAX_LOAD_RETRIES, e)
                        time.sleep(2 * (attempt + 1))
                    else:
                        log.error("Failed to load %s after %d attempts: %s", f.name, MAX_LOAD_RETRIES, e)
                        failed_files.append(f.name)
        elif f.suffix.lower() == ".docx":
            try:
                from docx import Document
                doc = Document(str(f))
                tables = {}
                for i, table in enumerate(doc.tables):
                    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                    if len(rows) >= 2:
                        df = pd.DataFrame(rows[1:], columns=rows[0])
                        df = df.apply(lambda col: pd.to_numeric(col, errors='coerce'))
                        tables[f"Table_{i}"] = df
                if tables:
                    result[f.name] = tables
                    log.info("Loaded %s: %d tables", f.name, len(tables))
            except Exception as e:
                log.warning("Failed to load %s: %s", f.name, e)
                failed_files.append(f.name)
        elif f.suffix.lower() == ".fcs":
            try:
                import fcsparser
                _, df = fcsparser.parse(str(f), reformat_meta=True)
                result[f.name] = {"Sheet1": df}
                log.info("Loaded %s: %d rows x %d cols", f.name, len(df), len(df.columns))
            except Exception as e:
                log.warning("Failed to load %s: %s", f.name, e)
                failed_files.append(f.name)
        elif f.suffix.lower() == ".sav":
            try:
                import pyreadstat
                df, _ = pyreadstat.read_sav(str(f))
                result[f.name] = {"Sheet1": df}
                log.info("Loaded %s: %d rows x %d cols", f.name, len(df), len(df.columns))
            except Exception as e:
                log.warning("Failed to load %s: %s", f.name, e)
                failed_files.append(f.name)
        elif f.suffix.lower() == ".rar":
            try:
                import rarfile
                import tempfile
                with tempfile.TemporaryDirectory() as tmp_dir:
                    rf = rarfile.RarFile(str(f))
                    rf.extractall(tmp_dir)
                    for extracted in sorted(Path(tmp_dir).rglob("*")):
                        if is_metadata_data_file(extracted):
                            continue  # crawler metadata, not experimental data
                        if extracted.suffix.lower() in (".xlsx", ".xls"):
                            engine = "xlrd" if extracted.suffix.lower() == ".xls" else "openpyxl"
                            try:
                                raw = pd.read_excel(extracted, sheet_name=None, engine=engine, header=None)
                                all_tables = {}
                                for sheet_name, df in raw.items():
                                    sub_tables = _split_sub_tables(df, extracted.name, sheet_name)
                                    all_tables.update(sub_tables)
                                result[f"{f.name}/{extracted.name}"] = all_tables
                                log.info("Loaded %s/%s: %d sub-tables", f.name, extracted.name, len(all_tables))
                            except Exception as e:
                                log.warning("Failed to load %s/%s: %s", f.name, extracted.name, e)
                        elif extracted.suffix.lower() == ".csv":
                            try:
                                df = pd.read_csv(extracted)
                                result[f"{f.name}/{extracted.name}"] = {"Sheet1": df}
                                log.info("Loaded %s/%s", f.name, extracted.name)
                            except Exception as e:
                                log.warning("Failed to load %s/%s: %s", f.name, extracted.name, e)
            except Exception as e:
                log.warning("Failed to extract RAR %s: %s", f.name, e)
                failed_files.append(f.name)

    return result, failed_files


def _looks_like_row_index(values: np.ndarray) -> bool:
    """Detect columns that are simple integer row indices (1,2,3,... or 0,1,2,...)."""
    if values.ndim != 1 or len(values) < 3:
        return False
    try:
        if not np.all(values == values.astype(int)):
            return False
        ints = values.astype(int)
        first = int(ints[0])
        if first in (0, 1) and np.array_equal(ints, np.arange(first, first + len(ints))):
            return True
    except (ValueError, TypeError):
        return False
    return False


def _is_arithmetic_axis(values: np.ndarray) -> bool:
    """Column-name-independent independent-variable test: a strictly monotonic, near-perfect
    arithmetic sequence (constant step) is an axis / swept parameter (time, displacement,
    concentration, wavelength, coordinate...), NOT measurement data — even when the column name
    was lost to a parsing artifact (col_N). Such columns must not be flagged as fraud.

    Requires >=5 points, a non-zero constant step, and tiny deviation from perfect spacing.
    """
    try:
        v = np.asarray(values, dtype=float)
    except (ValueError, TypeError):
        return False
    v = v[np.isfinite(v)]
    if len(v) < 4:
        return False
    diffs = np.diff(v)
    if not np.all(diffs > 0) and not np.all(diffs < 0):
        return False                      # must be strictly monotonic
    step = np.median(diffs)
    if step == 0:
        return False
    span = abs(v[-1] - v[0])
    if span == 0:
        return False
    # max deviation from a perfect arithmetic progression, relative to total span
    arith_ok = np.max(np.abs(diffs - step)) / (abs(step)) <= 0.02 if step != 0 else False
    geo_ok = False
    if np.all(v > 0):
        ld = np.diff(np.log(v))
        lstep = np.median(ld)
        geo_ok = np.max(np.abs(ld - lstep)) / (abs(lstep)) <= 0.02 if lstep != 0 else False
    if not (arith_ok or geo_ok):
        return False
    return True


def _looks_like_categorical_code(values: np.ndarray) -> bool:
    """Integer-coded categories/count flags should not drive fabrication statistics."""
    if values.ndim != 1 or len(values) < 10:
        return False
    try:
        v = np.asarray(values, dtype=float)
    except (ValueError, TypeError):
        return False
    v = v[np.isfinite(v)]
    if len(v) < 10:
        return False
    if not np.all(np.isclose(v, np.round(v))):
        return False
    unique_count = len(np.unique(v))
    if unique_count <= min(20, max(3, int(len(v) * 0.05))):
        return True
    return False


def _is_measurement_column(col_name: str, values: np.ndarray | None = None, *, allow_unnamed: bool = False) -> bool:
    """Return True only for columns suitable for statistical fabrication checks."""
    name = str(col_name)
    if (_is_unnamed_column(name) or _is_numeric_column_name(name)) and not allow_unnamed:
        return False
    if (
        _is_independent_variable(name)
        or _is_stat_column(name)
        or _is_whitelist_column(name)
        or _is_non_measurement_name(name)
    ):
        return False
    if values is not None:
        arr = np.asarray(values)
        if (
            _looks_like_row_index(arr)
            or _is_arithmetic_axis(arr)
            or _looks_like_categorical_code(arr)
        ):
            return False
    return True


def _analyze_column_group(values: np.ndarray, location: str, col_name: str = "") -> list[dict]:
    """Run all statistical tests on a single data group."""
    anomalies = []
    is_iv = not _is_measurement_column(col_name, values)

    if not is_iv:
        unique_vals = set(values)
        if len(unique_vals) <= 5 and all(float(v) == int(float(v)) for v in values if np.isfinite(v)):
            is_iv = True

    if not is_iv:
        cv_result = check_cv(values, CV_THRESHOLD)
        if cv_result.get("testable") and cv_result.get("flagged"):
            if not (cv_result.get("mean", 1) == 0 and cv_result.get("std", 1) == 0):
                anomalies.append({
                    "test": "coefficient_of_variation",
                    "location": location,
                    "severity": cv_result["severity"],
                    "details": cv_result,
                    "description": f"CV={cv_result['cv']:.6f} ({cv_result['cv']*100:.4f}%) — suspiciously low variation "
                                   f"(mean={cv_result['mean']:.4f}, std={cv_result['std']:.6f}, n={cv_result['n']})",
                })

    if not is_iv:
        arith_result = check_arithmetic_sequence(values, ARITHMETIC_SEQ_TOLERANCE)
        if arith_result.get("is_arithmetic") and len(values) >= 4:
            dev = arith_result.get("max_relative_deviation", 0)
            is_constant = arith_result.get("type") == "constant"
            is_constant_zero = is_constant and abs(arith_result.get("common_diff", 0)) < 1e-15 and abs(values[0]) < 1e-15
            is_preset = dev == 0 and not is_constant
            if not is_preset and not is_constant_zero:
                arith_sev = "high" if dev < 0.001 else "medium"
                if is_constant:
                    arith_sev = "medium"
                anomalies.append({
                    "test": "arithmetic_sequence",
                    "location": location,
                    "severity": arith_sev,
                    "details": arith_result,
                    "description": f"Data forms a near-perfect arithmetic sequence "
                                   f"(common difference={arith_result['common_diff']:.6f}, "
                                   f"max deviation={dev:.6f})",
                })

        geo_result = check_geometric_sequence(values, ARITHMETIC_SEQ_TOLERANCE)
        if geo_result.get("is_geometric") and len(values) >= 4:
            dev = geo_result.get("max_relative_deviation", 0)
            is_constant_geo = abs(geo_result.get("common_ratio", 1) - 1.0) < 1e-10
            is_preset_geo = dev == 0 and not is_constant_geo
            if not is_preset_geo:
                geo_sev = "high" if dev < 0.001 else "medium"
                anomalies.append({
                    "test": "geometric_sequence",
                    "location": location,
                    "severity": geo_sev,
                    "details": geo_result,
                    "description": f"Data forms a near-perfect geometric sequence "
                                   f"(common ratio={geo_result['common_ratio']:.6f})",
                })

    dec_result = check_decimal_uniformity(values)
    if dec_result.get("testable") and dec_result.get("flagged"):
        anomalies.append({
            "test": "decimal_uniformity",
            "location": location,
            "severity": "low",
            "details": dec_result,
            "description": "All values have identical decimal precision — possible fabrication indicator",
        })

    if not is_iv and len(values) >= VALUE_RECYCLING_MIN_SAMPLES:
        recycle_result = check_value_recycling(values, min_samples=VALUE_RECYCLING_MIN_SAMPLES)
        if recycle_result.get("flagged"):
            severity = recycle_result["severity"]
            # Dense curve/grid source-data often repeats rounded values many times.
            # Treat this as review context, not as stand-alone high-risk evidence.
            if severity == "high" and len(values) >= 500 and recycle_result.get("unique_count", 0) >= 20:
                severity = "medium"
            anomalies.append({
                "test": "value_recycling",
                "location": location,
                "severity": severity,
                "details": recycle_result,
                "description": f"Only {recycle_result['unique_count']} unique values fill "
                               f"{recycle_result['total_count']} data points "
                               f"(ratio={recycle_result['ratio']:.2f})",
            })

    if not is_iv:
        td_result = terminal_digit_test(values)
        if td_result.get("testable") and td_result.get("flagged"):
            anomalies.append({
                "test": "terminal_digit",
                "location": location,
                "severity": td_result["severity"],
                "details": td_result,
                "description": f"Last-digit distribution deviates from uniform "
                               f"(chi2={td_result['chi2']:.2f}, p={td_result['p_value']:.4f}, "
                               f"n={td_result['n']})",
            })

    if _is_sd_column(col_name):
        sd_result = sd_regularity_test(values)
        if sd_result.get("testable") and sd_result.get("flagged"):
            anomalies.append({
                "test": "sd_regularity",
                "location": location,
                "severity": sd_result["severity"],
                "details": sd_result,
                "description": f"Dispersion column shows regular pattern "
                               f"({sd_result['pattern']}, n={sd_result['n']})",
            })

    return anomalies


def _analyze_sheet(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Analyze a single sheet/dataframe for anomalies."""
    MAX_COLS_FOR_PAIRWISE = 200
    anomalies = []

    if _is_non_measurement_context(file_name, sheet_name):
        return anomalies

    if df.columns.duplicated().any():
        df = df.copy()
        new_cols = []
        seen = {}
        for c in df.columns:
            c_str = str(c)
            if c_str in seen:
                seen[c_str] += 1
                new_cols.append(f"{c_str}_{seen[c_str]}")
            else:
                seen[c_str] = 0
                new_cols.append(c_str)
        df.columns = new_cols

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    measurement_cols = [
        c for c in numeric_cols
        if _is_measurement_column(str(c), df[c].dropna().values)
    ]

    if not measurement_cols:
        return anomalies

    for col in measurement_cols:
        values = df[col].dropna().values
        if len(values) < 3:
            continue
        location = f"{file_name} / {sheet_name} / column '{col}'"
        anomalies.extend(_analyze_column_group(values, location, col_name=str(col)))

    benford_values = [df[c].dropna().values for c in measurement_cols if len(df[c].dropna().values) > 0]
    if benford_values:
        benford_all = np.concatenate(benford_values)
        benford_result = benfords_law_test(benford_all, BENFORD_MIN_SAMPLES)
        if benford_result.get("testable") and benford_result.get("flagged"):
            anomalies.append({
                "test": "benfords_law",
                "location": f"{file_name} / {sheet_name} (measurement numeric data)",
                "severity": "medium",
                "details": benford_result,
                "description": f"First-digit distribution deviates from Benford's law "
                               f"(chi2={benford_result['chi2']:.2f}, p={benford_result['p_value']:.4f}, "
                               f"n={benford_result['n']})",
            })

    if len(measurement_cols) >= 2 and len(measurement_cols) <= MAX_COLS_FOR_PAIRWISE:
        groups = {}
        for col in measurement_cols:
            vals = df[col].dropna().values
            if len(vals) < 3:
                continue
            groups[str(col)] = vals

        if len(groups) >= 2:
            dup_results = check_cross_group_duplicates(groups)
            for dup in dup_results:
                anomalies.append({
                    "test": "cross_group_duplicate",
                    "location": f"{file_name} / {sheet_name}",
                    "severity": dup["severity"],
                    "details": dup,
                    "description": f"Columns '{dup['group_a']}' and '{dup['group_b']}' share "
                                   f"{dup['overlap_ratio']*100:.0f}% of values",
                })

    y_cols = measurement_cols
    if len(y_cols) > MAX_COLS_FOR_PAIRWISE:
        log.info("Skipping linear dependency check: %d y-columns exceeds limit %d (%s / %s)",
                 len(y_cols), MAX_COLS_FOR_PAIRWISE, file_name, sheet_name)
    if len(y_cols) >= 2 and len(y_cols) <= MAX_COLS_FOR_PAIRWISE:
        for i in range(len(y_cols)):
            for j in range(i + 1, len(y_cols)):
                col_a, col_b = y_cols[i], y_cols[j]
                vals_a = df[col_a].dropna().values
                vals_b = df[col_b].dropna().values
                n = min(len(vals_a), len(vals_b))
                if n < LINEAR_DEP_MIN_SAMPLES:
                    continue
                result = check_linear_dependency(
                    vals_a[:n], vals_b[:n],
                    r2_threshold=LINEAR_DEP_R2_THRESHOLD,
                    min_samples=LINEAR_DEP_MIN_SAMPLES,
                )
                if result.get("flagged"):
                    if result.get("is_offset_pattern"):
                        severity = "high"
                        description = (f"Fixed offset pattern: {col_b} ≈ {col_a} + "
                                       f"{result['intercept']:.0f} "
                                       f"(R²={result['r_squared']:.15f}, n={result['n']})")
                    elif result["r_squared"] > 0.99999:
                        severity = "medium"
                        description = (f"Columns '{col_a}' and '{col_b}' are nearly perfectly "
                                       f"linearly related "
                                       f"({col_b}={result['slope']:.6f}*{col_a} + "
                                       f"{result['intercept']:.4f}, "
                                       f"R²={result['r_squared']:.15f}, n={result['n']})")
                    else:
                        severity = "low"
                        description = (f"Columns '{col_a}' and '{col_b}' are nearly perfectly "
                                       f"linearly related "
                                       f"({col_b}={result['slope']:.6f}*{col_a} + "
                                       f"{result['intercept']:.4f}, "
                                       f"R²={result['r_squared']:.15f}, n={result['n']})")
                    anomalies.append({
                        "test": "linear_dependency",
                        "location": f"{file_name} / {sheet_name} / columns '{col_a}' vs '{col_b}'",
                        "severity": severity,
                        "details": result,
                        "description": description,
                    })

    return anomalies


_WHITELIST_COL_KEYWORDS = {
    'control', 'ctrl', 'standard', 'std', 'calibr', 'blank',
    'background', 'bg', 'baseline', 'reference',
}


def _is_whitelist_column(col_name: str) -> bool:
    name = str(col_name).lower().strip()
    return any(kw in name for kw in _WHITELIST_COL_KEYWORDS)


def _get_measurement_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Extract numeric measurement columns, excluding IVs, stats, and whitelist columns."""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    keep = [c for c in numeric_cols
            if _is_measurement_column(str(c), df[c].dropna().values)]
    if not keep:
        return pd.DataFrame()
    return df[keep]


def _row_hash(row: np.ndarray) -> str:
    """Hash a numeric row rounded to 8 decimal places."""
    rounded = tuple(round(float(v), 8) if np.isfinite(v) else None for v in row)
    return str(rounded)


def _find_matching_rows(df_a: pd.DataFrame, df_b: pd.DataFrame) -> list[dict]:
    """Find rows in df_b that exactly match rows in df_a (by numeric values)."""
    if df_a.empty or df_b.empty:
        return []

    matches = []
    hashes_a = {}
    for idx_a in range(len(df_a)):
        row_a = df_a.iloc[idx_a].values
        if np.all(np.isnan(row_a)):
            continue
        h = _row_hash(row_a)
        if h not in hashes_a:
            hashes_a[h] = []
        hashes_a[h].append(idx_a)

    for idx_b in range(len(df_b)):
        row_b = df_b.iloc[idx_b].values
        if np.all(np.isnan(row_b)):
            continue
        h = _row_hash(row_b)
        if h in hashes_a:
            matches.append({"row_a": hashes_a[h][0], "row_b": idx_b, "hash": h})

    return matches


def _find_matching_columns(df_a: pd.DataFrame, df_b: pd.DataFrame) -> list[dict]:
    """Find columns across two sheets with >=90% identical values."""
    matches = []
    for col_a in df_a.columns:
        vals_a = df_a[col_a].dropna().values
        if vals_a.ndim != 1 or len(vals_a) < 3:
            continue
        if not _is_measurement_column(str(col_a), vals_a):
            continue                      # shared X-axis/time across sheets is normal
        for col_b in df_b.columns:
            vals_b = df_b[col_b].dropna().values
            if vals_b.ndim != 1 or len(vals_b) < 3:
                continue
            if not _is_measurement_column(str(col_b), vals_b):
                continue
            n = min(len(vals_a), len(vals_b))
            if n < 3:
                continue
            a_cmp = np.round(vals_a[:n], 8)
            b_cmp = np.round(vals_b[:n], 8)
            match_count = np.sum(a_cmp == b_cmp)
            ratio = match_count / n
            if ratio >= CROSS_SHEET_COL_MATCH_RATIO:
                all_same_val = len(np.unique(a_cmp)) <= 1
                if all_same_val:
                    continue
                has_high_precision = sum(
                    1 for v in vals_a[:n]
                    if '.' in str(v) and len(str(v).split('.')[-1].rstrip('0')) >= 5
                ) >= 2
                matches.append({
                    "col_a": str(col_a),
                    "col_b": str(col_b),
                    "match_ratio": float(ratio),
                    "matched_count": int(match_count),
                    "total_compared": int(n),
                    "has_high_precision": has_high_precision,
                })
    return matches


def _analyze_cross_sheet(sheets: dict[str, pd.DataFrame], file_name: str) -> list[dict]:
    """Compare data blocks across sheets within the same file."""
    MAX_SHEET_PAIRS = 200
    MAX_ROWS_FOR_CROSS = 5000
    MAX_COLS_FOR_CROSS = 50

    anomalies = []
    sheet_names = list(sheets.keys())
    pair_count = 0

    for i in range(len(sheet_names)):
        for j in range(i + 1, len(sheet_names)):
            if pair_count >= MAX_SHEET_PAIRS:
                break
            name_a, name_b = sheet_names[i], sheet_names[j]
            df_a, df_b = sheets[name_a], sheets[name_b]

            if _is_non_measurement_context(file_name, name_a, name_b):
                continue

            if len(df_a) > MAX_ROWS_FOR_CROSS or len(df_b) > MAX_ROWS_FOR_CROSS:
                continue

            num_a = _get_measurement_columns(df_a)
            num_b = _get_measurement_columns(df_b)

            if num_a.empty or num_b.empty:
                continue

            if len(num_a.columns) > MAX_COLS_FOR_CROSS:
                num_a = num_a.iloc[:, :MAX_COLS_FOR_CROSS]
            if len(num_b.columns) > MAX_COLS_FOR_CROSS:
                num_b = num_b.iloc[:, :MAX_COLS_FOR_CROSS]

            pair_count += 1

            row_matches = _find_matching_rows(num_a, num_b)
            if len(row_matches) >= CROSS_SHEET_MIN_MATCHING_ROWS:
                severity = "high" if len(row_matches) >= 5 else "medium"
                anomalies.append({
                    "test": "cross_sheet_row_duplicate",
                    "location": f"{file_name} / '{name_a}' vs '{name_b}'",
                    "severity": severity,
                    "details": {
                        "sheet_a": name_a,
                        "sheet_b": name_b,
                        "matching_rows": len(row_matches),
                        "total_rows_a": len(num_a),
                        "total_rows_b": len(num_b),
                    },
                    "description": f"{len(row_matches)} identical data rows found across "
                                   f"sheets '{name_a}' and '{name_b}'",
                })

            col_matches = _find_matching_columns(num_a, num_b)
            for match in col_matches:
                severity = "high" if match["has_high_precision"] else "medium"
                anomalies.append({
                    "test": "cross_sheet_column_duplicate",
                    "location": f"{file_name} / '{name_a}':'{match['col_a']}' vs "
                                f"'{name_b}':'{match['col_b']}'",
                    "severity": severity,
                    "details": {
                        "sheet_a": name_a,
                        "sheet_b": name_b,
                        **match,
                    },
                    "description": f"Column '{match['col_a']}' in '{name_a}' matches "
                                   f"'{match['col_b']}' in '{name_b}' "
                                   f"({match['match_ratio']*100:.0f}% identical, "
                                   f"n={match['total_compared']})",
                })

    return anomalies


def check_data_anomalies(data_dir: str) -> list[dict]:
    """Run all data anomaly checks on source data files."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        log.info("No source data directory found: %s", data_dir)
        return []

    all_files, _failed = _load_data_files(str(data_dir))
    if not all_files:
        log.info("No data files found in %s", data_dir)
        return []

    all_anomalies = []
    for fname, sheets in all_files.items():
        for sheet_name, df in sheets.items():
            sheet_anomalies = _analyze_sheet(df, fname, sheet_name)
            all_anomalies.extend(sheet_anomalies)

    for fname, sheets in all_files.items():
        if len(sheets) >= 2:
            cross_anomalies = _analyze_cross_sheet(sheets, fname)
            all_anomalies.extend(cross_anomalies)

    high = sum(1 for a in all_anomalies if a["severity"] == "high")
    medium = sum(1 for a in all_anomalies if a["severity"] == "medium")
    low = sum(1 for a in all_anomalies if a["severity"] == "low")
    log.info("Data anomalies found: %d total (high=%d, medium=%d, low=%d)", len(all_anomalies), high, medium, low)

    return all_anomalies


def check_data_with_validation(data_dir: str) -> tuple[list[dict], list[str]]:
    """Run data anomaly checks and report files that failed to load.
    Returns (anomalies, failed_file_names)."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        return [], []

    all_files, failed_files = _load_data_files(str(data_dir))
    if not all_files and not failed_files:
        return [], []

    all_anomalies = []
    for fname, sheets in all_files.items():
        for sheet_name, df in sheets.items():
            all_anomalies.extend(_analyze_sheet(df, fname, sheet_name))

    for fname, sheets in all_files.items():
        if len(sheets) >= 2:
            all_anomalies.extend(_analyze_cross_sheet(sheets, fname))

    if all_anomalies:
        high = sum(1 for a in all_anomalies if a["severity"] == "high")
        medium = sum(1 for a in all_anomalies if a["severity"] == "medium")
        low = sum(1 for a in all_anomalies if a["severity"] == "low")
        log.info("Data anomalies found: %d total (high=%d, medium=%d, low=%d)", len(all_anomalies), high, medium, low)

    return all_anomalies, failed_files
