import json
import logging
import re
from pathlib import Path

import numpy as np
import fitz


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.pdf_utils import extract_images, extract_text
from modules.image_checker import check_image_duplicates
from modules.splice_checker import check_splicing
from modules.data_checker import check_data_anomalies
from modules.reference_checker import check_references
from modules.chinese_report_generator import generate_chinese_pdf

log = logging.getLogger(__name__)


def find_pdf(paper_dir: str) -> str | None:
    """Find the main PDF file in the paper directory.

    Priority: 1) filename contains 'main'  2) matches dir name  3) last PDF sorted by name.
    """
    d = Path(paper_dir)
    pdfs = list(d.glob("*.pdf")) + list(d.glob("*.PDF"))
    if not pdfs:
        return None
    if len(pdfs) == 1:
        return str(pdfs[0])
    for p in sorted(pdfs):
        if "main" in p.stem.lower():
            log.info("Multiple PDFs found, using %s (contains 'main')", p.name)
            return str(p)
    dir_name = d.name
    for p in pdfs:
        if p.stem == dir_name:
            log.info("Multiple PDFs found, using %s (matches directory name)", p.name)
            return str(p)
    last = sorted(pdfs)[-1]
    log.info("Multiple PDFs found in %s, using last: %s", paper_dir, last.name)
    return str(last)


def find_data_dir(paper_dir: str) -> str | None:
    """Find source data directory or files in the paper directory."""
    d = Path(paper_dir)

    data_extensions = {".xlsx", ".xls", ".csv", ".docx", ".fcs", ".sav"}
    data_files = []
    for ext in data_extensions:
        data_files.extend(d.rglob(f"*{ext}"))

    if not data_files:
        return None

    return str(d)


def extract_metadata(pdf_path: str) -> dict:
    """Extract title, author, DOI, journal from PDF metadata."""
    doc = fitz.open(pdf_path)
    meta = doc.metadata or {}
    doc.close()

    title = meta.get("title", "") or ""
    author = meta.get("author", "") or ""
    subject = meta.get("subject", "") or ""

    doi = ""
    journal = ""
    if subject:
        if "doi:" in subject.lower():
            idx = subject.lower().index("doi:")
            journal = subject[:idx].strip().rstrip(",").strip()
            doi = subject[idx + 4:].strip()
            doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', doi)
        elif "10." in subject:
            m = re.search(r'(10\.\d{4,}/\S+)', subject)
            if m:
                doi = m.group(1)
                journal = subject[:m.start()].strip().rstrip(",").strip()
            else:
                journal = subject
        else:
            journal = subject

    if not title:
        title = extract_title_from_text(pdf_path) or Path(pdf_path).stem

    return {
        "title": title,
        "author": author,
        "doi": doi,
        "journal": journal,
    }


def extract_title_from_text(pdf_path: str) -> str:
    """Extract title from first page text when PDF metadata has no title."""
    doc = fitz.open(pdf_path)
    if len(doc) == 0:
        doc.close()
        return ""
    page = doc[0]
    blocks = page.get_text("dict")["blocks"]
    doc.close()

    candidate_lines = []
    for block in blocks:
        if block["type"] != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if not text or len(text) < 5:
                continue
            max_size = max(span["size"] for span in line["spans"])
            candidate_lines.append((max_size, text))

    if not candidate_lines:
        return ""

    max_font_size = max(c[0] for c in candidate_lines)
    title_parts = []
    for size, text in candidate_lines:
        if size >= max_font_size * 0.9:
            if any(kw in text.lower() for kw in ["©", "copyright", "doi:", "vol.", "issn"]):
                continue
            title_parts.append(text)
        elif title_parts:
            break

    title = " ".join(title_parts).strip()
    if len(title) > 300:
        title = title[:300]
    return title


