import logging
from pathlib import Path
from dataclasses import dataclass, field

import fitz  # PyMuPDF
import numpy as np
import pdfplumber

log = logging.getLogger(__name__)


@dataclass
class ExtractedImage:
    page_num: int
    img_index: int
    filepath: str
    array: np.ndarray
    width: int
    height: int
    xref: int = 0


def extract_images(pdf_path: str, output_dir: str, min_size: int = 200) -> list[ExtractedImage]:
    """Extract all embedded images from a PDF using PyMuPDF."""
    output_path = Path(output_dir) / "images"
    output_path.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    images = []
    seen_xrefs = set()

    for page_num in range(len(doc)):
        page = doc[page_num]
        img_list = page.get_images(full=True)

        for img_index, img_info in enumerate(img_list):
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.width < min_size or pix.height < min_size:
                    continue

                if pix.alpha or pix.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                elif pix.n == 1:
                    pix = fitz.Pixmap(fitz.csRGB, pix)

                fname = f"page_{page_num + 1:03d}_img_{img_index:03d}.png"
                fpath = str(output_path / fname)
                pix.save(fpath)

                arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
                if pix.n == 4:
                    arr = arr[:, :, :3]

                images.append(ExtractedImage(
                    page_num=page_num + 1,
                    img_index=img_index,
                    filepath=fpath,
                    array=arr,
                    width=pix.width,
                    height=pix.height,
                    xref=xref,
                ))
            except Exception as e:
                log.warning("Failed to extract image xref=%d from page %d: %s", xref, page_num + 1, e)

    doc.close()
    log.info("Extracted %d images from %s", len(images), pdf_path)
    return images


def extract_standalone_images(paper_dir: str, output_dir: str, min_size: int = 200, max_images: int = 30) -> list[ExtractedImage]:
    """Load standalone image files (TIF/TIFF/JPEG/PNG) from paper directory."""
    import cv2
    from PIL import Image

    paper_path = Path(paper_dir)
    output_images = Path(output_dir) / "images"
    images = []
    img_extensions = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}

    img_files = []
    for ext in img_extensions:
        img_files.extend(paper_path.rglob(f"*{ext}"))
        img_files.extend(paper_path.rglob(f"*{ext.upper()}"))

    seen_paths = set()
    for f in sorted(img_files):
        if f.resolve() in seen_paths:
            continue
        seen_paths.add(f.resolve())
        if str(f).startswith(str(output_images)):
            continue

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
            ))
            if len(images) >= max_images:
                log.info("Reached standalone image limit (%d), skipping remaining", max_images)
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
