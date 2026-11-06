"""Render review_v3 results as PDFs for inspection only.

    venv/bin/python scripts/review_v3_preview_pdf.py --out <dir> <run_dir|final.json> [...]

Uses the same renderer and coverage validation as scripts/generate_review_report.py,
but writes nothing else: no DB update, no review state file, no nginx copy and
no second-review queue entry.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.generate_review_report import _patch_coverage_entrypoint  # noqa: E402
from api.services.review_validation import renderer_module  # noqa: E402


def _final(path: Path) -> Path:
    if path.is_file():
        return path
    final = path / "final.json"
    if final.is_file():
        return final
    backups = sorted(path.glob("final.*.json"))
    if backups:
        return backups[-1]
    raise FileNotFoundError(f"no final.json under {path}")


def render(module, info: dict, out_dir: Path) -> Path:
    doi = info.get("doi") or "unknown"
    info = module._coverage_check(info)
    if info.get("_coverage_error"):
        raise RuntimeError(f"coverage validation failed: {info['_coverage_error']}")
    report_path = module._find_report_json(doi, info.get("_report_json"))
    findings = json.loads(Path(report_path).read_text(encoding="utf-8")) if report_path else None
    if findings is not None:
        findings["paper"] = module._sanitize_paper_dict_for_render(findings.get("paper", {}))
        header = module._header_html(findings["paper"])
    else:
        header = f"<h1>论文复核报告</h1><p><b>DOI：</b>{module._html_escape(doi)}</p>"
    risk = module._review_overview_html(findings, info)
    slug = module.doi_to_slug(doi) or "unknown"
    scratch = out_dir / ".evidence_scratch" / slug
    images = module._review_evidence_images(info, findings, scratch)
    body = (f"<body style='font-family: {module.FONT_REGULAR};'>{header}{risk}"
            f"{module._analysis_html(info, images)}{module.REVIEW_DISCLAIMER_HTML.format(date=module.TODAY)}</body>")
    output = out_dir / f"review_{slug}.pdf"
    module._render_pdf(body, str(output))
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--out", required=True)
    parser.add_argument("--candidates", help="candidate list giving each DOI's initial report (for older runs)")
    args = parser.parse_args()
    reports = {}
    if args.candidates:
        reports = {c["doi"]: c.get("report") for c in json.loads(Path(args.candidates).read_text(encoding="utf-8"))}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    module = renderer_module()
    _patch_coverage_entrypoint(module)
    for raw in args.paths:
        final = _final(Path(raw))
        info = json.loads(final.read_text(encoding="utf-8"))
        if not info.get("_report_json") and reports.get(info.get("doi")):
            info["_report_json"] = reports[info["doi"]]
        try:
            print(f"{info.get('doi')} {info.get('result')} -> {render(module, info, out_dir)}  (from {final.name})")
        except Exception as exc:
            print(f"{info.get('doi')}: failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