def extract_full_metadata(pdf_path: str) -> dict:
    """Use LLM to extract all authors and affiliations from first pages of PDF text."""
    from utils.llm_client import chat

    text_pages = extract_text(pdf_path)
    first_pages_text = "\n\n".join(p["text"] for p in text_pages[:4])

    prompt = f"""从以下学术论文的前几页文本中，提取所有作者姓名和机构信息。

要求：
1. 提取完整的作者列表（保持原文顺序）
2. 提取所有机构/单位信息
3. 作者姓名和机构名称必须保持论文原文语言和拼写，不要翻译、意译或中英混写
4. 如果无法确认完整机构，宁可留空也不要补写或翻译
5. 用JSON格式返回，格式如下：
{{"authors": ["作者1", "作者2", ...], "affiliations": ["1. 机构1", "2. 机构2", ...]}}
6. 只输出JSON，不要其他文字

论文文本：
{first_pages_text[:6000]}"""

    system = "你是学术论文元数据提取专家。请从论文文本中准确提取作者和机构信息，保持原文语言和拼写，不要翻译，以JSON格式返回。只输出JSON。"

    try:
        response = chat(prompt, system=system, temperature=0.1)
        response = re.sub(r'^```(?:json)?\s*', '', response)
        response = re.sub(r'\s*```$', '', response)
        import json as _json
        result = _json.loads(response)
        return {
            "authors_full": result.get("authors", []),
            "affiliations": result.get("affiliations", []),
        }
    except Exception as e:
        log.warning("Failed to extract full metadata via LLM: %s", e)
        return {"authors_full": [], "affiliations": []}


