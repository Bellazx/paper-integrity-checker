"""Render only the requested initial PDFs inside the detection child process."""
from __future__ import annotations

import copy
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def generate_initial_pdfs(findings, chinese_dir, first_pages_text="", *, chinese_renderer):
    # Legacy/batch callers retain their existing Chinese-only behaviour.
    language = os.environ.get("PAPER_INITIAL_REPORT_LANGUAGE", "zh")
    if language not in {"zh", "en", "both"}:
        raise ValueError(f"Unsupported report language: {language}")
    started = time.monotonic()
    if language == "zh":
        result = chinese_renderer(findings, chinese_dir, first_pages_text)
        findings['_initial_report_languages'] = ['zh'] if result[0] else []
        findings.setdefault('_timings', {})['initial_pdfs'] = round(time.monotonic() - started, 3)
        return result

    from modules.english_report_generator import generate_english_initial_pdf

    english_dir = Path(os.environ["PAPER_ENGLISH_REPORT_DIR"])
    snapshot = copy.deepcopy(findings)
    metadata = {key: snapshot.get("paper", {}).get(key, []) for key in ("authors_full", "affiliations")}
    timings = {}

    def timed(language, operation, *args):
        begin = time.monotonic()
        try:
            return operation(*args)
        finally:
            timings[f'initial_{language}_pdf'] = round(time.monotonic() - begin, 3)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="initial-pdf") as pool:
        english = pool.submit(timed, 'en', generate_english_initial_pdf, snapshot, english_dir)
        chinese = (pool.submit(timed, 'zh', chinese_renderer, findings, chinese_dir, first_pages_text)
                   if language == "both" else None)
        chinese_path = None
        if chinese is not None:
            chinese_path, metadata = chinese.result()
            if not chinese_path:
                raise RuntimeError("Chinese initial PDF generation failed")
        try:
            english_path = english.result()
        except Exception as exc:
            if not chinese_path:
                raise
            # Retain successful detection + Chinese PDF. The API may retry
            # just the English renderer, without repeating scientific checks.
            findings['_initial_report_languages'] = ['zh']
            findings['_english_initial_error'] = str(exc)
            return chinese_path, metadata
    # The Chinese renderer can apply an explainability correction. An English
    # sibling must reflect that same corrected risk, even in this rare case.
    if findings.get("_explainability_downgraded") != snapshot.get("_explainability_downgraded"):
        english_path = generate_english_initial_pdf(copy.deepcopy(findings), english_dir)
    if not english_path or not Path(english_path).is_file():
        raise RuntimeError("English initial PDF generation failed")
    findings["_initial_report_languages"] = ["zh", "en"] if chinese is not None else ["en"]
    findings["_english_initial_pdf"] = str(english_path)
    findings.setdefault('_timings', {}).update(timings)
    findings.setdefault("_timings", {})["initial_pdfs"] = round(time.monotonic() - started, 3)
    return chinese_path or english_path, metadata
