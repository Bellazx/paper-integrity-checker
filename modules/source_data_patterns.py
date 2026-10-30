from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from itertools import combinations
from typing import Any


MIN_EXACT_REUSE_VALUES = 8
MIN_LONG_RATIO_VALUES = 8
MIN_RATIO_MATCH_FRACTION = 0.90
RATIO_RTOL = 2e-6
RATIO_ATOL = 2e-8
MAX_PATTERN_ROWS = 500
MAX_SEMANTIC_TRANSFORM_ROWS = 100_000
MAX_PATTERN_COLS = 200
MAX_NUMERIC_SERIES = 600
SAME_CHANNEL_MIN_VALUES = 15


@dataclass(frozen=True)
class NumericSeries:
    file: str
    sheet: str
    panel: str
    row: int
    col_start: int
    col_end: int
    values: tuple[float, ...]
    row_label: str
    axis_trimmed: bool = False
    axis_explicit: bool = False
    axis_value: float | None = None
    axis_header: str = ""

    @property
    def n_values(self) -> int:
        return len(self.values)


_PANEL_RE = re.compile(
    r"^(?:(?:figure|fig)\.?\s*)?(s)?\s*(\d+)\s*([a-z])$",
    re.IGNORECASE,
)


def detect_source_data_patterns(file_name: str, sheet: str, grid: list[list[Any]]) -> dict:
    series = _numeric_series(file_name, sheet, grid)
    return {
        "same_sheet_exact_series_reuse": _find_same_sheet_exact_series_reuse(series),
        "same_sheet_channel_duplicate": _find_same_sheet_channel_duplicates(
            file_name, sheet, grid
        ),
        "fixed_ratio_series": _find_fixed_ratio_series(series),
        "semantic_transform_conflicts": _find_semantic_transform_conflicts(
            file_name, sheet, grid
        ),
    }


def _excel_column_name(index: int) -> str:
    """Return an Excel-style column name for a zero-based index."""
    value = index + 1
    out = []
    while value:
        value, remainder = divmod(value - 1, 26)
        out.append(chr(65 + remainder))
    return "".join(reversed(out))


def _numeric(value: Any) -> float | None:
    if not _is_number(value):
        return None
    return float(value)


def _first_data_row(grid: list[list[Any]]) -> int | None:
    """Find the first row that looks like aligned numeric source data."""
    for row_index, row in enumerate(grid[:30]):
        numeric = sum(1 for value in row if _is_number(value))
        if numeric >= 2:
            # Numeric specimen IDs above a units/blank row are headers, not
            # measurements (e.g. 1..20, 1..20 followed by '(min)').
            numbers = [float(value) for value in row if _is_number(value)]
            if (all(value.is_integer() for value in numbers)
                    and any(isinstance(v, str) and v.strip() for v in row)):
                continue
            return row_index
    return None


def _forward_filled_headers(row: list[Any], width: int) -> list[str]:
    """Expand sparse/merged-like header text across the columns it governs."""
    labels = []
    current = ""
    for index in range(width):
        value = row[index] if index < len(row) else None
        if isinstance(value, str) and value.strip():
            current = _text(value)
        labels.append(current)
    return labels


def _header_context(grid: list[list[Any]], data_start: int, width: int) -> tuple[list[str], list[str]]:
    """Return per-column channel and repeat labels from sparse header rows.

    Source-data workbooks commonly encode merged cells as one value at the left
    edge of a block.  The initial checker previously saw the other columns as
    ``col_N`` and consequently could not tell two different channels apart.
    The last text row before data is treated as the channel header; earlier
    rows provide the repeat/condition context.
    """
    if data_start <= 0:
        return [""] * width, [""] * width
    header_row = None
    for index in range(data_start - 1, -1, -1):
        texts = [str(v).strip().lower() for v in grid[index]
                 if isinstance(v, str) and v.strip() and not _is_number(v)]
        if any(re.search(r"fluorescen|intensity|signal|absorbance|luminescen|channel|荧光|强度|通道", v)
               for v in texts):
            header_row = index
            break
    if header_row is None:
        header_row = next((index for index in range(data_start - 1, -1, -1)
                           if sum(isinstance(v, str) and bool(v.strip())
                                  and not _is_number(v) for v in grid[index]) >= 2), None)
    if header_row is None:
        return [""] * width, [""] * width
    channel = _forward_filled_headers(grid[header_row], width)
    group = [""] * width
    for row in grid[:header_row]:
        filled = _forward_filled_headers(row, width)
        for index, value in enumerate(filled):
            if value:
                group[index] = value
    return channel, group


