#!/usr/bin/env python3
"""Re-score all papers in a batch using existing report.json, regenerate Chinese PDFs, and update DB.
Skips image/data/reference analysis — only re-generates PDF reports and updates scores."""
import json
import sys
import os
import logging
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))
from modules.chinese_report_generator import generate_chinese_pdf
from utils.db import insert_findings
from utils.pdf_utils import extract_full_text
from utils.excel_metadata import find_batch_excel, load_batch_excel, merge_excel_metadata

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("rescore")

INPUT_DIR = Path("data/input/20260520-649")
OUTPUT_DIR = Path("data/output")
CN_REPORTS_DIR = str(OUTPUT_DIR / "chinese_reports")

_excel_data = {}
for _batch_dir in [INPUT_DIR, Path("data/input/20260521")]:
    _ep = find_batch_excel(_batch_dir)
    if _ep:
        _excel_data.update(load_batch_excel(_ep))


def process_one(paper_dir_name: str) -> dict:
    t0 = time.time()
    report_path = OUTPUT_DIR / paper_dir_name / "report.json"
    if not report_path.exists():
        return {"paper": paper_dir_name, "status": "skip", "reason": "no report.json"}

    with open(report_path) as f:
        findings = json.load(f)

    doi = findings.get("paper", {}).get("doi", "")
    if not doi:
        return {"paper": paper_dir_name, "status": "skip", "reason": "no DOI"}

    # Get first pages text for LLM metadata extraction
    input_dir = INPUT_DIR / paper_dir_name
    first_pages_text = ""
    if input_dir.exists():
        pdfs = list(input_dir.glob("*.pdf")) + list(input_dir.glob("*.PDF"))
        if pdfs:
            try:
                full = extract_full_text(str(pdfs[0]))
                first_pages_text = full[:3000] if full else ""
            except Exception:
                pass

    # Regenerate Chinese PDF (calls LLM + renders PDF)
    pdf_path, metadata = generate_chinese_pdf(findings, CN_REPORTS_DIR, first_pages_text)

    if metadata.get("authors_full"):
        findings["paper"]["authors_full"] = metadata["authors_full"]
    if metadata.get("affiliations"):
        findings["paper"]["affiliations"] = metadata["affiliations"]

    if _excel_data:
        findings = merge_excel_metadata(findings, _excel_data)

    # Save updated report.json
    with open(report_path, "w") as f:
        json.dump(findings, f, indent=2, ensure_ascii=False)

    # Update DB
    html_path = str(input_dir / "article.html") if (input_dir / "article.html").exists() else None
    insert_findings(findings, chinese_reports_dir=CN_REPORTS_DIR, html_path=html_path)

    elapsed = time.time() - t0
    return {"paper": paper_dir_name, "status": "ok", "elapsed": round(elapsed, 1)}


def main():
    papers = sorted(os.listdir(INPUT_DIR))
    log.info("Rescore + PDF regeneration for %d papers (16 workers)", len(papers))

    results = []
    lock = threading.Lock()
    t_start = time.time()

    with ThreadPoolExecutor(max_workers=16) as executor:
        futures = {executor.submit(process_one, p): p for p in papers}
        for i, future in enumerate(as_completed(futures), 1):
            r = future.result()
            with lock:
                results.append(r)
            status = r["status"].upper()
            elapsed = r.get("elapsed", 0)
            log.info("[%d/%d] [%s] %s (%.0fs)", i, len(papers), status, r["paper"], elapsed)

    total_time = time.time() - t_start
    ok = sum(1 for r in results if r["status"] == "ok")
    skip = sum(1 for r in results if r["status"] == "skip")
    log.info("Done in %.0fs. OK=%d, Skip=%d", total_time, ok, skip)


if __name__ == "__main__":
    main()
