from __future__ import annotations

import argparse
import copy
import contextlib
import hashlib
import io
import json
import math
import os
import re
import sys
import threading
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any
from collections.abc import Iterable

try:
    from modules.source_data_patterns import detect_source_data_patterns
except ModuleNotFoundError:
    # Allow direct CLI execution in addition to ``python -m``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from modules.source_data_patterns import detect_source_data_patterns


AUDIT_VERSION = "8"

_AUDIT_CACHE_DIR = Path(os.environ.get(
    "SOURCE_DATA_AUDIT_CACHE_DIR",
    str(Path(__file__).resolve().parents[2] / "data" / "output" / "review_v2" / "source_data_audit_cache"),
))
_AUDIT_CACHE_LOCKS: dict[str, threading.Lock] = {}
_AUDIT_CACHE_LOCKS_GUARD = threading.Lock()

DATA_EXTS = {".xlsx", ".xls", ".csv"}
MAX_ARCHIVE_DATA_FILES = 100
MAX_ARCHIVE_MEMBER_BYTES = 200 * 1024 * 1024
_METADATA_FILENAMES = {"resources.csv", "figures.csv", "manifest.csv", "resources.tsv"}
MIN_MATRIX_ROWS = 3
MIN_MATRIX_COLS = 2
MIN_HIGH_RISK_CELLS = 15
MIN_NEAR_REUSE_SERIES_VALUES = 10
MAX_SMALL_BLOCK_ROWS = 30
MAX_SMALL_BLOCK_COLS = 12
# Exhaustively enumerating every rectangular sub-window is useful for ordinary
# source-data sheets, but becomes combinatorial for large proteomics exports
# containing thousands of short numeric blocks.  In that case the complete
# blocks still preserve the cross-sheet reuse signal and keep review latency
# bounded.
MAX_BLOCKS_FOR_EXHAUSTIVE_SUBWINDOWS = 200
MAX_MATRIX_WINDOWS_PER_SHEET = 1_000
MAX_MATRIX_CELLS_PER_SHEET = 2_000_000
MAX_MATRIX_BLOCK_AREA = 100_000
MAX_ROWS_FOR_SINGLE_COLUMN_WINDOWS = 5_000
MIN_NEAR_REUSE_DECIMAL_PLACES = 4
MIN_NEAR_REUSE_PRECISION_FRACTION = 0.80
MIN_NEAR_REUSE_CHANGED_FRACTION = 0.80
MIN_NEAR_REUSE_NONZERO_PREFIX_FRACTION = 0.80
NEAR_REUSE_PREFIX_DECIMALS = 2
NEAR_REUSE_MAX_ABS_DIFF = 0.01
MIN_DIGIT_EDIT_MATCH_FRACTION = 0.90
MIN_DIGIT_EDIT_CHANGED_FRACTION = 0.80
MAX_DIGIT_EDIT_TOKEN_LENGTH = 24
MAX_DIGIT_EDITS_PER_VALUE = 2


@dataclass(frozen=True)
class MatrixWindow:
    file: str
    sheet: str
    figure_label: str
    row_start: int
    row_end: int
    col_start: int
    col_end: int
    values: tuple[tuple[float, ...], ...]
    row_labels: tuple[str, ...]
    column_context: tuple[str, ...]

    @property
    def n_rows(self) -> int:
        return self.row_end - self.row_start + 1

    @property
    def n_cols(self) -> int:
        return self.col_end - self.col_start + 1

    @property
    def n_values(self) -> int:
        return self.n_rows * self.n_cols


@dataclass(frozen=True)
class DataSource:
    name: str
    suffix: str
    payload: Path | bytes


def _audit_cache_key(base: Path) -> str:
    """Identify deterministic audit inputs independently of task/file IDs."""
    candidates = [
        path for path in base.rglob("*")
        if path.is_file()
        and (
            path.suffix.lower() in DATA_EXTS | {".zip"}
            or (path.parent == base and path.name in {"article.html", "main_paper_text.txt"})
        )
    ]
    first_pdf = next(iter(sorted(base.glob("*.pdf"))), None)
    if first_pdf is not None:
        candidates.append(first_pdf)
    digest = hashlib.sha256(f"source-data-audit:{AUDIT_VERSION}\n".encode())
    for path in sorted(set(candidates), key=lambda item: item.relative_to(base).as_posix()):
        digest.update(path.relative_to(base).as_posix().encode() + b"\0")
        digest.update(str(path.stat().st_size).encode() + b"\0")
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest() if candidates else ""


def _audit_cache_lock(key: str) -> threading.Lock:
    with _AUDIT_CACHE_LOCKS_GUARD:
        return _AUDIT_CACHE_LOCKS.setdefault(key, threading.Lock())


def _cached_audit_for_request(result: dict, base: Path, doi: str) -> dict:
    adapted = copy.deepcopy(result)
    adapted["doi"] = doi
    adapted["input_dir"] = str(base)
    return adapted


@contextlib.contextmanager
def _audit_file_lock(cache_path: Path):
    """Serialize identical audits across API processes as well as threads."""
    lock_file = None
    try:
        import fcntl
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = cache_path.with_suffix(cache_path.suffix + ".lock").open("a+b")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError):
        lock_file = None
    try:
        yield
    finally:
        if lock_file is not None:
            try:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            lock_file.close()


def audit_source_data(input_dir: str | Path, doi: str = "") -> dict:
    """Run the deterministic audit, sharing only evidence for byte-identical inputs.

    Risk verdicts and LLM review stages are deliberately not cached.  The lock
    prevents concurrent API submissions of the same material from repeating a
    CPU-heavy workbook audit while preserving fresh end-to-end reviews.
    """
    base = Path(input_dir)
    try:
        key = _audit_cache_key(base)
    except OSError:
        key = ""
    if not key:
        return _audit_source_data_uncached(base, doi)

    cache_path = _AUDIT_CACHE_DIR / f"{key}.json"
    with _audit_cache_lock(key):
        with _audit_file_lock(cache_path):
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("version") == AUDIT_VERSION:
                    return _cached_audit_for_request(cached, base, doi)
            except (OSError, ValueError, TypeError):
                pass

            result = _audit_source_data_uncached(base, doi)
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = cache_path.with_name(
                    f".{cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                temporary.write_text(
                    json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str),
                    encoding="utf-8",
                )
                os.replace(temporary, cache_path)
            except OSError:
                pass
            return result


