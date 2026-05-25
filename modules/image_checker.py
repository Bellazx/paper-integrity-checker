import logging
from dataclasses import dataclass, field
from itertools import combinations

import cv2
import numpy as np

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import (
    PHASH_THRESHOLD, SIFT_RATIO_THRESHOLD, SIFT_MIN_MATCHES,
    SIFT_MIN_INLIER_RATIO, SIFT_MIN_REGION_AREA,
    RANSAC_THRESHOLD, TEMPLATE_MATCH_THRESHOLD, TEMPLATE_SCALES,
)
from utils.pdf_utils import ExtractedImage
from utils.visualization import draw_duplicate_annotation

log = logging.getLogger(__name__)

MAX_DIMENSION = 1500


@dataclass
class DuplicateMatch:
    image_a_path: str
    image_b_path: str
    page_a: int
    page_b: int
    match_type: str
    similarity_score: float
    region_a: tuple | None = None
    region_b: tuple | None = None
    annotation_path: str = ""
    severity: str = "medium"
    details: str = ""


def _resize_for_analysis(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    if max(h, w) <= MAX_DIMENSION:
        return img
    scale = MAX_DIMENSION / max(h, w)
    return cv2.resize(img, None, fx=scale, fy=scale)


def _to_gray(img: np.ndarray) -> np.ndarray:
    if len(img.shape) == 2:
        return img
    channels = img.shape[2]
    if channels == 1:
        return img[:, :, 0]
    if channels == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if channels == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    return img[:, :, 0]


def _compute_phashes(images: list[ExtractedImage]) -> list[np.ndarray]:
    hasher = cv2.img_hash.PHash_create()
    hashes = []
    for img in images:
        gray = _to_gray(_resize_for_analysis(img.array))
        h = hasher.compute(gray)
        hashes.append(h)
    return hashes


def _stage1_phash(images: list[ExtractedImage], output_dir: str) -> tuple[list[DuplicateMatch], set[tuple[int, int]]]:
    """Stage 1: Perceptual hash for near-full duplicates."""
    hashes = _compute_phashes(images)
    hasher = cv2.img_hash.PHash_create()
    matches = []
    matched_pairs = set()

    for i, j in combinations(range(len(images)), 2):
        dist = hasher.compare(hashes[i], hashes[j])
        if dist <= PHASH_THRESHOLD:
            similarity = 1.0 - dist / 64.0
            severity = "high" if dist <= 5 else "medium"

            ann_path = f"{output_dir}/annotations/phash_pair_{i:03d}_{j:03d}.png"
            draw_duplicate_annotation(
                images[i].array, images[j].array,
                None, None,
                f"Page {images[i].page_num} img {images[i].img_index}",
                f"Page {images[j].page_num} img {images[j].img_index}",
                similarity, "full_duplicate", ann_path,
            )

            matches.append(DuplicateMatch(
                image_a_path=images[i].filepath,
                image_b_path=images[j].filepath,
                page_a=images[i].page_num,
                page_b=images[j].page_num,
                match_type="full_duplicate",
                similarity_score=similarity,
                annotation_path=ann_path,
                severity=severity,
                details=f"PHash Hamming distance: {dist}",
            ))
            matched_pairs.add((i, j))
            log.info("PHash match: page %d img %d <-> page %d img %d (dist=%d)",
                     images[i].page_num, images[i].img_index,
                     images[j].page_num, images[j].img_index, dist)

    return matches, matched_pairs


def _stage2_sift(images: list[ExtractedImage], skip_pairs: set[tuple[int, int]], output_dir: str) -> tuple[list[DuplicateMatch], list[tuple[int, int, tuple, tuple]]]:
    """Stage 2: SIFT feature matching for partial region duplicates."""
    sift = cv2.SIFT_create()
    flann = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 5}, {"checks": 50})

    descriptors = []
    keypoints_list = []
    resized = []

    for img in images:
        small = _resize_for_analysis(img.array)
        gray = _to_gray(small)
        resized.append(small)
        kp, des = sift.detectAndCompute(gray, None)
        keypoints_list.append(kp)
        descriptors.append(des)

    matches = []
    region_candidates = []

    for i, j in combinations(range(len(images)), 2):
        if (i, j) in skip_pairs:
            continue
        if descriptors[i] is None or descriptors[j] is None:
            continue
        if len(descriptors[i]) < SIFT_MIN_MATCHES or len(descriptors[j]) < SIFT_MIN_MATCHES:
            continue

        try:
            raw_matches = flann.knnMatch(descriptors[i], descriptors[j], k=2)
        except cv2.error:
            continue

        good = []
        for pair in raw_matches:
            if len(pair) == 2:
                m, n = pair
                if m.distance < SIFT_RATIO_THRESHOLD * n.distance:
                    good.append(m)

        if len(good) < SIFT_MIN_MATCHES:
            continue

        pts_a = np.float32([keypoints_list[i][m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        pts_b = np.float32([keypoints_list[j][m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        H, mask = cv2.findHomography(pts_a, pts_b, cv2.RANSAC, RANSAC_THRESHOLD)
        if H is None or mask is None:
            continue

        inliers = mask.ravel().sum()
        if inliers < SIFT_MIN_MATCHES:
            continue

        inlier_ratio = inliers / len(good)
        if inlier_ratio < SIFT_MIN_INLIER_RATIO:
            continue

        inlier_pts_a = pts_a[mask.ravel() == 1].reshape(-1, 2)
        inlier_pts_b = pts_b[mask.ravel() == 1].reshape(-1, 2)

        x1a, y1a = inlier_pts_a.min(axis=0).astype(int)
        x2a, y2a = inlier_pts_a.max(axis=0).astype(int)
        x1b, y1b = inlier_pts_b.min(axis=0).astype(int)
        x2b, y2b = inlier_pts_b.max(axis=0).astype(int)

        region_a = (int(x1a), int(y1a), int(x2a - x1a), int(y2a - y1a))
        region_b = (int(x1b), int(y1b), int(x2b - x1b), int(y2b - y1b))

        area_a = region_a[2] * region_a[3]
        area_b = region_b[2] * region_b[3]
        img_area = resized[i].shape[0] * resized[i].shape[1]

        if area_a < SIFT_MIN_REGION_AREA * img_area and area_b < SIFT_MIN_REGION_AREA * img_area:
            continue

        similarity = inliers / len(good)

        h_a, w_a = images[i].array.shape[:2]
        h_s, w_s = resized[i].shape[:2]
        sx_a, sy_a = w_a / w_s, h_a / h_s
        h_b, w_b = images[j].array.shape[:2]
        h_s2, w_s2 = resized[j].shape[:2]
        sx_b, sy_b = w_b / w_s2, h_b / h_s2

        orig_region_a = (int(region_a[0] * sx_a), int(region_a[1] * sy_a),
                         int(region_a[2] * sx_a), int(region_a[3] * sy_a))
        orig_region_b = (int(region_b[0] * sx_b), int(region_b[1] * sy_b),
                         int(region_b[2] * sx_b), int(region_b[3] * sy_b))

        ann_path = f"{output_dir}/annotations/sift_pair_{i:03d}_{j:03d}.png"
        draw_duplicate_annotation(
            images[i].array, images[j].array,
            orig_region_a, orig_region_b,
            f"Page {images[i].page_num} img {images[i].img_index}",
            f"Page {images[j].page_num} img {images[j].img_index}",
            similarity, "partial_region", ann_path,
        )

        if inliers >= 40:
            sev = "high"
        else:
            sev = "medium"

        matches.append(DuplicateMatch(
            image_a_path=images[i].filepath,
            image_b_path=images[j].filepath,
            page_a=images[i].page_num,
            page_b=images[j].page_num,
            match_type="partial_region",
            similarity_score=similarity,
            region_a=orig_region_a,
            region_b=orig_region_b,
            annotation_path=ann_path,
            severity=sev,
            details=f"SIFT inliers: {inliers}/{len(good)}",
        ))
        region_candidates.append((i, j, orig_region_a, orig_region_b))

        log.info("SIFT match: page %d img %d <-> page %d img %d (inliers=%d/%d)",
                 images[i].page_num, images[i].img_index,
                 images[j].page_num, images[j].img_index,
                 inliers, len(good))

    return matches, region_candidates


def _stage3_template_verify(images: list[ExtractedImage], candidates: list[tuple[int, int, tuple, tuple]], output_dir: str) -> list[DuplicateMatch]:
    """Stage 3: Multi-scale template matching to verify suspicious regions."""
    verified = []

    for idx, (i, j, region_a, region_b) in enumerate(candidates):
        x, y, w, h = region_a
        if w < 20 or h < 20:
            continue

        src = images[i].array
        x = max(0, x)
        y = max(0, y)
        w = min(w, src.shape[1] - x)
        h = min(h, src.shape[0] - y)
        template = src[y:y+h, x:x+w]

        if template.size == 0:
            continue

        target_gray = _to_gray(images[j].array)
        template_gray = _to_gray(template)

        best_val = -1
        best_scale = 1.0
        best_loc = (0, 0)

        for scale in TEMPLATE_SCALES:
            tw = int(template_gray.shape[1] * scale)
            th = int(template_gray.shape[0] * scale)
            if tw < 10 or th < 10:
                continue
            if tw >= target_gray.shape[1] or th >= target_gray.shape[0]:
                continue

            scaled_tmpl = cv2.resize(template_gray, (tw, th))
            result = cv2.matchTemplate(target_gray, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val > best_val:
                best_val = max_val
                best_scale = scale
                best_loc = max_loc

        if best_val >= TEMPLATE_MATCH_THRESHOLD:
            tw = int(template_gray.shape[1] * best_scale)
            th = int(template_gray.shape[0] * best_scale)
            verified_region_b = (best_loc[0], best_loc[1], tw, th)

            ann_path = f"{output_dir}/annotations/template_verified_{idx:03d}.png"
            draw_duplicate_annotation(
                images[i].array, images[j].array,
                region_a, verified_region_b,
                f"Page {images[i].page_num} (source region)",
                f"Page {images[j].page_num} (matched region)",
                best_val, "template_verified", ann_path,
            )

            verified.append(DuplicateMatch(
                image_a_path=images[i].filepath,
                image_b_path=images[j].filepath,
                page_a=images[i].page_num,
                page_b=images[j].page_num,
                match_type="template_verified",
                similarity_score=best_val,
                region_a=region_a,
                region_b=verified_region_b,
                annotation_path=ann_path,
                severity="high",
                details=f"Template match: score={best_val:.4f}, scale={best_scale:.2f}",
            ))
            log.info("Template verified: page %d <-> page %d (score=%.4f, scale=%.2f)",
                     images[i].page_num, images[j].page_num, best_val, best_scale)

    return verified


def check_image_duplicates(images: list[ExtractedImage], output_dir: str) -> list[dict]:
    """Run the full three-stage image duplicate detection pipeline."""
    if len(images) < 2:
        log.info("Less than 2 images, skipping duplicate check")
        return []

    import os
    os.makedirs(f"{output_dir}/annotations", exist_ok=True)

    from collections import Counter
    page_counts = Counter(img.page_num for img in images)
    same_page_pairs = sum(c * (c - 1) // 2 for c in page_counts.values())

    log.info("Stage 1: PHash screening (%d images, %d same-page pairs)", len(images), same_page_pairs)
    phash_matches, phash_pairs = _stage1_phash(images, output_dir)

    log.info("Stage 2: SIFT partial region matching")
    sift_matches, sift_candidates = _stage2_sift(images, phash_pairs, output_dir)

    log.info("Stage 3: Template matching verification (%d candidates)", len(sift_candidates))
    template_matches = _stage3_template_verify(images, sift_candidates, output_dir)

    all_matches = phash_matches + sift_matches + template_matches

    results = []
    for m in all_matches:
        results.append({
            "image_a": m.image_a_path,
            "image_b": m.image_b_path,
            "page_a": m.page_a,
            "page_b": m.page_b,
            "match_type": m.match_type,
            "similarity_score": m.similarity_score,
            "region_a": m.region_a,
            "region_b": m.region_b,
            "annotation_path": m.annotation_path,
            "severity": m.severity,
            "details": m.details,
        })

    log.info("Total suspicious image pairs: %d (phash=%d, sift=%d, template=%d)",
             len(results), len(phash_matches), len(sift_matches), len(template_matches))
    return results
