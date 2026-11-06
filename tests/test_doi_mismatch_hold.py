"""A PDF whose own DOI differs from its directory name is screened but not filed."""
import json
from pathlib import Path

import fitz
import pytest

import main
from modules.doi_utils import dir_doi_conflict


@pytest.mark.parametrize("directory, pdf_doi, expected", [
    ("10.1016_j.cell.2026.09.048", "10.1016/j.medj.2026.101334", "10.1016/j.cell.2026.09.048"),
    ("10.1016_j.cell.2026.09.048", "10.1016/J.CELL.2026.09.048", ""),
    ("10.1093_aje_kwad223", "10.1093/aje/kwad223", ""),          # either 2- or 3-segment reading agrees
    ("20261027_10.1093_aje_kwad223", "10.1093/aje/kwad223", ""),
    ("Zhou_2026", "10.1073/pnas.2516128123", ""),                 # not a DOI-named directory
    ("10.1016_j.cell.2026.09.048", "", ""),
])
def test_dir_doi_conflict(directory, pdf_doi, expected):
    assert dir_doi_conflict(directory, pdf_doi) == expected


def _findings(doi, mismatch=None):
    return {"paper": {"doi": doi, "title": "t", "filename": "p.pdf"}, "summary": {"total_issues": 0, "high_severity": 0,
            "medium_severity": 0, "low_severity": 0}, "pdf_generated": True, "doi_mismatch": mismatch}


def _paper_dir(root, name):
    d = root / name
    d.mkdir(parents=True)
    pdf = fitz.open()
    pdf.new_page().insert_text((50, 72), "x")
    pdf.save(str(d / f"{name}.pdf"))
    return d


def test_batch_holds_misplaced_pdf_back_from_the_database(tmp_path, monkeypatch):
    root = tmp_path / "in"
    _paper_dir(root, "10.1016_j.cell.2026.09.048")
    _paper_dir(root, "10.1016_j.medj.2026.101334")
    mismatch = {"pdf_doi": "10.1016/j.medj.2026.101334", "directory_doi": "10.1016/j.cell.2026.09.048"}

    def analyze(input_dir, output_dir, **kw):
        misplaced = Path(input_dir).name == "10.1016_j.cell.2026.09.048"
        return _findings("10.1016/j.medj.2026.101334", mismatch if misplaced else None)

    inserted = []
    monkeypatch.setattr(main, "_analyze_one", analyze)
    monkeypatch.setattr(main, "insert_findings", lambda findings, **kw: inserted.append(findings["paper"]["doi"]))
    monkeypatch.setattr(main, "get_existing_dois", lambda: set())
    main.run_batch(str(root), str(tmp_path / "out"), isolate_complex=False, max_workers=1)
    assert inserted == ["10.1016/j.medj.2026.101334"]  # only the correctly named directory
    summary = json.loads((tmp_path / "out" / "batch_summary.json").read_text())
    assert summary["success"] == 1 and summary["failed"] == 0 and summary["held"] == 1
    held = [r for r in summary["results"] if r["status"] == "held"]
    assert held[0]["paper"] == "10.1016_j.cell.2026.09.048" and held[0]["doi_mismatch"] == mismatch


def test_single_run_holds_misplaced_pdf_back_from_the_database(tmp_path, monkeypatch):
    d = _paper_dir(tmp_path / "in", "10.1016_j.cell.2026.09.048")
    mismatch = {"pdf_doi": "10.1016/j.medj.2026.101334", "directory_doi": "10.1016/j.cell.2026.09.048"}
    monkeypatch.setattr(main, "_analyze_one", lambda *a, **kw: _findings("10.1016/j.medj.2026.101334", mismatch))
    monkeypatch.setattr(main, "insert_findings", lambda *a, **kw: pytest.fail("must not be written"))
    main.run_single(str(d), str(tmp_path / "out"))
