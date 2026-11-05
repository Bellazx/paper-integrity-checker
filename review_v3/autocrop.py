"""Code-made evidence screenshots for risk issues no capture session covered.

A risk issue names its location in reader terms ("正文第8页 Fig. 5g", "补充材料第29页
Table S5"). The figure or table is found on that page by its caption and cropped
together with the caption; a cited value is used when no caption matches. Crops
are saved under the run's dossier/renders with the same naming as pv3 render
--clip, so they count as local screenshots everywhere else.
"""
from __future__ import annotations

import re
from pathlib import Path

DPI = 150
PAGE = re.compile(r"第\s*(\d+)\s*页|\bp\.?\s*(\d+)\b|page\s*(\d+)", re.I)
LABEL = re.compile(r"(Extended\s+Data\s+Fig(?:ure)?|Supplementary\s+Fig(?:ure)?|Supplementary\s+Table|SFig|STable|"
                   r"Fig(?:ure)?|Table|补充图|补充表|图|表)\s*\.?\s*(S?\d+)", re.I)
ANY_LABEL = re.compile(r"^(?:Supplementary\s+|Extended\s+Data\s+)?(?:Fig(?:ure)?|Table)\s*\.?\s*S?\d+", re.I)
SUPPLEMENT = re.compile(r"补充|Supplementary|SI\b|Supporting", re.I)
NUMBER = re.compile(r"(?<![\w.])\d+\.\d{2,}(?![\w])")


def _docs(dossier: dict, supplementary: bool) -> list[dict]:
    docs = [d for d in dossier.get("docs") or [] if d.get("path") and Path(d["path"]).is_file()]
    return [d for d in docs if (d.get("id") != "main") == supplementary]


def _labels(location: str) -> list[tuple[str, str, bool]]:
    """(figure|table, number, in the supplement) for each label the location cites."""
    out = []
    for kind, number in LABEL.findall(location):
        low = kind.lower()
        table = "table" in low or low in ("表", "补充表")
        supplementary = (low.startswith(("supplementary", "sfig", "stable")) or kind.startswith("补充")
                         or number.upper().startswith("S"))
        item = ("table" if table else "figure", number.upper().lstrip("S"), supplementary)
        if item not in out:
            out.append(item)
    return out


def _caption_rect(page, kind: str, number: str):
    """The text block that starts this figure's or table's caption."""
    names = (r"(?:Supplementary\s+)?Table|表" if kind == "table" else
             r"(?:Extended\s+Data\s+|Supplementary\s+)?Fig(?:ure)?|图")
    head = re.compile(rf"^(?:{names})\s*\.?\s*{re.escape(number)}(?![0-9])", re.I)
    blocks = page.get_text("blocks")
    # A contents page lists many captions in a row; it shows no figure.
    if sum(bool(ANY_LABEL.match(" ".join(str(b[4]).split()))) for b in blocks) >= 4:
        return None
    for x0, y0, x1, y1, text, *_ in blocks:
        flat = " ".join(str(text).split())
        # A caption describes the figure; a list of contents names it only.
        if head.match(flat) and len(flat) >= 60:
            return x0, y0, x1, y1
    return None


def _figure_rect(page, caption):
    """From the last paragraph above the caption (or the page header) down to
    the caption's first lines: figures sit above their captions. None when
    that space is too small to hold a figure (the figure is on another page)."""
    import fitz
    x0, y0, x1, y1 = caption
    paragraphs = [b[3] for b in page.get_text("blocks")
                  if b[3] <= y0 - 2 and len(" ".join(str(b[4]).split())) > 180]
    top = max(paragraphs, default=page.rect.y0 + 36)
    if y0 - top < 150:
        return None
    return fitz.Rect(page.rect.x0 + 20, top + 2, page.rect.x1 - 20, min(page.rect.y1, y1 + 4, y0 + 90))


