import json
import logging
import hashlib
import re
import time
from pathlib import Path

import numpy as np
from lxml import html as lxml_html

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.pdf_utils import extract_images
from modules.image_checker import check_image_duplicates
from modules.splice_checker import check_splicing
from modules.data_checker import DATA_FILE_EXTENSIONS, check_data_anomalies, check_data_with_validation
from modules.reference_checker import (
    _verify_by_doi, _verify_by_text, _compare_titles,
    CROSSREF_RATE_LIMIT_DELAY, REF_TITLE_SIMILARITY_THRESHOLD,
)
from modules.chinese_report_generator import generate_chinese_pdf
from utils.db import _find_sjtu_authors
from core.pipeline import NumpyEncoder

log = logging.getLogger(__name__)

SKIP_TYPES = {"Correspondence", "Comment", "Erratum"}

MAX_RETRIES = 3
MAX_CONSECUTIVE_API_FAILURES = 10


def _file_fingerprint(path: Path) -> tuple[int, str] | None:
    try:
        return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        log.warning("Failed to fingerprint %s: %s", path, e)
        return None


def _retry(fn, max_retries=MAX_RETRIES, delay=2, label="operation"):
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            if attempt < max_retries - 1:
                log.warning("%s failed (attempt %d/%d): %s", label, attempt + 1, max_retries, e)
                time.sleep(delay * (attempt + 1))
            else:
                raise


def is_nature_crawl(paper_dir: str) -> bool:
    d = Path(paper_dir)
    has_manifest = (d / "manifest.json").exists()
    has_html = (d / "article.html").exists() or (d / "html" / "article.html").exists()
    return has_manifest and has_html


def _load_manifest(paper_dir: str) -> dict:
    with open(Path(paper_dir) / "manifest.json", "r", encoding="utf-8") as f:
        return json.load(f)


def extract_metadata_from_manifest(paper_dir: str) -> dict:
    d = Path(paper_dir)
    manifest = _load_manifest(paper_dir)
    meta = manifest.get("article_meta", {})

    doi = manifest.get("input_doi", "")
    title = meta.get("dc_title") or meta.get("title", "")
    journal_raw = meta.get("dc_source", "")
    journal = re.sub(r'\s+\d{4}\s+\d+:\d+$', '', journal_raw).strip()
    dc_type = meta.get("dc_type", "")
    dc_date = meta.get("dc_date", "")

    authors_full = []
    affiliations = []
    author_first = ""

    html_path = d / "article.html"
    if not html_path.exists():
        html_path = d / "html" / "article.html"
    if html_path.exists():
        try:
            with open(html_path, "r", encoding="utf-8") as f:
                html_content = f.read()

            jsonld_blocks = re.findall(
                r'<script type="application/ld\+json">(.*?)</script>',
                html_content, re.DOTALL,
            )
            for block in jsonld_blocks:
                try:
                    data = json.loads(block)
                    entity = data.get("mainEntity", data)
                    if "author" in entity:
                        seen_affs = {}
                        aff_index = 0
                        for a in entity["author"]:
                            name = a.get("name", "")
                            if name:
                                authors_full.append(name)
                            for af in a.get("affiliation", []):
                                addr = af.get("address", {})
                                aff_name = addr.get("name") or af.get("name", "")
                                if aff_name and aff_name not in seen_affs:
                                    aff_index += 1
                                    seen_affs[aff_name] = aff_index
                                    affiliations.append(f"{aff_index}. {aff_name}")
                        break
                except (json.JSONDecodeError, AttributeError):
                    continue

            if not authors_full:
                authors_full = re.findall(
                    r'<meta\s+name="dc\.creator"\s+content="([^"]+)"', html_content
                )
                authors_full = [a.split(",")[1].strip() + " " + a.split(",")[0].strip()
                                if "," in a else a for a in authors_full]

        except Exception as e:
            log.warning("Failed to parse HTML metadata: %s", e)

    if authors_full:
        author_first = authors_full[0]

    return {
        "title": title or Path(paper_dir).name,
        "doi": doi,
        "journal": journal,
        "author": author_first,
        "authors_full": authors_full,
        "affiliations": affiliations,
        "dc_type": dc_type,
        "dc_date": dc_date,
        "source_format": "nature_crawl",
    }


