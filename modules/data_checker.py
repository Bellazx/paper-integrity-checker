import logging
import hashlib
import io
import math
import re
import time
import zipfile
from collections import defaultdict
from pathlib import Path

try:
    import gemmi
except ImportError:  # Keep non-CIF screening available until the optional dependency is installed.
    gemmi = None

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
    check_trailing_digit_uniformity, check_passage_offset_pattern,
)
from modules.source_data_patterns import detect_source_data_patterns
from utils.delimited_source import TEXT_TABLE_SUFFIXES, read_screen_table

log = logging.getLogger(__name__)

MAX_LOAD_RETRIES = 3
TABLE_DATA_EXTENSIONS = {".xlsx", ".xls", ".csv", ".docx", ".fcs", ".sav"} | TEXT_TABLE_SUFFIXES
ARCHIVE_DATA_EXTENSIONS = {".zip", ".rar"}
STRUCTURE_DATA_EXTENSIONS = {".pdb", ".cif"}
DATA_FILE_EXTENSIONS = TABLE_DATA_EXTENSIONS | ARCHIVE_DATA_EXTENSIONS | STRUCTURE_DATA_EXTENSIONS
MAX_SUBTABLES_PER_WORKBOOK = 40
MAX_SUBTABLES_FOR_FULL_SCAN = 500
MAX_ARCHIVE_DATA_FILES = 100
MAX_ARCHIVE_MEMBER_BYTES = 200 * 1024 * 1024
_SHEET_SCAN_SKIP_WORKBOOKS: set[str] = set()
_CROSS_SHEET_SKIP_WORKBOOKS: set[str] = set()

# Crawler-generated metadata files (resource manifests / figure download logs) that live
# alongside the paper but are NOT experimental data. Analyzing them produces meaningless
# anomalies (e.g. decimal_uniformity on a 'figure_no' column) and masks real source-data
# absence, so they must be skipped during data-anomaly detection.
_METADATA_FILENAMES = {"resources.csv", "figures.csv", "manifest.csv", "resources.tsv", "doi.txt"}


def _cap_workbook_subtables(all_tables: dict[str, pd.DataFrame], workbook_name: str) -> dict[str, pd.DataFrame]:
    """Bound per-workbook sub-table scans for pathological split outputs.

    Some source-data workbooks contain thousands of tiny gene/feature blocks. Running
    every per-sheet statistical check over all of them can dominate screening for
    hours while adding little practical coverage beyond a broad sample. Keep a stable
    prefix sample so screening finishes and the PDF still records available evidence.
    """
    if len(all_tables) <= MAX_SUBTABLES_FOR_FULL_SCAN:
        return all_tables
    _SHEET_SCAN_SKIP_WORKBOOKS.add(workbook_name)
    _CROSS_SHEET_SKIP_WORKBOOKS.add(workbook_name)
    capped_items = list(all_tables.items())[:MAX_SUBTABLES_PER_WORKBOOK]
    skipped = len(all_tables) - len(capped_items)
    log.warning(
        "Workbook %s has %d sub-tables; scanning first %d and skipping %d to avoid pathological runtime",
        workbook_name,
        len(all_tables),
        len(capped_items),
        skipped,
    )
    return dict(capped_items)


def is_metadata_data_file(path: str or Path) -> bool:
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


def _looks_like_feature_matrix_raw(df: pd.DataFrame) -> bool:
    """True for gene/transcript x stage score matrices, not stacked sub-tables."""
    if df.shape[0] < 200 or df.shape[1] < 4:
        return False
    header = df.iloc[0]
    first_header = header.iloc[0] if len(header) else None
    if pd.notna(first_header) and str(first_header).strip():
        return False
    named_value_cols = sum(
        1 for v in header.iloc[1:]
        if isinstance(v, str) and v.strip()
    )
    if named_value_cols < 3:
        return False

    sample = df.iloc[1:min(len(df), 501)]
    if sample.empty:
        return False
    id_col = sample.iloc[:, 0]
    id_like = sum(
        1 for v in id_col
        if isinstance(v, str) and v.strip() and not pd.notna(pd.to_numeric(v, errors="coerce"))
    )
    id_ratio = id_like / max(len(id_col.dropna()), 1)
    value_block = sample.iloc[:, 1:]
    flat_values = pd.Series(value_block.to_numpy().ravel()).dropna()
    flat_values = flat_values[flat_values.astype(str).str.strip() != ""]
    if flat_values.empty:
        return False
    numeric_ratio = pd.to_numeric(flat_values, errors="coerce").notna().mean()
    return id_ratio >= 0.8 and numeric_ratio >= 0.8


def _feature_matrix_from_raw(df: pd.DataFrame) -> pd.DataFrame:
    headers = df.iloc[0].tolist()
    columns = ["feature_id"]
    seen = {"feature_id": 0}
    for j, h in enumerate(headers[1:], start=1):
        name = str(h).strip() if pd.notna(h) and str(h).strip() else f"value_{j}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        columns.append(name)

    out = pd.DataFrame(df.iloc[1:].values, columns=columns)
    out = out.dropna(how="all")
    out = out[out["feature_id"].notna()]
    for col in out.columns[1:]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.reset_index(drop=True)


def _looks_like_feature_matrix(df: pd.DataFrame) -> bool:
    if df.shape[0] < 200 or df.shape[1] < 4:
        return False
    first_col = str(df.columns[0]).lower()
    if first_col not in {"feature_id", "gene", "gene_id", "transcript", "transcript_id"}:
        id_series = df.iloc[:min(len(df), 500), 0]
        id_like = sum(1 for v in id_series if isinstance(v, str) and v.strip())
        if id_like / max(len(id_series.dropna()), 1) < 0.8:
            return False
    numeric_cols = df.iloc[:, 1:].select_dtypes(include=[np.number]).columns
    return len(numeric_cols) >= 3


def _looks_like_sequence_alignment_table(df: pd.DataFrame) -> bool:
    """True for computational Query/Target alignment output tables."""
    if df.shape[1] < 6:
        return False
    cols = {str(c).strip().lower().replace(" ", "_") for c in df.columns}
    has_query_target = {"query", "target"} <= cols
    has_lengths = bool({"length_query", "length_target"} & cols)
    has_scores = bool({"dg", "_dg", "ndg", "_ndg"} & cols)
    has_positions = any("start_position" in c or "end_position" in c for c in cols)
    return has_query_target and has_lengths and has_scores and has_positions


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
    if _looks_like_feature_matrix_raw(df):
        return {sheet_name: _feature_matrix_from_raw(df)}

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


def _raw_workbook_pattern_anomalies(
    raw_sheets: dict[str, pd.DataFrame], file_name: str
) -> list[dict]:
    anomalies = []
    for sheet_name, df in raw_sheets.items():
        grid = df.astype(object).where(df.notna(), None).values.tolist()
        patterns = detect_source_data_patterns(file_name, str(sheet_name), grid)
        anomalies.extend(patterns["same_sheet_exact_series_reuse"])
        anomalies.extend(patterns["same_sheet_channel_duplicate"])
        anomalies.extend(patterns["fixed_ratio_series"])
        anomalies.extend(patterns["semantic_transform_conflicts"])
    return anomalies


def _read_delimited_text_xls(path: Path) -> dict[str, pd.DataFrame] | None:
    """Read a text table that was delivered with an ``.xls`` suffix.

    Source-data exports occasionally use the legacy Excel extension for TSV or
    CSV content. Keep binary/corrupt workbooks on the normal failure path and
    only accept clearly delimited, multi-line text with at least two columns.
    """
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    if not payload or b"\x00" in payload[:65536]:
        return None

    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    delimiter = max(("\t", ",", ";"), key=lambda value: lines[0].count(value))
    if lines[0].count(delimiter) < 1:
        return None

    try:
        frame = pd.read_csv(io.StringIO(text), sep=delimiter, header=None)
    except Exception:
        return None
    if frame.shape[0] < 2 or frame.shape[1] < 2:
        return None
    return {"Sheet1": frame}