def _previous_page_figure(page):
    """The figure a caption at the top of the next page belongs to: the images
    and vector drawings above this page's first paragraph."""
    import fitz
    paragraphs = sorted(b[1] for b in page.get_text("blocks") if len(" ".join(str(b[4]).split())) > 180)
    limit = paragraphs[0] - 2 if paragraphs else page.rect.y1 - 36
    parts = ([fitz.Rect(i["bbox"]) for i in page.get_image_info()] + [fitz.Rect(d["rect"]) for d in page.get_drawings()]
             + [fitz.Rect(b[:4]) for b in page.get_text("blocks") if len(" ".join(str(b[4]).split())) <= 60])
    parts = [r for r in parts if r.y1 <= limit + 2 and r.y0 >= page.rect.y0 + 20 and not r.is_empty]
    if not parts:
        return None
    box = parts[0]
    for r in parts[1:]:
        box |= r
    if box.height < 150:
        return None
    return fitz.Rect(page.rect.x0 + 20, max(page.rect.y0, box.y0 - 6), page.rect.x1 - 20, min(limit, box.y1 + 6))


def _table_rect(page, caption):
    """From the caption down to the table's last row: its rules, its rows (a
    long block counts when it starts inside the table or a rule closes it),
    then short notes that follow without a wide gap. A short table no longer
    carries half a page of blank space."""
    import fitz
    x0, y0, x1, y1 = caption
    limit = page.rect.y1 - 30  # above the page number
    rules = sorted((d["rect"].y0, d["rect"].y1) for d in page.get_drawings() if y1 - 1 < d["rect"].y0 <= limit)
    blocks = sorted(((b[1], b[3], len(" ".join(str(b[4]).split()))) for b in page.get_text("blocks")
                     if y1 - 1 < b[1] <= limit and str(b[4]).strip()), key=lambda b: b[0])
    bottom, grown = y1, True
    while grown:
        grown = False
        for top, end in rules:
            if end > bottom and top - bottom <= 60:
                bottom, grown = end, True
        for top, end, length in blocks:
            if end <= bottom or top - bottom > 36:
                continue
            closed = any(end - 4 <= rule <= end + 14 for rule, _ in rules)
            if top < bottom - 1 or length <= 180 or closed:
                bottom, grown = end, True
    return fitz.Rect(page.rect.x0 + 20, y0 - 4, page.rect.x1 - 20, min(limit, bottom) + 6)


def _value_rect(page, values: list[str]):
    import fitz
    for value in values:
        hits = page.search_for(value)
        if hits:
            hit = hits[0]
            return fitz.Rect(page.rect.x0 + 20, max(page.rect.y0, hit.y0 - 110),
                             page.rect.x1 - 20, min(page.rect.y1, hit.y1 + 110))
    return None


def _save(pdf, doc: dict, page_no: int, rect, out_dir: Path) -> str:
    name = f"{doc['id']}_p{page_no:03d}_c{int(rect.x0)}_{int(rect.y0)}_{int(rect.x1)}_{int(rect.y1)}_d{DPI}.png"
    path = out_dir / name
    if not path.exists():
        pdf[page_no - 1].get_pixmap(dpi=DPI, clip=rect).save(str(path))
    return str(path)


