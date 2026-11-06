import json
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import fitz

from modules.doi_utils import normalize_doi


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

from utils.pdf_utils import extract_full_text, extract_images, extract_text
from modules.image_checker import check_image_duplicates
from modules.splice_checker import check_splicing
from modules.data_checker import DATA_FILE_EXTENSIONS, check_data_anomalies, is_metadata_data_file
from modules.reference_checker import check_references
from modules.chinese_report_generator import generate_chinese_pdf
from modules.initial_report_bundle import generate_initial_pdfs
from modules.initial_content_screen import screen_manuscript

log = logging.getLogger(__name__)


def find_pdf(paper_dir: str) -> str | None:
    """Find the main PDF file in the paper directory.

    Prefer the manuscript/article PDF when uploads contain peer-review files,
    author responses, cover letters, or supplemental PDFs. Also exclude PDFs
    that live inside known supplementary/figure sub-directories
    (extended_data/, supplementary/, figure/, supporting_information/, etc.)
    so they are not mistakenly treated as the main paper.
    """
    d = Path(paper_dir)
    pdfs = list(d.glob("*.pdf")) + list(d.glob("*.PDF"))
    if not pdfs:
        return None

    # Exclude PDFs that live inside a known supplementary/figure sub-directory.
    try:
        from core.nature_adapter import SUPPLEMENTARY_DIR_PATTERNS as _SUPSET
    except Exception:
        _SUPSET = (
            "extended_data", "extended data", "extend data", "expended data",
            "supplementary", "supplement", "supplemental", "supplementary_material",
            "supporting_information", "supporting info", "supporting", "si",
            "source_data", "source data", "supp_data", "additional", "appendix",
            "附录", "补充材料", "附加材料", "figure", "figures", "fig", "figs",
            "图片", "图表",
        )
    supset_lower = {p.lower() for p in _SUPSET}
    pdfs = [p for p in pdfs if p.parent == d or p.parent.name.lower() not in supset_lower]
    if not pdfs:
        return None
    if len(pdfs) == 1:
        return str(pdfs[0])

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    dir_key = norm(d.name)
    positive_terms = (
        "manuscript", "article", "paper", "main", "fulltext", "full_text",
        "maintext", "main_text", "ms", "论文", "正文",
    )
    negative_terms = (
        "peer_review", "peer-review", "peerreview", "reviewer", "review",
        "response", "rebuttal", "reply", "cover_letter", "coverletter",
        "supplement", "supplementary", "supporting", "appendix", "附录",
        "补充", "审稿", "回复",
    )

    def score(p: Path) -> tuple[int, int, str]:
        stem = p.stem.lower()
        key = norm(p.stem)
        value = 0
        reasons = []

        if key == dir_key:
            value += 80
            reasons.append("matches directory name")
        elif dir_key and (dir_key in key or key in dir_key):
            value += 35
            reasons.append("partially matches directory name")

        for term in positive_terms:
            if term in stem:
                value += 50
                reasons.append(f"contains '{term}'")
                break

        for term in negative_terms:
            if term in stem:
                value -= 120
                reasons.append(f"excludes '{term}'")
                break

        # Prefer likely article-sized PDFs over tiny covers or huge review packs
        # only as a weak tie breaker; filename semantics carry the decision.
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        if 200_000 <= size <= 80_000_000:
            value += 2

        return value, size, "; ".join(reasons) or "best filename score"

    scored = [(score(p), p) for p in pdfs]
    (chosen_score, _size, reason), chosen = max(
        scored,
        key=lambda item: (item[0][0], item[0][1], item[1].name.lower()),
    )
    log.info(
        "Multiple PDFs found in %s, using %s (%s, score=%s)",
        paper_dir,
        chosen.name,
        reason,
        score(chosen)[0],
    )
    return str(chosen)