def _is_monotonic_measurement_axis(values: list[float]) -> bool:
    if len(values) < 4:
        return False
    differences = [right - left for left, right in zip(values, values[1:])]
    return all(diff > 0 for diff in differences) or all(diff < 0 for diff in differences)


def _find_same_sheet_channel_duplicates(
    file_name: str, sheet: str, grid: list[list[Any]]
) -> list[dict]:
    """Find exact duplicate measurement columns inside one repeat block.

    The older ``cross_group_duplicate`` rule deliberately focused on columns
    belonging to different groups.  That misses the important case where two
    distinct channels (for example GFP and mCherry) sit inside the same
    Repeat 1 block.  This check uses the merged-like header context from the
    raw workbook, requires a substantial continuous measurement vector, and
    excludes axes and identifiers. Adjacent channels and monotonically changing
    measurement curves remain eligible; geometry is not experimental semantics.
    """
    data_start = _first_data_row(grid)
    if data_start is None:
        return []
    width = max((len(row) for row in grid[: max(data_start + 1, 1)]), default=0)
    if width < 2:
        return []
    channel_headers, group_headers = _header_context(grid, data_start, width)

    columns: dict[int, list[float | None]] = {}
    for column in range(width):
        values = []
        for row in grid[data_start:]:
            values.append(_numeric(row[column] if column < len(row) else None))
        numeric = [value for value in values if value is not None]
        if len(numeric) < SAME_CHANNEL_MIN_VALUES:
            continue
        if len(set(numeric)) < 3:
            continue
        header = channel_headers[column].strip()
        if not header:
            continue
        lowered = header.lower()
        if re.fullmatch(r"(?:col|value|data)[ _-]?\d+", lowered):
            continue
        if _is_axis_header(header) or re.search(
            r"\b(?:id|index|timepoint|time point|number|replicate|repeat)\b", lowered
        ):
            continue
        if re.fullmatch(r"[xy](?:[ _-]?\d+)?", lowered):
            continue
        columns[column] = values

    # Comparing every candidate column with every other column is catastrophic
    # for wide source tables (one real API sample has 3,144 columns).  Exact
    # duplicates can be found losslessly by grouping on condition + the aligned
    # value vector first, reducing O(columns^2 * rows) to O(columns * rows).
    identical_groups: dict[tuple[str, tuple[float | None, ...]], list[int]] = {}
    for column, values in columns.items():
        identical_groups.setdefault(
            (group_headers[column], tuple(values)), []
        ).append(column)

    findings = []
    for (_group, signature), matching_columns in identical_groups.items():
        if len(matching_columns) < 2:
            continue
        numeric_pairs = [(value, value) for value in signature if value is not None]
        if len(numeric_pairs) < SAME_CHANNEL_MIN_VALUES:
            continue
        fractional = sum(
            1 for left, right in numeric_pairs
            if abs(left - round(left)) > 1e-9 or abs(right - round(right)) > 1e-9
        ) / max(1, len(numeric_pairs))
        if fractional < 0.5:
            continue
        for left_index, right_index in combinations(matching_columns, 2):
            left_header = channel_headers[left_index]
            right_header = channel_headers[right_index]
            if not left_header or not right_header or left_header == right_header:
                continue
            left_group = group_headers[left_index]
            right_group = group_headers[right_index]
            if left_group != right_group:
                continue
            findings.append({
                "test": "same_sheet_channel_duplicate",
                "severity": "high",
                "location": (
                    f"{file_name} / {sheet} / {left_group or '测量通道'} / "
                    f"{_excel_column_name(left_index)} vs {_excel_column_name(right_index)}"
                ),
                "description": (
                    f"同一重复组中两个不同测量通道（{left_header}；{right_header}）的两列"
                    f"逐值完全相同，共 {len(numeric_pairs)} 个测量值。"
                ),
                "details": {
                    "group": left_group,
                    "channel_a": left_header,
                    "channel_b": right_header,
                    "col_a_idx": left_index,
                    "col_b_idx": right_index,
                    "col_a": _excel_column_name(left_index),
                    "col_b": _excel_column_name(right_index),
                    "n_values": len(numeric_pairs),
                    "row_numbers": [data_start + i + 1 for i, (left, right)
                                    in enumerate(zip(signature, signature))
                                    if left is not None and right is not None],
                    "match_ratio": 1.0,
                    "fractional_ratio": fractional,
                    "sample_values": [left for left, _ in numeric_pairs[:5]],
                },
            })
            if len(findings) >= 30:
                return findings
    return findings