def extract_text_from_html(html_path: str) -> str:
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        doc = lxml_html.fromstring(content)
    except Exception:
        text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        return re.sub(r'\s+', ' ', text).strip()

    for tag in doc.iter("script", "style", "noscript"):
        tag.drop_tree()

    body_sections = doc.xpath('//section[@data-title]')
    if body_sections:
        parts = []
        for sec in body_sections:
            title = sec.get("data-title", "")
            if title.lower() in ("references", "bibliography"):
                break
            parts.append(sec.text_content())
        return "\n\n".join(parts)

    article_body = doc.xpath('//article') or doc.xpath('//main') or [doc]
    return article_body[0].text_content()


def extract_references_from_html(html_path: str) -> list[dict]:
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    try:
        doc = lxml_html.fromstring(content)
    except Exception:
        return []

    refs = []

    items = doc.xpath('//li[contains(@class, "c-article-references__item")]')
    for item in items:
        counter = item.get("data-counter", "").strip().rstrip(".")
        try:
            num = int(counter)
        except (ValueError, TypeError):
            num = len(refs) + 1

        text_el = item.xpath('.//p[contains(@class, "c-article-references__text")]')
        text = text_el[0].text_content().strip() if text_el else item.text_content().strip()

        doi = None
        doi_links = item.xpath('.//*[@data-doi]')
        if doi_links:
            doi = doi_links[0].get("data-doi")

        refs.append({"number": num, "text": text, "doi": doi})

    if not refs:
        doi_all = re.findall(r'data-doi="([^"]+)"', content)
        for i, d in enumerate(doi_all, 1):
            refs.append({"number": i, "text": "", "doi": d})

    log.info("Extracted %d references from HTML", len(refs))
    return refs


def check_references_from_html(html_path: str) -> list[dict]:
    refs = extract_references_from_html(html_path)
    if not refs:
        return []

    issues = []
    verified = 0
    failed = 0
    consecutive_failures = 0

    for ref in refs:
        time.sleep(CROSSREF_RATE_LIMIT_DELAY)

        if ref["doi"]:
            result = None
            for attempt in range(MAX_RETRIES):
                result = _verify_by_doi(ref["doi"])
                if result.get("error") != "lookup_failed":
                    break
                if attempt < MAX_RETRIES - 1:
                    log.warning("CrossRef lookup failed for DOI %s, retrying (%d/%d)...",
                                ref["doi"], attempt + 1, MAX_RETRIES)
                    time.sleep(2 * (attempt + 1))

            if result.get("error") == "lookup_failed":
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_API_FAILURES:
                    raise RuntimeError(
                        f"CrossRef API unreachable ({consecutive_failures} consecutive failures), aborting reference check"
                    )
            else:
                consecutive_failures = 0

            if result.get("doi_valid"):
                verified += 1
                title_sim = _compare_titles(ref["text"], result.get("crossref_title"))
                if ref["text"] and title_sim < REF_TITLE_SIMILARITY_THRESHOLD:
                    issues.append({
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "title_mismatch",
                        "severity": "medium",
                        "details": {
                            "doi": ref["doi"],
                            "title_similarity": round(title_sim, 3),
                            "crossref_title": result.get("crossref_title"),
                        },
                        "description": f"Reference #{ref['number']}: DOI exists but title similarity is low "
                                       f"({title_sim:.1%})",
                    })
            else:
                failed += 1
                issues.append({
                    "ref_number": ref["number"],
                    "ref_text": ref["text"][:200],
                    "issue_type": "doi_not_found",
                    "severity": "high",
                    "details": {"doi": ref["doi"], "error": result.get("error")},
                    "description": f"Reference #{ref['number']}: DOI '{ref['doi']}' not found in CrossRef",
                })
        elif ref["text"]:
            result = None
            for attempt in range(MAX_RETRIES):
                result = _verify_by_text(ref["text"])
                if result.get("error") != "no_match" or result.get("found"):
                    break
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 * (attempt + 1))

            if result.get("found"):
                verified += 1
                title_sim = _compare_titles(ref["text"], result.get("crossref_title"))
                if result.get("crossref_score", 0) < 50:
                    issues.append({
                        "ref_number": ref["number"],
                        "ref_text": ref["text"][:200],
                        "issue_type": "low_match_score",
                        "severity": "low",
                        "details": {
                            "matched_doi": result.get("matched_doi"),
                            "crossref_score": result.get("crossref_score"),
                            "title_similarity": round(title_sim, 3),
                        },
                        "description": f"Reference #{ref['number']}: Low confidence match in CrossRef "
                                       f"(score={result.get('crossref_score', 0):.1f})",
                    })
            else:
                failed += 1
                issues.append({
                    "ref_number": ref["number"],
                    "ref_text": ref["text"][:200],
                    "issue_type": "not_found",
                    "severity": "medium",
                    "details": {},
                    "description": f"Reference #{ref['number']}: Could not verify in CrossRef",
                })

    log.info("Reference verification: %d total, %d verified, %d failed, %d issues",
             len(refs), verified, failed, len(issues))
    return issues


