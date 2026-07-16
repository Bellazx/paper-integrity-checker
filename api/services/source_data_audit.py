from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

AUDIT_VERSION = "1"

DATA_EXTS = {".xlsx", ".xls", ".csv"}
MIN_MATRIX_ROWS = 3
MIN_MATRIX_COLS = 2
MIN_HIGH_RISK_CELLS = 15
MAX_SMALL_BLOCK_ROWS = 30
MAX_SMALL_BLOCK_COLS = 12


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


def audit_source_data(input_dir: str | Path, doi: str = "") -> dict:
    base = Path(input_dir)
    data_files = _list_data_files(base)
    windows: list[MatrixWindow] = []
    n_mismatches: list[dict] = []
    text = _combined_article_text(base)

    for path in data_files:
        try:
            sheets = _read_workbook(path)
        except Exception as exc:
            continue
        rel = _relpath(path, base)
        for sheet, grid in sheets.items():
            windows.extend(_matrix_windows(rel, sheet, grid))
            declared_n = _declared_n_for_sheet(sheet, text)
            if declared_n:
                n_mismatches.extend(_sheet_n_mismatches(rel, sheet, grid, declared_n))

    matrix_reuse = _find_matrix_reuse(windows)
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
    for item in n_mismatches:
        high_findings.append({
            "test": "figure_n_mismatch",
            "severity": "high",
            "location": item["location"],
            "description": item["description"],
            "declared_n": item["declared_n"],
            "source_n": item["source_n"],
        })

    return {
        "version": AUDIT_VERSION,
        "doi": doi,
        "input_dir": str(base),
        "data_file_count": len(data_files),
        "matrix_window_count": len(windows),
        "high_findings": high_findings,
        "rectangular_matrix_reuse": matrix_reuse,
        "figure_n_mismatch": n_mismatches,
    }


def _list_data_files(base: Path) -> list[Path]:
    if not base.exists():
        return []
    return sorted(
        p for p in base.rglob("*")
        if p.is_file()
        and p.suffix.lower() in DATA_EXTS
        and not p.name.startswith("~$")
    )


def _read_workbook(path: Path) -> dict[str, list[list[Any]]]:
    import pandas as pd

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path, header=None, dtype=object)
        return {path.stem[:31] or "csv": _df_to_grid(df)}

    sheets: dict[str, list[list[Any]]] = {}
    xl = pd.ExcelFile(path)
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
    for r0, r1, c0, c1 in _numeric_blocks(grid):
        h = r1 - r0 + 1
        w = c1 - c0 + 1
        candidates = {(r0, r1, c0, c1)}
        if h <= MAX_SMALL_BLOCK_ROWS and w <= MAX_SMALL_BLOCK_COLS:
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
                figure_label=_figure_label(sheet),
                row_start=rr0 + 1,
                row_end=rr1 + 1,
                col_start=cc0 + 1,
                col_end=cc1 + 1,
                values=values,
                row_labels=_row_labels(grid, rr0, rr1, cc0),
                column_context=_column_context(grid, rr0, cc0, cc1),
            ))
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


def _find_matrix_reuse(windows: list[MatrixWindow]) -> list[dict]:
    by_values: dict[tuple[tuple[float, ...], ...], list[MatrixWindow]] = {}
    for window in windows:
        if window.n_values >= MIN_HIGH_RISK_CELLS:
            by_values.setdefault(window.values, []).append(window)

    findings = []
    for values, matches in by_values.items():
        if len(matches) < 2:
            continue
        for i, left in enumerate(matches):
            for right in matches[i + 1:]:
                if (left.file, left.sheet) == (right.file, right.sheet):
                    continue
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