def _load_data_files(
    data_dir: str or Path or list,
) -> tuple[dict[str, dict[str, pd.DataFrame]], list[str], list[dict]]:
    """Load all Excel/CSV files with retry logic.
    Returns (loaded_data, failed_file_names, raw_pattern_anomalies)."""
    data_dirs = [Path(data_dir)] if not isinstance(data_dir, list) else [Path(d) for d in data_dir]
    result = {}
    failed_files = []
    pattern_anomalies = []
    seen_fingerprints: set[tuple[int, str]] = set()

    def display_name(path: Path) -> str:
        if len(data_dirs) == 1:
            return path.name
        rel_candidates = []
        for root in data_dirs:
            try:
                rel_candidates.append(path.relative_to(root.parent))
            except ValueError:
                continue
        if rel_candidates:
            return min(rel_candidates, key=lambda p: len(p.parts)).as_posix()
        return path.name

    candidates = []
    for root in data_dirs:
        if root.exists():
            candidates.extend(root.rglob("*"))

    for f in sorted(candidates):
        if is_metadata_data_file(f):
            continue  # crawler metadata, not experimental data
        if f.is_file() and f.suffix.lower() in TABLE_DATA_EXTENSIONS:
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
        name = display_name(f)
        if f.suffix.lower() in (".xlsx", ".xls"):
            loaded = False
            engine = "xlrd" if f.suffix.lower() == ".xls" else "openpyxl"
            text_workbook = _read_delimited_text_xls(f) if f.suffix.lower() == ".xls" else None
            if text_workbook is not None:
                pattern_anomalies.extend(_raw_workbook_pattern_anomalies(text_workbook, name))
                all_tables = {}
                for sheet_name, df in text_workbook.items():
                    all_tables.update(_split_sub_tables(df, name, sheet_name))
                all_tables = _cap_workbook_subtables(all_tables, name)
                result[name] = all_tables
                loaded = True
                log.info("Loaded text-delimited XLS %s: %d sub-tables", name, len(all_tables))
            for attempt in range(MAX_LOAD_RETRIES):
                if loaded:
                    break
                try:
                    raw = pd.read_excel(f, sheet_name=None, engine=engine, header=None)
                    pattern_anomalies.extend(_raw_workbook_pattern_anomalies(raw, name))
                    all_tables = {}
                    for sheet_name, df in raw.items():
                        sub_tables = _split_sub_tables(df, name, sheet_name)
                        all_tables.update(sub_tables)
                    all_tables = _cap_workbook_subtables(all_tables, name)
                    result[name] = all_tables
                    log.info("Loaded %s: %d sub-tables", name, len(all_tables))
                    loaded = True
                    break
                except Exception as e:
                    if attempt < MAX_LOAD_RETRIES - 1:
                        log.warning("Failed to load %s (attempt %d/%d): %s", name, attempt + 1, MAX_LOAD_RETRIES, e)
                        time.sleep(2 * (attempt + 1))
                    else:
                        log.error("Failed to load %s after %d attempts: %s", name, MAX_LOAD_RETRIES, e)
                        failed_files.append(name)
        elif f.is_file() and f.suffix.lower() in TEXT_TABLE_SUFFIXES:
            try:
                frame, coverage_warnings = read_screen_table(f)
                result[name] = {"Sheet1": frame}
                failed_files.extend(coverage_warnings)
            except (ValueError, OSError, UnicodeError) as exc:
                failed_files.append(f"{name}: {exc}")
                log.warning("Text source table not checked: %s: %s", name, exc)
        elif f.suffix.lower() == ".csv":
            loaded = False
            for attempt in range(MAX_LOAD_RETRIES):
                try:
                    df = pd.read_csv(f)
                    result[name] = {"Sheet1": df}
                    log.info("Loaded %s", name)
                    loaded = True
                    break
                except Exception as e:
                    if attempt < MAX_LOAD_RETRIES - 1:
                        log.warning("Failed to load %s (attempt %d/%d): %s", name, attempt + 1, MAX_LOAD_RETRIES, e)
                        time.sleep(2 * (attempt + 1))
                    else:
                        log.error("Failed to load %s after %d attempts: %s", name, MAX_LOAD_RETRIES, e)
                        failed_files.append(name)
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
                    result[name] = tables
                    log.info("Loaded %s: %d tables", name, len(tables))
            except Exception as e:
                log.warning("Failed to load %s: %s", name, e)
                failed_files.append(name)
        elif f.suffix.lower() == ".fcs":
            # 2026-06-25: 跳过大型 .fcs（flow cytometry 原始事件，s44318-024 一个 fcs 就 24MB、
            # 数百万 events，fcsparser.parse 几十分钟不出结果）。统计 derived xlsx 已覆盖。
            try:
                fcs_size_mb = f.stat().st_size / 1024 / 1024
            except OSError:
                fcs_size_mb = 0
            if fcs_size_mb > 5:
                log.info("Skipping large .fcs file (%.1fMB > 5MB cap): %s", fcs_size_mb, name)
                continue
            try:
                import fcsparser
                _, df = fcsparser.parse(str(f), reformat_meta=True)
                # Row cap — _analyze_sheet 对百万行 DataFrame 会卡死
                if len(df) > 100000:
                    df = df.sample(n=100000, random_state=42)
                result[name] = {"Sheet1": df}
                log.info("Loaded %s: %d rows x %d cols", name, len(df), len(df.columns))
            except Exception as e:
                log.warning("Failed to load %s: %s", name, e)
                failed_files.append(name)
        elif f.suffix.lower() == ".sav":
            try:
                import pyreadstat
                df, _ = pyreadstat.read_sav(str(f))
                if len(df) > 100000:
                    df = df.sample(n=100000, random_state=42)
                result[name] = {"Sheet1": df}
                log.info("Loaded %s: %d rows x %d cols", name, len(df), len(df.columns))
            except Exception as e:
                log.warning("Failed to load %s: %s", name, e)
                failed_files.append(name)
        elif f.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(f) as zf:
                    members = [
                        info for info in zf.infolist()
                        if not info.is_dir()
                        and Path(info.filename).suffix.lower() in ({".xlsx", ".xls", ".csv"} | TEXT_TABLE_SUFFIXES)
                        and not Path(info.filename).name.startswith("~$")
                        and info.file_size <= MAX_ARCHIVE_MEMBER_BYTES
                    ][:MAX_ARCHIVE_DATA_FILES]
                    for info in members:
                        member_name = Path(info.filename).name
                        member_suffix = Path(info.filename).suffix.lower()
                        payload = zf.read(info)
                        archive_name = f"{name}/{info.filename}"
                        if member_suffix in (".xlsx", ".xls"):
                            engine = "xlrd" if member_suffix == ".xls" else "openpyxl"
                            try:
                                raw = pd.read_excel(
                                    io.BytesIO(payload), sheet_name=None, engine=engine, header=None
                                )
                                pattern_anomalies.extend(
                                    _raw_workbook_pattern_anomalies(raw, archive_name)
                                )
                                all_tables = {}
                                for sheet_name, df in raw.items():
                                    all_tables.update(_split_sub_tables(df, member_name, sheet_name))
                                all_tables = _cap_workbook_subtables(all_tables, archive_name)
                                result[archive_name] = all_tables
                                log.info("Loaded %s: %d sub-tables", archive_name, len(all_tables))
                            except Exception as e:
                                log.warning("Failed to load %s: %s", archive_name, e)
                                failed_files.append(archive_name)
                        elif member_suffix in TEXT_TABLE_SUFFIXES:
                            try:
                                import tempfile
                                with tempfile.TemporaryDirectory(prefix="source_table_") as temp:
                                    extracted = Path(temp) / member_name
                                    extracted.write_bytes(payload)
                                    frame, coverage_warnings = read_screen_table(extracted)
                                    result[archive_name] = {"Sheet1": frame}
                                    failed_files.extend(f"{archive_name}: {w}" for w in coverage_warnings)
                            except (OSError, ValueError, UnicodeError) as exc:
                                failed_files.append(f"{archive_name}: {exc}")
                        elif member_suffix == ".csv":
                            try:
                                df = pd.read_csv(io.BytesIO(payload))
                                result[archive_name] = {"Sheet1": df}
                                log.info("Loaded %s", archive_name)
                            except Exception as e:
                                log.warning("Failed to load %s: %s", archive_name, e)
                                failed_files.append(archive_name)
            except (OSError, zipfile.BadZipFile) as e:
                log.warning("Failed to read ZIP %s: %s", name, e)
                failed_files.append(name)
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
                                pattern_anomalies.extend(
                                    _raw_workbook_pattern_anomalies(
                                        raw, f"{f.name}/{extracted.name}"
                                    )
                                )
                                all_tables = {}
                                for sheet_name, df in raw.items():
                                    sub_tables = _split_sub_tables(df, extracted.name, sheet_name)
                                    all_tables.update(sub_tables)
                                all_tables = _cap_workbook_subtables(all_tables, f"{f.name}/{extracted.name}")
                                result[f"{f.name}/{extracted.name}"] = all_tables
                                log.info("Loaded %s/%s: %d sub-tables", f.name, extracted.name, len(all_tables))
                            except Exception as e:
                                log.warning("Failed to load %s/%s: %s", f.name, extracted.name, e)
                        elif extracted.is_file() and extracted.suffix.lower() in TEXT_TABLE_SUFFIXES:
                            try:
                                frame, coverage_warnings = read_screen_table(extracted)
                                result[f"{f.name}/{extracted.name}"] = {"Sheet1": frame}
                                failed_files.extend(f"{f.name}: {w}" for w in coverage_warnings)
                            except (OSError, ValueError, UnicodeError) as exc:
                                failed_files.append(f"{f.name}/{extracted.name}: {exc}")
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

    return result, failed_files, pattern_anomalies


def _parse_pdb_atom_line(line: str, line_no: int, model_id: str) -> dict:
    """Parse one PDB ATOM/HETATM record using fixed-width fields with a loose fallback."""
    record = line[0:6].strip()
    try:
        return {
            "record": record,
            "serial": line[6:11].strip(),
            "atom_name": line[12:16].strip(),
            "altloc": line[16:17].strip(),
            "resname": line[17:20].strip(),
            "chain": line[21:22].strip(),
            "resseq": line[22:26].strip(),
            "icode": line[26:27].strip(),
            "x": float(line[30:38]),
            "y": float(line[38:46]),
            "z": float(line[46:54]),
            "occupancy": float(line[54:60]) if line[54:60].strip() else None,
            "bfactor": float(line[60:66]) if line[60:66].strip() else None,
            "model_id": model_id,
            "line_no": line_no,
        }
    except (ValueError, IndexError):
        parts = line.split()
        if len(parts) < 9:
            raise ValueError("too few fields")
        try:
            # Standard whitespace layout:
            # ATOM serial atom res chain resseq x y z occ b
            return {
                "record": parts[0],
                "serial": parts[1],
                "atom_name": parts[2],
                "altloc": "",
                "resname": parts[3],
                "chain": parts[4],
                "resseq": parts[5],
                "icode": "",
                "x": float(parts[6]),
                "y": float(parts[7]),
                "z": float(parts[8]),
                "occupancy": float(parts[9]) if len(parts) > 9 else None,
                "bfactor": float(parts[10]) if len(parts) > 10 else None,
                "model_id": model_id,
                "line_no": line_no,
            }
        except (ValueError, IndexError) as e:
            raise ValueError(str(e))


