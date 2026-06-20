"""Extract tables from PDF files (main paper + supplementary PDFs) into CSVs
that data_checker can process for anomalies.

Two strategies:
  1. lines strategy: tables with visible ruling lines (lattice-style)
  2. text  strategy: tables aligned by whitespace, with stronger heuristics to
     reject double-column body text and figure captions (which pdfplumber
     often misclassifies as "tables").

Heuristics to keep a candidate table:
  - rows ≥ MIN_ROWS (default 3)
  - cols ≥ MIN_COLS (default 3, NOT 2 — body text has 2 cols)
  - first non-empty row must look like a header (mostly short strings, no figures)
  - at least 30% of cells must be numeric — otherwise it's prose
  - overall cell density > 50%
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MIN_ROWS = 3
MIN_COLS = 3
MIN_CELL_RATIO = 0.4
MIN_NUMERIC_RATIO = 0.2
MAX_TABLES_PER_PDF = 50  # safety cap


def _is_numeric_cell(s: str) -> bool:
    if s is None:
        return False
    t = s.strip()
    if not t:
        return False
    t = t.replace("−", "-").replace("−", "-").replace(",", "").replace(" ", "").replace(" ", "")
    try:
        float(t)
        return True
    except ValueError:
        return False
    return False


def _looks_like_header(row: list) -> bool:
    """A header row should be mostly short alphabetic tokens, no numerics, no
    full sentences (no internal period or comma)."""
    if not row:
        return False
    nonempty = [c for c in row if c and c.strip()]
    if not nonempty:
        return False
    if any(_is_numeric_cell(c) for c in nonempty):
        return False
    for c in nonempty:
        cc = c.strip()
        # reject anything that looks like prose
        if len(cc) > 80:
            return False
        if cc.count(" ") > 8:
            return False
    # Reject if any cell contains "FIGURE" or "Table" caption
    joined = " ".join(nonempty).lower()
    if "figure" in joined and len(joined) < 30:
        return False
    return True


_STRATEGIES = [
    ("lines", {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 3,
    }),
    ("text", {
        "vertical_strategy": "text",
        "horizontal_strategy": "text",
        "snap_tolerance": 4,
        "join_tolerance": 3,
        "min_words_vertical": 1,
        "min_words_horizontal": 1,
    }),
]


def _extract_with_strategy(pdf, settings, min_rows: int, min_cols: int) -> list[list[list[str]]]:
    accepted: list[list[list[str]]] = []
    seen_table_fps: set = set()
    for page_num, page in enumerate(pdf.pages, start=1):
        try:
            tables = page.extract_tables(table_settings=settings) or []
        except Exception:
            continue
        for t in tables:
            if not t:
                continue
            rows = [r for r in t if r and any((c or "").strip() for c in r)]
            if len(rows) < min_rows:
                continue
            ncols = max(len(r) for r in rows)
            if ncols < min_cols:
                continue
            rows = [r + [""] * (ncols - len(r)) for r in rows]
            flat = [c for r in rows for c in r]
            n = len(flat)
            nonempty = sum(1 for c in flat if (c or "").strip())
            if nonempty / n < MIN_CELL_RATIO:
                continue
            nnum = sum(1 for c in flat if _is_numeric_cell(c))
            if nnum / n < MIN_NUMERIC_RATIO:
                continue
            # first non-empty row must look like a header
            header = next((r for r in rows if any((c or "").strip() for c in r)), None)
            if not _looks_like_header(header):
                continue
            # fingerprint by (rows, cols, first-cell-content) to dedup across pages
            fp = (len(rows), ncols, (header[0] or "").strip()[:30])
            if fp in seen_table_fps:
                continue
            seen_table_fps.add(fp)
            accepted.append(rows)
            if len(accepted) >= MAX_TABLES_PER_PDF:
                return accepted
    return accepted


def extract_tables_from_pdf(pdf_path: str, output_dir: str, *,
                             min_rows: int = MIN_ROWS,
                             min_cols: int = MIN_COLS) -> list[Path]:
    """Extract tables from one PDF and write them as CSVs under
    `<output_dir>/data_tables/`. Returns the list of written CSV files.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return []
    target_dir = Path(output_dir) / "data_tables"
    target_dir.mkdir(parents=True, exist_ok=True)
    pdf_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", pdf_path.stem)[:80]

    written: list[Path] = []
    try:
        import pdfplumber
    except ImportError:
        log.warning("pdfplumber not installed; cannot extract tables from %s", pdf_path)
        return []

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for strat_name, settings in _STRATEGIES:
                tables = _extract_with_strategy(pdf, settings, min_rows, min_cols)
                for t_idx, rows in enumerate(tables, start=1):
                    out_name = f"{pdf_stem}__{strat_name}_table_{t_idx:03d}.csv"
                    out_path = target_dir / out_name
                    try:
                        with open(out_path, "w", encoding="utf-8", newline="") as f:
                            w = csv.writer(f)
                            for r in rows:
                                w.writerow([(c or "").strip() for c in r])
                        written.append(out_path)
                    except OSError as e:
                        log.warning("Failed to write %s: %s", out_path, e)
    except Exception as e:
        log.warning("pdfplumber failed to open %s: %s", pdf_path, e)
    if written:
        log.info("Extracted %d table(s) from %s", len(written), pdf_path)
    return written
