"""Extract inline images embedded in .docx files.

Strategy:
  Primary: open the .docx as a zip and extract every file under
  word/media/. This works regardless of how python-docx represents the
  shapes (InlineShape / Anchor / VML) and reliably yields PNG/JPEG/TIFF.
  Secondary: walk doc.inline_shapes via python-docx for files where the
  zip approach misses (rare). Currently unused; kept for diagnostics.
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif"}
_SKIP_EXTS = {".emf", ".wmf", ".rels", ".xml"}


def _safe_image_filename(stem: str, source_name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:60]
    src = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(source_name).stem)[:40]
    ext = Path(source_name).suffix.lower() or ".png"
    return f"docx_{base}__{src}{ext}"


def extract_images_from_docx(docx_path: str | Path, output_dir: str) -> list[Path]:
    """Extract embedded images from one .docx file (reads word/media/*)."""
    docx_path = Path(docx_path)
    if not docx_path.exists() or docx_path.suffix.lower() != ".docx":
        return []

    target_dir = Path(output_dir) / "images"
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = docx_path.stem

    written: list[Path] = []
    seen_blobs: set[str] = set()
    try:
        with zipfile.ZipFile(str(docx_path)) as z:
            for name in z.namelist():
                # word/media/image1.png etc. (case varies)
                lower = name.lower()
                if not lower.startswith("word/media/"):
                    continue
                ext = Path(name).suffix.lower()
                if ext in _SKIP_EXTS or ext not in _IMG_EXTS:
                    continue
                if name.endswith("/"):  # directory entry
                    continue
                try:
                    blob = z.read(name)
                except KeyError:
                    continue
                if not blob:
                    continue
                bh = hashlib.sha256(blob).hexdigest()
                if bh in seen_blobs:
                    continue
                seen_blobs.add(bh)
                out_name = _safe_image_filename(stem, Path(name).name)
                out_path = target_dir / out_name
                try:
                    out_path.write_bytes(blob)
                    written.append(out_path)
                except OSError as e:
                    log.warning("Failed to write %s: %s", out_path, e)
    except zipfile.BadZipFile:
        log.warning("Not a valid .docx (bad zip): %s", docx_path)
    except Exception as e:
        log.warning("Failed to read %s: %s", docx_path, e)
    if written:
        log.info("Extracted %d image(s) from %s", len(written), docx_path)
    return written


def extract_docx_images_in_dir(paper_dir: str | Path, output_dir: str) -> list[Path]:
    """Find all .docx files under paper_dir and extract their embedded images."""
    paper_dir = Path(paper_dir)
    if not paper_dir.exists():
        return []
    written: list[Path] = []
    seen_docx: set[Path] = set()
    for f in sorted(paper_dir.rglob("*.docx")):
        try:
            resolved = f.resolve()
        except OSError:
            continue
        if resolved in seen_docx:
            continue
        seen_docx.add(resolved)
        written.extend(extract_images_from_docx(f, output_dir))
    return written