def _audit_source_data_uncached(input_dir: str | Path, doi: str = "") -> dict:
    base = Path(input_dir)
    data_files = _list_data_files(base)
    windows: list[MatrixWindow] = []
    n_mismatches: list[dict] = []
    exact_series_reuse: list[dict] = []
    same_sheet_channel_duplicates: list[dict] = []
    fixed_ratio_series: list[dict] = []
    semantic_transform_conflicts: list[dict] = []
    text = _combined_article_text(base)

    for source in data_files:
        try:
            sheets = _read_workbook(source)
        except Exception:
            continue
        rel = source.name
        for sheet, grid in sheets.items():
            windows.extend(_matrix_windows(rel, sheet, grid))
            windows.extend(_single_column_windows(rel, sheet, grid))
            patterns = detect_source_data_patterns(rel, sheet, grid)
            exact_series_reuse.extend(patterns["same_sheet_exact_series_reuse"])
            same_sheet_channel_duplicates.extend(patterns["same_sheet_channel_duplicate"])
            fixed_ratio_series.extend(patterns["fixed_ratio_series"])
            semantic_transform_conflicts.extend(patterns["semantic_transform_conflicts"])
            declared_n = _declared_n_for_sheet(sheet, text)
            if declared_n:
                n_mismatches.extend(_sheet_n_mismatches(rel, sheet, grid, declared_n))

    matrix_reuse = _find_matrix_reuse(windows)
    near_modified_reuse = _find_near_modified_matrix_reuse(windows)
    digit_edit_reuse = _find_digit_edit_matrix_reuse(windows)
    digit_edit_reuse = [
        item for item in digit_edit_reuse
        if not any(_matrix_finding_contains(near, item) for near in near_modified_reuse)
    ]
    high_findings = []
    for item in matrix_reuse:
        if item.get("severity") == "high":
            high_findings.append({
                "test": "rectangular_matrix_reuse",
                "severity": "high",
                "location": item["location"],
                "description": item["description"],
                "n_values": item["n_values"],
                "sample": item.get("sample", []),
            })
    for item in near_modified_reuse:
        high_findings.append({
            "test": "near_modified_matrix_reuse",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "n_values": item["n_values"],
            "matching_prefix_count": item["matching_prefix_count"],
            "changed_count": item["changed_count"],
            "max_abs_diff": item["max_abs_diff"],
            "sample_pairs": item.get("sample_pairs", []),
        })
    for item in digit_edit_reuse:
        high_findings.append({
            "test": "digit_edit_matrix_reuse",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "n_values": item["n_values"],
            "matched_count": item["matched_count"],
            "changed_count": item["changed_count"],
            "operation_counts": item["operation_counts"],
            "position_counts": item["position_counts"],
            "edit_count_distribution": item["edit_count_distribution"],
            "max_edits_per_value": item["max_edits_per_value"],
            "sample_pairs": item.get("sample_pairs", []),
        })
    for item in n_mismatches:
        high_findings.append({
            "test": "figure_n_mismatch",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "declared_n": item["declared_n"],
            "source_n": item["source_n"],
        })
    for item in exact_series_reuse:
        if item.get("severity") != "high":
            continue
        high_findings.append({
            "test": "same_sheet_exact_series_reuse",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "n_values": item["n_values"],
            "sample": item.get("sample", []),
            "left": item.get("left", {}),
            "right": item.get("right", {}),
        })
    for item in same_sheet_channel_duplicates:
        if item.get("severity") != "high":
            continue
        high_findings.append({
            "test": "same_sheet_channel_duplicate",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "n_values": item.get("details", {}).get("n_values", 0),
            "sample": item.get("details", {}).get("sample_values", []),
            "details": item.get("details", {}),
        })
    for item in fixed_ratio_series:
        high_findings.append({
            "test": "fixed_ratio_series",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "n_values": item["n_values"],
            "ratio": item["ratio"],
            "matched_count": item["matched_count"],
            "total_compared": item["total_compared"],
            "sample": item.get("sample", []),
        })
    for item in semantic_transform_conflicts:
        high_findings.append({
            "test": "semantic_transform_conflict",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "details": item.get("details", {}),
        })

    return {
        "version": AUDIT_VERSION,
        "doi": doi,
        "input_dir": str(base),
        "data_file_count": len(data_files),
        "matrix_window_count": len(windows),
        "high_findings": high_findings,
        "rectangular_matrix_reuse": matrix_reuse,
        "near_modified_matrix_reuse": near_modified_reuse,
        "digit_edit_matrix_reuse": digit_edit_reuse,
        "figure_n_mismatch": n_mismatches,
        "same_sheet_exact_series_reuse": exact_series_reuse,
        "same_sheet_channel_duplicate": same_sheet_channel_duplicates,
        "fixed_ratio_series": fixed_ratio_series,
        "semantic_transform_conflicts": semantic_transform_conflicts,
    }