def _normalized_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _text(value).lower())


def _column_values(
    grid: list[list[Any]], header_row: int, column: int, *, row_limit: int = MAX_PATTERN_ROWS
) -> list[tuple[int, float]]:
    values = []
    for row_index in range(header_row + 1, min(len(grid), row_limit)):
        row = grid[row_index]
        if column >= len(row) or not _is_number(row[column]):
            continue
        values.append((row_index + 1, _num(row[column])))
    return values


def _find_semantic_transform_conflicts(
    file_name: str, sheet: str, grid: list[list[Any]]
) -> list[dict]:
    """Find columns copied across labels that require a mathematical transform.

    The first supported relationship is the common differential-expression pair
    ``avg_log2FC`` and linear ``FC``.  These columns may legitimately coexist, but
    FC must equal ``2 ** avg_log2FC``; copying the log values directly produces
    impossible negative fold changes and is a deterministic integrity problem.
    """
    findings = []
    for header_row, row in enumerate(grid[:40]):
        headers = {_normalized_header(value): index for index, value in enumerate(row)}
        log_col = next(
            (index for name, index in headers.items() if name in {"avglog2fc", "log2fc", "log2foldchange"}),
            None,
        )
        fc_col = next(
            (index for name, index in headers.items() if name in {"fc", "foldchange", "linearfoldchange"}),
            None,
        )
        if log_col is None or fc_col is None or log_col == fc_col:
            continue

        # Unlike pairwise pattern mining, this is a linear two-column validation;
        # scan the complete scientific table so counts in evidence and reports are
        # not silently truncated at the general 500-row performance cap.
        left = dict(
            _column_values(
                grid, header_row, log_col, row_limit=MAX_SEMANTIC_TRANSFORM_ROWS
            )
        )
        right = dict(
            _column_values(
                grid, header_row, fc_col, row_limit=MAX_SEMANTIC_TRANSFORM_ROWS
            )
        )
        common_rows = sorted(set(left) & set(right))
        if len(common_rows) < 20:
            continue
        exact = [row_no for row_no in common_rows if abs(left[row_no] - right[row_no]) <= 1e-9]
        transformed = [
            row_no for row_no in common_rows
            if math.isclose(right[row_no], 2 ** left[row_no], rel_tol=1e-6, abs_tol=1e-8)
        ]
        exact_fraction = len(exact) / len(common_rows)
        transformed_fraction = len(transformed) / len(common_rows)
        if exact_fraction < 0.95 or transformed_fraction > 0.10:
            continue
        negative_fc = sum(1 for row_no in common_rows if right[row_no] < 0)
        sample_rows = common_rows[:5]
        sample = [
            {
                "row": row_no,
                "avg_log2fc": left[row_no],
                "reported_fc": right[row_no],
                "expected_fc": round(2 ** left[row_no], 8),
            }
            for row_no in sample_rows
        ]
        findings.append({
            "test": "semantic_transform_conflict",
            "severity": "high",
            "location": (
                f"{file_name} / {sheet} / row {min(common_rows)}-{max(common_rows)}, "
                f"columns {row[log_col]} vs {row[fc_col]}"
            ),
            "description": (
                f"列“{row[log_col]}”与“{row[fc_col]}”在 {len(exact)}/{len(common_rows)} 行"
                "逐值相同，但线性倍数应为 2 的对数倍数次方；"
                f"按该关系仅 {len(transformed)}/{len(common_rows)} 行成立，且 FC 列含"
                f" {negative_fc} 个负值。"
            ),
            "details": {
                "left_header": str(row[log_col]),
                "right_header": str(row[fc_col]),
                "row_count": len(common_rows),
                "exact_count": len(exact),
                "expected_transform_match_count": len(transformed),
                "negative_fc_count": negative_fc,
                "formula": "FC = 2 ** avg_log2FC",
                "sample": sample,
            },
        })
        break
    return findings


