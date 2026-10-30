"""Clean disposable image extraction artifacts from completed paper outputs."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".j2k",
    ".jfif",
    ".jp2",
    ".jpeg",
    ".jpg",
    ".jpf",
    ".jpx",
    ".pbm",
    ".pgm",
    ".png",
    ".pnm",
    ".ppm",
    ".tif",
    ".tiff",
    ".webp",
}
_IMAGE_SUFFIX_TUPLE = tuple(IMAGE_SUFFIXES)

REPORT_SCAN_SKIP_DIRS = {
    "annotations",
    "chinese_reports",
    "data_tables",
    "images",
    "review_runtime_scratch",
}


@dataclass
class CleanupStats:
    reports_scanned: int = 0
    invalid_reports: int = 0
    image_dirs_scanned: int = 0
    files_scanned: int = 0
    files_removed: int = 0
    referenced_files_kept: int = 0
    recent_files_kept: int = 0
    other_files_kept: int = 0
    image_dirs_removed: int = 0
    bytes_reclaimed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["gib_reclaimed"] = round(self.bytes_reclaimed / (1024 ** 3), 2)
        return result


def _is_within(path: Path, root: Path) -> bool:
    path_text = os.path.abspath(os.fspath(path))
    root_text = os.path.abspath(os.fspath(root))
    return path_text == root_text or path_text.startswith(root_text + os.sep)


def _absolute(path: Path) -> Path:
    """Normalize a path lexically without filesystem lookups."""
    return Path(os.path.abspath(os.fspath(path)))


def _walk_image_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) <= 4096 and stripped.lower().endswith(_IMAGE_SUFFIX_TUPLE):
            yield stripped
    elif isinstance(value, dict):
        for child in value.values():
            yield from _walk_image_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_image_strings(child)


def _referenced_images(report: Any, report_path: Path, root: Path) -> set[Path]:
    references: set[Path] = set()
    for value in _walk_image_strings(report):

        candidate = Path(value).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [
            report_path.parent / candidate,
            report_path.parent / "images" / candidate,
        ]
        for path in candidates:
            resolved = _absolute(path)
            if _is_within(resolved, root):
                references.add(resolved)
    return references


def _iter_reports(root: Path) -> Iterable[Path]:
    for current_dir, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in REPORT_SCAN_SKIP_DIRS
            and not name.endswith("_scratch")
            and not Path(current_dir, name).is_symlink()
        ]
        if "report.json" in filenames:
            yield Path(current_dir) / "report.json"


def cleanup_output_images(
    root: str | os.PathLike[str],
    *,
    min_age_seconds: int = 24 * 3600,
    dry_run: bool = False,
) -> CleanupStats:
    """Remove old, unreferenced extracted images from completed paper outputs.

    A directory is eligible only when it has both ``report.json`` and a sibling
    ``images`` directory. Image paths mentioned anywhere in any valid report are
    retained. Symlinks and non-image files are never removed.
    """
    output_root = Path(root).expanduser().resolve()
    stats = CleanupStats()
    if not output_root.is_dir():
        stats.errors.append(f"output root does not exist: {output_root}")
        return stats

    cutoff = time.time() - max(0, int(min_age_seconds))
    references: set[Path] = set()
    image_dirs: list[Path] = []

    for report_path in _iter_reports(output_root):
        if not report_path.is_file() or report_path.is_symlink():
            continue
        stats.reports_scanned += 1
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            stats.invalid_reports += 1
            stats.errors.append(f"cannot parse {report_path}: {exc}")
            continue

        references.update(_referenced_images(report, report_path, output_root))
        images_dir = report_path.parent / "images"
        if images_dir.is_dir() and not images_dir.is_symlink():
            image_dirs.append(images_dir)

    for images_dir in image_dirs:
        resolved_images_dir = _absolute(images_dir)
        if not _is_within(resolved_images_dir, output_root):
            stats.errors.append(f"skipped image directory outside root: {images_dir}")
            continue

        stats.image_dirs_scanned += 1
        for current_dir, dirnames, filenames in os.walk(images_dir, topdown=False, followlinks=False):
            current = Path(current_dir)
            for filename in filenames:
                path = current / filename
                stats.files_scanned += 1
                if path.is_symlink() or path.suffix.lower() not in IMAGE_SUFFIXES:
                    stats.other_files_kept += 1
                    continue

                resolved = _absolute(path)
                if not _is_within(resolved, output_root):
                    stats.other_files_kept += 1
                    continue
                if resolved in references:
                    stats.referenced_files_kept += 1
                    continue

                try:
                    file_stat = path.stat()
                except OSError as exc:
                    stats.errors.append(f"cannot stat {path}: {exc}")
                    continue
                if file_stat.st_mtime >= cutoff:
                    stats.recent_files_kept += 1
                    continue

                if not dry_run:
                    try:
                        path.unlink()
                    except OSError as exc:
                        stats.errors.append(f"cannot remove {path}: {exc}")
                        continue
                stats.files_removed += 1
                stats.bytes_reclaimed += file_stat.st_size

            if dry_run:
                continue
            try:
                current.rmdir()
                stats.image_dirs_removed += 1
            except OSError:
                pass

    return stats


def cleanup_paper_output(output_dir: str | os.PathLike[str]) -> CleanupStats:
    """Clean one completed paper immediately after its report is persisted."""
    return cleanup_output_images(output_dir, min_age_seconds=0, dry_run=False)
