import logging
import hashlib
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import numpy as np
import pdfplumber

log = logging.getLogger(__name__)

MAX_IMAGE_PDF_PAGES = max(0, int(os.getenv("PDF_IMAGE_MAX_PAGES", "200")))


@dataclass
class ExtractedImage:
    page_num: int
    img_index: int
    filepath: str
    array: np.ndarray
    width: int
    height: int
    xref: int = 0
    source_id: str = ""


def _file_fingerprint(path: Path) -> tuple[int, str] | None:
    """Return a stable content fingerprint for duplicate-file skipping."""
    try:
        return path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as e:
        log.warning("Failed to fingerprint %s: %s", path, e)
        return None


def _pdf_source_id(pdf_path: str) -> str:
    return str(Path(pdf_path).resolve())


def _pdf_output_prefix(pdf_path: str) -> str:
    source_id = _pdf_source_id(pdf_path)
    stem = "".join(ch if ch.isalnum() else "_" for ch in Path(pdf_path).stem).strip("_")[:40]
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:10]
    return f"{stem}_{digest}" if stem else digest


def _extract_xref(pdf_path: str, page_num: int, img_index: int, xref: int, min_size: int, output_path: Path, output_prefix: str) -> ExtractedImage | None:
    """Worker: extract one xref image. Opens its own fitz doc (thread-safe via per-thread context)."""
    try:
        doc = fitz.open(pdf_path)
        try:
            # 2026-06-24: 二次保险 — Pixmap() 之前先看 xref 编码，JPXDecode segfault
            xref_info = doc.xref_object(xref, compressed=False)
            if "/JPXDecode" in xref_info or "/Filter /JPXDecode" in xref_info:
                log.debug("xref=%d uses JPXDecode, skipping", xref)
                return None
            pix = fitz.Pixmap(doc, xref)
            if pix.width < min_size or pix.height < min_size:
                return None

            if pix.alpha or pix.n > 3:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            elif pix.n == 1:
                pix = fitz.Pixmap(fitz.csRGB, pix)

            fname = f"{output_prefix}_page_{page_num + 1:03d}_img_{img_index:03d}.png"
            fpath = str(output_path / fname)
            pix.save(fpath)

            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            if pix.n == 4:
                arr = arr[:, :, :3]

            return ExtractedImage(
                page_num=page_num + 1,
                img_index=img_index,
                filepath=fpath,
                array=arr,
                width=pix.width,
                height=pix.height,
                xref=xref,
                source_id=_pdf_source_id(pdf_path),
            )
        finally:
            doc.close()
    except Exception as e:
        log.warning("Failed to extract image xref=%d from page %d: %s", xref, page_num + 1, e)
        return None


def _select_page_indices(page_count: int, max_pages: int) -> list[int]:
    if page_count <= 0:
        return []
    if max_pages <= 0 or page_count <= max_pages:
        return list(range(page_count))
    if max_pages == 1:
        return [0]
    return [round(i * (page_count - 1) / (max_pages - 1)) for i in range(max_pages)]


