"""Deterministic per-paper dossier shared by every review session.

The measured bottleneck of the old flow was the model re-extracting the same
PDF text, figure pages and file listings in every stage. The dossier extracts
them once; sessions read small, page-addressed files through ``pv3``.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

DATA_EXTS = (".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt", ".docx", ".cif", ".pdb")
MAX_PAGES_PER_DOC = 400
CAPTION_RE = re.compile(
    r"^\s*(?P<label>(?:Extended\s+Data\s+|Supplementary\s+|Supporting\s+)?"
    r"(?:Fig(?:ure)?\.?|FIG(?:URE)?\.?|Table|TABLE|图|表)\s*(?P<num>S?\d+[A-Za-z]?))"
    r"\s*[\.:|：\-–—]?\s*(?P<rest>.{0,400})",
)
REF_HEADING_RE = re.compile(r"^\s*(References|REFERENCES|Reference|Bibliography|参考文献|Literature Cited|References and Notes)\s*$")


def _doc_id(index: int, path: Path, main: Path | None) -> str:
    if main is not None and path == main:
        return "main"
    stem = re.sub(r"[^A-Za-z0-9]+", "_", path.stem)[:40].strip("_") or "doc"
    return f"s{index}_{stem}"


def _list_pdfs(input_dir: Path) -> list[Path]:
    pdfs = []
    for path in sorted(input_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".pdf" and not path.name.startswith("."):
            pdfs.append(path)
    return pdfs


def _main_pdf(input_dir: Path, evidence: dict, pdfs: list[Path]) -> Path | None:
    candidate = ((evidence or {}).get("paper") or {}).get("filepath")
    if candidate and Path(candidate).is_file():
        return Path(candidate)
    try:
        from core.pipeline import find_pdf
        found = find_pdf(str(input_dir))
        if found:
            return Path(found)
    except Exception as exc:  # the detector heuristic is optional here
        log.debug("find_pdf failed: %s", exc)
    return max(pdfs, key=lambda p: p.stat().st_size) if pdfs else None


def _classify_caption(label: str) -> str:
    low = label.lower()
    if "table" in low or label.startswith("表"):
        return "table"
    return "figure"


def _extract_pdf(doc_id: str, path: Path, text_dir: Path) -> tuple[dict, list[dict], list[str]]:
    import fitz
    captions, warnings = [], []
    info = {"id": doc_id, "path": str(path), "name": path.name, "pages": 0}
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        warnings.append(f"{path.name}: 无法打开 PDF（{exc}）")
        return info, captions, warnings
    with doc:
        info["pages"] = doc.page_count
        if doc.page_count:
            rect = doc[0].rect
            info["page_size_pt"] = [round(rect.width, 1), round(rect.height, 1)]
        limit = min(doc.page_count, MAX_PAGES_PER_DOC)
        if doc.page_count > limit:
            warnings.append(f"{path.name}: 共 {doc.page_count} 页，仅抽取前 {limit} 页文本")
        chars = 0
        for number in range(limit):
            try:
                text = doc[number].get_text("text")
            except Exception as exc:
                warnings.append(f"{path.name} 第 {number + 1} 页文本抽取失败（{exc}）")
                text = ""
            chars += len(text.strip())
            (text_dir / f"{doc_id}_p{number + 1:03d}.txt").write_text(text, encoding="utf-8")
            seen = set()
            for line in text.splitlines():
                match = CAPTION_RE.match(line)
                if not match:
                    continue
                label = re.sub(r"\s+", " ", match.group("label")).strip()
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                captions.append({
                    "doc": doc_id, "page": number + 1, "label": label,
                    "kind": _classify_caption(label), "number": match.group("num"),
                    "text": (match.group("rest") or "").strip()[:300],
                })
        info["text_chars"] = chars
        if limit and chars < 200 * limit * 0.1:
            warnings.append(f"{path.name}: 可抽取文本很少，可能是扫描件，需要看渲染图")
    return info, captions, warnings


def _reference_section(text_dir: Path, doc: dict) -> str:
    pages = [text_dir / f"{doc['id']}_p{n:03d}.txt" for n in range(1, min(doc.get("pages", 0), MAX_PAGES_PER_DOC) + 1)]
    lines: list[tuple[int, str]] = []
    for number, page in enumerate(pages, start=1):
        if page.is_file():
            lines.extend((number, line) for line in page.read_text(encoding="utf-8").splitlines())
    start = None
    for index, (_, line) in enumerate(lines):
        if REF_HEADING_RE.match(line):
            start = index
    if start is None:
        return ""
    out, size = [], 0
    current_page = None
    for number, line in lines[start:]:
        if number != current_page:
            out.append(f"[第{number}页]")
            current_page = number
        out.append(line)
        size += len(line)
        if size > 80000:
            out.append("…（参考文献段过长，已截断；其余用 pv3 page 查看）")
            break
    return "\n".join(out)


def _compact_inventory(evidence: dict) -> list[dict]:
    files = []
    for entry in (evidence or {}).get("inventory") or []:
        sheets = []
        for sheet in entry.get("sheets") or []:
            if not isinstance(sheet, dict):
                continue
            columns = [c for c in sheet.get("columns") or [] if isinstance(c, dict)]
            sheets.append({
                "sheet": sheet.get("sheet") or sheet.get("name"),
                "rows": sheet.get("n_rows") or sheet.get("rows"),
                "cols": len(columns),
                # Column role from the deterministic classifier, e.g. axis vs measurement.
                "columns": [f"{str(c.get('header'))[:40]}<{c.get('category', '?')}>" for c in columns[:30]],
            })
        files.append({"file": entry.get("file") or Path(str(entry.get("path", ""))).name,
                      "path": entry.get("path"), "sheets": sheets[:200]})
    return files


def _image_alerts(report: dict) -> list[dict]:
    alerts = []
    for index, item in enumerate(report.get("image_duplicates") or []):
        alerts.append({
            "id": f"img{index}", "type": item.get("match_type"), "severity": item.get("severity"),
            "pages": [item.get("page_a"), item.get("page_b")],
            "score": round(float(item.get("similarity_score") or 0), 3),
            "annotation": item.get("annotation_path"),
            "image_a": item.get("image_a"), "image_b": item.get("image_b"),
            "region_a": item.get("region_a"), "region_b": item.get("region_b"),
            "details": str(item.get("details") or "")[:200],
        })
    for index, item in enumerate(report.get("image_splicing") or []):
        alerts.append({
            "id": f"splice{index}", "type": "splicing", "severity": item.get("severity", "high"),
            "pages": [item.get("page")], "annotation": item.get("annotation_path"),
            "image_a": item.get("image_path") or item.get("image"),
            "details": str(item.get("details") or item.get("description") or "")[:200],
        })
    return alerts


def _reference_alerts(report: dict) -> list[dict]:
    out = []
    for item in report.get("reference_issues") or []:
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        out.append({
            "ref": item.get("ref_number"), "type": item.get("issue_type"), "severity": item.get("severity"),
            "text": str(item.get("ref_text") or "")[:300],
            "matched_doi": details.get("matched_doi"), "title_similarity": details.get("title_similarity"),
            "description": str(item.get("description") or "")[:200],
        })
    return out


def _data_files(input_dir: Path) -> list[str]:
    return sorted(str(p) for p in input_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in DATA_EXTS
                  and p.name.lower() not in {"doi.txt"} and not p.name.startswith(("~$", ".")))


def build(workdir: Path, doi: str, input_dir: str, report_json: str, evidence: dict) -> dict:
    """Build (or reuse) the dossier. Returns the index dict."""
    root = Path(workdir) / "dossier"
    index_path = root / "index.json"
    if index_path.is_file():
        try:
            return json.loads(index_path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    text_dir = root / "text"
    text_dir.mkdir(parents=True, exist_ok=True)
    (root / "renders").mkdir(exist_ok=True)
    input_path = Path(input_dir)
    try:
        report = json.loads(Path(report_json).read_text(encoding="utf-8")) if report_json else {}
    except (OSError, ValueError):
        report = {}

    pdfs = _list_pdfs(input_path) if input_path.is_dir() else []
    main = _main_pdf(input_path, evidence, pdfs)
    if main is not None and main not in pdfs:
        pdfs.insert(0, main)
    ordered = ([main] if main else []) + [p for p in pdfs if p != main]
    docs, captions, warnings = [], [], []
    for index, path in enumerate(ordered):
        info, doc_captions, doc_warnings = _extract_pdf(_doc_id(index, path, main), path, text_dir)
        docs.append(info)
        captions.extend(doc_captions)
        warnings.extend(doc_warnings)
    if not docs:
        warnings.append("未找到论文 PDF")

    references = _reference_section(text_dir, docs[0]) if docs and docs[0]["id"] == "main" else ""
    (root / "references.txt").write_text(references, encoding="utf-8")

    # Only whole-figure numbers: "Fig. 4d" at a line start is usually an
    # in-text panel reference, not a caption.
    main_figures = sorted({c["number"] for c in captions if c["doc"] == "main" and c["kind"] == "figure"
                           and c["number"].isdigit() and "supplementary" not in c["label"].lower()
                           and "extended" not in c["label"].lower()},
                          key=int)
    index = {
        "doi": doi,
        "input_dir": str(input_path),
        "report_json": report_json,
        "paper": {k: (evidence.get("paper") or report.get("paper") or {}).get(k)
                  for k in ("title", "journal", "doi", "total_pages")},
        "docs": docs,
        "captions": captions,
        "main_figure_numbers": main_figures,
        "data_files": _data_files(input_path) if input_path.is_dir() else [],
        "sheets": _compact_inventory(evidence),
        "image_alerts": _image_alerts(report),
        "reference_alerts": _reference_alerts(report),
        "content_screening": report.get("content_screening") or {},
        "analysis_warnings": [str(w)[:300] for w in (report.get("analysis_warnings") or [])][:40],
        "warnings": warnings,
    }
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return index