def _list_data_files(base: Path) -> list[DataSource]:
    if not base.exists():
        return []
    sources = [
        DataSource(name=_relpath(path, base), suffix=path.suffix.lower(), payload=path)
        for path in sorted(base.rglob("*"))
        if path.is_file()
        and path.suffix.lower() in DATA_EXTS
        and not path.name.startswith("~$")
        and path.name.lower() not in _METADATA_FILENAMES
    ]
    for archive in sorted(base.rglob("*.zip")):
        try:
            with zipfile.ZipFile(archive) as zf:
                members = [
                    info for info in zf.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in DATA_EXTS
                    and not Path(info.filename).name.startswith("~$")
                    and Path(info.filename).name.lower() not in _METADATA_FILENAMES
                    and info.file_size <= MAX_ARCHIVE_MEMBER_BYTES
                ][:MAX_ARCHIVE_DATA_FILES]
                for info in members:
                    sources.append(DataSource(
                        name=f"{_relpath(archive, base)}!/{info.filename}",
                        suffix=Path(info.filename).suffix.lower(),
                        payload=zf.read(info),
                    ))
        except (OSError, zipfile.BadZipFile):
            continue
    return sources


def _read_workbook(source: DataSource) -> dict[str, list[list[Any]]]:
    import pandas as pd

    payload = io.BytesIO(source.payload) if isinstance(source.payload, bytes) else source.payload
    if source.suffix == ".csv":
        df = pd.read_csv(payload, header=None, dtype=object)
        return {Path(source.name).stem[:31] or "csv": _df_to_grid(df)}

    sheets: dict[str, list[list[Any]]] = {}
    engine = "xlrd" if source.suffix == ".xls" else "openpyxl"
    xl = pd.ExcelFile(payload, engine=engine)
    try:
        for sheet in xl.sheet_names:
            df = pd.read_excel(xl, sheet_name=sheet, header=None, dtype=object)
            sheets[str(sheet)] = _df_to_grid(df)
    finally:
        xl.close()
    return sheets


def _df_to_grid(df) -> list[list[Any]]:
    rows = []
    for row in df.where(df.notna(), None).itertuples(index=False, name=None):
        rows.append(list(row))
    return rows


def _is_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        x = float(value)
    except Exception:
        return False
    return math.isfinite(x)


def _num(value: Any) -> float:
    x = round(float(value), 10)
    return 0.0 if x == -0.0 else x


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return re.sub(r"\s+", " ", text)


def _figure_label(sheet: str) -> str:
    s = str(sheet or "")
    m = re.search(r"extended\s+data\s+fig(?:ure)?\.?\s*(\d+[a-z]?)", s, re.I)
    if m:
        return f"extended data fig {m.group(1).lower()}"
    m = re.search(r"fig(?:ure)?\.?\s*(\d+[a-z]?)", s, re.I)
    if m:
        return f"fig {m.group(1).lower()}"
    return s.strip().lower()


def _figure_marker(value: Any) -> str:
    text = _cell_text(value)
    compact = re.sub(r"\s+", " ", text)
    patterns = (
        (r"^(?:extended\s+data|ed)\s*fig(?:ure)?\.?\s*(\d+)\s*([a-z])\b", "Extended Data Fig."),
        (r"^(?:supplementary|supp)\s*fig(?:ure)?\.?\s*(\d+)\s*([a-z])\b", "Supplementary Fig."),
        (r"^fig(?:ure)?\.?\s*(\d+)\s*([a-z])\b", "Fig."),
    )
    for pattern, prefix in patterns:
        match = re.search(pattern, compact, re.I)
        if match:
            return f"{prefix}{match.group(1)}{match.group(2).lower()}"
    return ""


def _nearest_figure_label(grid: list[list[Any]], row: int, fallback: str) -> str:
    for ri in range(row, -1, -1):
        for value in grid[ri] if ri < len(grid) else []:
            marker = _figure_marker(value)
            if marker:
                return marker
    return fallback


def _figure_labels_by_row(grid: list[list[Any]], fallback: str) -> list[str]:
    """Resolve nearest figure labels once instead of rescanning to row zero.

    Large workbooks can contain tens of thousands of rows and thousands of
    numeric blocks. Calling ``_nearest_figure_label`` per block made this step
    quadratic even after candidate-window counts were bounded.
    """
    labels = []
    current = fallback
    for row in grid:
        for value in row:
            marker = _figure_marker(value)
            if marker:
                current = marker
        labels.append(current)
    return labels


def _row_numeric_runs(row: list[Any], min_cols: int = MIN_MATRIX_COLS) -> list[tuple[int, int]]:
    runs = []
    start = None
    for idx, value in enumerate(row + [None]):
        if idx < len(row) and _is_number(value):
            if start is None:
                start = idx
        elif start is not None:
            end = idx - 1
            if end - start + 1 >= min_cols:
                runs.append((start, end))
            start = None
    return runs


def _numeric_blocks(grid: list[list[Any]]) -> list[tuple[int, int, int, int]]:
    active: dict[tuple[int, int], int] = {}
    blocks: list[tuple[int, int, int, int]] = []
    for ri, row in enumerate(grid):
        runs = set(_row_numeric_runs(row))
        for span, start_row in list(active.items()):
            if span not in runs:
                if ri - start_row >= MIN_MATRIX_ROWS:
                    blocks.append((start_row, ri - 1, span[0], span[1]))
                del active[span]
        for span in runs:
            active.setdefault(span, ri)
    last = len(grid)
    for span, start_row in active.items():
        if last - start_row >= MIN_MATRIX_ROWS:
            blocks.append((start_row, last - 1, span[0], span[1]))
    return blocks