def find_data_dir(paper_dir: str, extra_dirs: list[str] | None = None) -> str | None:
    """Find source data directory or files in the paper directory.

    extra_dirs: optional list of additional directories to include in the
    scan (e.g. the `<output_dir>/data_tables/` folder holding CSVs extracted
    from supplementary PDFs). They are scanned as siblings of paper_dir.
    """
    d = Path(paper_dir)
    scan_roots = [d] + [Path(x) for x in (extra_dirs or []) if x]

    data_files = []
    for root in scan_roots:
        if not root.exists():
            continue
        for ext in DATA_FILE_EXTENSIONS:
            data_files.extend(p for p in root.rglob(f"*{ext}") if not is_metadata_data_file(p))

    if not data_files:
        return None

    # Return the first existing root (data_checker only needs *a* base to walk);
    # the data_checker uses rglob anyway so this works as long as paper_dir is
    # the common ancestor. If paper_dir is missing, fall back to a real root.
    for root in scan_roots:
        if root.exists():
            return str(root)
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
            # 2026-06-26: 一些出版商 (Oxford UP, Elsevier) 把整段 abstract 塞到 PDF subject,
            # 形如 "DOI: 10.1093/bjd/ljaf185; British Journal of Dermatology, ...; Abstract: ...".
            # 旧代码 doi = subject[idx+4:] 会拿掉整段 200 字符。让 normalize_doi 重新抽出第一段合法 DOI。
            raw_doi = subject[idx + 4:].strip()
            raw_doi = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', raw_doi)
            from modules.doi_utils import normalize_doi as _norm_doi
            doi = _norm_doi(raw_doi)
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


class _SkipPdf(Exception):
    """The initial PDF is deliberately not rendered."""


def skip_initial_pdf(risk_level: str) -> bool:
    """High-risk screenings get no PDF of their own: they always go on to
    review and the review report is what is delivered (2026-11-06)."""
    return str(risk_level or "").strip() == "高风险" and os.environ.get("PAPER_INITIAL_PDF_FOR_HIGH_RISK", "0") != "1"


