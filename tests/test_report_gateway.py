"""Report gateway (deployment/report_gateway.py): URL to file resolution."""
import importlib.util
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[1] / "deployment" / "report_gateway.py"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("report_gateway", GATEWAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migrated, local = (tmp_path / "migrated").resolve(), (tmp_path / "output").resolve()
    monkeypatch.setattr(module, "ROOTS", (migrated, local))
    module.migrated, module.local = migrated, local
    return module


def _put(root: Path, relative: str, body: bytes = b"%PDF-1.4") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def test_legacy_layout_still_wins(gateway):
    # Every URL that resolved before resolves to the same file.
    legacy = _put(gateway.local, "yujing_quanliang/chinese_reports/a.pdf", b"%PDF legacy")
    _put(gateway.local, "chinese_reports/yujing_quanliang/a.pdf", b"%PDF other")
    assert gateway.resolve_report("/chinese_reports/yujing_quanliang/a.pdf") == legacy
    review = _put(gateway.local, "review_v2/detection_reports/20261031-81c5e133/review_x.pdf")
    assert gateway.resolve_report("/review_reports/detection_reports/20261031-81c5e133/review_x.pdf") == review


def test_migrated_copy_takes_precedence(gateway):
    migrated = _put(gateway.migrated, "yujing_4050/chinese_reports/a.pdf")
    _put(gateway.local, "yujing_4050/chinese_reports/a.pdf")
    assert gateway.resolve_report("/chinese_reports/yujing_4050/a.pdf") == migrated


@pytest.mark.parametrize("url, relative", [
    # Developer API: {table}/{task_id}/ under each report directory.
    ("/chinese_reports/yujing_test/20261104-f71f600c/a.pdf", "chinese_reports/yujing_test/20261104-f71f600c/a.pdf"),
    ("/english_reports/yujing_test/20261104-f71f600c/a_en.pdf", "english_reports/yujing_test/20261104-f71f600c/a_en.pdf"),
    ("/review_reports/yujing_test/20261104-f71f600c/review_a.pdf", "review_v2/yujing_test/20261104-f71f600c/review_a.pdf"),
    # Namespaces written as chinese_reports/{namespace}/ (yujing_audit in yujing_quanliang).
    ("/chinese_reports/yujing_audit/a.pdf", "chinese_reports/yujing_audit/a.pdf"),
])
def test_per_namespace_layout_resolves(gateway, url, relative):
    expected = _put(gateway.local, relative)
    assert gateway.resolve_report(url) == expected


@pytest.mark.parametrize("url", [
    "/review_reports/10.1001_jama.2022.12584_evidence.json",
    "/review_reports/yujing_test/20261104-f71f600c/evidence.json",
    "/chinese_reports/yujing_audit/notes.txt",
])
def test_only_pdf_reports_are_served(gateway, url):
    relative = url.replace("/review_reports/", "review_v2/").replace("/chinese_reports/", "chinese_reports/")
    _put(gateway.local, relative, b"{}")
    assert gateway.resolve_report(url) is None


@pytest.mark.parametrize("url", [
    "/chinese_reports/../secret.pdf",
    "/chinese_reports/%2e%2e/secret.pdf",
    "/review_reports/a/../../secret.pdf",
    "/english_reports//secret.pdf",
    "/chinese_reports/%2Fsecret.pdf",
    "/other/secret.pdf",
])
def test_traversal_is_refused(gateway, url):
    _put(gateway.local.parent, "secret.pdf")
    _put(gateway.local, "secret.pdf")
    assert gateway.resolve_report(url) is None


def test_symlink_out_of_root_is_refused(gateway, tmp_path):
    outside = _put(tmp_path, "outside/secret.pdf")
    link = gateway.local / "chinese_reports" / "yujing_x" / "a.pdf"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert gateway.resolve_report("/chinese_reports/yujing_x/a.pdf") is None