def _matrix_windows(file_name: str, sheet: str, grid: list[list[Any]]) -> list[MatrixWindow]:
    out: list[MatrixWindow] = []
    seen: set[tuple[int, int, int, int]] = set()
    blocks = _numeric_blocks(grid)
    fallback_label = _figure_label(sheet)
    row_figure_labels = _figure_labels_by_row(grid, fallback_label)
    exhaustive = (
        len(blocks) <= 50
        and max(
            ((block[1] - block[0] + 1) * (block[3] - block[2] + 1) for block in blocks),
            default=0,
        ) <= 1_000
    )
    # Under a budget, prefer larger blocks because they carry stronger reuse
    # evidence.  Tie breakers retain deterministic output across runs.
    if len(blocks) > MAX_MATRIX_WINDOWS_PER_SHEET:
        blocks = sorted(
            blocks,
            key=lambda block: (
                -((block[1] - block[0] + 1) * (block[3] - block[2] + 1)),
                block,
            ),
        )[:MAX_MATRIX_WINDOWS_PER_SHEET]
    cells_used = 0
    for r0, r1, c0, c1 in blocks:
        h = r1 - r0 + 1
        w = c1 - c0 + 1
        area = h * w
        if area > MAX_MATRIX_BLOCK_AREA or cells_used + area > MAX_MATRIX_CELLS_PER_SHEET:
            continue
        candidates = {(r0, r1, c0, c1)}
        if exhaustive and h <= MAX_SMALL_BLOCK_ROWS and w <= MAX_SMALL_BLOCK_COLS:
            for hh in range(MIN_MATRIX_ROWS, h + 1):
                for ww in range(MIN_MATRIX_COLS, w + 1):
                    if hh * ww < MIN_HIGH_RISK_CELLS:
                        continue
                    for rr in range(r0, r1 - hh + 2):
                        for cc in range(c0, c1 - ww + 2):
                            candidates.add((rr, rr + hh - 1, cc, cc + ww - 1))
        for rr0, rr1, cc0, cc1 in candidates:
            if (rr0, rr1, cc0, cc1) in seen:
                continue
            seen.add((rr0, rr1, cc0, cc1))
            values = _slice_values(grid, rr0, rr1, cc0, cc1)
            if not values:
                continue
            out.append(MatrixWindow(
                file=file_name,
                sheet=sheet,
                figure_label=(
                    row_figure_labels[rr0]
                    if 0 <= rr0 < len(row_figure_labels)
                    else fallback_label
                ),
                row_start=rr0 + 1,
                row_end=rr1 + 1,
                col_start=cc0 + 1,
                col_end=cc1 + 1,
                values=values,
                row_labels=_row_labels(grid, rr0, rr1, cc0),
                column_context=_column_context(grid, rr0, cc0, cc1),
            ))
            cells_used += (rr1 - rr0 + 1) * (cc1 - cc0 + 1)
    return out


def _single_column_windows(file_name: str, sheet: str, grid: list[list[Any]]) -> list[MatrixWindow]:
    """Build candidate series for experimental groups separated by blank columns."""
    # Very large omics exports are row-wise records rather than chart-ready
    # experimental series. Building every suffix-length window from a long
    # numeric column is quadratic and does not add valid group-level evidence.
    if len(grid) > MAX_ROWS_FOR_SINGLE_COLUMN_WINDOWS:
        return []
    out = []
    fallback_label = _figure_label(sheet)
    # Resolve figure markers once for the whole sheet.  Calling
    # ``_nearest_figure_label`` for every candidate column/window repeatedly
    # rescanned all preceding rows and cells; on short-but-very-wide CSV files
    # that became the dominant (effectively quadratic) cost.
    row_figure_labels = _figure_labels_by_row(grid, fallback_label)
    max_columns = max((len(row) for row in grid), default=0)
    for column in range(max_columns):
        start = None
        for row in range(len(grid) + 1):
            is_numeric = row < len(grid) and column < len(grid[row]) and _is_number(grid[row][column])
            if is_numeric and start is None:
                start = row
                continue
            if is_numeric or start is None:
                continue
            end = row - 1
            run_length = end - start + 1
            if run_length >= MIN_NEAR_REUSE_SERIES_VALUES:
                lengths = {run_length}
                if run_length <= MAX_SMALL_BLOCK_ROWS:
                    lengths.update(range(MIN_NEAR_REUSE_SERIES_VALUES, run_length + 1))
                for length in lengths:
                    for row_start in range(start, end - length + 2):
                        row_end = row_start + length - 1
                        values = _slice_values(grid, row_start, row_end, column, column)
                        if not values:
                            continue
                        out.append(MatrixWindow(
                            file=file_name,
                            sheet=sheet,
                            figure_label=(
                                row_figure_labels[row_start]
                                if 0 <= row_start < len(row_figure_labels)
                                else fallback_label
                            ),
                            row_start=row_start + 1,
                            row_end=row_end + 1,
                            col_start=column + 1,
                            col_end=column + 1,
                            values=values,
                            row_labels=_row_labels(grid, row_start, row_end, column),
                            column_context=_direct_column_context(grid, row_start, column),
                        ))
            start = None
    return out


def _slice_values(grid: list[list[Any]], r0: int, r1: int, c0: int, c1: int) -> tuple[tuple[float, ...], ...] | None:
    rows = []
    for ri in range(r0, r1 + 1):
        row = grid[ri]
        vals = []
        for ci in range(c0, c1 + 1):
            if ci >= len(row) or not _is_number(row[ci]):
                return None
            vals.append(_num(row[ci]))
        rows.append(tuple(vals))
    if len({v for row in rows for v in row}) <= 1:
        return None
    return tuple(rows)


def _row_labels(grid: list[list[Any]], r0: int, r1: int, c0: int) -> tuple[str, ...]:
    labels = []
    label_col = c0 - 1
    if label_col < 0:
        return tuple()
    for ri in range(r0, r1 + 1):
        row = grid[ri]
        labels.append(_cell_text(row[label_col]) if label_col < len(row) else "")
    return tuple(x for x in labels if x)


def _column_context(grid: list[list[Any]], r0: int, c0: int, c1: int) -> tuple[str, ...]:
    items = []
    for ri in range(max(0, r0 - 3), r0):
        row = grid[ri]
        for ci in range(max(0, c0 - 1), min(c1 + 2, len(row))):
            text = _cell_text(row[ci])
            if text and not _is_number(text):
                items.append(text)
    return tuple(dict.fromkeys(items))


