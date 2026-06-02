"""Image splicing (拼接/PS痕迹) pre-screen for Western-blot / gel-style panels.

This is a CONSERVATIVE first-pass screen. It flags single images that show physical
signs of being assembled from multiple sources, implementing four checkpoints:

  1. 竖直分界线   — unnatural full-height vertical boundary lines between lanes
  2. 背景灰度断层 — abrupt background grey-level discontinuity across such a line
  3. 曝光突变     — sudden exposure (brightness/contrast) steps between adjacent bands
  4. 分辨率/压缩差异 — regions with inconsistent high-frequency / compression energy

Every finding is rated **medium** only ("suspicious, needs visual confirmation in
review"). The detector never declares high risk on its own — that is the reviewer's
call after looking at the annotation image. The goal is recall with bounded false
positives, not a verdict.
"""
import logging
import os
from dataclasses import dataclass

import cv2
import numpy as np

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from utils.pdf_utils import ExtractedImage

log = logging.getLogger(__name__)

# ---------- Tunables (deliberately conservative) ----------
MAX_DIMENSION = 1500          # downscale very large panels before analysis
MIN_W = 120                   # skip tiny images (icons, markers, thumbnails)
MIN_H = 40
MAX_ASPECT = 12.0             # skip extreme strips (rulers, color bars)
BLOT_MAX_ASPECT = 8.0         # WB/gel panels are wide-ish but not infinitely so
BLOT_MIN_ASPECT = 1.3         # taller-than-wide panels are rarely lane blots
SEAM_Z = 9.0                  # robust z-score for a column to count as a seam candidate
SEAM_MIN_SPAN = 0.85          # seam must span >=85% of image height
SEAM_EDGE_MARGIN = 0.06       # ignore seams within 6% of either side (panel borders/frames)
BG_ROWS_FRAC = 0.12           # top/bottom fraction treated as "background" rows
BG_STEP_FRAC = 0.18           # background mean must jump >=18% of dynamic range
EXPOSURE_STEP_FRAC = 0.20     # adjacent-band mean brightness step threshold
BLOCK = 48                    # block size for compression/resolution energy map
COMPRESS_CV = 0.85            # coeff. of variation of block high-freq energy threshold


@dataclass
class SpliceFinding:
    image_path: str
    page: int
    severity: str
    signals: list          # list of signal names that fired
    seam_columns: list     # x positions (original-scale) of suspect seams
    details: str
    annotation_path: str = ""


def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    ch = img.shape[2]
    if ch == 1:
        return img[:, :, 0]
    if ch == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if ch == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return img[:, :, 0]


def _resize(img: np.ndarray) -> tuple[np.ndarray, float]:
    h, w = img.shape[:2]
    if max(h, w) <= MAX_DIMENSION:
        return img, 1.0
    scale = MAX_DIMENSION / max(h, w)
    return cv2.resize(img, None, fx=scale, fy=scale), scale