def extract_images(pdf_path: str, output_dir: str, min_size: int = 200,
                   max_workers: int = 4,
                   max_pages: int = MAX_IMAGE_PDF_PAGES) -> list[ExtractedImage]:
    """Extract all embedded images from a PDF using PyMuPDF.

    Per-xref extraction is parallelized across pages (each worker opens its own
    fitz doc to avoid the GIL contention and PyMuPDF's non-thread-safe doc handle).
    For small PDFs (< 8 images) the parallel overhead would dominate, so we fall
    back to the sequential path. For large 20-30MB Elsevier PDFs with 30+ images,
    page-level parallelism cuts extraction time roughly 2-3x.
    """
    output_path = Path(output_dir) / "images"
    output_path.mkdir(parents=True, exist_ok=True)
    output_prefix = _pdf_output_prefix(pdf_path)

    # Pass 1: collect (page_num, img_index, xref) tuples sequentially
    # 2026-06-24: 跳过 JPXDecode (JPEG2000) — PyMuPDF 1.26 在 Pixmap(doc, xref) 时 segfault
    # (Frontiers PDF 用 jpx + 1-bit index masks，主图仍 jpeg 可提)
    doc = fitz.open(pdf_path)
    page_indices = _select_page_indices(len(doc), max_pages)
    if len(page_indices) < len(doc):
        log.warning(
            "PDF has %d pages; sampling %d evenly distributed pages for image extraction: %s",
            len(doc),
            len(page_indices),
            pdf_path,
        )
    xref_jobs: list[tuple[int, int, int]] = []
    seen_xrefs = set()
    skipped_jpx = 0
    for page_num in page_indices:
        page = doc[page_num]
        for img_index, img_info in enumerate(page.get_images(full=True)):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            # img_info: (xref, smask, w, h, bpc, cs, alt, name, filter, ref)
            filt = img_info[8] if len(img_info) > 8 else ""
            if filt == "JPXDecode":
                skipped_jpx += 1
                continue
            xref_jobs.append((page_num, img_index, xref))
    doc.close()
    if skipped_jpx:
        log.info("Skipped %d JPXDecode images from %s (PyMuPDF segfault workaround)",
                 skipped_jpx, pdf_path)

    if not xref_jobs:
        log.info("Extracted 0 images from %s", pdf_path)
        return []

    # Small docs: sequential (avoid thread overhead)
    if len(xref_jobs) < 8:
        images: list[ExtractedImage] = []
        for page_num, img_index, xref in xref_jobs:
            r = _extract_xref(pdf_path, page_num, img_index, xref, min_size, output_path, output_prefix)
            if r is not None:
                images.append(r)
        log.info("Extracted %d images from %s", len(images), pdf_path)
        return images

    # Large docs: parallel
    images = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_extract_xref, pdf_path, p, i, x, min_size, output_path, output_prefix): (p, i, x)
            for p, i, x in xref_jobs
        }
        for fut in as_completed(futures):
            r = fut.result()
            if r is not None:
                images.append(r)

    # Sort by page_num for deterministic ordering
    images.sort(key=lambda e: (e.page_num, e.img_index))
    log.info("Extracted %d images from %s (parallel, %d workers)", len(images), pdf_path, max_workers)
    return images


def extract_standalone_images(paper_dir: str, output_dir: str, min_size: int = 200, max_images: int = 30, extra_image_dirs: list[str] | None = None) -> list[ExtractedImage]:
    """Load standalone image files (TIF/TIFF/JPEG/PNG/GIF) from paper directory.

    extra_image_dirs: optional list of additional directories whose image files
    should be included in the scan (e.g. an `<output_dir>/images/` populated
    by the docx extractor that lives inside the recursion guard's exclusion
    zone).
    """
    import cv2
    from PIL import Image

    paper_path = Path(paper_dir)
    output_images = Path(output_dir) / "images"
    images = []
    img_extensions = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".gif"}

    # Allow callers to inject extra image roots that the default rglob guard
    # would otherwise skip (the output-images folder is the canonical case).
    extra_image_dirs = list(extra_image_dirs or [])

    img_files = []
    for ext in img_extensions:
        img_files.extend(paper_path.rglob(f"*{ext}"))
        img_files.extend(paper_path.rglob(f"*{ext.upper()}"))
    # Also pull from explicitly injected dirs (e.g. docx_extractor output)
    for extra in extra_image_dirs:
        ep = Path(extra)
        if not ep.exists():
            continue
        for ext in img_extensions:
            img_files.extend(ep.glob(f"*{ext}"))
            img_files.extend(ep.glob(f"*{ext.upper()}"))

    seen_paths = set()
    seen_file_fingerprints: set[tuple[int, str]] = set()
    for f in sorted(img_files):
        if f.resolve() in seen_paths:
            continue
        seen_paths.add(f.resolve())
        # NOTE: previously had `if str(f).startswith(str(output_images)): continue`
        # which silently dropped legitimate images (e.g. docx_extractor output).
        # We now rely on file-fingerprint dedup to avoid double-counting.

        fp = _file_fingerprint(f)
        if fp is not None:
            if fp in seen_file_fingerprints:
                log.info("Skipping duplicate standalone image file by content: %s", f.name)
                continue
            seen_file_fingerprints.add(fp)

        try:
            arr = cv2.imread(str(f))
            if arr is None:
                pil_img = Image.open(str(f)).convert("RGB")
                arr = np.array(pil_img)
                arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

            if arr.shape[0] < min_size or arr.shape[1] < min_size:
                continue

            max_dim = 4000
            h, w = arr.shape[:2]
            if h > max_dim or w > max_dim:
                scale = max_dim / max(h, w)
                arr = cv2.resize(arr, (int(w * scale), int(h * scale)))

            arr = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)

            images.append(ExtractedImage(
                page_num=0,
                img_index=len(images),
                filepath=str(f),
                array=arr,
                width=arr.shape[1],
                height=arr.shape[0],
                source_id=str(f.resolve()),
            ))
            if len(images) >= max_images:
                log.info("Reached standalone image limit (%d), skipping remaining", max_images)
                break
            # Early-stop when dedup ratio is high: if we've already dedup'd >= 80%
            # of files seen, the remaining ones are overwhelmingly duplicates of
            # the ones we already have. This skips the long tail of large mmc
            # directories where 200+ figures are mostly redrawn versions of the
            # same Western-blot / bar-chart template.
            if len(seen_paths) >= 30:
                dedup_ratio = 1 - (len(seen_file_fingerprints) / len(seen_paths))
                if dedup_ratio >= 0.80 and len(images) >= 20:
                    log.info(
                        "Early-stop standalone: dedup_ratio=%.2f (%d unique / %d seen)",
                        dedup_ratio, len(seen_file_fingerprints), len(seen_paths),
                    )
                    break
        except Exception as e:
            log.warning("Failed to load image %s: %s", f.name, e)

    return images