def _is_number(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed)


def _num(value: Any) -> float:
    parsed = round(float(value), 10)
    return 0.0 if parsed == -0.0 else parsed


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _normalize_panel(value: Any) -> str:
    text = _text(value)
    compact = re.sub(r"[\s._-]+", "", text)
    match = _PANEL_RE.fullmatch(compact)
    if not match:
        return ""
    supplement = "S" if match.group(1) else ""
    return f"Fig.{supplement}{match.group(2)}{match.group(3).lower()}"


def _panel_markers(grid: list[list[Any]]) -> list[tuple[int, int, str]]:
    markers = []
    for row_index, row in enumerate(grid[:MAX_PATTERN_ROWS]):
        for col_index, value in enumerate(row[:MAX_PATTERN_COLS]):
            panel = _normalize_panel(value)
            if panel:
                markers.append((row_index, col_index, panel))
    return markers


def _panel_for_run(
    markers: list[tuple[int, int, str]], row: int, col_start: int, fallback: str
) -> str:
    candidates = [
        marker for marker in markers
        if marker[0] < row or (marker[0] == row and marker[1] <= col_start)
    ]
    if not candidates:
        return fallback
    marker = max(candidates, key=lambda item: (item[0], item[1]))
    return marker[2]


def _numeric_runs(row: list[Any], min_values: int = 3) -> list[tuple[int, int]]:
    runs = []
    start = None
    for index, value in enumerate(row + [None]):
        if index < len(row) and _is_number(value):
            if start is None:
                start = index
            continue
        if start is not None:
            end = index - 1
            if end - start + 1 >= min_values:
                runs.append((start, end))
            start = None
    return runs


def _row_label(row: list[Any], col_start: int) -> str:
    for col in range(col_start - 1, -1, -1):
        value = row[col]
        if _is_number(value):
            continue
        text = _text(value)
        if text:
            return text
    return ""


def _axis_header(grid: list[list[Any]], row: int, col_start: int) -> str:
    for ri in range(row - 1, max(-1, row - 7), -1):
        if ri < 0:
            break
        source_row = grid[ri]
        for ci in range(col_start, max(-1, col_start - 2), -1):
            if ci >= len(source_row):
                continue
            value = source_row[ci]
            if _is_number(value):
                continue
            text = _text(value)
            if text:
                return text
    return ""


def _is_axis_header(value: str) -> bool:
    text = str(value or "").lower()
    terms = (
        "day", "time", "hour", "week", "month", "dose", "concentration",
        "conc", "wavelength", "wavenumber", "frequency", "temperature",
        "potential", "voltage", "pressure", "cycle", "distance", "position",
        "au h/", "auh/", "dlat/", "/um", "/μm", "μm", "µm",
    )
    return any(term in text for term in terms)


def _numeric_series(file_name: str, sheet: str, grid: list[list[Any]]) -> list[NumericSeries]:
    markers = _panel_markers(grid)
    fallback = str(sheet or "")
    series = []
    for row_index, row in enumerate(grid[:MAX_PATTERN_ROWS]):
        for col_start, col_end in _numeric_runs(row[:MAX_PATTERN_COLS]):
            values = tuple(_num(row[col]) for col in range(col_start, col_end + 1))
            if len(set(values)) <= 1:
                continue
            panel = _panel_for_run(markers, row_index, col_start, fallback)
            label = _row_label(row, col_start)
            series.append(NumericSeries(
                file=file_name,
                sheet=sheet,
                panel=panel,
                row=row_index + 1,
                col_start=col_start + 1,
                col_end=col_end + 1,
                values=values,
                row_label=label,
            ))

            if len(values) < 4:
                continue
            header = _axis_header(grid, row_index, col_start)
            series.append(NumericSeries(
                file=file_name,
                sheet=sheet,
                panel=panel,
                row=row_index + 1,
                col_start=col_start + 2,
                col_end=col_end + 1,
                values=values[1:],
                row_label=label,
                axis_trimmed=True,
                axis_explicit=_is_axis_header(header),
                axis_value=values[0],
                axis_header=header,
            ))
            if len(series) >= MAX_NUMERIC_SERIES:
                return series
    return series


