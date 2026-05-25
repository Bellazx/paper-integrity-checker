#!/usr/bin/env python3
"""Process remaining cell-2 papers that were skipped as duplicates."""
import sys
import time
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.pipeline import analyze_paper
from core.nature_adapter import is_nature_crawl, analyze_nature_paper
from utils.db import insert_findings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("remaining")

INPUT_ROOT = Path("data/input/cell-2/cell-2全")
OUTPUT_ROOT = Path("data/output/cell-2-new")
REMAINING_LIST = Path("/tmp/cell2_extra.txt")
MAX_WORKERS = 16


def _analyze_one(input_dir, output_dir, chinese_reports_dir):
    if is_nature_crawl(input_dir):
        return analyze_nature_paper(input_dir, output_dir, chinese_reports_dir=chinese_reports_dir)
    return analyze_paper(input_dir, output_dir, chinese_reports_dir=chinese_reports_dir)


def main():
    papers = [p.strip() for p in REMAINING_LIST.read_text().splitlines() if p.strip()]
    log.info("Processing %d remaining papers with %d workers", len(papers), MAX_WORKERS)

    cn_dir = str(OUTPUT_ROOT / "chinese_reports")
    results = []
    lock = threading.Lock()
    start = time.time()

    def process(name):
        t0 = time.time()
        try:
            inp = str(INPUT_ROOT / name)
            out = str(OUTPUT_ROOT / name)
            findings = _analyze_one(inp, out, cn_dir)
            html_path = str(INPUT_ROOT / name / "article.html") if (INPUT_ROOT / name / "article.html").exists() else None
            try:
                insert_findings(findings, chinese_reports_dir=cn_dir, html_path=html_path)
            except Exception as e:
                log.error("DB insert failed for %s: %s", name, e)
            return {"paper": name, "status": "success", "issues": findings["summary"]["total_issues"], "time": round(time.time() - t0, 1)}
        except Exception as e:
            log.error("Failed %s: %s", name, e, exc_info=True)
            return {"paper": name, "status": "error", "error": str(e), "time": round(time.time() - t0, 1)}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process, n): n for n in papers}
        for i, f in enumerate(as_completed(futures), 1):
            r = f.result()
            with lock:
                results.append(r)
            s = "OK" if r["status"] == "success" else "FAILED"
            log.info("[%d/%d] [%s] %s (%s issues, %.0fs)", i, len(papers), s, r["paper"], r.get("issues", "N/A"), r["time"])

    ok = sum(1 for r in results if r["status"] == "success")
    fail = sum(1 for r in results if r["status"] == "error")
    log.info("Done: %d OK, %d failed, %.0fs total", ok, fail, time.time() - start)


if __name__ == "__main__":
    main()
