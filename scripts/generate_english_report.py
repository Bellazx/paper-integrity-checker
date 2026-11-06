"""Isolated English renderer; publish the PDF only after a successful render."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("initial", "review"), required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--slug", default="")
    args = parser.parse_args()
    value = json.loads(Path(args.input).read_text(encoding="utf-8"))
    from modules.english_report_generator import generate_english_initial_pdf, generate_english_review_pdf
    if args.kind == "review":
        from api.services.review_validation import renderer_module
        from scripts.generate_review_report import _patch_coverage_entrypoint, _patch_generated_evidence_collector
        renderer = renderer_module()
        _patch_coverage_entrypoint(renderer)
        _patch_generated_evidence_collector(renderer)
        value = renderer._coverage_check(value)
        if value.get("trigger") == "review_error" or value.get("_coverage_error") or value.get("_coverage_gaps"):
            raise ValueError("English publication validation failed: " + str(
                value.get("_coverage_error") or value.get("_coverage_gaps") or "review_error"))
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".english-", dir=output) as scratch:
        path = Path(generate_english_review_pdf(value, scratch, args.slug) if args.kind == "review"
                    else generate_english_initial_pdf(value, scratch))
        if path.read_bytes()[:5] != b"%PDF-":
            raise ValueError("Renderer did not produce a PDF")
        target = output / path.name
        os.replace(path, target)
    print("[PDF] " + str(target))


if __name__ == "__main__":
    main()