def _direct_column_context(grid: list[list[Any]], r0: int, column: int) -> tuple[str, ...]:
    items = []
    for row in range(max(0, r0 - 3), r0):
        value = grid[row][column] if column < len(grid[row]) else None
        text = _cell_text(value)
        if text and not _is_number(text):
            items.append(text)
    return tuple(dict.fromkeys(items))


def _find_matrix_reuse(windows: list[MatrixWindow]) -> list[dict]:
    by_values: dict[tuple[tuple[float, ...], ...], list[MatrixWindow]] = {}
    for window in windows:
        if window.n_cols >= MIN_MATRIX_COLS and window.n_values >= MIN_HIGH_RISK_CELLS:
            by_values.setdefault(window.values, []).append(window)

    findings = []
    for values, matches in by_values.items():
        if len(matches) < 2:
            continue
        locations: dict[tuple[str, str], list[MatrixWindow]] = {}
        for match in matches:
            locations.setdefault((match.file, match.sheet), []).append(match)
        location_groups = list(locations.values())
        for group_index, left_group in enumerate(location_groups):
            for right_group in location_groups[group_index + 1:]:
                for left in left_group:
                    for right in right_group:
                        if left.figure_label == right.figure_label and left.figure_label:
                            continue
                        finding = _matrix_reuse_finding(left, right, values)
                        if finding:
                            findings.append(finding)

    findings.sort(key=lambda item: (item["n_values"], item["n_rows"], item["n_cols"]), reverse=True)
    deduped = []
    for item in findings:
        if any(_matrix_finding_contains(parent, item) for parent in deduped):
            continue
        deduped.append(item)
        if len(deduped) >= 30:
            break
    return deduped


def _decimal_places(value: float) -> int:
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _truncated_prefix(value: float, decimals: int = NEAR_REUSE_PREFIX_DECIMALS) -> int:
    scale = 10 ** decimals
    epsilon = math.copysign(1e-8, float(value))
    return math.trunc(float(value) * scale + epsilon)


def _near_reuse_key(window: MatrixWindow) -> tuple[tuple[int, ...], ...]:
    return tuple(
        tuple(_truncated_prefix(value) for value in row)
        for row in window.values
    )


def _find_near_modified_matrix_reuse(windows: list[MatrixWindow]) -> list[dict]:
    """Find matrices whose leading decimals match while later digits are rewritten."""
    by_prefix: dict[tuple[tuple[int, ...], ...], list[MatrixWindow]] = {}
    for window in windows:
        minimum = MIN_NEAR_REUSE_SERIES_VALUES if window.n_cols == 1 else MIN_HIGH_RISK_CELLS
        if window.n_values < minimum:
            continue
        values = [value for row in window.values for value in row]
        precise = sum(_decimal_places(value) >= MIN_NEAR_REUSE_DECIMAL_PLACES for value in values)
        if precise / len(values) < MIN_NEAR_REUSE_PRECISION_FRACTION:
            continue
        prefix_key = _near_reuse_key(window)
        informative = sum(prefix != 0 for row in prefix_key for prefix in row)
        if informative / len(values) < MIN_NEAR_REUSE_NONZERO_PREFIX_FRACTION:
            continue
        by_prefix.setdefault(prefix_key, []).append(window)

    findings = []
    for matches in by_prefix.values():
        if len(matches) < 2:
            continue
        contexts: dict[tuple[str, str, str], list[MatrixWindow]] = {}
        for match in matches:
            contexts.setdefault(
                (match.file, match.sheet, match.figure_label), []
            ).append(match)
        context_groups = list(contexts.values())
        for group_index, left_group in enumerate(context_groups):
            for right_group in context_groups[group_index + 1:]:
                for left in left_group:
                    for right in right_group:
                        if left.n_cols == 1 and not _compatible_series_context(left, right):
                            continue
                        left_values = [value for row in left.values for value in row]
                        right_values = [value for row in right.values for value in row]
                        differences = [abs(a - b) for a, b in zip(left_values, right_values)]
                        changed_count = sum(diff > 1e-10 for diff in differences)
                        if changed_count / len(differences) < MIN_NEAR_REUSE_CHANGED_FRACTION:
                            continue
                        if max(differences) >= NEAR_REUSE_MAX_ABS_DIFF:
                            continue
                        finding = _near_modified_matrix_reuse_finding(left, right, differences, changed_count)
                        if finding:
                            findings.append(finding)

    findings.sort(key=lambda item: (item["n_values"], item["n_rows"], item["n_cols"]), reverse=True)
    deduped = []
    for item in findings:
        if any(_matrix_finding_contains(parent, item) for parent in deduped):
            continue
        deduped.append(item)
        if len(deduped) >= 30:
            break
    return deduped


def _compatible_series_context(left: MatrixWindow, right: MatrixWindow) -> bool:
    if left.row_labels != right.row_labels or not left.row_labels:
        return False
    ignored = {
        "no.of neuron", "no. of neuron", "number", "sample", "replicate",
        "total dendritic length (7 div, 100μm)",
    }
    left_context = {item.strip().lower() for item in left.column_context if item.strip().lower() not in ignored}
    right_context = {item.strip().lower() for item in right.column_context if item.strip().lower() not in ignored}
    return bool(left_context & right_context)


def _numeric_token(value: float) -> str:
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _digit_deletion_variants(token: str, max_deletions: int = MAX_DIGIT_EDITS_PER_VALUE) -> set[str]:
    variants = {token}
    digit_positions = [index for index, char in enumerate(token) if char.isdigit()]
    for count in range(1, min(max_deletions, len(digit_positions)) + 1):
        for removed in combinations(digit_positions, count):
            removed_set = set(removed)
            variants.add("".join(char for index, char in enumerate(token) if index not in removed_set))
    return variants


def _digit_position(token: str, char_index: int) -> str:
    decimal_index = token.find(".")
    if decimal_index < 0 or char_index < decimal_index:
        return "integer"
    fractional_digits = [
        index for index in range(decimal_index + 1, len(token))
        if token[index].isdigit()
    ]
    if not fractional_digits or char_index <= fractional_digits[0]:
        return "decimal_first"
    if char_index >= fractional_digits[-1]:
        return "decimal_last"
    return "decimal_middle"