def _analyze_pdb_file(pdb_path: Path, display_name: str) -> tuple[list[dict], bool]:
    """Run conservative, directly verifiable checks on PDB structure files."""
    anomalies = []
    identity_lines: dict[tuple, list[int]] = defaultdict(list)
    coordinate_lines: dict[tuple, list[tuple[int, tuple]]] = defaultdict(list)
    serial_lines: dict[tuple, list[int]] = defaultdict(list)
    malformed_examples = []
    invalid_occupancy = []
    invalid_bfactor = []
    zero_coordinates = []

    atom_record_count = 0
    parsed_count = 0
    model_id = "1"
    seen_model = False

    try:
        with pdb_path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                record = line[0:6].strip()
                if record == "MODEL":
                    parts = line.split()
                    model_id = parts[1] if len(parts) > 1 else str(line_no)
                    seen_model = True
                    continue
                if record == "ENDMDL":
                    model_id = "1" if not seen_model else model_id
                    continue
                if record not in ("ATOM", "HETATM"):
                    continue

                atom_record_count += 1
                try:
                    parsed = _parse_pdb_atom_line(line, line_no, model_id)
                except ValueError as e:
                    if len(malformed_examples) < 5:
                        malformed_examples.append({"line": line_no, "error": str(e)})
                    continue

                parsed_count += 1
                identity = (
                    parsed["model_id"], parsed["record"], parsed["chain"], parsed["resseq"],
                    parsed["icode"], parsed["resname"], parsed["atom_name"], parsed["altloc"],
                )
                identity_lines[identity].append(line_no)
                serial_lines[(parsed["model_id"], parsed["serial"])].append(line_no)

                coord_key = (
                    parsed["model_id"],
                    round(parsed["x"], 3),
                    round(parsed["y"], 3),
                    round(parsed["z"], 3),
                )
                coordinate_lines[coord_key].append((line_no, identity))

                occ = parsed["occupancy"]
                if occ is not None and (occ < 0 or occ > 1):
                    invalid_occupancy.append({"line": line_no, "occupancy": occ})
                bfactor = parsed["bfactor"]
                if bfactor is not None and bfactor < 0:
                    invalid_bfactor.append({"line": line_no, "bfactor": bfactor})
                if abs(parsed["x"]) < 0.0005 and abs(parsed["y"]) < 0.0005 and abs(parsed["z"]) < 0.0005:
                    zero_coordinates.append(line_no)
    except OSError as e:
        return ([{
            "test": "pdb_read_error",
            "location": display_name,
            "severity": "medium",
            "details": {"error": str(e)},
            "description": f"PDB file could not be read: {e}",
        }], False)

    if atom_record_count == 0:
        anomalies.append({
            "test": "pdb_no_atom_records",
            "location": display_name,
            "severity": "medium",
            "details": {"atom_records": 0},
            "description": "PDB file contains no ATOM/HETATM coordinate records.",
        })
        return anomalies, True

    malformed_count = atom_record_count - parsed_count
    if malformed_count:
        ratio = malformed_count / max(atom_record_count, 1)
        severity = "medium" if malformed_count >= 5 or ratio >= 0.05 else "low"
        anomalies.append({
            "test": "pdb_malformed_atom_records",
            "location": display_name,
            "severity": severity,
            "details": {
                "malformed_records": malformed_count,
                "atom_records": atom_record_count,
                "ratio": round(ratio, 4),
                "examples": malformed_examples,
            },
            "description": f"{malformed_count} ATOM/HETATM records could not be parsed.",
        })

    duplicate_identity = {k: v for k, v in identity_lines.items() if len(v) > 1}
    if duplicate_identity:
        duplicate_records = sum(len(v) - 1 for v in duplicate_identity.values())
        severity = "high" if duplicate_records >= 10 or len(duplicate_identity) >= 5 else "medium"
        examples = [
            {"identity": "|".join(str(x) for x in key), "lines": lines[:5]}
            for key, lines in list(duplicate_identity.items())[:5]
        ]
        anomalies.append({
            "test": "pdb_duplicate_atom_identity",
            "location": display_name,
            "severity": severity,
            "details": {
                "duplicate_identity_groups": len(duplicate_identity),
                "duplicate_records": duplicate_records,
                "examples": examples,
            },
            "description": f"{len(duplicate_identity)} atom identities are repeated within the same model.",
        })

    duplicate_serials = {k: v for k, v in serial_lines.items() if k[1] and len(v) > 1}
    if duplicate_serials:
        duplicate_records = sum(len(v) - 1 for v in duplicate_serials.values())
        severity = "medium" if duplicate_records >= 10 else "low"
        examples = [
            {"model_serial": f"{key[0]}:{key[1]}", "lines": lines[:5]}
            for key, lines in list(duplicate_serials.items())[:5]
        ]
        anomalies.append({
            "test": "pdb_duplicate_atom_serial",
            "location": display_name,
            "severity": severity,
            "details": {
                "duplicate_serial_groups": len(duplicate_serials),
                "duplicate_records": duplicate_records,
                "examples": examples,
            },
            "description": f"{len(duplicate_serials)} atom serial numbers are reused within the same model.",
        })

    duplicate_coordinate_groups = []
    for coord_key, entries in coordinate_lines.items():
        distinct_identities = {entry[1] for entry in entries}
        if len(distinct_identities) >= 2:
            duplicate_coordinate_groups.append((coord_key, entries, distinct_identities))
    if duplicate_coordinate_groups:
        duplicate_records = sum(len(g[1]) - 1 for g in duplicate_coordinate_groups)
        severity = "high" if duplicate_records >= 20 or len(duplicate_coordinate_groups) >= 10 else "medium"
        examples = [
            {
                "model_xyz": f"{coord[0]}:{coord[1]:.3f},{coord[2]:.3f},{coord[3]:.3f}",
                "lines": [line_no for line_no, _identity in entries[:6]],
                "distinct_atoms": len(identities),
            }
            for coord, entries, identities in duplicate_coordinate_groups[:5]
        ]
        anomalies.append({
            "test": "pdb_duplicate_coordinates",
            "location": display_name,
            "severity": severity,
            "details": {
                "duplicate_coordinate_groups": len(duplicate_coordinate_groups),
                "duplicate_records": duplicate_records,
                "examples": examples,
            },
            "description": f"{len(duplicate_coordinate_groups)} exact coordinate positions are shared by distinct atoms.",
        })

    if invalid_occupancy:
        severity = "high" if len(invalid_occupancy) >= 20 else "medium"
        anomalies.append({
            "test": "pdb_invalid_occupancy",
            "location": display_name,
            "severity": severity,
            "details": {
                "invalid_records": len(invalid_occupancy),
                "examples": invalid_occupancy[:5],
            },
            "description": f"{len(invalid_occupancy)} atom records have occupancy outside the expected 0-1 range.",
        })

    if invalid_bfactor:
        severity = "medium" if len(invalid_bfactor) >= 10 else "low"
        anomalies.append({
            "test": "pdb_negative_bfactor",
            "location": display_name,
            "severity": severity,
            "details": {
                "invalid_records": len(invalid_bfactor),
                "examples": invalid_bfactor[:5],
            },
            "description": f"{len(invalid_bfactor)} atom records have negative B-factor values.",
        })

    if len(zero_coordinates) >= 3:
        ratio = len(zero_coordinates) / max(parsed_count, 1)
        severity = "high" if len(zero_coordinates) >= 10 or ratio >= 0.01 else "medium"
        anomalies.append({
            "test": "pdb_zero_coordinates",
            "location": display_name,
            "severity": severity,
            "details": {
                "zero_coordinate_records": len(zero_coordinates),
                "parsed_atom_records": parsed_count,
                "ratio": round(ratio, 4),
                "example_lines": zero_coordinates[:10],
            },
            "description": f"{len(zero_coordinates)} atom records have exactly zero XYZ coordinates.",
        })

    return anomalies, True


def _check_pdb_files(data_dir: str or Path or list) -> tuple[list[dict], list[str], int]:
    """Check PDB files separately from table-like source data."""
    data_dirs = [Path(data_dir)] if not isinstance(data_dir, list) else [Path(d) for d in data_dir]
    anomalies = []
    failed_files = []
    inspected = 0
    seen_fingerprints: set[tuple[int, str]] = set()

    candidates = []
    for root in data_dirs:
        if root.exists():
            candidates.extend(root.rglob("*"))

    for f in sorted(candidates):
        if not f.is_file() or f.suffix.lower() != ".pdb":
            continue
        try:
            fp = (f.stat().st_size, hashlib.sha256(f.read_bytes()).hexdigest())
        except OSError as e:
            log.warning("Failed to fingerprint %s: %s", f.name, e)
            failed_files.append(f.name)
            continue
        if fp in seen_fingerprints:
            log.info("Skipping duplicate PDB file by content: %s", f.name)
            continue
        seen_fingerprints.add(fp)

        inspected += 1
        file_anomalies, ok = _analyze_pdb_file(f, f.name)
        anomalies.extend(file_anomalies)
        if not ok:
            failed_files.append(f.name)
        else:
            log.info("Checked PDB %s", f.name)

    return anomalies, failed_files, inspected


_CIF_CELL_TAGS = (
    "_cell_length_a", "_cell_length_b", "_cell_length_c",
    "_cell_angle_alpha", "_cell_angle_beta", "_cell_angle_gamma",
)
_CIF_SPACEGROUP_TAGS = (
    "_space_group_name_H-M_alt",
    "_space_group.name_H-M_alt",
    "_symmetry_space_group_name_H-M",
    "_symmetry_space_group_H-M",
    "_space_group_IT_number",
    "_symmetry_Int_Tables_number",
)
_CIF_FORMULA_TAGS = ("_chemical_formula_sum", "_chemical_formula_moiety")


def _cif_text(block, *tags: str) -> str:
    if gemmi is None:
        return ""
    for tag in tags:
        try:
            raw = block.find_value(tag)
            if raw and not gemmi.cif.is_null(raw):
                return gemmi.cif.as_string(raw).strip()
        except (KeyError, RuntimeError, ValueError):
            continue
    return ""


def _cif_number(block, *tags: str) -> float | None:
    if gemmi is None:
        return None
    for tag in tags:
        try:
            raw = block.find_value(tag)
            if not raw or gemmi.cif.is_null(raw):
                continue
            value = float(gemmi.cif.as_number(raw))
            if math.isfinite(value):
                return value
        except (KeyError, RuntimeError, TypeError, ValueError):
            continue
    return None