def extract_text(pdf_path: str) -> list[dict]:
    """Extract text from each page using pdfplumber."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            pages.append({"page": i + 1, "text": text})
    return pages


def _detect_columns(blocks, page_width):
    """Detect if text blocks form a two-column layout.
    Returns (is_two_column, split_x) where split_x is the column boundary."""
    if not blocks or page_width < 100:
        return False, 0

    mid = page_width / 2
    gap_left = mid - page_width * 0.10
    gap_right = mid + page_width * 0.10

    narrow = [b for b in blocks if b["x1"] - b["x0"] < page_width * 0.55]
    left_blocks = [b for b in narrow if b["x1"] < gap_right and b["x0"] < mid]
    right_blocks = [b for b in narrow if b["x0"] > gap_left and b["x0"] >= mid * 0.9]

    if len(left_blocks) >= 3 and len(right_blocks) >= 3:
        left_rights = [b["x1"] for b in left_blocks]
        right_lefts = [b["x0"] for b in right_blocks]
        avg_left_right = sum(left_rights) / len(left_rights)
        avg_right_left = sum(right_lefts) / len(right_lefts)
        if avg_right_left - avg_left_right > page_width * 0.01:
            split_x = (avg_left_right + avg_right_left) / 2
            return True, split_x

    return False, 0


def _extract_page_text_columns(page):
    """Extract text from a fitz page with column-aware ordering."""
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
    text_blocks = []
    for b in blocks:
        if b["type"] != 0:
            continue
        lines_text = []
        for line in b.get("lines", []):
            spans_text = "".join(s["text"] for s in line.get("spans", []))
            if spans_text.strip():
                lines_text.append(spans_text)
        if lines_text:
            text_blocks.append({
                "x0": b["bbox"][0], "y0": b["bbox"][1],
                "x1": b["bbox"][2], "y1": b["bbox"][3],
                "text": "\n".join(lines_text),
            })

    if not text_blocks:
        return ""

    page_width = page.rect.width
    is_two_col, split_x = _detect_columns(text_blocks, page_width)

    if is_two_col:
        left = sorted([b for b in text_blocks if b["x0"] < split_x], key=lambda b: b["y0"])
        right = sorted([b for b in text_blocks if b["x0"] >= split_x], key=lambda b: b["y0"])
        ordered = left + right
    else:
        ordered = sorted(text_blocks, key=lambda b: (b["y0"], b["x0"]))

    return "\n".join(b["text"] for b in ordered)


def extract_full_text(pdf_path: str) -> str:
    """Extract all text from PDF with column-aware ordering."""
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        text = _extract_page_text_columns(page)
        pages.append(text)
    doc.close()
    return "\n\n".join(pages)