def _digit_edit(left_value: float, right_value: float, max_edits: int = MAX_DIGIT_EDITS_PER_VALUE) -> dict | None:
    left = _numeric_token(left_value)
    right = _numeric_token(right_value)
    if len(left) > MAX_DIGIT_EDIT_TOKEN_LENGTH or len(right) > MAX_DIGIT_EDIT_TOKEN_LENGTH:
        return None
    if left == right:
        return {
            "operation": "exact", "position": "none", "edit_count": 0,
            "operations": [], "positions": [], "left": left, "right": right,
        }
    if left.startswith("-") != right.startswith("-"):
        return None
    if abs(len(left) - len(right)) > max_edits:
        return None

    @lru_cache(maxsize=None)
    def align(left_index: int, right_index: int, remaining: int):
        if left_index == len(left) and right_index == len(right):
            return ()
        if remaining < 0:
            return None
        if left_index < len(left) and right_index < len(right) and left[left_index] == right[right_index]:
            return align(left_index + 1, right_index + 1, remaining)
        if remaining == 0:
            return None

        candidates = []
        if (
            left_index < len(left) and right_index < len(right)
            and left[left_index].isdigit() and right[right_index].isdigit()
        ):
            tail = align(left_index + 1, right_index + 1, remaining - 1)
            if tail is not None:
                candidates.append((("substitute", _digit_position(left, left_index)),) + tail)
        if left_index < len(left) and left[left_index].isdigit():
            tail = align(left_index + 1, right_index, remaining - 1)
            if tail is not None:
                candidates.append((("delete", _digit_position(left, left_index)),) + tail)
        if right_index < len(right) and right[right_index].isdigit():
            tail = align(left_index, right_index + 1, remaining - 1)
            if tail is not None:
                candidates.append((("insert", _digit_position(right, right_index)),) + tail)
        if not candidates:
            return None
        return min(candidates, key=lambda script: (len(script), script))

    script = align(0, 0, max_edits)
    if script is None or not script:
        return None
    operations = [item[0] for item in script]
    positions = [item[1] for item in script]
    return {
        "operation": "+".join(operations),
        "position": "+".join(positions),
        "edit_count": len(script),
        "operations": operations,
        "positions": positions,
        "left": left, "right": right,
    }


def _single_digit_edit(left_value: float, right_value: float) -> dict | None:
    """Backward-compatible one-edit helper used by older integrations."""
    return _digit_edit(left_value, right_value, max_edits=1)