# Directories whose PDFs are likely supplementary figures/data — not the main paper.
# Match is case-insensitive against the first path segment of each PDF under paper_dir.
SUPPLEMENTARY_DIR_PATTERNS = (
    "extended_data", "extended data", "extend data", "expended data",
    "supplementary", "supplement", "supplemental", "supplemental_material",
    "supplementary_material", "supplementary_materials", "supplementary info",
    "supplementary_info", "supplemental_information", "supporting_information",
    "supporting info", "supporting", "supporting data", "supporting_data",
    "si", "s1", "sm", "s/m",
    "source_data", "source data", "supplementary_data", "supp_data",
    "additional", "appendix", "附录", "补充材料", "附加材料",
    "figure", "figures", "fig", "figs", "图片", "图表",
)


def find_image_pdfs(paper_dir: str) -> list[str]:
    """Find supplementary/figure PDFs that should be image-mined for duplication.

    Matches the common variants: extended_data / extended data / extend data,
    supplementary / supplement / supporting_information / figure / figures.
    """
    d = Path(paper_dir)
    if not d.exists():
        return []

    supset = {p.lower() for p in SUPPLEMENTARY_DIR_PATTERNS}
    pdfs: list[str] = []
    seen_fingerprints: set[tuple[int, str]] = set()

    # Search top-level matching subdirs + any nested 'figure/figures' (one level deep)
    candidate_dirs: set[Path] = set()
    for entry in sorted(d.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.lower() in supset:
            candidate_dirs.add(entry)
    # nested figure/figures
    for sub in ("figure", "figures", "fig", "figs", "图片", "图表"):
        for nested in d.glob(f"*/{sub}"):
            if nested.is_dir():
                candidate_dirs.add(nested)

    for cdir in sorted(candidate_dirs):
        for f in sorted(cdir.glob("*.pdf")):
            name_lower = f.name.lower()
            if "reporting" in name_lower:
                continue
            fp = _file_fingerprint(f)
            if fp is not None:
                if fp in seen_fingerprints:
                    log.info("Skipping duplicate supplementary PDF by content: %s", f.name)
                    continue
                seen_fingerprints.add(fp)
            pdfs.append(str(f))

    return pdfs


def _find_data_dirs(paper_dir: str) -> list[str]:
    d = Path(paper_dir)
    dirs = []
    for subdir_name in ("source_data", "extended_data"):
        subdir = d / subdir_name
        if not subdir.exists():
            continue
        has_data = any(subdir.rglob(f"*{ext}") for ext in DATA_FILE_EXTENSIONS)
        if has_data:
            dirs.append(str(subdir))

    return dirs


def analyze_nature_paper(
    paper_dir: str,
    output_dir: str,
    skip_refs: bool = False,
    chinese_reports_dir: str = None,
    author_type: str = "",
    doi_override: str = "",
) -> dict:
    paper_dir = str(Path(paper_dir).resolve())
    output_dir = str(Path(output_dir).resolve())
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if os.environ.get("PAPER_CLEAN_JUNK", "1") != "0":
        try:
            from utils.paper_preprocessor import clean_junk_files
            junk_stats = clean_junk_files(paper_dir, dry_run=False, log_fn=log.info)
            removed = sum(junk_stats.get("actions", {}).values())
            if removed:
                log.info("clean-junk removed %d noise entries in %s", removed, paper_dir)
            for warning in junk_stats.get("warnings", []):
                log.warning("clean-junk: %s", warning)
        except Exception as e:
            log.warning("clean_junk_files failed (continuing without): %s", e)

    metadata = extract_metadata_from_manifest(paper_dir)

    # A user-supplied DOI is authoritative — apply it over the manifest DOI so the
    # override behaves identically on the Nature path and the normal PDF path.
    if doi_override:
        metadata["doi"] = doi_override

    log.info("=" * 60)
    log.info("Analyzing (Nature): %s", metadata["title"])
    log.info("  DOI: %s | Journal: %s | Type: %s", metadata["doi"], metadata["journal"], metadata["dc_type"])
    log.info("  Authors: %d | Affiliations: %d", len(metadata["authors_full"]), len(metadata["affiliations"]))
    log.info("=" * 60)

    if metadata["dc_type"] in SKIP_TYPES:
        log.info("Skipping non-research paper (type: %s)", metadata["dc_type"])
        return _empty_findings(metadata, "Skipped: " + metadata["dc_type"])

    manifest = _load_manifest(paper_dir)
    if manifest.get("status") != "success":
        log.info("Skipping failed crawl")
        return _empty_findings(metadata, "Skipped: crawl failed")

    html_path = Path(paper_dir) / "article.html"
    if not html_path.exists():
        html_path = Path(paper_dir) / "html" / "article.html"
    html_path = str(html_path)

    log.info("[1/4] Extracting images from supplementary PDFs...")
    image_pdfs = find_image_pdfs(paper_dir)
    all_images = []
    all_image_results = []
    for pdf_path in image_pdfs:
        pdf_name = Path(pdf_path).stem
        images = extract_images(pdf_path, output_dir)
        all_images.extend(images)
        if len(images) >= 2:
            results = check_image_duplicates(images, output_dir)
            all_image_results.extend(results)
        log.info("  %s: %d images extracted", pdf_name[-40:], len(images))
    log.info("Total images: %d from %d PDFs", len(all_images), len(image_pdfs))

    log.info("Screening images for splicing artifacts...")
    all_splice_results = check_splicing(all_images, output_dir)

    log.info("[2/4] Checking source data anomalies...")
    data_dirs = _find_data_dirs(paper_dir)
    all_data_results, all_failed_files = check_data_with_validation(data_dirs)

    if all_failed_files:
        log.warning("Some data files failed to load (analysis continues): %s", ", ".join(all_failed_files))

    log.info("[3/4] Verifying references from HTML...")
    if skip_refs:
        log.info("Reference check skipped (--skip-refs)")
        ref_results = []
    else:
        ref_results = check_references_from_html(html_path)

    refs_from_html = extract_references_from_html(html_path)
    ref_count = len(refs_from_html)

    log.info("[4/4] Generating report...")
    article_text = extract_text_from_html(html_path)
    first_pages_text = article_text[:6000]

    sjtu_info = _find_sjtu_authors(html_path)
    log.info("SJTU authors: %s", sjtu_info["sjtu_authors"])

    findings = {
        "paper": {
            "filename": "article.html",
            "filepath": html_path,
            "title": metadata["title"],
            "author": metadata["author"],
            "authors_full": metadata["authors_full"],
            "affiliations": metadata["affiliations"],
            "journal": metadata["journal"],
            "doi": metadata["doi"],
            "total_pages": 0,
            "total_images": len(all_images),
            "total_references": ref_count,
            "dc_type": metadata["dc_type"],
            "source_format": "nature_crawl",
            "sjtu_authors": sjtu_info["sjtu_authors"],
            "sjtu_author_type": author_type,
            "sjtu_departments": sjtu_info["sjtu_departments"],
        },
        "image_duplicates": all_image_results,
        "image_splicing": all_splice_results,
        "data_anomalies": all_data_results,
        "reference_issues": ref_results,
        "summary": {
            "total_issues": len(all_image_results) + len(all_data_results) + len(ref_results) + len(all_splice_results),
            "image_issues": len(all_image_results),
            "image_splicing_suspects": len(all_splice_results),
            "data_issues": len(all_data_results),
            "reference_issues": len(ref_results),
            "high_severity": sum(1 for r in all_image_results + all_data_results + ref_results if r.get("severity") == "high"),
            # splice findings are always severity 'medium' (conservative pre-screen)
            "medium_severity": sum(1 for r in all_image_results + all_data_results + ref_results if r.get("severity") == "medium") + len(all_splice_results),
            "low_severity": sum(1 for r in all_image_results + all_data_results + ref_results if r.get("severity") == "low"),
        },
    }

    log.info("Generating Chinese PDF report...")
    cn_path = None
    try:
        # Same fallback logic as core/pipeline.py:537-562 — try 2 levels up (per-table
        # layout), then 1 level up (legacy flat), then the dir itself (legacy batch).
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
        saved_authors = findings["paper"]["authors_full"][:]
        saved_affiliations = findings["paper"]["affiliations"][:]
        cn_path, _ = _retry(
            lambda: generate_chinese_pdf(findings, cn_dir, first_pages_text),
            label="Chinese PDF generation",
        )
        findings["paper"]["authors_full"] = saved_authors
        findings["paper"]["affiliations"] = saved_affiliations
        if cn_path:
            log.info("Chinese PDF: %s", cn_path)
        else:
            log.error("Chinese PDF generation returned None, paper will not be inserted to DB")
    except Exception as e:
        log.error("Failed to generate Chinese PDF after %d attempts: %s", MAX_RETRIES, e)

    findings["pdf_generated"] = cn_path is not None

    json_path = Path(output_dir) / "report.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2, default=str, cls=NumpyEncoder)
    log.info("Saved report to %s", json_path)

    log.info("Analysis complete. Total issues: %d", findings["summary"]["total_issues"])
    return findings


def _empty_findings(metadata: dict, reason: str) -> dict:
    return {
        "paper": {
            "filename": "article.html",
            "filepath": "",
            "title": metadata.get("title", ""),
            "author": metadata.get("author", ""),
            "authors_full": metadata.get("authors_full", []),
            "affiliations": metadata.get("affiliations", []),
            "journal": metadata.get("journal", ""),
            "doi": metadata.get("doi", ""),
            "total_pages": 0,
            "total_images": 0,
            "total_references": 0,
            "dc_type": metadata.get("dc_type", ""),
            "source_format": "nature_crawl",
            "skipped_reason": reason,
        },
        "image_duplicates": [],
        "image_splicing": [],
        "data_anomalies": [],
        "reference_issues": [],
        "summary": {
            "total_issues": 0,
            "image_issues": 0,
            "image_splicing_suspects": 0,
            "data_issues": 0,
            "reference_issues": 0,
            "high_severity": 0,
            "medium_severity": 0,
            "low_severity": 0,
        },
    }