def _decimal_places(value: float) -> int:
    text = f"{float(value):.10f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _has_measurement_precision(values: tuple[float, ...]) -> bool:
    return sum(_decimal_places(value) >= 3 for value in values) >= min(2, len(values))


def _longest_common_run(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[int, int, int]:
    best_left = best_right = best_len = 0
    previous = [0] * (len(right) + 1)
    for left_index, left_value in enumerate(left, start=1):
        current = [0] * (len(right) + 1)
        for right_index, right_value in enumerate(right, start=1):
            if left_value == right_value:
                current[right_index] = previous[right_index - 1] + 1
                if current[right_index] > best_len:
                    best_len = current[right_index]
                    best_left = left_index - best_len
                    best_right = right_index - best_len
        previous = current
    return best_left, best_right, best_len


def _find_same_sheet_exact_series_reuse(series: list[NumericSeries]) -> list[dict]:
    base = [item for item in series if not item.axis_trimmed]
    findings = []
    for index, left in enumerate(base):
        for right in base[index + 1:]:
            if (left.file, left.sheet) != (right.file, right.sheet):
                continue
            if not left.panel or not right.panel or left.panel == right.panel:
                continue
            if left.row_label and right.row_label and left.row_label.lower() != right.row_label.lower():
                continue
            left_start, right_start, run_len = _longest_common_run(left.values, right.values)
            if run_len < MIN_EXACT_REUSE_VALUES:
                continue
            shared = left.values[left_start:left_start + run_len]
            if len(set(shared)) < 4 or not _has_measurement_precision(shared):
                continue
            left_cols = [left.col_start + left_start, left.col_start + left_start + run_len - 1]
            right_cols = [right.col_start + right_start, right.col_start + right_start + run_len - 1]
            location = (
                f"{left.file} / {left.sheet} / {left.panel} row {left.row} "
                f"C{left_cols[0]}:C{left_cols[1]} vs {right.panel} row {right.row} "
                f"C{right_cols[0]}:C{right_cols[1]}"
            )
            left_payload = {
                "file": left.file,
                "sheet": left.sheet,
                "panel": left.panel,
                "row": left.row,
                "col_range": left_cols,
                "row_label": left.row_label,
            }
            right_payload = {
                "file": right.file,
                "sheet": right.sheet,
                "panel": right.panel,
                "row": right.row,
                "col_range": right_cols,
                "row_label": right.row_label,
            }
            sample = list(shared[:6])
            findings.append({
                "test": "same_sheet_exact_series_reuse",
                "severity": "medium",
                "location": location,
                "description": (
                    f"同一工作表中 {left.panel} 与 {right.panel} 的不同数据行出现 "
                    f"{run_len} 个连续测量值逐值完全相同。"
                ),
                "n_values": run_len,
                "sample": sample,
                "left": left_payload,
                "right": right_payload,
                "details": {
                    "n_values": run_len,
                    "sample": sample,
                    "left": left_payload,
                    "right": right_payload,
                },
            })

    pair_counts = {}
    for finding in findings:
        pair = tuple(sorted((finding["left"]["panel"], finding["right"]["panel"])))
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    for finding in findings:
        pair = tuple(sorted((finding["left"]["panel"], finding["right"]["panel"])))
        if finding["n_values"] >= 15 or pair_counts[pair] >= 2:
            finding["severity"] = "high"

    findings.sort(key=lambda item: (item["severity"] == "high", item["n_values"]), reverse=True)
    deduped = []
    seen = set()
    for finding in findings:
        key = (
            finding["left"]["panel"], finding["right"]["panel"],
            finding["left"]["row"], finding["right"]["row"],
            tuple(finding["sample"]),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
        if len(deduped) >= 30:
            break
    return deduped


def _axis_sequence_is_plausible(items: list[NumericSeries]) -> bool:
    values = [item.axis_value for item in sorted(items, key=lambda item: item.row)]
    values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if len(values) < 5 or len(set(values)) < 5:
        return False
    increasing = all(left < right for left, right in zip(values, values[1:]))
    decreasing = all(left > right for left, right in zip(values, values[1:]))
    return increasing or decreasing


def _fixed_ratio_match(left: NumericSeries, right: NumericSeries) -> dict | None:
    if left.n_values != right.n_values or left.n_values < 3:
        return None
    ratios = []
    positions = []
    for index, (left_value, right_value) in enumerate(zip(left.values, right.values)):
        if abs(left_value) <= 1e-12:
            continue
        ratio = right_value / left_value
        if math.isfinite(ratio):
            ratios.append(ratio)
            positions.append(index)
    if len(ratios) < 3:
        return None
    scale = statistics.median(ratios)
    if not (0.05 <= scale <= 20.0) or abs(scale - 1.0) < 0.005:
        return None
    matched_positions = [
        positions[index] for index, ratio in enumerate(ratios)
        if math.isclose(ratio, scale, rel_tol=RATIO_RTOL, abs_tol=RATIO_ATOL)
    ]
    matched = len(matched_positions)
    total = len(ratios)
    if total >= MIN_LONG_RATIO_VALUES:
        if matched < MIN_LONG_RATIO_VALUES or matched / total < MIN_RATIO_MATCH_FRACTION:
            return None
    elif matched != total:
        return None
    if len(set(left.values)) < 3 or len(set(right.values)) < 3:
        return None
    if not _has_measurement_precision(left.values) or not _has_measurement_precision(right.values):
        return None
    return {
        "scale": scale,
        "matched_positions": matched_positions,
        "matched_count": matched,
        "total_compared": total,
    }


def _is_calibration_context(item: NumericSeries) -> bool:
    context = " ".join((item.sheet, item.panel, item.axis_header, item.row_label)).lower()
    return any(term in context for term in ("standard curve", "calibration", "calibrant"))


def _find_fixed_ratio_series(series: list[NumericSeries]) -> list[dict]:
    trimmed = [item for item in series if item.axis_trimmed]
    groups: dict[tuple[str, str, str, int, int], list[NumericSeries]] = {}
    for item in trimmed:
        key = (item.file, item.sheet, item.panel, item.col_start, item.col_end)
        groups.setdefault(key, []).append(item)

    findings = []
    for items in groups.values():
        if len(items) < 2:
            continue
        if any(_is_calibration_context(item) for item in items):
            continue
        axis_ok = any(item.axis_explicit for item in items) or _axis_sequence_is_plausible(items)
        if not axis_ok:
            continue
        items = sorted(items, key=lambda item: item.row)
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                match = _fixed_ratio_match(left, right)
                if not match:
                    continue
                matched_positions = match["matched_positions"]
                sample = [
                    {
                        "left": left.values[pos],
                        "right": right.values[pos],
                    }
                    for pos in matched_positions[:5]
                ]
                location = (
                    f"{left.file} / {left.sheet} / {left.panel} rows {left.row} vs {right.row} "
                    f"C{left.col_start}:C{left.col_end}"
                )
                details = {
                    "ratio": match["scale"],
                    "matched_count": match["matched_count"],
                    "total_compared": match["total_compared"],
                    "match_fraction": match["matched_count"] / match["total_compared"],
                    "axis_header": left.axis_header or right.axis_header,
                    "left_axis_value": left.axis_value,
                    "right_axis_value": right.axis_value,
                    "left_row": left.row,
                    "right_row": right.row,
                    "panel": left.panel,
                    "sample": sample,
                }
                findings.append({
                    "test": "fixed_ratio_series",
                    "severity": "high",
                    "location": location,
                    "description": (
                        f"同一图面板的两个时间点、剂量或浓度行中，"
                        f"{match['matched_count']}/{match['total_compared']} 个对应测量值共享固定比例 "
                        f"{match['scale']:.8g}。"
                    ),
                    "n_values": match["matched_count"],
                    "ratio": details["ratio"],
                    "matched_count": details["matched_count"],
                    "total_compared": details["total_compared"],
                    "match_fraction": details["match_fraction"],
                    "axis_header": details["axis_header"],
                    "left_axis_value": details["left_axis_value"],
                    "right_axis_value": details["right_axis_value"],
                    "left_row": details["left_row"],
                    "right_row": details["right_row"],
                    "panel": details["panel"],
                    "sample": sample,
                    "details": details,
                })

    findings.sort(
        key=lambda item: (item["matched_count"], item["match_fraction"]),
        reverse=True,
    )
    deduped = []
    seen = set()
    for finding in findings:
        key = (
            finding["location"],
            round(float(finding["ratio"]), 8),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
        if len(deduped) >= 30:
            break
    return deduped
