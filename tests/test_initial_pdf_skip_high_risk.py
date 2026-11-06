"""A high-risk screening gets no PDF of its own; the review report is delivered."""
import json
from pathlib import Path

import fitz
import pytest

import main
from core import pipeline
from utils import db


def test_only_high_risk_skips_the_initial_pdf(monkeypatch):
    monkeypatch.delenv("PAPER_INITIAL_PDF_FOR_HIGH_RISK", raising=False)
    assert pipeline.skip_initial_pdf("高风险")
    assert not pipeline.skip_initial_pdf("低风险") and not pipeline.skip_initial_pdf("")
    monkeypatch.setenv("PAPER_INITIAL_PDF_FOR_HIGH_RISK", "1")
    assert not pipeline.skip_initial_pdf("高风险")


def test_skipped_screening_has_no_report_url(monkeypatch):
    findings = {"paper": {"doi": "10.1/x", "title": "t"}, "summary": {}, "pdf_generated": False, "pdf_skipped_high_risk": True}
    assert pipeline.initial_pdf_skipped(findings)
    assert pipeline.initial_pdf_skipped({"pdf_skipped_low_risk": True})
    assert not pipeline.initial_pdf_skipped({"pdf_generated": True})
    monkeypatch.setattr(db, "_make_report_url", lambda *a, **kw: pytest.fail("no URL for a skipped PDF"))
    monkeypatch.setattr(db, "build_paper_metadata", lambda f, **kw: {"title": "t", "doi": "10.1/x", "author": "", "author_type": "",
                                                                     "department": "", "author_all": "", "department_all": "", "journal": ""})
    executed = []

    class Cursor:
        def execute(self, sql, params=None):
            executed.append((sql, params))

        def fetchone(self):
            return None

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(db, "get_connection", lambda: Conn())
    db.insert_findings(findings, chinese_reports_dir="/tmp", namespace="yujing_quanliang")
    inserts = [params for sql, params in executed if "INSERT" in sql.upper()]
    assert inserts and inserts[0]["report_url"] == ""


def _paper_dir(root, name):
    d = root / name
    d.mkdir(parents=True)
    pdf = fitz.open()
    pdf.new_page().insert_text((50, 72), "x")
    pdf.save(str(d / f"{name}.pdf"))
    return d


def test_batch_writes_a_high_risk_paper_without_its_pdf(tmp_path, monkeypatch):
    root = tmp_path / "in"
    _paper_dir(root, "10.1016_j.cell.2026.09.048")
    findings = {"paper": {"doi": "10.1016/j.cell.2026.09.048", "title": "t", "filename": "p.pdf"},
                "summary": {"total_issues": 3, "high_severity": 1, "medium_severity": 1, "low_severity": 1},
                "pdf_generated": False, "pdf_skipped_low_risk": False, "pdf_skipped_high_risk": True}
    inserted = []
    monkeypatch.setattr(main, "_analyze_one", lambda *a, **kw: findings)
    monkeypatch.setattr(main, "insert_findings", lambda f, **kw: inserted.append(f["paper"]["doi"]))
    monkeypatch.setattr(main, "get_existing_dois", lambda: set())
    main.run_batch(str(root), str(tmp_path / "out"), isolate_complex=False, max_workers=1)
    assert inserted == ["10.1016/j.cell.2026.09.048"]
    summary = json.loads((tmp_path / "out" / "batch_summary.json").read_text())
    assert summary["success"] == 1 and summary["failed"] == 0