def _looks_like_blot(img: np.ndarray, gray: np.ndarray) -> bool:
    """Heuristic gate: only screen images whose shape/contrast/colour resemble a
    lane-based blot or gel. This keeps the screen off bar charts, plots, schematics,
    photos, fluorescence panels, and labelled multi-panel composite figures."""
    h, w = gray.shape
    aspect = w / max(h, 1)
    if aspect < BLOT_MIN_ASPECT or aspect > BLOT_MAX_ASPECT:
        return False
    # Blots/gels are essentially grayscale. Reject coloured images
    # (fluorescence micrographs, schematics, lattice diagrams, journal cover banners).
    if img.ndim == 3 and img.shape[2] >= 3:
        b, g, r = img[:, :, 0].astype(np.int16), img[:, :, 1].astype(np.int16), img[:, :, 2].astype(np.int16)
        chroma = float(np.mean(np.maximum(np.maximum(np.abs(r - g), np.abs(g - b)), np.abs(r - b))))
        if chroma > 12:
            return False
    # Blots are largely grayscale with bands; require moderate, not extreme, contrast.
    std = float(np.std(gray))
    if std < 12 or std > 110:
        return False
    # Reject panel grids: a labelled multi-panel composite has strong horizontal
    # dividers too. A single blot/gel strip does not stack rows of sub-panels.
    sob_y = np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))
    row_thr = np.percentile(sob_y, 95) + 1e-6
    row_span = (sob_y > row_thr).mean(axis=1)
    full_width_rows = int(np.sum(row_span >= 0.85))
    if full_width_rows >= 2:
        return False
    # Require actual band-like foreground: cover pages, text banners and near-empty
    # crops have almost no dark content and should not be screened as blots.
    otsu, _ = cv2.threshold(gray.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    fg = gray < otsu
    fg_frac = float(fg.mean())
    if fg_frac < 0.05 or fg_frac > 0.95:
        return False
    # Lane blots spread foreground (bands) across most of the width. Cover banners and
    # captions concentrate content in a few columns over an otherwise empty canvas.
    col_fg = fg.mean(axis=0)
    active_cols = float((col_fg > 0.03).mean())
    if active_cols < 0.5:
        return False
    return True


def _seam_candidates(gray: np.ndarray) -> tuple[list[int], np.ndarray]:
    """Checkpoint 1: full-height vertical boundary lines.
    Returns (seam_x_positions, per_column_vertical_edge_energy)."""
    h, w = gray.shape
    sob = np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
    # Fraction of column height that is a strong vertical edge (a real seam runs top-to-bottom).
    thr = np.percentile(sob, 95) + 1e-6
    span_frac = (sob > thr).mean(axis=0)
    col_energy = sob.mean(axis=0)
    med = np.median(col_energy)
    mad = np.median(np.abs(col_energy - med)) + 1e-6
    z = (col_energy - med) / (1.4826 * mad)
    margin = int(SEAM_EDGE_MARGIN * w)
    seams = []
    for x in range(max(2, margin), min(w - 2, w - margin)):
        if z[x] > SEAM_Z and span_frac[x] >= SEAM_MIN_SPAN:
            if z[x] >= z[x - 1] and z[x] >= z[x + 1]:   # local peak
                seams.append(x)
    # Merge seams that are within a few px of each other.
    merged = []
    for x in seams:
        if merged and x - merged[-1] <= 3:
            continue
        merged.append(x)
    return merged, col_energy


def _background_step(gray: np.ndarray, x: int) -> bool:
    """Checkpoint 2: background grey-level discontinuity across column x."""
    h, w = gray.shape
    band = max(4, int(0.02 * w))
    left = gray[:, max(0, x - band):x]
    right = gray[:, x + 1:min(w, x + 1 + band)]
    if left.size == 0 or right.size == 0:
        return False
    rows = max(2, int(BG_ROWS_FRAC * h))
    bg_left = np.concatenate([left[:rows].ravel(), left[-rows:].ravel()])
    bg_right = np.concatenate([right[:rows].ravel(), right[-rows:].ravel()])
    dyn = float(gray.max() - gray.min()) + 1e-6
    return abs(bg_left.mean() - bg_right.mean()) / dyn >= BG_STEP_FRAC


def _exposure_step(gray: np.ndarray, x: int) -> bool:
    """Checkpoint 3: abrupt exposure (overall brightness) step across column x."""
    h, w = gray.shape
    band = max(6, int(0.04 * w))
    left = gray[:, max(0, x - band):x]
    right = gray[:, x + 1:min(w, x + 1 + band)]
    if left.size == 0 or right.size == 0:
        return False
    dyn = float(gray.max() - gray.min()) + 1e-6
    return abs(float(left.mean()) - float(right.mean())) / dyn >= EXPOSURE_STEP_FRAC


def _compression_inconsistency(gray: np.ndarray) -> bool:
    """Checkpoint 4: blockwise high-frequency energy varies too much across the image,
    suggesting regions pasted from sources of different resolution / JPEG quality."""
    h, w = gray.shape
    energies = []
    for by in range(0, h - BLOCK, BLOCK):
        for bx in range(0, w - BLOCK, BLOCK):
            block = gray[by:by + BLOCK, bx:bx + BLOCK]
            energies.append(float(cv2.Laplacian(block, cv2.CV_64F).var()))
    if len(energies) < 6:
        return False
    energies = np.array(energies)
    mean = energies.mean()
    if mean < 1e-6:
        return False
    cv = energies.std() / mean
    return cv >= COMPRESS_CV


def _draw_splice_annotation(img: np.ndarray, seams_orig: list[int],
                            label: str, out_path: str):
    """Mark suspect seam columns on a single panel for reviewer inspection."""
    disp = img if img.ndim == 3 else cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    disp = disp.copy()
    h = disp.shape[0]
    header = 50
    canvas = np.ones((h + header, disp.shape[1], 3), dtype=np.uint8) * 255
    canvas[header:, :] = disp
    for x in seams_orig:
        cv2.line(canvas, (x, header), (x, header + h), (0, 0, 230), 2)
    cv2.putText(canvas, label, (10, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)
    cv2.putText(canvas, "splice_suspect", (10, 44), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 200), 1)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, canvas)