def _digit_edit_anchor_positions(window: MatrixWindow) -> list[int]:
    flat = [value for row in window.values for value in row]
    precise = [index for index, value in enumerate(flat) if _decimal_places(value) >= MIN_NEAR_REUSE_DECIMAL_PLACES]
    if not precise:
        return []
    return sorted({precise[0], precise[len(precise) // 2], precise[-1]})


def _find_digit_edit_matrix_reuse(windows: list[MatrixWindow]) -> list[dict]:
    """Find copied matrices changed by up to two digit edits per value."""
    eligible = []
    variant_index: dict[tuple[int, int, int, int, str], list[int]] = {}
    for window in windows:
        minimum = MIN_NEAR_REUSE_SERIES_VALUES if window.n_cols == 1 else MIN_HIGH_RISK_CELLS
        if window.n_values < minimum:
            continue
        values = [value for row in window.values for value in row]
        precise = sum(_decimal_places(value) >= MIN_NEAR_REUSE_DECIMAL_PLACES for value in values)
        if precise / len(values) < MIN_NEAR_REUSE_PRECISION_FRACTION:
            continue
        window_id = len(eligible)
        eligible.append(window)
        for anchor in _digit_edit_anchor_positions(window):
            token = _numeric_token(values[anchor])
            for variant in _digit_deletion_variants(token):
                key = (window.n_rows, window.n_cols, anchor, len(variant), variant)
                variant_index.setdefault(key, []).append(window_id)

    candidate_pairs = set()
    for matches in variant_index.values():
        if len(matches) < 2 or len(matches) > 200:
            continue
        unique = sorted(set(matches))
        for index, left_id in enumerate(unique):
            for right_id in unique[index + 1:]:
                candidate_pairs.add((left_id, right_id))

    findings = []
    for left_id, right_id in candidate_pairs:
        left, right = eligible[left_id], eligible[right_id]
        if (left.file, left.sheet, left.row_start, left.row_end, left.col_start, left.col_end) == (
            right.file, right.sheet, right.row_start, right.row_end, right.col_start, right.col_end
        ):
            continue
        if (left.file, left.sheet) == (right.file, right.sheet) and left.figure_label == right.figure_label:
            continue
        if left.n_cols == 1 and not _compatible_series_context(left, right):
            continue
        finding = _digit_edit_matrix_reuse_finding(left, right)
        if finding:
            findings.append(finding)

    findings.sort(key=lambda item: (item["matched_count"], item["n_values"]), reverse=True)
    deduped = []
    for item in findings:
        if any(_matrix_finding_contains(parent, item) for parent in deduped):
            continue
        deduped.append(item)
        if len(deduped) >= 30:
            break
    return deduped


def _digit_edit_matrix_reuse_finding(left: MatrixWindow, right: MatrixWindow) -> dict | None:
    left_values = [value for row in left.values for value in row]
    right_values = [value for row in right.values for value in row]
    edits = [_digit_edit(a, b) for a, b in zip(left_values, right_values)]
    matched = [edit for edit in edits if edit]
    changed = [edit for edit in matched if edit["operation"] != "exact"]
    if len(matched) / len(edits) < MIN_DIGIT_EDIT_MATCH_FRACTION:
        return None
    if len(changed) / len(edits) < MIN_DIGIT_EDIT_CHANGED_FRACTION:
        return None

    operation_counts = {
        operation: sum(operation in edit.get("operations", []) for edit in changed)
        for operation in ("substitute", "insert", "delete")
    }
    position_counts = {
        position: sum(position in edit.get("positions", []) for edit in changed)
        for position in ("integer", "decimal_first", "decimal_middle", "decimal_last")
    }
    edit_count_distribution = {
        str(count): sum(edit.get("edit_count") == count for edit in changed)
        for count in range(1, MAX_DIGIT_EDITS_PER_VALUE + 1)
    }
    location = (
        f"{left.file} / {left.sheet} / {left.figure_label} "
        f"R{left.row_start}:R{left.row_end} C{left.col_start}:C{left.col_end} vs "
        f"{right.file} / {right.sheet} / {right.figure_label} "
        f"R{right.row_start}:R{right.row_end} C{right.col_start}:C{right.col_end}"
    )
    operations = "、".join(
        f"{name} {operation_counts[key]} 个"
        for key, name in (("substitute", "替换"), ("insert", "插入"), ("delete", "删除"))
        if operation_counts[key]
    )
    positions = "、".join(
        f"{name} {position_counts[key]} 个"
        for key, name in (
            ("integer", "整数位"),
            ("decimal_first", "小数首位"),
            ("decimal_middle", "小数中间位"),
            ("decimal_last", "小数末位"),
        )
        if position_counts[key]
    )
    description = (
        f"{left.figure_label} 与 {right.figure_label} 的 {left.n_rows}x{left.n_cols} 对应区域中，"
        f"{len(matched)}/{len(edits)} 个数值数字串完全相同或最多相差 {MAX_DIGIT_EDITS_PER_VALUE} 次数字编辑，"
        f"其中 {len(changed)}/{len(edits)} 个确有改动；操作包括 {operations}，位置包括 {positions}。"
    )
    return {
        "severity": "high",
        "location": location,
        "description": description,
        "n_values": len(edits),
        "n_rows": left.n_rows,
        "n_cols": left.n_cols,
        "matched_count": len(matched),
        "changed_count": len(changed),
        "operation_counts": operation_counts,
        "position_counts": position_counts,
        "edit_count_distribution": edit_count_distribution,
        "max_edits_per_value": MAX_DIGIT_EDITS_PER_VALUE,
        "sample_pairs": changed[:6],
        "left": _window_payload(left),
        "right": _window_payload(right),
    }


def _near_modified_matrix_reuse_finding(
    left: MatrixWindow, right: MatrixWindow, differences: list[float], changed_count: int
) -> dict:
    location = (
        f"{left.file} / {left.sheet} / {left.figure_label} "
        f"R{left.row_start}:R{left.row_end} C{left.col_start}:C{left.col_end} vs "
        f"{right.file} / {right.sheet} / {right.figure_label} "
        f"R{right.row_start}:R{right.row_end} C{right.col_start}:C{right.col_end}"
    )
    pairs = []
    for left_row, right_row in zip(left.values, right.values):
        for left_value, right_value in zip(left_row, right_row):
            pairs.append({"left": left_value, "right": right_value})
            if len(pairs) >= 6:
                break
        if len(pairs) >= 6:
            break
    description = (
        f"{left.figure_label} 与 {right.figure_label} 的 {left.n_rows}x{left.n_cols} 对应测量矩阵中，"
        f"{left.n_values}/{left.n_values} 个值截断到小数点后 {NEAR_REUSE_PREFIX_DECIMALS} 位完全相同，"
        f"但 {changed_count}/{left.n_values} 个原始值的后续小数位被轻微改写；"
        f"最大绝对差为 {max(differences):.6g}，平均绝对差为 {sum(differences) / len(differences):.6g}。"
    )
    return {
        "severity": "high",
        "location": location,
        "description": description,
        "n_values": left.n_values,
        "n_rows": left.n_rows,
        "n_cols": left.n_cols,
        "matching_prefix_count": left.n_values,
        "changed_count": changed_count,
        "prefix_decimals": NEAR_REUSE_PREFIX_DECIMALS,
        "max_abs_diff": max(differences),
        "mean_abs_diff": sum(differences) / len(differences),
        "sample_pairs": pairs,
        "left": _window_payload(left),
        "right": _window_payload(right),
    }


def _matrix_finding_contains(parent: dict, child: dict) -> bool:
    for side in ("left", "right"):
        if parent[side]["location_id"] != child[side]["location_id"]:
            return False
        pr = parent[side]["row_range"]
        pc = parent[side]["col_range"]
        cr = child[side]["row_range"]
        cc = child[side]["col_range"]
        if not (pr[0] <= cr[0] and cr[1] <= pr[1] and pc[0] <= cc[0] and cc[1] <= pc[1]):
            return False
    return True


def _matrix_reuse_finding(left: MatrixWindow, right: MatrixWindow, values) -> dict | None:
    n_values = left.n_values
    if n_values < MIN_HIGH_RISK_CELLS:
        return None
    loc = (
        f"{left.file} / {left.sheet} R{left.row_start}:R{left.row_end} C{left.col_start}:C{left.col_end}"
        " vs "
        f"{right.file} / {right.sheet} R{right.row_start}:R{right.row_end} C{right.col_start}:C{right.col_end}"
    )
    sample = [v for row in values[:2] for v in row[: min(3, len(row))]]
    desc = (
        f"{left.file} / {left.sheet} 与 {right.file} / {right.sheet} 存在 "
        f"{left.n_rows}x{left.n_cols} 数值矩阵逐格完全相同，共 {n_values} 个测量值。"
        f"左侧上下文：{_context_text(left)}；右侧上下文：{_context_text(right)}。"
    )
    return {
        "severity": "high",
        "location": loc,
        "description": desc,
        "n_values": n_values,
        "n_rows": left.n_rows,
        "n_cols": left.n_cols,
        "sample": sample,
        "left": _window_payload(left),
        "right": _window_payload(right),
    }


def _window_payload(window: MatrixWindow) -> dict:
    return {
        "file": window.file,
        "sheet": window.sheet,
        "figure_label": window.figure_label,
        "row_range": [window.row_start, window.row_end],
        "col_range": [window.col_start, window.col_end],
        "row_labels": list(window.row_labels[:10]),
        "column_context": list(window.column_context[:10]),
        "location_id": f"{window.file}::{window.sheet}",
    }


def _context_text(window: MatrixWindow) -> str:
    bits = []
    if window.column_context:
        bits.append("列/分组=" + "、".join(window.column_context[:4]))
    if window.row_labels:
        bits.append("行=" + "、".join(window.row_labels[:4]))
    return "；".join(bits) or "未识别到文字标签"


def _combined_article_text(base: Path) -> str:
    parts = []
    for name in ("article.html", "main_paper_text.txt"):
        p = base / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if p.suffix.lower() == ".html":
            text = _html_to_text(text)
        parts.append(text)
    for pdf in sorted(base.glob("*.pdf"))[:1]:
        try:
            import fitz
            doc = fitz.open(str(pdf))
            parts.append("\n".join(page.get_text() for page in doc))
            doc.close()
        except Exception:
            pass
    return _normalize_text("\n".join(parts))


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return " ".join(BeautifulSoup(html, "html.parser").stripped_strings)
    except Exception:
        text = re.sub(r"<script\b.*?</script>", " ", html, flags=re.I | re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
        return re.sub(r"<[^>]+>", " ", text)


def _normalize_text(text: str) -> str:
    text = text.replace("\u2009", " ").replace("\u00a0", " ").replace("\u202f", " ")
    text = text.replace(" ", " ").replace("–", "-").replace("−", "-")
    return re.sub(r"\s+", " ", text)


def _sheet_fig_parts(sheet: str) -> tuple[str, str, str] | None:
    s = str(sheet or "")
    ext = bool(re.search(r"extended\s+data", s, re.I))
    m = re.search(r"fig(?:ure)?\.?\s*(\d+)([a-z]?)", s, re.I)
    if not m:
        return None
    prefix = "Extended Data Fig." if ext else "Fig."
    return prefix, m.group(1), m.group(2).lower()


def _declared_n_for_sheet(sheet: str, text: str) -> int | None:
    parts = _sheet_fig_parts(sheet)
    if not parts or not text:
        return None
    prefix, fig_num, panel = parts
    if prefix.startswith("Extended"):
        fig_pat = rf"Extended Data Fig\.?\s*{re.escape(fig_num)}\b"
    else:
        fig_pat = rf"(?<!Extended Data )Fig\.?\s*{re.escape(fig_num)}\b"
    for match in re.finditer(fig_pat, text, flags=re.I):
        segment = text[match.start(): match.start() + 3500]
        if panel:
            panel_pat = rf"\b{re.escape(panel)}\s*[,.)]\s*.{{0,900}}?\bn\s*[=:]?\s*(\d+)\s*(?:mice|mouse|animals|biological|independent|replicates|organoids|tumours|tumors)?"
            pm = re.search(panel_pat, segment, flags=re.I)
            if pm:
                return int(pm.group(1))
        nm = re.search(r"\bn\s*[=:]?\s*(\d+)\s*(?:mice|mouse|animals|biological|independent|replicates|organoids|tumours|tumors)", segment, flags=re.I)
        if nm:
            return int(nm.group(1))
    return None


def _axis_like_header(text: str) -> bool:
    h = str(text or "").strip().lower()
    return h in {"day", "days", "time", "dose", "concentration", "vehicle/tmx"} or any(
        token in h for token in ("time", "dose", "concentration", "wavelength", "day ", "axis")
    )


def _sheet_n_mismatches(file_name: str, sheet: str, grid: list[list[Any]], declared_n: int) -> list[dict]:
    out = []
    for r0, _r1, c0, c1 in _numeric_blocks(grid):
        header_rows = range(max(0, r0 - 3), r0)
        for hr in header_rows:
            row = grid[hr]
            labels = []
            for ci in range(c0, min(c1 + 1, len(row))):
                text = _cell_text(row[ci])
                if text and not _is_number(text) and not _axis_like_header(text):
                    labels.append((ci, text))
            if len(labels) < 2:
                continue
            for idx, (ci, label) in enumerate(labels):
                end = (labels[idx + 1][0] - 1) if idx + 1 < len(labels) else c1
                source_n = _numeric_column_count(grid, r0, ci, end)
                if 2 <= source_n <= 20 and source_n != declared_n:
                    location = f"{file_name} / {sheet} / {label}"
                    out.append({
                        "severity": "high",
                        "location": location,
                        "description": (
                            f"{sheet} 图注/正文声明 n={declared_n}，但源数据分组 {label} "
                            f"在第 {hr + 1} 行表头下有 {source_n} 个数值重复列。"
                        ),
                        "file": file_name,
                        "sheet": sheet,
                        "group": label,
                        "declared_n": declared_n,
                        "source_n": source_n,
                        "header_row": hr + 1,
                        "data_start_row": r0 + 1,
                    })
            if out:
                break
    seen = set()
    deduped = []
    for item in out:
        key = (item["file"], item["sheet"], item["group"], item["declared_n"], item["source_n"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _numeric_column_count(grid: list[list[Any]], row_start: int, c0: int, c1: int) -> int:
    count = 0
    for ci in range(c0, c1 + 1):
        if any(ci < len(grid[ri]) and _is_number(grid[ri][ci]) for ri in range(row_start, min(row_start + 3, len(grid)))):
            count += 1
    return count


def _relpath(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except Exception:
        return path.name


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit source data for exact matrix reuse and n mismatches.")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--doi", default="")
    parser.add_argument("--out")
    args = parser.parse_args()
    result = audit_source_data(args.input_dir, doi=args.doi)
    payload = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