def crop_for(record: dict, dossier: dict, run_dir: Path) -> list[dict]:
    """Up to two screenshots of the figures or tables the issue cites (or of the
    cited value on the cited page), or [] when none can be located."""
    import fitz
    location = str(record.get("location") or "")
    pages = [int(next(g for g in m.groups() if g)) for m in PAGE.finditer(location)]
    labels = _labels(location)
    values = NUMBER.findall(str(record.get("finding") or ""))[:6]
    out_dir = Path(run_dir) / "dossier" / "renders"
    out_dir.mkdir(parents=True, exist_ok=True)
    shots: list[dict] = []
    for kind, number, supplementary in labels:
        for doc in _docs(dossier, supplementary):
            with fitz.open(doc["path"]) as pdf:
                cited = [p for p in pages if 1 <= p <= pdf.page_count]
                order = cited + [p for p in range(1, pdf.page_count + 1) if p not in cited]
                for page_no in order:
                    page = pdf[page_no - 1]
                    caption = _caption_rect(page, kind, number)
                    rect = caption and (_table_rect(page, caption) if kind == "table" else _figure_rect(page, caption))
                    if caption and not rect and kind == "figure" and page_no > 1:
                        # Caption at the top of a page: the figure fills the page before.
                        page_no -= 1
                        rect = _previous_page_figure(pdf[page_no - 1])
                    if rect:
                        label = f"{'Table' if kind == 'table' else 'Fig.'} {'S' if supplementary else ''}{number}"
                        shots.append({"path": _save(pdf, doc, page_no, rect, out_dir),
                                      "caption": f"{'补充材料' if supplementary else '正文'}第{page_no}页 {label}（原文截图）"})
                        break
            if shots and shots[-1]["caption"].endswith(f"{number}（原文截图）"):
                break
        if len(shots) >= 2:
            return shots
    if not shots and pages:
        for doc in _docs(dossier, bool(SUPPLEMENT.search(location))):
            with fitz.open(doc["path"]) as pdf:
                for page_no in (p for p in pages if 1 <= p <= pdf.page_count):
                    rect = _value_rect(pdf[page_no - 1], values)
                    if rect:
                        shots.append({"path": _save(pdf, doc, page_no, rect, out_dir),
                                      "caption": f"第{page_no}页 相关段落（原文截图）"})
                        return shots
    return shots


ROWS = re.compile(r"(?:第\s*|rows?\s*|R)(\d{1,6})\s*(?:[–\-~至到]|\.\.)\s*(?:第\s*)?(\d{1,6})\s*行?", re.I)
ROW = re.compile(r"第\s*(\d{1,6})\s*行|\brow\s*(\d{1,6})\b", re.I)
COLS = re.compile(r"\b([A-Z]{1,2})\s*[–\-~:]\s*([A-Z]{1,2})\s*列|\b([A-Z]{1,2})\s*列")
SHOWN_ROWS = 12
SHOWN_COLS = 8