def _analyze_one(img: ExtractedImage, output_dir: str, idx: int) -> SpliceFinding | None:
    arr = img.array
    if arr is None or arr.size == 0:
        return None
    h0, w0 = arr.shape[:2]
    if w0 < MIN_W or h0 < MIN_H:
        return None
    if w0 / max(h0, 1) > MAX_ASPECT:
        return None

    small, scale = _resize(arr)
    gray = _to_gray(small)
    if not _looks_like_blot(small, gray):
        return None

    seams, _ = _seam_candidates(gray)
    signals = []
    confirmed_seams = []
    for x in seams:
        local = []
        if _background_step(gray, x):
            local.append("背景灰度断层")
        if _exposure_step(gray, x):
            local.append("曝光突变")
        # A seam only counts if accompanied by at least one corroborating signal,
        # otherwise it is likely a legitimate lane divider drawn by the author.
        if local:
            confirmed_seams.append(x)
            signals.extend(local)

    compress_bad = _compression_inconsistency(gray)
    if compress_bad:
        signals.append("分辨率/压缩差异")

    # Decision: require a confirmed seam (boundary + corroboration). Compression
    # inconsistency alone is too weak to flag (kept only as a supporting note).
    if not confirmed_seams:
        return None

    signals = sorted(set(signals))
    inv_scale = 1.0 / scale if scale else 1.0
    seams_orig = [int(x * inv_scale) for x in confirmed_seams]

    ann_path = f"{output_dir}/annotations/splice_{idx:03d}.png"
    try:
        _draw_splice_annotation(arr, seams_orig, f"Page {img.page_num} img {img.img_index}", ann_path)
    except Exception as e:
        log.warning("Failed to draw splice annotation: %s", e)
        ann_path = ""

    cols = "、".join(f"第{x}列" for x in seams_orig[:5])
    details = f"在{cols}检出疑似拼接边界（{'、'.join(signals)}）"
    log.info("Splice suspect: page %d img %d, seams=%s, signals=%s",
             img.page_num, img.img_index, seams_orig, signals)
    return SpliceFinding(
        image_path=img.filepath,
        page=img.page_num,
        severity="medium",
        signals=signals,
        seam_columns=seams_orig,
        details=details,
        annotation_path=ann_path,
    )


def check_splicing(images: list[ExtractedImage], output_dir: str) -> list[dict]:
    """Screen each image for splicing artifacts. Returns a list of finding dicts.

    Findings are always severity 'medium' (suspect, pending visual confirmation)."""
    results = []
    for idx, img in enumerate(images):
        try:
            finding = _analyze_one(img, output_dir, idx)
        except Exception as e:
            log.warning("Splice check failed for %s: %s", getattr(img, "filepath", "?"), e)
            continue
        if finding is None:
            continue
        results.append({
            "test": "image_splicing",
            "image": finding.image_path,
            "page": finding.page,
            "severity": finding.severity,
            "signals": finding.signals,
            "seam_columns": finding.seam_columns,
            "annotation_path": finding.annotation_path,
            "details": finding.details,
        })
    if results:
        log.info("Image splicing suspects: %d", len(results))
    return results