def analyze_paper(paper_dir: str, output_dir: str, skip_refs: bool = False, chinese_reports_dir: str = None, author_type: str = "", doi_override: str = "") -> dict:
    """Run the full analysis pipeline on a single paper."""
    paper_dir = str(Path(paper_dir).resolve())
    output_dir = str(Path(output_dir).resolve())
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    pdf_path = find_pdf(paper_dir)
    if not pdf_path:
        raise FileNotFoundError(f"No PDF found in {paper_dir}")

    pdf_name = Path(pdf_path).name
    metadata = extract_metadata(pdf_path)

    # A user-supplied DOI is authoritative: apply it before the doi.txt/folder-name
    # fallback, CrossRef enrichment, the reference self-DOI header filter, and DB metadata.
    if doi_override:
        metadata["doi"] = doi_override

    if not metadata.get("doi"):
        doi_file = Path(paper_dir) / "doi.txt"
        if doi_file.exists():
            metadata["doi"] = doi_file.read_text(encoding="utf-8").strip()
        else:
            folder_name = Path(paper_dir).name
            if folder_name.startswith("10."):
                metadata["doi"] = folder_name.replace("_", "/", 1)

    from utils.crossref import is_bad_title, enrich_metadata
    if metadata.get("doi") and (is_bad_title(metadata.get("title", "")) or not metadata.get("journal")):
        crossref_info = enrich_metadata(metadata["doi"])
        if crossref_info:
            if is_bad_title(metadata.get("title", "")) and crossref_info["title"]:
                log.info("Fixed bad title via CrossRef: %s -> %s", metadata["title"][:40], crossref_info["title"][:60])
                metadata["title"] = crossref_info["title"]
            if not metadata.get("journal") and crossref_info["journal"]:
                metadata["journal"] = crossref_info["journal"]

    log.info("=" * 60)
    log.info("Analyzing: %s", metadata.get("title", pdf_name))
    log.info("  Author: %s | Journal: %s | DOI: %s", metadata.get("author"), metadata.get("journal"), metadata.get("doi"))
    log.info("=" * 60)

    doc = fitz.open(pdf_path)
    total_pages = len(doc)
    doc.close()

    log.info("[1/4] Extracting images from PDF...")
    images = extract_images(pdf_path, output_dir)
    log.info("Extracted %d images from PDF", len(images))

    from utils.pdf_utils import extract_standalone_images
    standalone = extract_standalone_images(paper_dir, output_dir)
    if standalone:
        log.info("Found %d standalone images (TIF/JPEG/PNG)", len(standalone))
        images = images + standalone

    log.info("[2/4] Checking image duplicates...")
    image_results = check_image_duplicates(images, output_dir)

    log.info("Screening images for splicing artifacts...")
    splice_results = check_splicing(images, output_dir)

    log.info("[3/4] Checking source data anomalies...")
    data_dir = find_data_dir(paper_dir)
    data_results = check_data_anomalies(data_dir) if data_dir else []

    log.info("[4/4] Verifying references...")
    if skip_refs:
        log.info("Reference check skipped (--skip-refs)")
        ref_results = []
    else:
        ref_results = check_references(pdf_path, metadata.get("doi"))

    ref_count = 0
    text_pages = extract_text(pdf_path)
    full_text = "\n".join(p["text"] for p in text_pages)
    ref_match = re.search(r'(?:References|REFERENCES|Bibliography)', full_text)
    if ref_match:
        ref_section = full_text[ref_match.end():]
        ref_count = len(re.findall(r'^\s*\[?\d{1,3}\]?[.\s]', ref_section, re.MULTILINE))

    first_pages_text = "\n\n".join(p["text"] for p in text_pages[:4])

    findings = {
        "paper": {
            "filename": pdf_name,
            "filepath": pdf_path,
            "title": metadata.get("title", pdf_name),
            "author": metadata.get("author", ""),
            "authors_full": [],
            "affiliations": [],
            "journal": metadata.get("journal", ""),
            "doi": metadata.get("doi", ""),
            "total_pages": total_pages,
            "total_images": len(images),
            "total_references": ref_count,
            "sjtu_author_type": author_type,
        },
        "image_duplicates": image_results,
        "image_splicing": splice_results,
        "data_anomalies": data_results,
        "reference_issues": ref_results,
        "summary": {
            "total_issues": len(image_results) + len(data_results) + len(ref_results) + len(splice_results),
            "image_issues": len(image_results),
            "image_splicing_suspects": len(splice_results),
            "data_issues": len(data_results),
            "reference_issues": len(ref_results),
            "high_severity": sum(1 for r in image_results + data_results + ref_results if r.get("severity") == "high"),
            # splice findings are always severity 'medium' (conservative pre-screen)
            "medium_severity": sum(1 for r in image_results + data_results + ref_results if r.get("severity") == "medium") + len(splice_results),
            "low_severity": sum(1 for r in image_results + data_results + ref_results if r.get("severity") == "low"),
        },
    }

    log.info("Generating Chinese PDF report (combined metadata + analysis)...")
    cn_path = None
    try:
        cn_dir = chinese_reports_dir or str(Path(output_dir).parent / "chinese_reports")
        cn_path, full_meta = generate_chinese_pdf(findings, cn_dir, first_pages_text)
        findings["paper"]["authors_full"] = full_meta.get("authors_full", [])
        findings["paper"]["affiliations"] = full_meta.get("affiliations", [])
        log.info("Found %d authors, %d affiliations",
                 len(findings["paper"]["authors_full"]), len(findings["paper"]["affiliations"]))
        if cn_path:
            log.info("Chinese PDF: %s", cn_path)
        else:
            log.error("Chinese PDF generation returned None, paper will not be inserted to DB")
    except Exception as e:
        log.error("Failed to generate Chinese PDF: %s", e)

    # Persisted to report.json below so downstream readers (main.py DB guards, API
    # self-insert paths) skip inserting records that would point at a missing PDF.
    findings["pdf_generated"] = cn_path is not None

    json_path = Path(output_dir) / "report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2, default=str, cls=NumpyEncoder)
    log.info("Saved structured results to %s", json_path)

    log.info("Analysis complete. Total issues: %d", findings["summary"]["total_issues"])
    return findings
