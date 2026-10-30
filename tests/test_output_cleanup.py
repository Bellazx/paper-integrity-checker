import json
import os
import time
from pathlib import Path

from utils.output_cleanup import cleanup_output_images


def _old(path: Path) -> None:
    timestamp = time.time() - 7200
    os.utime(path, (timestamp, timestamp))


def _paper(root: Path, name: str, report: dict) -> Path:
    paper = root / "table" / "init" / name
    (paper / "images").mkdir(parents=True)
    (paper / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return paper


def test_removes_only_old_unreferenced_images(tmp_path):
    root = tmp_path / "output"
    paper = _paper(root, "paper", {"image_a": "images/keep.png"})
    keep = paper / "images" / "keep.png"
    remove = paper / "images" / "remove.png"
    recent = paper / "images" / "recent.jpg"
    metadata = paper / "images" / "metadata.json"
    for path in (keep, remove, recent, metadata):
        path.write_bytes(b"content")
    _old(keep)
    _old(remove)
    _old(metadata)

    stats = cleanup_output_images(root, min_age_seconds=3600)

    assert keep.exists()
    assert not remove.exists()
    assert recent.exists()
    assert metadata.exists()
    assert stats.files_removed == 1
    assert stats.referenced_files_kept == 1
    assert stats.recent_files_kept == 1


def test_absolute_reference_is_kept_and_outside_file_is_untouched(tmp_path):
    root = tmp_path / "output"
    paper = _paper(root, "paper", {})
    keep = paper / "images" / "keep.gif"
    outside = tmp_path / "outside.png"
    keep.write_bytes(b"keep")
    outside.write_bytes(b"outside")
    _old(keep)
    _old(outside)
    (paper / "report.json").write_text(
        json.dumps({"image_a": str(keep), "image_b": str(outside)}),
        encoding="utf-8",
    )

    cleanup_output_images(root, min_age_seconds=3600)

    assert keep.exists()
    assert outside.exists()


def test_dry_run_does_not_modify_files(tmp_path):
    root = tmp_path / "output"
    paper = _paper(root, "paper", {})
    image = paper / "images" / "remove.png"
    image.write_bytes(b"content")
    _old(image)

    stats = cleanup_output_images(root, min_age_seconds=3600, dry_run=True)

    assert image.exists()
    assert stats.files_removed == 1
    assert stats.bytes_reclaimed == len(b"content")


def test_invalid_report_skips_its_images(tmp_path):
    root = tmp_path / "output"
    paper = _paper(root, "paper", {})
    image = paper / "images" / "keep.png"
    image.write_bytes(b"content")
    _old(image)
    (paper / "report.json").write_text("{invalid", encoding="utf-8")

    stats = cleanup_output_images(root, min_age_seconds=3600)

    assert image.exists()
    assert stats.invalid_reports == 1
    assert stats.image_dirs_scanned == 0