def _cif_formula_weight(formula: str) -> float | None:
    """Calculate a simple CIF formula weight; return None for ambiguous expressions."""
    if gemmi is None or not formula:
        return None
    compact = re.sub(r"\s+", "", formula)
    if not compact or any(ch in compact for ch in "()[]{}+-/,;·"):
        return None
    tokens = list(re.finditer(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)", compact))
    if not tokens or "".join(m.group(0) for m in tokens) != compact:
        return None
    total = 0.0
    for match in tokens:
        try:
            element = gemmi.Element(match.group(1))
        except ValueError:
            return None
        if element.atomic_number <= 0:
            return None
        count = float(match.group(2)) if match.group(2) else 1.0
        total += element.weight * count
    return total if total > 0 else None


def _cif_location(path: Path, block_name: str) -> str:
    return f"{path.name} / data_{block_name or '?'}"


def _cif_has_structural_content(block) -> bool:
    tags = _CIF_CELL_TAGS + _CIF_SPACEGROUP_TAGS + (
        "_atom_site_label", "_atom_site_fract_x", "_atom_site.Cartn_x",
        "_refln_index_h", "_diffrn_refln_index_h",
    )
    return any(_cif_text(block, tag) for tag in tags)


def _cif_loop_values(block, label_tags: tuple[str, ...]) -> tuple[list[str], list[list[str]]]:
    """Return one CIF loop as (tags, rows), locating it by any candidate label tag."""
    if gemmi is None:
        return [], []
    column = None
    for tag in label_tags:
        try:
            candidate = block.find_loop(tag)
        except (KeyError, RuntimeError, ValueError):
            continue
        if candidate:
            column = candidate
            break
    if column is None:
        return [], []
    loop = column.get_loop()
    tags = [str(tag) for tag in loop.tags]
    width = int(loop.width())
    rows = []
    for offset in range(0, len(loop.values), width):
        rows.append([str(value) for value in loop.values[offset:offset + width]])
    return tags, rows


def _cif_row_value(row: dict[str, str], *tags: str) -> str:
    for tag in tags:
        value = row.get(tag.lower(), "")
        if value and value not in {".", "?"}:
            return value
    return ""


def _cif_raw_number(value: str, default: float | None = None) -> float | None:
    if gemmi is None or not value or value in {".", "?"}:
        return default
    try:
        number = float(gemmi.cif.as_number(value))
    except (RuntimeError, TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _make_small_structure_compat(block):
    """Build a SmallStructure from common CIF/mmCIF atom loops when strict conversion fails."""
    cell_values = [
        _cif_number(block, standard, mmcif)
        for standard, mmcif in (
            ("_cell_length_a", "_cell.length_a"),
            ("_cell_length_b", "_cell.length_b"),
            ("_cell_length_c", "_cell.length_c"),
            ("_cell_angle_alpha", "_cell.angle_alpha"),
            ("_cell_angle_beta", "_cell.angle_beta"),
            ("_cell_angle_gamma", "_cell.angle_gamma"),
        )
    ]
    structure = gemmi.SmallStructure()
    structure.name = block.name
    if all(value is not None for value in cell_values):
        structure.cell = gemmi.UnitCell(*cell_values)

    spacegroup = _cif_text(block, *_CIF_SPACEGROUP_TAGS)
    if spacegroup:
        structure.spacegroup_hm = spacegroup
        try:
            found = gemmi.find_spacegroup_by_name(spacegroup)
            if found is not None:
                structure.spacegroup = found
        except (RuntimeError, ValueError):
            pass

    tags, raw_rows = _cif_loop_values(
        block,
        ("_atom_site_label", "_atom_site.label_atom_id", "_atom_site.auth_atom_id"),
    )
    if not raw_rows:
        return structure
    lower_tags = [tag.lower() for tag in tags]
    parsed_sites = 0
    for index, values in enumerate(raw_rows, 1):
        row = dict(zip(lower_tags, values))
        label = _cif_row_value(
            row, "_atom_site_label", "_atom_site.label_atom_id", "_atom_site.auth_atom_id",
        ) or f"site_{index}"
        symbol = _cif_row_value(row, "_atom_site_type_symbol", "_atom_site.type_symbol")
        if not symbol:
            match = re.match(r"([A-Za-z]{1,2})", label)
            symbol = match.group(1).title() if match else "X"

        fract_values = [
            _cif_raw_number(_cif_row_value(row, standard, mmcif))
            for standard, mmcif in (
                ("_atom_site_fract_x", "_atom_site.fract_x"),
                ("_atom_site_fract_y", "_atom_site.fract_y"),
                ("_atom_site_fract_z", "_atom_site.fract_z"),
            )
        ]
        if all(value is not None for value in fract_values):
            fractional = gemmi.Fractional(*fract_values)
        else:
            cartesian_values = [
                _cif_raw_number(_cif_row_value(row, tag))
                for tag in ("_atom_site.cartn_x", "_atom_site.cartn_y", "_atom_site.cartn_z")
            ]
            if not all(value is not None for value in cartesian_values) or not structure.cell.is_crystal():
                continue
            fractional = structure.cell.fractionalize(gemmi.Position(*cartesian_values))

        site = gemmi.SmallStructure.Site()
        site.label = label
        site.type_symbol = symbol
        try:
            site.element = gemmi.Element(symbol)
        except ValueError:
            site.element = gemmi.Element("X")
        site.fract = fractional
        site.occ = _cif_raw_number(
            _cif_row_value(row, "_atom_site_occupancy", "_atom_site.occupancy"),
            1.0,
        )
        u_iso = _cif_raw_number(
            _cif_row_value(row, "_atom_site_u_iso_or_equiv", "_atom_site.u_iso_or_equiv")
        )
        if u_iso is None:
            b_iso = _cif_raw_number(
                _cif_row_value(row, "_atom_site_b_iso_or_equiv", "_atom_site.b_iso_or_equiv")
            )
            u_iso = b_iso / (8 * math.pi ** 2) if b_iso is not None else 0.0
        site.u_iso = u_iso
        structure.add_site(site)
        parsed_sites += 1

    if raw_rows and parsed_sites == 0:
        raise ValueError("atom-site loop exists but no coordinates could be parsed")
    return structure


def _analyze_cif_block(block, cif_path: Path) -> tuple[list[dict], bool]:
    anomalies: list[dict] = []
    location = _cif_location(cif_path, block.name)
    try:
        structure = gemmi.make_small_structure_from_block(block)
    except Exception as strict_error:
        try:
            structure = _make_small_structure_compat(block)
        except Exception as compat_error:
            return [{
                "test": "cif_structure_parse_error",
                "location": location,
                "severity": "high",
                "details": {
                    "strict_error": str(strict_error)[:300],
                    "compatibility_error": str(compat_error)[:300],
                },
                "description": "CIF data block could not be converted into a crystal structure.",
            }], False

    sites = list(structure.sites)
    if not sites:
        anomalies.append({
            "test": "cif_no_atom_sites",
            "location": location,
            "severity": "medium",
            "details": {"data_block": block.name},
            "description": "CIF data block contains crystallographic metadata but no readable atom sites.",
        })
        return anomalies, True

    cell = structure.cell
    cell_details = {
        "a": round(float(cell.a), 6),
        "b": round(float(cell.b), 6),
        "c": round(float(cell.c), 6),
        "alpha": round(float(cell.alpha), 6),
        "beta": round(float(cell.beta), 6),
        "gamma": round(float(cell.gamma), 6),
        "calculated_volume": round(float(cell.volume), 6),
    }
    if not cell.is_crystal() or cell.volume <= 0:
        anomalies.append({
            "test": "cif_invalid_unit_cell",
            "location": location,
            "severity": "high",
            "details": cell_details,
            "description": "CIF unit-cell parameters are missing, non-positive, or geometrically invalid.",
        })
    else:
        reported_volume = _cif_number(block, "_cell_volume")
        if reported_volume is not None:
            rel_error = abs(reported_volume - cell.volume) / max(abs(cell.volume), 1e-12)
            if rel_error > 0.02:
                anomalies.append({
                    "test": "cif_cell_volume_mismatch",
                    "location": location,
                    "severity": "high" if rel_error > 0.10 else "medium",
                    "details": {
                        **cell_details,
                        "reported_volume": round(reported_volume, 6),
                        "relative_difference": round(rel_error, 4),
                    },
                    "description": "Reported CIF cell volume is inconsistent with the six unit-cell parameters.",
                })

    spacegroup = _cif_text(block, *_CIF_SPACEGROUP_TAGS)
    if not spacegroup:
        anomalies.append({
            "test": "cif_missing_space_group",
            "location": location,
            "severity": "medium",
            "details": {"atom_site_count": len(sites)},
            "description": "CIF contains atom sites but does not declare a readable space group.",
        })
    else:
        try:
            spacegroup_problem = structure.check_spacegroup()
        except Exception as e:
            spacegroup_problem = str(e)
        if spacegroup_problem:
            anomalies.append({
                "test": "cif_space_group_inconsistency",
                "location": location,
                "severity": "medium",
                "details": {"space_group": spacegroup, "problem": spacegroup_problem[:500]},
                "description": "CIF space-group declaration is inconsistent with its symmetry information.",
            })

    labels: dict[str, list[int]] = defaultdict(list)
    coordinates: dict[tuple[float, float, float], list[dict]] = defaultdict(list)
    invalid_occupancy = []
    negative_displacement = []
    for index, site in enumerate(sites, 1):
        label = str(site.label or f"site_{index}")
        labels[label].append(index)
        fract = site.fract.wrap_to_unit()
        coord = (round(float(fract.x), 6), round(float(fract.y), 6), round(float(fract.z), 6))
        coordinates[coord].append({
            "label": label,
            "element": site.element.name,
            "occupancy": round(float(site.occ), 6) if math.isfinite(float(site.occ)) else None,
        })
        occupancy = float(site.occ)
        if not math.isfinite(occupancy) or occupancy < 0 or occupancy > 1.001:
            invalid_occupancy.append({"label": label, "occupancy": occupancy})
        elif occupancy == 0:
            invalid_occupancy.append({"label": label, "occupancy": occupancy})

        u_iso = float(site.u_iso)
        if math.isfinite(u_iso) and u_iso < -0.0001:
            negative_displacement.append({"label": label, "u_iso": round(u_iso, 6)})
        try:
            if site.aniso.nonzero():
                eigenvalues = [float(v) for v in site.aniso.calculate_eigenvalues()]
                if min(eigenvalues) < -0.005:
                    negative_displacement.append({
                        "label": label,
                        "anisotropic_eigenvalues": [round(v, 6) for v in eigenvalues],
                    })
        except (RuntimeError, TypeError, ValueError):
            pass

    duplicate_labels = {label: rows for label, rows in labels.items() if len(rows) > 1}
    if duplicate_labels:
        anomalies.append({
            "test": "cif_duplicate_atom_labels",
            "location": location,
            "severity": "high" if len(duplicate_labels) >= 5 else "medium",
            "details": {
                "duplicate_label_count": len(duplicate_labels),
                "examples": dict(list(duplicate_labels.items())[:5]),
            },
            "description": "CIF reuses atom-site labels within the same data block.",
        })

    duplicate_coordinates = []
    for coord, entries in coordinates.items():
        if len(entries) < 2:
            continue
        occupancy_sum = sum(float(item.get("occupancy") or 0) for item in entries)
        duplicate_coordinates.append({
            "fractional_coordinate": coord,
            "sites": entries[:8],
            "occupancy_sum": round(occupancy_sum, 6),
        })
    if duplicate_coordinates:
        overoccupied = sum(1 for item in duplicate_coordinates if item["occupancy_sum"] > 1.05)
        anomalies.append({
            "test": "cif_duplicate_fractional_coordinates",
            "location": location,
            "severity": "high" if overoccupied else "medium",
            "details": {
                "duplicate_coordinate_groups": len(duplicate_coordinates),
                "overoccupied_groups": overoccupied,
                "examples": duplicate_coordinates[:5],
            },
            "description": "Distinct CIF atom sites share identical fractional coordinates; occupancy must justify any disorder model.",
        })

    if invalid_occupancy:
        has_out_of_range = any(
            item["occupancy"] is None or item["occupancy"] < 0 or item["occupancy"] > 1.001
            for item in invalid_occupancy
        )
        anomalies.append({
            "test": "cif_invalid_occupancy",
            "location": location,
            "severity": "high" if has_out_of_range else "medium",
            "details": {
                "invalid_site_count": len(invalid_occupancy),
                "examples": invalid_occupancy[:8],
            },
            "description": "CIF contains atom-site occupancies outside the expected 0-1 range or equal to zero.",
        })

    if negative_displacement:
        anomalies.append({
            "test": "cif_negative_displacement_parameter",
            "location": location,
            "severity": "medium",
            "details": {
                "invalid_site_count": len(negative_displacement),
                "examples": negative_displacement[:8],
            },
            "description": "CIF contains materially negative isotropic or anisotropic displacement parameters.",
        })

    if cell.is_crystal() and len(sites) <= 500:
        short_contacts = []
        min_distance = None
        for i, site_a in enumerate(sites):
            for site_b in sites[i + 1:]:
                try:
                    distance = float(cell.find_nearest_pbc_image(site_a.fract, site_b.fract).dist())
                except (RuntimeError, TypeError, ValueError):
                    continue
                if distance <= 1e-5:
                    continue
                involves_h = site_a.element.is_hydrogen or site_b.element.is_hydrogen
                threshold = 0.35 if involves_h else max(
                    0.55,
                    0.45 * (float(site_a.element.covalent_r) + float(site_b.element.covalent_r)),
                )
                if distance < threshold:
                    min_distance = distance if min_distance is None else min(min_distance, distance)
                    short_contacts.append({
                        "site_a": site_a.label,
                        "site_b": site_b.label,
                        "distance_angstrom": round(distance, 4),
                        "threshold_angstrom": round(threshold, 4),
                    })
        if short_contacts:
            anomalies.append({
                "test": "cif_implausibly_short_contact",
                "location": location,
                "severity": "high" if (min_distance or 1) < 0.25 or len(short_contacts) >= 5 else "medium",
                "details": {
                    "short_contact_count": len(short_contacts),
                    "minimum_distance_angstrom": round(min_distance or 0, 4),
                    "examples": short_contacts[:8],
                },
                "description": "CIF contains interatomic contacts far below conservative covalent-distance limits.",
            })

    formula = _cif_text(block, *_CIF_FORMULA_TAGS)
    formula_weight = _cif_formula_weight(formula)
    reported_formula_weight = _cif_number(block, "_chemical_formula_weight")
    if formula_weight and reported_formula_weight:
        rel_error = abs(formula_weight - reported_formula_weight) / formula_weight
        if rel_error > 0.05:
            anomalies.append({
                "test": "cif_formula_weight_mismatch",
                "location": location,
                "severity": "high" if rel_error > 0.20 else "medium",
                "details": {
                    "formula": formula,
                    "calculated_formula_weight": round(formula_weight, 4),
                    "reported_formula_weight": round(reported_formula_weight, 4),
                    "relative_difference": round(rel_error, 4),
                },
                "description": "Reported CIF formula weight is inconsistent with the declared chemical formula.",
            })

    z_value = _cif_number(block, "_cell_formula_units_Z")
    reported_density = _cif_number(block, "_exptl_crystal_density_diffrn", "_exptl_crystal_density_meas")
    if formula_weight and z_value and reported_density and cell.is_crystal():
        calculated_density = formula_weight * z_value * 1.66053906660 / cell.volume
        rel_error = abs(calculated_density - reported_density) / max(calculated_density, 1e-12)
        if rel_error > 0.20 and abs(calculated_density - reported_density) > 0.15:
            anomalies.append({
                "test": "cif_density_mismatch",
                "location": location,
                "severity": "high" if rel_error > 0.50 else "medium",
                "details": {
                    "formula": formula,
                    "z": z_value,
                    "calculated_density_g_cm3": round(calculated_density, 4),
                    "reported_density_g_cm3": round(reported_density, 4),
                    "relative_difference": round(rel_error, 4),
                },
                "description": "CIF density is inconsistent with its formula, Z value, and unit-cell volume.",
            })

    r_factor = _cif_number(
        block,
        "_refine_ls_R_factor_gt", "_refine_ls_R_factor_all", "_refine_ls_R_factor_obs",
    )
    wr_factor = _cif_number(block, "_refine_ls_wR_factor_ref", "_refine_ls_wR_factor_gt")
    goodness = _cif_number(block, "_refine_ls_goodness_of_fit_ref", "_refine_ls_goodness_of_fit_all")
    poor_refinement = (
        (r_factor is not None and r_factor > 0.15)
        or (wr_factor is not None and wr_factor > 0.35)
        or (goodness is not None and (goodness < 0.5 or goodness > 2.5))
    )
    if poor_refinement:
        extreme = (
            (r_factor is not None and r_factor > 0.50)
            or (wr_factor is not None and wr_factor > 0.80)
            or (goodness is not None and (goodness < 0.25 or goodness > 4.0))
        )
        anomalies.append({
            "test": "cif_poor_refinement_quality",
            "location": location,
            "severity": "high" if extreme else "medium",
            "details": {
                "r_factor": r_factor,
                "weighted_r_factor": wr_factor,
                "goodness_of_fit": goodness,
            },
            "description": "CIF refinement statistics are outside conservative quality ranges and require expert review.",
        })

    return anomalies, True


def _analyze_cif_file(cif_path: Path) -> tuple[list[dict], bool]:
    if gemmi is None:
        return [{
            "test": "cif_dependency_missing",
            "location": cif_path.name,
            "severity": "high",
            "details": {"required_package": "gemmi"},
            "description": "CIF file was not checked because the gemmi parser is unavailable.",
        }], False
    try:
        document = gemmi.cif.read_file(str(cif_path))
    except Exception as e:
        return [{
            "test": "cif_read_error",
            "location": cif_path.name,
            "severity": "high",
            "details": {"error": str(e)[:500]},
            "description": "CIF file is unreadable or syntactically invalid.",
        }], False

    if len(document) == 0:
        return [{
            "test": "cif_empty_document",
            "location": cif_path.name,
            "severity": "high",
            "details": {},
            "description": "CIF file contains no data blocks.",
        }], False

    anomalies: list[dict] = []
    structural_blocks = 0
    all_blocks_ok = True
    for block in document:
        if not _cif_has_structural_content(block):
            continue
        structural_blocks += 1
        block_anomalies, ok = _analyze_cif_block(block, cif_path)
        anomalies.extend(block_anomalies)
        all_blocks_ok = all_blocks_ok and ok

    if structural_blocks == 0:
        anomalies.append({
            "test": "cif_no_structure_block",
            "location": cif_path.name,
            "severity": "medium",
            "details": {"data_block_count": len(document)},
            "description": "CIF file has data blocks but no recognizable cell, symmetry, reflection, or atom-site data.",
        })
    return anomalies, all_blocks_ok


def _check_cif_files(data_dir: str or Path or list) -> tuple[list[dict], list[str], int]:
    """Check CIF files separately from table-like and PDB source data."""
    data_dirs = [Path(data_dir)] if not isinstance(data_dir, list) else [Path(d) for d in data_dir]
    anomalies: list[dict] = []
    failed_files: list[str] = []
    inspected = 0
    seen_fingerprints: set[tuple[int, str]] = set()

    candidates = []
    for root in data_dirs:
        if root.exists():
            candidates.extend(root.rglob("*"))

    for path in sorted(candidates):
        if not path.is_file() or path.suffix.lower() != ".cif":
            continue
        try:
            fingerprint = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError as e:
            log.warning("Failed to fingerprint CIF %s: %s", path.name, e)
            failed_files.append(path.name)
            continue
        if fingerprint in seen_fingerprints:
            log.info("Skipping duplicate CIF file by content: %s", path.name)
            continue
        seen_fingerprints.add(fingerprint)

        inspected += 1
        file_anomalies, ok = _analyze_cif_file(path)
        anomalies.extend(file_anomalies)
        if not ok:
            failed_files.append(path.name)
        else:
            log.info("Checked CIF %s", path.name)

    return anomalies, failed_files, inspected


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


def _is_measurement_column(col_name: str, values: np.ndarray or None, *, allow_unnamed: bool = False) -> bool:
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


_FIGURE_LABEL_COL_PATTERN = re.compile(
    r'^\s*(?:fig(?:ure)?[\s._-]*s?|sfig|supp(?:lementary)?\s*fig(?:ure)?|extended\s*data\s*fig(?:ure)?)'
    r'[\s._-]*\d+[a-z]?\s*$',
    re.IGNORECASE,
)


def _clean_numeric_values(values: np.ndarray) -> np.ndarray:
    try:
        arr = np.asarray(values, dtype=float)
    except (ValueError, TypeError):
        return np.array([], dtype=float)
    return arr[np.isfinite(arr)]


def _is_figure_label_column(col_name: str) -> bool:
    return bool(_FIGURE_LABEL_COL_PATTERN.match(str(col_name).strip()))


def _has_small_integer_code_shape(values: np.ndarray) -> bool:
    vals = _clean_numeric_values(values)
    if len(vals) < 3:
        return False
    if not np.all(np.isclose(vals, np.round(vals), atol=1e-10)):
        return False
    unique_count = len(np.unique(np.round(vals)))
    max_abs = float(np.max(np.abs(vals))) if len(vals) else 0.0
    return max_abs <= 20 and unique_count <= min(8, max(3, len(vals) // 2))


def _looks_like_serial_dilution_axis(values: np.ndarray) -> bool:
    vals = _clean_numeric_values(values)
    if len(vals) < 4:
        return False
    nonzero = vals[np.abs(vals) > 1e-12]
    if len(nonzero) < 4 or not np.all(nonzero > 0):
        return False
    diffs = np.diff(nonzero)
    if not (np.all(diffs > 0) or np.all(diffs < 0)):
        return False
    ratios = nonzero[1:] / nonzero[:-1]
    ratio = float(np.median(ratios))
    if ratio <= 0 or abs(ratio - 1.0) < 1e-6:
        return False
    return bool(np.max(np.abs(ratios - ratio)) / abs(ratio) <= 0.02)


def _looks_like_repeated_axis_cycle(values: np.ndarray) -> bool:
    vals = _clean_numeric_values(values)
    n = len(vals)
    if n < 8:
        return False
    for period in range(4, (n // 2) + 1):
        if n % period != 0:
            continue
        first = vals[:period]
        if not all(np.allclose(vals[start:start + period], first, atol=1e-10, rtol=1e-10)
                   for start in range(period, n, period)):
            continue
        if _is_arithmetic_axis(first) or _looks_like_serial_dilution_axis(first):
            return True
    return False


def _passes_basic_source_measurement_name_filter(col_name: str) -> bool:
    name = str(col_name)
    if _is_numeric_column_name(name):
        return False
    if _is_figure_label_column(name):
        return False
    return not (
        _is_independent_variable(name)
        or _is_stat_column(name)
        or _is_whitelist_column(name)
        or _is_non_measurement_name(name)
    )


def _is_short_curve_candidate(col_name: str, values: np.ndarray) -> bool:
    """Allow short source-data curves through even when Excel headers became col_N."""
    vals = _clean_numeric_values(values)
    if len(vals) < 5 or len(vals) > 80:
        return False
    if len(np.unique(np.round(vals, 8))) < 3:
        return False
    if np.all(np.isclose(vals, 0.0)):
        return False
    if not _passes_basic_source_measurement_name_filter(col_name):
        return False
    if (
        _looks_like_row_index(vals)
        or _is_arithmetic_axis(vals)
        or _looks_like_repeated_axis_cycle(vals)
        or _looks_like_categorical_code(vals)
        or _has_small_integer_code_shape(vals)
    ):
        return False
    return True


def _analyze_short_curve_duplicates(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Detect exact duplicated short curves in source/figure tables with lost headers.

    This is intentionally separate from the broad measurement-column filter. Multi-row
    Excel headers often become col_N, but short tumor/release curves remain directly
    comparable by aligned row position.
    """
    MAX_CURVE_COLS = 120
    MAX_HITS_PER_SHEET = 12
    anomalies = []

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if len(numeric_cols) < 2 or len(numeric_cols) > MAX_CURVE_COLS:
        return anomalies

    candidates: list[tuple[str, np.ndarray]] = []
    for col in numeric_cols:
        vals = _clean_numeric_values(df[col].dropna().values)
        if _is_short_curve_candidate(str(col), vals):
            candidates.append((str(col), vals))

    for i in range(len(candidates)):
        if len(anomalies) >= MAX_HITS_PER_SHEET:
            break
        col_a, vals_a = candidates[i]
        for j in range(i + 1, len(candidates)):
            col_b, vals_b = candidates[j]
            if len(vals_a) != len(vals_b):
                continue
            n = len(vals_a)
            if n < 5:
                continue
            a_cmp = np.round(vals_a, 8)
            b_cmp = np.round(vals_b, 8)
            match_ratio = float(np.mean(a_cmp == b_cmp))
            if match_ratio < 0.95:
                continue
            severity = "high" if match_ratio >= 0.98 else "medium"
            anomalies.append({
                "test": "short_curve_duplicate",
                "location": f"{file_name} / {sheet_name} / columns '{col_a}' vs '{col_b}'",
                "severity": severity,
                "details": {
                    "group_a": col_a,
                    "group_b": col_b,
                    "match_ratio": match_ratio,
                    "matched_count": int(np.sum(a_cmp == b_cmp)),
                    "total_compared": n,
                    "sample_values": a_cmp[:8].tolist(),
                },
                "description": f"Short source-data curves '{col_a}' and '{col_b}' are "
                               f"{match_ratio*100:.0f}% identical across {n} aligned points",
            })
            if len(anomalies) >= MAX_HITS_PER_SHEET:
                break

    return anomalies


def _is_rowwise_reuse_column_candidate(col_name: str, values: np.ndarray) -> bool:
    vals = _clean_numeric_values(values)
    if len(vals) < 3:
        return False
    if not _passes_basic_source_measurement_name_filter(col_name):
        return False
    if (
        _looks_like_row_index(vals)
        or _is_arithmetic_axis(vals)
        or _looks_like_categorical_code(vals)
    ):
        return False
    return True


def _analyze_rowwise_value_reuse(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Detect repeated exact values across many treatment columns in compact figure tables."""
    MAX_ROWS = 250
    MAX_COLS = 100
    anomalies = []

    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if len(df) > MAX_ROWS or len(numeric_cols) < 4 or len(numeric_cols) > MAX_COLS:
        return anomalies

    candidate_cols = [
        c for c in numeric_cols
        if _is_rowwise_reuse_column_candidate(str(c), df[c].dropna().values)
    ]
    if len(candidate_cols) < 4:
        return anomalies

    repeated_rows = []
    total_reused_cells = 0
    max_repeat = 0
    for row_idx, row in df[candidate_cols].iterrows():
        counts: dict[float, int] = defaultdict(int)
        for value in row.values:
            if pd.isna(value):
                continue
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(value_f):
                continue
            if abs(value_f) <= 1.0:
                continue
            if abs(value_f - round(value_f)) < 1e-10 and abs(value_f) <= 20:
                continue
            counts[round(value_f, 8)] += 1

        reused = [
            {"value": value, "count": count}
            for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if count >= 3
        ]
        if not reused:
            continue
        repeated_rows.append({
            "row_index": int(row_idx) if isinstance(row_idx, (int, np.integer)) else str(row_idx),
            "reused_values": reused[:5],
        })
        total_reused_cells += sum(item["count"] for item in reused)
        max_repeat = max(max_repeat, max(item["count"] for item in reused))

    if len(repeated_rows) < 3 or total_reused_cells < 9:
        return anomalies

    severity = "high" if len(repeated_rows) >= 4 or total_reused_cells >= 12 else "medium"
    anomalies.append({
        "test": "rowwise_value_reuse",
        "location": f"{file_name} / {sheet_name}",
        "severity": severity,
        "details": {
            "repeated_rows": len(repeated_rows),
            "total_reused_cells": total_reused_cells,
            "max_repeat_in_row": max_repeat,
            "candidate_columns": [str(c) for c in candidate_cols[:30]],
            "examples": repeated_rows[:8],
        },
        "description": f"{len(repeated_rows)} rows contain the same nontrivial numeric value "
                       f"reused across at least three treatment columns",
    })
    return anomalies


def _analyze_named_arithmetic_measurements(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Recover suspicious arithmetic sequences in named treatment columns.

    The generic column filter treats perfect monotonic arithmetic series as axes. That
    is correct for Time/Day/Dose columns, but named treatment readouts can also be
    short perfect arithmetic sequences and should be reviewed.
    """
    MAX_HITS_PER_SHEET = 8
    anomalies = []
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if len(numeric_cols) < 3:
        return anomalies

    for col in numeric_cols:
        if len(anomalies) >= MAX_HITS_PER_SHEET:
            break
        name = str(col)
        if _is_unnamed_column(name) or not _passes_basic_source_measurement_name_filter(name):
            continue
        vals = _clean_numeric_values(df[col].dropna().values)
        if len(vals) < 5 or len(vals) > 30:
            continue
        if len(np.unique(np.round(vals, 8))) < 5:
            continue
        if _has_small_integer_code_shape(vals):
            continue
        if not _is_arithmetic_axis(vals):
            continue

        arith_result = check_arithmetic_sequence(vals, ARITHMETIC_SEQ_TOLERANCE)
        if not arith_result.get("is_arithmetic"):
            continue
        dev = arith_result.get("max_relative_deviation", 1.0)
        if dev is None or dev > 0.001:
            continue
        anomalies.append({
            "test": "arithmetic_sequence",
            "location": f"{file_name} / {sheet_name} / column '{col}'",
            "severity": "high",
            "details": {
                **arith_result,
                "recovered_from_axis_filter": True,
            },
            "description": f"Named treatment column forms a near-perfect arithmetic sequence "
                           f"(common difference={arith_result['common_diff']:.6f}, "
                           f"max deviation={dev:.6f})",
        })

    return anomalies


def _analyze_passage_offset_pattern(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Run `check_passage_offset_pattern` on sub-tables AND on 3-consecutive-column
    windows within sub-tables.

    This is a cross-row, cross-column check that flags passage/timepoint tables
    where two rows share byte-equal first-N columns (replicate 1..N) but differ
    in the last column by an integer / half-step offset — a synthetic
    pseudo-replicate pattern documented by scitranslmed.aba3571 Fig.S11.

    Multi-panel sheets (e.g. Fig.S11 has Sections A/B/C/D × 3 strains × 3
    replicates) MUST be split into sub-tables first AND then into per-strain
    3-column windows. Running on the whole sheet mixes rows from different
    sections; running on a sub-table but with all 12 columns mixes columns
    from different strains.
    """
    anomalies: list[dict] = []
    try:
        sub_tables = _split_sub_tables(df, file_name, sheet_name)
    except Exception:
        sub_tables = {"__whole__": df}

    for sub_name, sub_df in sub_tables.items():
        n_rows = sub_df.shape[0]
        # Restrict to sub-tables with enough rows for the pattern to be
        # statistically meaningful. <10 rows gives too many false positives
        # because with so few row pairs the matching first-N columns are
        # more likely coincidental.
        # Cap at 200 rows: the O(n^2) row-pair scan becomes too costly on
        # large sheets (e.g. 18k-row time series) AND a real passage × replicate
        # table rarely exceeds ~50 passages.
        if n_rows < 10 or n_rows > 200:
            continue

        # Try the whole sub-table first (covers the single-strain case)
        _scan_passage_window(anomalies, sub_df, sub_name, window=None,
                             label_suffix="")

        # Also try 3-consecutive-column windows to handle multi-strain layouts
        # where columns 1-3 = strain1 replicates, 5-7 = strain2, 9-11 = strain3.
        # Cap at 12 numeric columns (4 strains × 3 replicates is the realistic
        # max for a passage layout) — wider layouts aren't passage tables.
        # Only useful when there are >3 numeric cols (single-strain case is
        # already covered by the whole-sub-table call above).
        num_cols = sub_df.select_dtypes(include=[np.number]).shape[1]
        if num_cols > 3 and num_cols <= 12:
            for start in range(0, num_cols - 2):
                window_df = sub_df.iloc[:, start:start + 3].copy()
                if window_df.select_dtypes(include=[np.number]).shape[1] < 3:
                    continue
                _scan_passage_window(anomalies, window_df, sub_name, window=start,
                                     label_suffix=f" (cols {start}-{start + 2})")
    return anomalies


def _scan_passage_window(anomalies: list, sub_df: pd.DataFrame, sub_name: str, *,
                         window: int or None, label_suffix: str) -> None:
    """Helper: run check_passage_offset_pattern on one (sub-table, window) and
    append any flagged results to `anomalies`. sub_name is the full path
    (e.g. "Fig.S11 / A") — already includes sheet + sub-section."""
    # Sanity: the LAST column of the window must be a real measurement, not a
    # constant ceiling (e.g. 200.0 ug/mL "no inhibition" in MIC) or a row-index
    # axis. If it has <5 unique values across the rows we consider, skip —
    # the "offset" wouldn't be a meaningful synthesis indicator.
    try:
        num_df = sub_df.select_dtypes(include=[np.number])
        if num_df.shape[1] < 3 or num_df.shape[0] < 10:
            return
        last_col = num_df.iloc[:, -1]
        finite_last = last_col[np.isfinite(last_col)]
        if len(finite_last) < 10 or finite_last.nunique() < 5:
            return
    except Exception:
        return

    # Filter out "baseline" rows where the first K columns are all zero (or
    # all NaN). These are 0-dose / control rows that LEGITIMATELY match across
    # panels (e.g. dose-response tables that each start with NC1=0 → all
    # measurements=0 → next panel's NC1=0 → all measurements=0 again). They
    # are not fraud signals.
    try:
        first_K = num_df.iloc[:, :-1]  # all numeric cols except the last
        last = num_df.iloc[:, -1]
        keep_mask = pd.Series(True, index=num_df.index)
        for ridx in num_df.index:
            row_K = first_K.loc[ridx]
            row_last = last.loc[ridx]
            row_K_finite = row_K[np.isfinite(row_K)]
            if len(row_K_finite) == 0:
                # row is all-NaN in first K cols — skip as structural blank
                keep_mask.loc[ridx] = False
                continue
            # Baseline row: all first K finite values are 0 → control/baseline
            if (row_K_finite.abs() <= 1e-12).all():
                keep_mask.loc[ridx] = False
        filtered = num_df[keep_mask]
        if filtered.shape[0] < 10:
            return
        # Also need the last col to still have enough unique values after filter
        if filtered.iloc[:, -1].nunique() < 5:
            return
    except Exception:
        return

    try:
        # Require at least 3 row-pairs (more selective than 2) to reduce
        # false positives on small sub-tables.
        results = check_passage_offset_pattern(filtered, min_pairs=3)
    except Exception:
        return
    for r in results:
        pair_records = r.get("pairs", [])[:5]
        pair_str = ", ".join(
            f"R{p['row_a']}↔R{p['row_b']} (offset={p['offset']:+.2f})"
            for p in pair_records
        )
        truncated = r.get("pairs_truncated", False)
        if truncated:
            pair_str += f" ... (+{r['n_pairs'] - len(pair_records)} more)"
        anomalies.append({
            "test": "passage_offset_pattern",
            "location": f"{sub_name}{label_suffix}",
            "severity": r["severity"],
            "details": r,
            "description": (
                f"{r['n_pairs']} row-pair(s) share identical first {r['n_first_cols']} "
                f"column(s); last column ({r['last_col']}) differs by integer/half-step "
                f"offset ({r['n_exact_matches']} exact, {r['n_integer_offsets']} integer-offset). "
                f"Examples: {pair_str}. Pattern is consistent with synthetic "
                f"pseudo-replicates across passages/timepoints."
            ),
        })


def _analyze_decimal_injection(
    df: pd.DataFrame,
    measurement_cols: list,
    file_name: str,
    sheet_name: str,
) -> list[dict]:
    """Detect decimal-digit injection / systematic decimal-place rewriting.

    Pattern (per user spec): a column's decimal portion looks like it was
    produced by taking values from a sibling column and randomly inserting
    1-2 extra digits into the decimal portion. Concretely we test for:

    1. **Truncation match** — for each value v in this column, round v down
       to N decimal places for N ∈ {min_dec, min_dec+1} (where min_dec is the
       smaller of the two columns' median decimal-place counts). The rounded
       value must appear in the sibling column (rounded to the same N).
    2. **Single-digit removal** — for each value v, try removing one digit
       at every position of the decimal portion (e.g. 0.9137 → 0.137,
       0.9137 → 0.937, 0.9137 → 0.917, 0.9137 → 0.913). The result must
       appear in the sibling column.
    3. **Two-digit removal** — same but remove 2 digits.

    If >=40% of v's transformations land in the sibling column's value set,
    the column is flagged. The pattern is "digit injection" because the
    suspect column has systematically more decimal digits than its sibling
    AND those extra digits are random (i.e. removing them recovers the
    sibling's value).

    Severity: medium (single indicator) → high if both (1) AND (2) fire
    with >=60% match ratio.
    """
    anomalies: list[dict] = []

    def _decimal_places(v: float) -> int:
        s = f"{v:.10f}".rstrip("0")
        if "." not in s:
            return 0
        return len(s.split(".")[1])

    def _values_with_min_n(df_col: pd.Series, n: int) -> set[float]:
        # Round all non-null values to N decimal places and dedup
        out = set()
        for v in df_col.dropna().values:
            try:
                f = float(v)
                if not np.isfinite(f):
                    continue
                out.add(round(f, n))
            except (TypeError, ValueError):
                continue
        return out

    def _one_digit_removals(v: float) -> set[float]:
        # Remove one digit at every position of the decimal portion
        s = f"{v:.10f}".rstrip("0")
        if "." not in s:
            return set()
        int_part, dec_part = s.split(".")
        out = set()
        for i in range(len(dec_part)):
            new_dec = dec_part[:i] + dec_part[i + 1:]
            if not new_dec:
                continue
            try:
                out.add(float(f"{int_part}.{new_dec}"))
            except ValueError:
                continue
        return out

    def _two_digit_removals(v: float) -> set[float]:
        s = f"{v:.10f}".rstrip("0")
        if "." not in s:
            return set()
        int_part, dec_part = s.split(".")
        out = set()
        for i in range(len(dec_part)):
            for j in range(i + 1, len(dec_part)):
                new_dec = dec_part[:i] + dec_part[i + 1:j] + dec_part[j + 1:]
                if not new_dec:
                    continue
                try:
                    out.add(float(f"{int_part}.{new_dec}"))
                except ValueError:
                    continue
        return out

    for i, col_a in enumerate(measurement_cols):
        vals_a = df[col_a].dropna().values
        if len(vals_a) < 5:
            continue
        vals_a = [float(v) for v in vals_a if np.isfinite(float(v))]
        if len(vals_a) < 5:
            continue
        dec_a_med = sorted(_decimal_places(v) for v in vals_a)[len(vals_a) // 2]

        for col_b in measurement_cols[i + 1:]:
            vals_b = df[col_b].dropna().values
            vals_b = [float(v) for v in vals_b if np.isfinite(float(v))]
            if len(vals_b) < 5:
                continue
            dec_b_med = sorted(_decimal_places(v) for v in vals_b)[len(vals_b) // 2]

            # The suspect column must have strictly more median decimal places
            # than its sibling for the digit-injection hypothesis to be
            # plausible. Even a 1-decimal-place shift is a real signal — the
            # sibling's clean N places vs suspect's noisy N+1 / N+2 places
            # is exactly the "1-2 digits randomly inserted" pattern.
            # Skip pairs where both columns have identical precision — those
            # are normal reporting precision, not injection.
            dec_diff = abs(dec_a_med - dec_b_med)
            if dec_diff < 1:
                continue

            if dec_a_med > dec_b_med:
                suspect_vals, sibling_vals = vals_a, vals_b
                suspect_dec, sibling_dec = dec_a_med, dec_b_med
                suspect_name, sibling_name = col_a, col_b
            else:
                suspect_vals, sibling_vals = vals_b, vals_a
                suspect_dec, sibling_dec = dec_b_med, dec_a_med
                suspect_name, sibling_name = col_b, col_a

            min_dec = min(suspect_dec, sibling_dec)
            # Build sibling lookup: the suspect's truncated values must land in
            # the sibling at the same rounding precision. We expand the
            # rounding window by ±1 decimal place to absorb legitimate
            # instrument-precision differences (e.g. 0.95 reported as 0.950).
            sibling_at_n = set()
            for n in range(min_dec, min(min_dec + 2, sibling_dec + 2)):
                sibling_at_n |= _values_with_min_n(df[sibling_name], n)

            # Same-row-position sibling lookup. Real injection papers have
            # treatment[i] derived from control[i] (sibling values paired by
            # replicate). A pure set-membership test (above) cannot tell
            # injection from random collision when the sibling has few
            # distinct values (1-decimal data only has 10 candidates, so
            # random 3-decimal values collide ~100% on round-to-1).
            sibling_at_n_rows: dict[float, set[int]] = {}
            for row_idx, v in df[sibling_name].dropna().items():
                try:
                    f = float(v)
                    if not np.isfinite(f):
                        continue
                    for n in range(min_dec, min(min_dec + 2, sibling_dec + 2)):
                        rounded = round(f, n)
                        sibling_at_n_rows.setdefault(rounded, set()).add(row_idx)
                except (TypeError, ValueError):
                    continue
            sibling_at_n_rows_full: dict[float, set[int]] = {rounded: rows for rounded, rows in sibling_at_n_rows.items()}

            # One- and two-digit removal tests are only meaningful when the
            # suspect column has >=2 (or >=3) more decimal places than the
            # sibling. Otherwise the candidate removal set is large enough
            # that random values collide spuriously.
            do_one_digit = dec_diff >= 2
            do_two_digit = dec_diff >= 3

            trunc_hits = 0
            one_digit_hits = 0
            two_digit_hits = 0
            for row_idx_v, v in enumerate(suspect_vals):
                # Test 1: truncation match at min_dec / min_dec+1
                r1, r2 = round(v, min_dec), round(v, min_dec + 1)
                hit = (r1 in sibling_at_n) or (r2 in sibling_at_n)
                # If hit, also require same-row coincidence — distinguishes
                # injection (treatment[i] ≈ control[i]) from random collision
                # (treatment[i] ≈ some control[j], j != i).
                if hit:
                    rows_r1 = sibling_at_n_rows_full.get(r1, set())
                    rows_r2 = sibling_at_n_rows_full.get(r2, set())
                    if row_idx_v in rows_r1 or row_idx_v in rows_r2:
                        trunc_hits += 1
                # Test 2: one-digit removal
                if do_one_digit:
                    for rem in _one_digit_removals(v):
                        if rem in sibling_at_n:
                            rows = sibling_at_n_rows_full.get(rem, set())
                            if row_idx_v in rows:
                                one_digit_hits += 1
                                break
                # Test 3: two-digit removal
                if do_two_digit:
                    for rem in _two_digit_removals(v):
                        if rem in sibling_at_n:
                            rows = sibling_at_n_rows_full.get(rem, set())
                            if row_idx_v in rows:
                                two_digit_hits += 1
                                break

            n = len(suspect_vals)
            trunc_ratio = trunc_hits / n
            one_ratio = one_digit_hits / n
            two_ratio = two_digit_hits / n
            best_ratio = max(trunc_ratio, one_ratio, two_ratio)
            best_kind = (
                "truncation" if trunc_ratio == best_ratio
                else "one_digit_removal" if one_ratio == best_ratio
                else "two_digit_removal"
            )

            # Truncation alone is too permissive when sibling has very few
            # distinct values (e.g. 1-decimal data only has 10 candidates, so
            # 2-decimal values truncate-collide randomly ~70% of the time).
            # Same-row constraint cuts this to a 60% threshold (random
            # alignment is much less likely than same-row coincidence).
            if not (one_ratio >= 0.40 or two_ratio >= 0.40):
                if not (trunc_ratio >= 0.60):
                    continue

            # Both (1) and (2) firing with >=60% → high
            # Or a single kind firing with >=80% AND a 2+ decimal-place shift
            # (extreme injection: 5-decimal vs 3-decimal column is unambiguous
            # fabrication, even when truncation can't find the sibling values).
            if (trunc_ratio >= 0.60 and one_ratio >= 0.60) or (trunc_ratio + one_ratio >= 1.20):
                severity = "high"
            elif best_ratio >= 0.80 and dec_diff >= 2:
                severity = "high"
            else:
                severity = "medium"

            location = f"{file_name} / {sheet_name} / column '{suspect_name}' (vs '{sibling_name}')"
            anomalies.append({
                "test": "decimal_injection",
                "location": location,
                "severity": severity,
                "details": {
                    "suspect_column": str(suspect_name),
                    "sibling_column": str(sibling_name),
                    "suspect_dec_med": suspect_dec,
                    "sibling_dec_med": sibling_dec,
                    "truncation_match_ratio": round(trunc_ratio, 3),
                    "one_digit_removal_ratio": round(one_ratio, 3),
                    "two_digit_removal_ratio": round(two_ratio, 3),
                    "best_match_kind": best_kind,
                    "best_match_ratio": round(best_ratio, 3),
                    "n_suspect": n,
                    "n_sibling": len(sibling_vals),
                },
                "description": (
                    f"Column '{suspect_name}' shows decimal-digit injection pattern vs sibling "
                    f"'{sibling_name}': median {suspect_dec} vs {sibling_dec} decimal places, "
                    f"with {best_ratio*100:.0f}% of values ({best_kind}, "
                    f"n={n}) recoverable by digit removal/truncation to match the sibling"
                ),
            })

    return anomalies


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

    # Synthetic '末位填充' detection (末 N 位数字完全相同) — complements decimal_uniformity
    # which only checks the COUNT of decimal places, not the digit CONTENT of the trailing
    # portion. Strong fabrication marker when many values share the same last 3-4 digits.
    # min_samples lowered to 5 to match decimal_uniformity — multi-panel sheets get
    # split into ~5-row sub-tables and we still want the check to fire there.
    trail_result = check_trailing_digit_uniformity(values, n_digits=4, min_samples=5)
    if trail_result.get("testable") and trail_result.get("flagged"):
        # Filter out "round-number" patterns that are not fabrication signals.
        # Small concentration values (e.g. 1e-4 M dose-response) legitimately
        # share suffixes like "0001" or "0002" because the trailing digits are
        # determined by the magnitude of the value, not by the experimenter.
        # Real fabrication markers (e.g. aba3571 Fig.S9's "1246") have non-zero,
        # non-trivial trailing digits.
        suffix = str(trail_result.get("most_common_suffix", ""))
        is_round_pattern = (
            len(suffix) >= 4
            and suffix.startswith("00")
            and trail_result.get("match_ratio", 0) < 0.7
        )
        if not is_round_pattern:
            anomalies.append({
                "test": "trailing_digit_uniformity",
                "location": location,
                "severity": trail_result["severity"],
                "details": trail_result,
                "description": (
                    f"{trail_result['match_count']}/{trail_result['total']} values share the "
                    f"same last {trail_result['n_digits']} decimal digits "
                    f"(\"{trail_result['most_common_suffix']}\", ratio={trail_result['match_ratio']:.2f}) "
                    f"— likely synthetic-data trailing-digit padding"
                ),
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


def _analyze_feature_matrix(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Lightweight vectorized checks for gene/transcript x stage matrices."""
    anomalies: list[dict] = []
    feature_col = df.columns[0]
    numeric = df.iloc[:, 1:].select_dtypes(include=[np.number]).copy()
    if numeric.empty or len(numeric.columns) < 3:
        return anomalies

    row_count = len(numeric)
    valid_counts = numeric.notna().sum()
    zero_counts = (numeric.fillna(0) == 0).sum()
    empty_cols = [str(c) for c in numeric.columns if valid_counts[c] == 0]
    all_zero_cols = [str(c) for c in numeric.columns if valid_counts[c] > 0 and zero_counts[c] == row_count]
    if empty_cols or all_zero_cols:
        anomalies.append({
            "test": "feature_matrix_empty_or_zero_columns",
            "location": f"{file_name} / {sheet_name}",
            "severity": "medium",
            "details": {
                "empty_columns": empty_cols[:20],
                "all_zero_columns": all_zero_cols[:20],
                "rows": row_count,
            },
            "description": "Feature matrix contains stage columns that are empty or entirely zero.",
        })

    row_sums = numeric.sum(axis=1, skipna=True)
    finite_sums = row_sums[np.isfinite(row_sums)]
    near_one_ratio = float(((finite_sums - 1.0).abs() <= 1e-6).mean()) if len(finite_sums) else 0.0
    if len(finite_sums) >= 100 and near_one_ratio >= 0.95:
        log.info(
            "Feature matrix %s / %s appears to be normalized proportions: %.1f%% rows sum to 1",
            file_name,
            sheet_name,
            near_one_ratio * 100,
        )

    rounded = numeric.round(10)
    valid_profiles = rounded.dropna(how="all")
    nonzero_counts = (valid_profiles.fillna(0).abs() > 1e-12).sum(axis=1)
    # Sparse one-hot/mostly-zero profiles are expected in developmental
    # specificity matrices; exact duplicates there are not useful signals.
    valid_profiles = valid_profiles[nonzero_counts >= 3]
    duplicate_rows = valid_profiles.duplicated(keep=False)
    dup_row_count = int(duplicate_rows.sum())
    if dup_row_count >= max(25, int(0.01 * row_count)):
        examples = []
        duplicate_index = valid_profiles.index[duplicate_rows]
        feature_ids = df.loc[duplicate_index, feature_col].astype(str).head(20).tolist()
        examples.extend(feature_ids)
        anomalies.append({
            "test": "feature_matrix_duplicate_profiles",
            "location": f"{file_name} / {sheet_name}",
            "severity": "medium",
            "details": {
                "duplicate_profile_rows": dup_row_count,
                "total_rows": row_count,
                "example_features": examples,
            },
            "description": f"{dup_row_count} features share an identical rounded stage profile.",
        })

    col_hashes: dict[tuple, list[str]] = defaultdict(list)
    for col in rounded.columns:
        vals = rounded[col].dropna().values
        if len(vals) < max(100, int(0.5 * row_count)):
            continue
        col_hashes[tuple(vals.tolist())].append(str(col))
    duplicate_col_groups = [cols for cols in col_hashes.values() if len(cols) > 1]
    if duplicate_col_groups:
        anomalies.append({
            "test": "feature_matrix_duplicate_columns",
            "location": f"{file_name} / {sheet_name}",
            "severity": "high",
            "details": {
                "duplicate_column_groups": duplicate_col_groups[:10],
                "rows": row_count,
            },
            "description": "Feature matrix has stage columns with identical values across features.",
        })

    if len(numeric.columns) <= 20 and row_count >= 100:
        corr = numeric.corr(method="pearson", min_periods=max(100, int(row_count * 0.5))).abs()
        high_pairs = []
        cols = list(corr.columns)
        for i, col_a in enumerate(cols):
            for col_b in cols[i + 1:]:
                value = corr.loc[col_a, col_b]
                if pd.notna(value) and float(value) >= 0.999999:
                    high_pairs.append({
                        "col_a": str(col_a),
                        "col_b": str(col_b),
                        "abs_pearson": float(value),
                    })
        if high_pairs:
            anomalies.append({
                "test": "feature_matrix_near_identical_columns",
                "location": f"{file_name} / {sheet_name}",
                "severity": "medium",
                "details": {
                    "pairs": high_pairs[:20],
                    "rows": row_count,
                },
                "description": "Feature matrix has stage columns with near-perfect correlation.",
            })

    return anomalies


def _analyze_sheet(df: pd.DataFrame, file_name: str, sheet_name: str) -> list[dict]:
    """Analyze a single sheet/dataframe for anomalies."""
    MAX_COLS_FOR_PAIRWISE = 200
    anomalies = []

    if _is_non_measurement_context(file_name, sheet_name):
        return anomalies

    if _looks_like_sequence_alignment_table(df):
        log.info("Skipping sequence alignment table statistics: %s / %s", file_name, sheet_name)
        return anomalies

    if _looks_like_feature_matrix(df):
        return _analyze_feature_matrix(df, file_name, sheet_name)

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

    anomalies.extend(_analyze_short_curve_duplicates(df, file_name, sheet_name))
    anomalies.extend(_analyze_rowwise_value_reuse(df, file_name, sheet_name))
    anomalies.extend(_analyze_named_arithmetic_measurements(df, file_name, sheet_name))
    anomalies.extend(_analyze_passage_offset_pattern(df, file_name, sheet_name))

    if not measurement_cols:
        return anomalies

    for col in measurement_cols:
        values = df[col].dropna().values
        if len(values) < 3:
            continue
        location = f"{file_name} / {sheet_name} / column '{col}'"
        anomalies.extend(_analyze_column_group(values, location, col_name=str(col)))

    # Per-column decimal-injection / systematic-digit-rewrite pattern.
    # Fabrication pattern: a column's decimal portion looks like it was generated
    # by taking values from another (sibling) column and randomly inserting 1-2
    # extra digits into the decimal portion. The result: most of this column's
    # values, when truncated to a shorter decimal length, coincide with values
    # in the sibling column. Detected by:
    #   (1) truncation-match: each value, rounded down to N decimal places
    #       where N ∈ {min_dec, min_dec+1}, must hit a value (rounded to the
    #       same N) in another measurement column on the same sheet.
    #   (2) digit-insertion: each value, with 1 digit removed from its decimal
    #       portion at every position, must hit a value in another column.
    # If >40% of the column's values satisfy (1) or (2) against any single
    # sibling column, the column is flagged. Single high ratio, no minimum
    # sample count beyond 5, because this is precisely a small-N fabrication
    # pattern (Figure panels often have n=3 or n=5).
    if len(measurement_cols) >= 2:
        anomalies.extend(
            _analyze_decimal_injection(df, measurement_cols, file_name, sheet_name)
        )

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


def _transpose_row_subtables(sheets: dict) -> dict:
    """Transpose (1, N) sub-tables so cross-sheet column comparison can match.

    A common data layout (especially in behavioral/biology papers) is one metric
    per sub-table with all biological replicates arranged as columns, e.g.
        Total distance(cm): [v1, v2, v3, ..., v20]  (shape (1, 20))
    The cross-sheet column matcher requires len(vals) >= 3 to find duplicates,
    so a 1-row shape with N columns never triggers because each column vector
    has length 1. Transposing (1, N) -> (N, 1) lets the matcher see all 20
    replicates in one column. Multi-row sub-tables pass through unchanged.
    """
    out = {}
    for name, df in sheets.items():
        if isinstance(df, pd.DataFrame) and df.shape[0] == 1 and df.shape[1] > 1:
            t = df.T.copy()
            t.columns = ["value"]
            out[name] = t
        else:
            out[name] = df
    return out


def _analyze_cross_sheet(sheets: dict[str, pd.DataFrame], file_name: str) -> list[dict]:
    """Compare data blocks across sheets within the same file.

    2026-06-24: _transpose_row_subtables() pre-flattens (1, N) sub-tables
    (replicate-as-column layout) so the column matcher actually sees the
    replicate data; without this fix a 1-row 20-column sheet silently yields
    zero matches even when an identical (1, 20) sheet exists elsewhere in
    the file (e.g. fcimb 2025.1559182 Average speed vs RI%).
    """
    sheets = _transpose_row_subtables(sheets)
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

            if _looks_like_sequence_alignment_table(df_a) or _looks_like_sequence_alignment_table(df_b):
                continue

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


def check_data_anomalies(data_dir: str or Path or list) -> list[dict]:
    """Run all data anomaly checks on source data files."""
    data_dirs = [Path(data_dir)] if not isinstance(data_dir, list) else [Path(d) for d in data_dir]
    existing_dirs = [d for d in data_dirs if d.exists()]
    if not existing_dirs:
        log.info("No source data directory found: %s", data_dir)
        return []

    _SHEET_SCAN_SKIP_WORKBOOKS.clear()
    _CROSS_SHEET_SKIP_WORKBOOKS.clear()
    pdb_anomalies, _pdb_failed, pdb_count = _check_pdb_files(existing_dirs)
    cif_anomalies, _cif_failed, cif_count = _check_cif_files(existing_dirs)
    all_files, _failed, pattern_anomalies = _load_data_files(existing_dirs)
    if not all_files and not pdb_count and not cif_count:
        log.info("No data files found in %s", existing_dirs)
        return []

    all_anomalies = list(pdb_anomalies) + list(cif_anomalies) + pattern_anomalies
    for fname, sheets in all_files.items():
        if fname in _SHEET_SCAN_SKIP_WORKBOOKS:
            log.warning("Skipping per-subtable statistical scan for capped workbook %s (%d sampled sub-tables)", fname, len(sheets))
            continue
        for sheet_name, df in sheets.items():
            sheet_anomalies = _analyze_sheet(df, fname, sheet_name)
            all_anomalies.extend(sheet_anomalies)

    for fname, sheets in all_files.items():
        if len(sheets) >= 2:
            if fname in _CROSS_SHEET_SKIP_WORKBOOKS:
                log.warning("Skipping cross-sheet duplicate scan for capped workbook %s (%d sampled sub-tables)", fname, len(sheets))
                continue
            cross_anomalies = _analyze_cross_sheet(sheets, fname)
            all_anomalies.extend(cross_anomalies)

    all_anomalies = _suppress_redundant_transform_findings(all_anomalies)
    high = sum(1 for a in all_anomalies if a["severity"] == "high")
    medium = sum(1 for a in all_anomalies if a["severity"] == "medium")
    low = sum(1 for a in all_anomalies if a["severity"] == "low")
    log.info("Data anomalies found: %d total (high=%d, medium=%d, low=%d)", len(all_anomalies), high, medium, low)

    return all_anomalies


def check_data_with_validation(data_dir: str or Path or list) -> tuple[list[dict], list[str]]:
    """Run data anomaly checks and report files that failed to load.
    Returns (anomalies, failed_file_names)."""
    data_dirs = [Path(data_dir)] if not isinstance(data_dir, list) else [Path(d) for d in data_dir]
    existing_dirs = [d for d in data_dirs if d.exists()]
    if not existing_dirs:
        return [], []

    _SHEET_SCAN_SKIP_WORKBOOKS.clear()
    _CROSS_SHEET_SKIP_WORKBOOKS.clear()
    pdb_anomalies, pdb_failed, pdb_count = _check_pdb_files(existing_dirs)
    cif_anomalies, cif_failed, cif_count = _check_cif_files(existing_dirs)
    all_files, failed_files, pattern_anomalies = _load_data_files(existing_dirs)
    failed_files = failed_files + pdb_failed + cif_failed
    if not all_files and not failed_files and not pdb_count and not cif_count:
        return [], []

    all_anomalies = list(pdb_anomalies) + list(cif_anomalies) + pattern_anomalies
    for fname, sheets in all_files.items():
        if fname in _SHEET_SCAN_SKIP_WORKBOOKS:
            log.warning("Skipping per-subtable statistical scan for capped workbook %s (%d sampled sub-tables)", fname, len(sheets))
            continue
        for sheet_name, df in sheets.items():
            all_anomalies.extend(_analyze_sheet(df, fname, sheet_name))

    for fname, sheets in all_files.items():
        if len(sheets) >= 2:
            if fname in _CROSS_SHEET_SKIP_WORKBOOKS:
                log.warning("Skipping cross-sheet duplicate scan for capped workbook %s (%d sampled sub-tables)", fname, len(sheets))
                continue
            all_anomalies.extend(_analyze_cross_sheet(sheets, fname))

    all_anomalies = _suppress_redundant_transform_findings(all_anomalies)
    if all_anomalies:
        high = sum(1 for a in all_anomalies if a["severity"] == "high")
        medium = sum(1 for a in all_anomalies if a["severity"] == "medium")
        low = sum(1 for a in all_anomalies if a["severity"] == "low")
        log.info("Data anomalies found: %d total (high=%d, medium=%d, low=%d)", len(all_anomalies), high, medium, low)

    return all_anomalies, failed_files


def _suppress_redundant_transform_findings(anomalies: list[dict]) -> list[dict]:
    """Do not report the same mislabeled transform columns three times.

    A semantic transform conflict already states that two named columns are
    identical and explains the violated formula.  Generic duplicate/correlation
    detectors add no independent evidence for that same pair and make the report
    look like several different problems.
    """
    semantic_pairs = []
    for issue in anomalies:
        if issue.get("test") != "semantic_transform_conflict":
            continue
        details = issue.get("details", {})
        pair = frozenset({str(details.get("left_header", "")), str(details.get("right_header", ""))})
        if len(pair) == 2 and "" not in pair:
            semantic_pairs.append((str(issue.get("location", "")).split(" / ", 1)[0], pair))

    filtered = []
    for issue in anomalies:
        test = issue.get("test")
        location_file = str(issue.get("location", "")).split(" / ", 1)[0]
        redundant = False
        if test == "feature_matrix_duplicate_columns":
            groups = issue.get("details", {}).get("duplicate_column_groups", [])
            redundant = any(
                location_file == source_file and frozenset(map(str, group)) == pair
                for source_file, pair in semantic_pairs
                for group in groups
            )
        elif test == "feature_matrix_near_identical_columns":
            pairs = issue.get("details", {}).get("pairs", [])
            redundant = any(
                location_file == source_file
                and frozenset({str(item.get("col_a", "")), str(item.get("col_b", ""))}) == pair
                for source_file, pair in semantic_pairs
                for item in pairs
            )
        if not redundant:
            filtered.append(issue)
    return filtered
