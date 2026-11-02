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
    import fitz
    paragraphs = [b[3] for b in page.get_text("blocks") if len(" ".join(str(b[4]).split())) > 180]
    top = max(paragraphs, default=page.rect.y0 + 36)
    bottom = page.rect.y1 - 36
    return fitz.Rect(page.rect.x0 + 20, top + 2, page.rect.x1 - 20, bottom) if bottom - top >= 150 else None


def _table_rect(page, caption):
    import fitz
    x0, y0, x1, y1 = caption
    return fitz.Rect(page.rect.x0 + 20, y0 - 4, page.rect.x1 - 20, min(page.rect.y1, y0 + page.rect.height * 0.45))


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