def _column_index(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + ord(char) - 64
    return value


def _column_letters(index: int) -> str:
    out = ""
    while index:
        index, rest = divmod(index - 1, 26)
        out = chr(65 + rest) + out
    return out


def cells_for(record: dict, input_dir: str) -> list[dict]:
    """The cited cells of a submitted workbook, rendered from the file at report
    time: the workbook and sheet the location names. Cells holding the values
    the finding quotes anchor the range, widened to their block's header row and
    label column; otherwise the rows and columns the location cites (the first
    rows and columns when it cites none). One range per cited sheet, at most two."""
    from openpyxl import load_workbook
    text = f"{record.get('location') or ''} {record.get('finding') or ''}"
    books = sorted(Path(input_dir).rglob("*.xls[xm]")) if input_dir and Path(input_dir).is_dir() else []
    named = [b for b in books if b.name in text or b.stem in text]
    values = _quoted_values(text)
    for book in named or books:
        try:
            workbook = load_workbook(book, read_only=True, data_only=True)
        except Exception:
            continue
        try:
            sheets = _cited_sheets(workbook.sheetnames, text)
            if not sheets:
                continue
            anchored = []
            for name in sheets:
                box = _anchor_box(workbook[name], values)
                if box:
                    anchored.append((name, box))
            if anchored:
                return [_cells(book.name, name, *box) for name, box in anchored[:2]]
            sheet = workbook[sheets[0]]
            width = sheet.max_column or SHOWN_COLS
            rows = ROWS.search(text)
            if rows:
                first = int(rows.group(1))
            else:
                single = ROW.search(text)
                first = int(next(g for g in single.groups() if g)) if single else 1
            first = max(1, first - 1)  # keep the row above (often the header)
            cols = COLS.search(text)
            if cols and cols.group(1):
                left, right = _column_index(cols.group(1)), _column_index(cols.group(2))
            elif cols:
                left = right = _column_index(cols.group(3))
            else:
                left, right = 1, min(width, SHOWN_COLS)
            left, right = sorted((left, right))
            right = min(right, left + SHOWN_COLS - 1)
            return [_cells(book.name, sheets[0], first, first + SHOWN_ROWS - 1, left, right)]
        finally:
            workbook.close()
    return []


def _cells(file: str, sheet: str, top: int, bottom: int, left: int, right: int) -> dict:
    cell_range = f"{_column_letters(left)}{top}:{_column_letters(right)}{bottom}"
    return {"file": file, "sheet": sheet, "range": cell_range,
            "caption": f"随文源数据 {sheet} 表 {cell_range}（单元格原值）"}


def _cited_sheets(names: list[str], text: str) -> list[str]:
    """Sheets the text names, in the order it first names them. A name followed
    by another digit ("Fig.1" in "Fig.10") is not a citation of that sheet; a
    panel letter is ("Fig.1" in "Fig.1d")."""
    found = []
    for name in sorted((n for n in names if n), key=len, reverse=True):
        spans = [m.start() for m in re.finditer(re.escape(name), text)
                 if not text[m.end():m.end() + 1].isdigit()]
        if spans:
            found.append((spans[0], name))
    return [name for _, name in sorted(found)]


# Values a finding quotes with enough digits to identify a cell.
QUOTED = re.compile(r"(?<![\w.])-?\d+\.\d{3,}(?![\d])")
SCAN_ROWS = 5000
SCAN_COLS = 200


def _quoted_values(text: str) -> list[str]:
    seen = []
    for value in QUOTED.findall(text):
        if value not in seen:
            seen.append(value)
    return seen[:8]


def _matches(cell, value: str) -> bool:
    """The cell holds the quoted value, written in full or rounded."""
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        return False
    decimals = len(value.split(".")[1])
    return abs(float(cell) - float(value)) <= 0.5 * 10 ** -decimals + 1e-12


def _anchor_box(sheet, values: list[str]):
    """(top, bottom, left, right) around the cells holding the quoted values,
    widened to the block they sit in (header row, label column) and capped at
    SHOWN_ROWS x SHOWN_COLS; None when no quoted value is in the sheet."""
    if not values:
        return None
    grid = [list(row[:SCAN_COLS]) for row in sheet.iter_rows(max_row=SCAN_ROWS, values_only=True)]
    hits = [(r, c) for r, row in enumerate(grid) for c, cell in enumerate(row)
            if any(_matches(cell, v) for v in values)]
    if not hits:
        return None

    def filled(r: int, c: int) -> bool:
        return 0 <= r < len(grid) and 0 <= c < len(grid[r]) and grid[r][c] not in (None, "")

    def text(r: int, c: int) -> bool:
        return filled(r, c) and isinstance(grid[r][c], str)

    top, bottom = min(r for r, _ in hits), max(r for r, _ in hits)
    left, right = min(c for _, c in hits), max(c for _, c in hits)
    # Up to the block's header row: the first row above that carries a label.
    while (top > 0 and bottom - top + 1 < SHOWN_ROWS
           and any(filled(top - 1, c) for c in range(left, right + 1))):
        top -= 1
        if any(text(top, c) for c in range(left, right + 1)):
            break
    # Left to the row-label column, then a title column that is filled only on
    # the header row ("Fig.1d" above "SSP1"); a neighbouring block placed side
    # by side starts beyond it.
    while (left > 0 and right - left + 1 < SHOWN_COLS
           and any(filled(r, left - 1) for r in range(top, bottom + 1))):
        left -= 1
        if any(text(r, left) for r in range(top + 1, bottom + 1)):
            break
    if (left > 0 and right - left + 1 < SHOWN_COLS and filled(top, left - 1)
            and not any(filled(r, left - 1) for r in range(top + 1, bottom + 1))):
        left -= 1
    # The rest of the block to the right and below, within the shown size.
    while right - left + 1 < SHOWN_COLS and any(filled(r, right + 1) for r in range(top, bottom + 1)):
        right += 1
    while bottom - top + 1 < SHOWN_ROWS and any(filled(bottom + 1, c) for c in range(left, right + 1)):
        bottom += 1
    return top + 1, bottom + 1, left + 1, right + 1