def initial_pdf_skipped(findings: dict) -> bool:
    """True when screening finished without a PDF on purpose (low- or high-risk skip)."""
    return bool(findings.get("pdf_skipped_low_risk") or findings.get("pdf_skipped_high_risk"))


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

    if os.environ.get("PAPER_CLEAN_JUNK", "1") != "0":
        try:
            from utils.paper_preprocessor import clean_junk_files
            _junk_stats = clean_junk_files(paper_dir, dry_run=False, log_fn=log.info)
            removed = sum(_junk_stats.get("actions", {}).values())
            if removed:
                log.info("clean-junk removed %d noise entries in %s", removed, paper_dir)
            for _w in _junk_stats.get("warnings", []):
                log.warning("clean-junk: %s", _w)
        except Exception as _e:
            log.warning("clean_junk_files failed (continuing without): %s", _e)

    # Optional diagnostics only. Duplicate source files are skipped by the
    # image/data readers themselves, so screening does not need a full-tree
    # preprocessing scan or any mutation of uploaded sources.
    if os.environ.get("PAPER_PREPROCESS", "0") == "1":
        try:
            from utils.paper_preprocessor import preprocess_paper_dir
            _pre_stats = preprocess_paper_dir(paper_dir, dry_run=True, log_fn=log.info)
            if _pre_stats.get("touched"):
                log.info("preprocess dry-run would touch %d paths in %s",
                         len(_pre_stats["touched"]), paper_dir)
            for _w in _pre_stats.get("warnings", []):
                log.warning("preprocess: %s", _w)
        except Exception as _e:
            log.warning("preprocess_paper_dir failed (continuing without): %s", _e)

    pdf_path = find_pdf(paper_dir)
    if not pdf_path:
        raise FileNotFoundError(f"No PDF found in {paper_dir}")

    pdf_name = Path(pdf_path).name
    metadata = extract_metadata(pdf_path)

    # A user-supplied DOI is authoritative: apply it before the doi.txt/folder-name
    # fallback, CrossRef enrichment, the reference self-DOI header filter, and DB metadata.
    if doi_override:
        metadata["doi"] = normalize_doi(doi_override) or doi_override.strip()

    doi_mismatch = None
    if not metadata.get("doi"):
        doi_file = Path(paper_dir) / "doi.txt"
        if doi_file.exists():
            metadata["doi"] = normalize_doi(doi_file.read_text(encoding="utf-8"))
        else:
            folder_name = Path(paper_dir).name
            from modules.doi_utils import dir_name_to_doi
            metadata["doi"] = normalize_doi(dir_name_to_doi(folder_name))
    elif not doi_override:
        # The DOI came from the PDF itself. A directory named after another DOI
        # most likely holds a misplaced file; the result is kept but not filed.
        from modules.doi_utils import dir_doi_conflict
        conflict = dir_doi_conflict(Path(paper_dir).name, metadata["doi"])
        if conflict:
            doi_mismatch = {"pdf_doi": metadata["doi"], "directory_doi": conflict}
            log.warning("DOI in the PDF (%s) differs from the directory name (%s): the file may be misplaced; "
                        "the result will not be written to the database", metadata["doi"], conflict)

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
    # Extract inline images from any .docx files under paper_dir BEFORE
    # extract_standalone_images walks the tree — that way the dumped images
    # get included in the standalone image set, deduped by file fingerprint.
    try:
        from utils.docx_extractor import extract_docx_images_in_dir
        docx_imgs = extract_docx_images_in_dir(paper_dir, output_dir)
        if docx_imgs:
            log.info("Extracted %d inline image(s) from .docx files", len(docx_imgs))
    except Exception as _e:
        log.warning("docx image extraction failed: %s", _e)
        docx_imgs = []

    # Only this run's docx images: <output_dir>/images may still hold PDF
    # extractions from an interrupted run, which would otherwise be re-read as
    # page-0 standalone images and shadow the real supplementary extraction.
    standalone = extract_standalone_images(paper_dir, output_dir, max_images=50,
                                            extra_image_files=[str(p) for p in docx_imgs])
    if standalone:
        if images:
            existing_image_paths = set()
            for img in images:
                try:
                    existing_image_paths.add(Path(img.filepath).resolve())
                except OSError:
                    existing_image_paths.add(Path(img.filepath))
            before = len(standalone)
            standalone = [
                img for img in standalone
                if Path(img.filepath).resolve() not in existing_image_paths
            ]
            skipped = before - len(standalone)
            if skipped:
                log.info(
                    "Skipped %d standalone images that point to already extracted PDF images",
                    skipped,
                )

            figures_dir = (Path(paper_dir) / "figures").resolve()
            before = len(standalone)
            standalone = [
                img for img in standalone
                if not Path(img.filepath).resolve().is_relative_to(figures_dir)
            ]
            skipped = before - len(standalone)
            if skipped:
                log.info(
                    "Skipped %d standalone article figure images already represented in PDF",
                    skipped,
                )
        log.info("Found %d standalone images (TIF/JPEG/PNG/GIF)", len(standalone))
        images = images + standalone

    # Mine images from supplementary/figure PDFs (extended_data, supplementary,
    # figure/, supporting_information, etc.). These were previously dropped on
    # the main pipeline path because find_pdf excludes them as "supplement".
    try:
        from core.nature_adapter import find_image_pdfs as _find_image_pdfs
        supp_pdfs = _find_image_pdfs(paper_dir)
    except Exception as _e:
        log.warning("find_image_pdfs lookup failed: %s", _e)
        supp_pdfs = []
    # Deduplicate by content fingerprint (size+SHA256 of the extracted PNG),
    # NOT by file path — supplementary PDFs and main PDF both produce images
    # named page_XXX_img_XXX.png inside the same output/images/ folder, so
    # path-based dedup would drop most of the supplementary figures.
    try:
        from utils.pdf_utils import _file_fingerprint as _fp
    except Exception:
        _fp = None
    existing_fps: set = set()
    for im in images:
        if _fp is None:
            continue
        try:
            existing_fps.add(_fp(Path(im.filepath)))
        except Exception:
            pass
    supp_main_pdf_resolved = Path(pdf_path).resolve()
    supp_images_added = 0
    supp_dups_skipped = 0
    for spdf in supp_pdfs:
        spdf_resolved = Path(spdf).resolve()
        if spdf_resolved == supp_main_pdf_resolved:
            continue
        try:
            supp_imgs = extract_images(spdf, output_dir)
        except Exception as _e:
            log.warning("Failed to extract images from supplementary PDF %s: %s", spdf, _e)
            continue
        # Drop images whose extracted PNG is content-identical to one already in
        # the main set. Different paths, same content → truly duplicate.
        kept = []
        for im in supp_imgs:
            if _fp is not None:
                try:
                    fpf = _fp(Path(im.filepath))
                except Exception:
                    fpf = None
                if fpf is not None and fpf in existing_fps:
                    supp_dups_skipped += 1
                    continue
                if fpf is not None:
                    existing_fps.add(fpf)
            kept.append(im)
        images.extend(kept)
        supp_images_added += len(kept)
    if supp_images_added or supp_dups_skipped:
        log.info(
            "Extracted %d additional images from %d supplementary PDF(s) (skipped %d content-duplicates)",
            supp_images_added, len(supp_pdfs), supp_dups_skipped,
        )

    # Extract tables from main PDF + supplementary PDFs → CSVs in
    # `<output_dir>/data_tables/`. data_checker picks them up via the
    # find_data_dir fallback below.
    extraction_warnings = []
    n_tables = 0
    try:
        from utils.pdf_table_extractor import extract_tables_from_pdf
        try:
            n_tables += len(extract_tables_from_pdf(pdf_path, output_dir))
        except Exception as _e:
            extraction_warnings.append(f"正文PDF表格提取未完成: {_e}")
            log.warning("Table extraction from main PDF failed: %s", _e)
        for spdf in supp_pdfs:
            try:
                n_tables += len(extract_tables_from_pdf(spdf, output_dir))
            except Exception as _e:
                extraction_warnings.append(f"{Path(spdf).name}表格提取未完成: {_e}")
                log.warning("Table extraction from supplementary PDF %s failed: %s", spdf, _e)
        if n_tables:
            log.info("Extracted %d table(s) total from PDFs into %s/data_tables/",
                     n_tables, output_dir)
    except ImportError:
        extraction_warnings.append("PDF表格提取工具不可用，未完成结构化表格检查")

    log.info("[2/4] Checking image duplicates...")
    image_results = check_image_duplicates(images, output_dir)

    log.info("Screening images for splicing artifacts...")
    splice_results = check_splicing(images, output_dir)

    log.info("[3/4] Checking source data anomalies...")
    # Merge paper_dir with <output>/data_tables/ (PDF-extracted CSVs) so
    # data_checker walks both trees. extra_dirs is a no-op if data_tables/ is
    # empty or absent.
    tables_dir = Path(output_dir) / "data_tables"
    extra = [str(tables_dir)] if tables_dir.exists() and any(tables_dir.glob("*.csv")) else []
    data_dirs_for_check = [paper_dir] + extra if extra else paper_dir
    from utils.safe_data_checker import check_data_anomalies_safe
    data_results, data_warnings = check_data_anomalies_safe(data_dirs_for_check)
    data_warnings.extend(extraction_warnings)
    if data_warnings:
        log.warning("Source-data screening warnings: %s", "; ".join(data_warnings))

    # Shared by workflow and the API's main.py detection subprocess. This reads
    # source material, never review results or an existing report.json.
    try:
        from modules.quantitative_consistency import check_quantitative_consistency
        quantitative_issues, quantitative_warnings = check_quantitative_consistency(pdf_path, paper_dir)
        data_results.extend(quantitative_issues)
        data_warnings.extend(quantitative_warnings)
    except Exception as exc:
        data_warnings.append(f"数量一致性核查未完成: {type(exc).__name__}: {exc}")
        log.exception("Quantitative consistency check failed")

    log.info("[4/4] Verifying references...")
    if skip_refs:
        log.info("Reference check skipped (--skip-refs)")
        ref_results = []
    else:
        ref_results = check_references(pdf_path, metadata.get("doi"))

    ref_count = 0
    text_pages = extract_text(pdf_path)
    full_text = "\n".join(p["text"] for p in text_pages)
    from modules.reference_checker import _extract_references
    reference_text = extract_full_text(pdf_path)
    if not n_tables and re.search(r"(?i)\bTable\s+\d+|表\s*\d+", reference_text):
        data_warnings.append("正文含表格引用，但未提取到结构化PDF表格；可识别的数量关系检查不代表已完成全部表格核验")
    parsed_refs = _extract_references(reference_text, metadata.get("doi"))
    numeric_ref_numbers = [
        int(ref["number"])
        for ref in parsed_refs
        if str(ref.get("number", "")).isdigit()
    ]
    ref_count = max(numeric_ref_numbers, default=len(parsed_refs))

    # Check contradictions that live in the manuscript itself rather than in
    # attached source-data files: broken constraints, missing result values,
    # and figure-condition label ordering.
    try:
        from modules.manuscript_consistency_checker import check_manuscript_consistency
        consistency_issues = check_manuscript_consistency(pdf_path, reference_text)
        data_results.extend(consistency_issues)
        if consistency_issues:
            log.info("Manuscript consistency checks: %d finding(s)", len(consistency_issues))
    except Exception as exc:
        log.warning("Manuscript consistency checks failed (continuing): %s", exc)

    # Cross-document checks must run in the initial screening itself.  They compare
    # statements in the article with attached supplementary tables, so they cannot
    # be recovered by scanning either file in isolation.
    if supp_pdfs:
        try:
            from modules.supplementary_consistency_checker import check_supplementary_consistency
            supplementary_issues = check_supplementary_consistency(pdf_path, supp_pdfs)
            data_results.extend(supplementary_issues)
            if supplementary_issues:
                log.info(
                    "Main/supplement consistency checks: %d finding(s)",
                    len(supplementary_issues),
                )
        except Exception as exc:
            log.warning("Main/supplement consistency checks failed (continuing): %s", exc)

    # Short correspondence pieces can make a content-level error while citing a
    # perfectly real paper.  DOI existence checks cannot detect that, so compare
    # named subtypes with the cited article when references are enabled.
    if not skip_refs:
        try:
            from modules.citation_content_checker import check_citation_content_consistency
            citation_content_issues = check_citation_content_consistency(pdf_path)
            ref_results.extend(citation_content_issues)
            if citation_content_issues:
                log.info(
                    "Citation content consistency checks: %d finding(s)",
                    len(citation_content_issues),
                )
        except Exception as exc:
            log.warning("Citation content consistency checks failed (continuing): %s", exc)

    # Humanities-oriented integrity checks feed the existing reference/data issue
    # streams. They do not introduce a new risk dimension or alter risk thresholds.
    try:
        from modules.humanities_checker import check_humanities_integrity
        humanities = check_humanities_integrity(
            reference_text,
            parsed_refs,
            skip_references=skip_refs,
        )
        ref_results.extend(humanities["reference_issues"])
        data_results.extend(humanities["text_issues"])
        if humanities["reference_issues"] or humanities["text_issues"]:
            log.info(
                "Humanities integrity checks: %d reference/text findings, %d text findings",
                len(humanities["reference_issues"]),
                len(humanities["text_issues"]),
            )
    except Exception as exc:
        log.warning("Humanities integrity checks failed (continuing): %s", exc)

    # API detection and batch workflows both dispatch through this pipeline.
    # Read the paper independently BEFORE scoring; a detector-only report cannot
    # discover a missing manuscript contradiction from an empty findings list.
    content_audit_path = Path(output_dir) / "initial_content_screen.json"
    try:
        content_issues, content_warnings, content_audit = screen_manuscript(pdf_path)
        data_results.extend(content_issues)
        data_warnings.extend(content_warnings)
        content_audit_path.write_text(json.dumps(content_audit, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        content_audit_path.write_text(json.dumps({
            **getattr(exc, 'audit', {}),
            "status": "incomplete", "source_pdf": str(pdf_path),
            "error_type": type(exc).__name__, "error": str(exc),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # Do not produce a success report or write a low-risk record on failure.
        raise RuntimeError(f"初审内容核查未完成，未生成放行结论，详情见 {content_audit_path}") from exc

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
        "analysis_warnings": data_warnings,
        "doi_mismatch": doi_mismatch,
        "content_screening": {k: v for k, v in content_audit.items()
                              if k not in {"candidates", "decisions", "calculations"}},
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

    from modules.chinese_report_generator import _compute_overall_risk
    overall_risk = _compute_overall_risk(findings)
    findings["initial_risk_level"] = overall_risk.get("level", "")
    findings["initial_risk_score"] = overall_risk.get("score", 0)

    cn_path = None
    # 2026-06-24: 强制为低风险也生成 PDF。原始逻辑跳过低风险以省时间，但导致
    # batch 重审后 yujing_quanliang.report_url 仍指向不存在的文件（404）。
    # 本次修复以 DB URL 健康度优先，恢复对低风险也走 PDF 生成路径。
    pdf_skipped_low_risk = False
    # 2026-11-06: a high-risk screening always goes on to review, whose report
    # is the one delivered; its own PDF is not rendered (PAPER_INITIAL_PDF_FOR_HIGH_RISK=1 restores it).
    pdf_skipped_high_risk = skip_initial_pdf(overall_risk.get("level", ""))
    if pdf_skipped_high_risk:
        log.info("Initial screening is high risk: no initial PDF, the review report will be delivered")
        try:
            full_meta = extract_full_metadata(pdf_path)
            findings["paper"]["authors_full"] = full_meta.get("authors_full", [])
            findings["paper"]["affiliations"] = full_meta.get("affiliations", [])
        except Exception as e:
            log.warning("Author/affiliation extraction failed: %s", e)
    else:
        log.info("Generating Chinese PDF report...")
    try:
        if pdf_skipped_high_risk:
            raise _SkipPdf()
        # Fallback when caller didn't supply chinese_reports_dir:
        # With the new per-table layout (output_dir = <table>/init/<doi_slug>/),
        # the chinese_reports/ dir is two levels up at <table>/chinese_reports/.
        # With the legacy flat layout (output_dir = <doi_slug>/) it's one level up.
        # Try both: two levels first (new), one level second (legacy), and finally
        # OUTPUT_DIR/chinese_reports (legacy batch mode where output_dir IS the root).
        if not chinese_reports_dir:
            od = Path(output_dir).resolve()
            candidates = [
                od.parent.parent / "chinese_reports",
                od.parent / "chinese_reports",
                od / "chinese_reports",
            ]
            cn_dir = None
            for cand in candidates:
                if cand.is_dir():
                    cn_dir = str(cand)
                    break
            if cn_dir is None:
                cn_dir = str(candidates[0])
        else:
            cn_dir = chinese_reports_dir
        cn_path, full_meta = generate_initial_pdfs(findings, cn_dir, first_pages_text,
                                                  chinese_renderer=generate_chinese_pdf)
        findings["paper"]["authors_full"] = full_meta.get("authors_full", [])
        findings["paper"]["affiliations"] = full_meta.get("affiliations", [])
        log.info("Found %d authors, %d affiliations",
                 len(findings["paper"]["authors_full"]), len(findings["paper"]["affiliations"]))
        if cn_path:
            log.info("Chinese PDF: %s", cn_path)
        else:
            log.error("Chinese PDF generation returned None, paper will not be inserted to DB")
    except _SkipPdf:
        pass
    except Exception as e:
        log.error("Failed to generate Chinese PDF: %s", e)

    # Persisted to report.json below so downstream readers (main.py DB guards, API
    # self-insert paths) skip inserting records that would point at a missing PDF.
    findings["pdf_generated"] = cn_path is not None
    findings["pdf_skipped_low_risk"] = pdf_skipped_low_risk
    findings["pdf_skipped_high_risk"] = pdf_skipped_high_risk

    json_path = Path(output_dir) / "report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2, default=str, cls=NumpyEncoder)
    log.info("Saved structured results to %s", json_path)

    try:
        from utils.output_cleanup import cleanup_paper_output

        cleanup_stats = cleanup_paper_output(output_dir)
        if cleanup_stats.files_removed:
            log.info(
                "Removed %d unreferenced extraction images (%.2f MiB)",
                cleanup_stats.files_removed,
                cleanup_stats.bytes_reclaimed / (1024 ** 2),
            )
        if cleanup_stats.errors:
            log.warning("Output cleanup completed with errors: %s", cleanup_stats.errors[:3])
    except Exception as exc:
        log.warning("Failed to clean output extraction images: %s", exc)

    log.info("Analysis complete. Total issues: %d", findings["summary"]["total_issues"])
    return findings
