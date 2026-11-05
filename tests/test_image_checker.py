from pathlib import Path

import cv2
import numpy as np

from modules.image_checker import _content_edges, _is_full_image_copy, _regions_cover_both_images, _stage1_phash
from utils.pdf_utils import ExtractedImage


def _image(array: np.ndarray, name: str) -> ExtractedImage:
    return ExtractedImage(
        page_num=1,
        img_index=0,
        filepath=str(Path("/tmp") / name),
        array=array,
        width=array.shape[1],
        height=array.shape[0],
        source_id=f"source:{name}",
    )


def _structured_image(height: int = 300, width: int = 480) -> np.ndarray:
    image = np.full((height, width, 3), 245, dtype=np.uint8)
    cv2.rectangle(image, (30, 35), (210, 260), (20, 80, 180), -1)
    cv2.circle(image, (345, 145), 95, (180, 40, 50), -1)
    cv2.putText(image, "Figure 2", (250, 285), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
    return image


def test_cross_resolution_copy_is_equivalent_and_not_a_risk():
    original = _structured_image()
    clearer_copy = cv2.resize(original, (960, 600), interpolation=cv2.INTER_CUBIC)
    images = [_image(original, "pdf.png"), _image(clearer_copy, "figure.png")]

    assert _is_full_image_copy(*images)
    assert _stage1_phash(images) == {(0, 1)}


def test_partial_reuse_is_not_discarded_as_a_whole_image_copy():
    original = _structured_image()
    altered = original.copy()
    altered[:, altered.shape[1] // 2 :] = 255

    assert not _is_full_image_copy(_image(original, "a.png"), _image(altered, "b.png"))


def test_same_path_is_ignored_before_copy_classification():
    original = _structured_image()
    images = [_image(original, "same.png"), _image(original.copy(), "same.png")]

    assert _stage1_phash(images) == set()


def test_sift_regions_must_cover_both_complete_images():
    image_a = np.zeros((100, 200, 3), dtype=np.uint8)
    image_b = np.zeros((200, 400, 3), dtype=np.uint8)

    assert _regions_cover_both_images((5, 5, 180, 90), (10, 10, 360, 180), image_a, image_b)
    assert not _regions_cover_both_images((5, 5, 80, 90), (10, 10, 160, 180), image_a, image_b)


def test_content_edges_remove_long_chart_axes_but_keep_local_signal():
    chart = np.zeros((200, 300), dtype=np.uint8)
    cv2.line(chart, (10, 180), (290, 180), 255, 2)
    cv2.line(chart, (10, 10), (10, 180), 255, 2)
    cv2.circle(chart, (150, 90), 20, 255, 2)

    content = _content_edges(chart)

    assert np.count_nonzero(content[60:120, 120:180]) > 0
    assert np.count_nonzero(content[175:190, :]) < np.count_nonzero(cv2.Canny(chart, 50, 150)[175:190, :])


def _textured(seed: int, height: int = 360, width: int = 480) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = rng.integers(0, 255, (height // 8, width // 8, 3), dtype=np.uint8)
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_NEAREST)


def test_parallel_sift_and_template_stages_match_a_serial_run_and_repeat(tmp_path, monkeypatch):
    # A shared panel pasted into two different figures: SIFT candidates and
    # template verification both fire. FLANN is seeded per pair, so the
    # findings repeat exactly and do not depend on the worker count.
    from modules import image_checker

    shared = _textured(1, 160, 200)
    images = []
    for k in range(6):
        base = _textured(10 + k)
        if k in (1, 4):
            base[40:200, 60:260] = shared
        images.append(ExtractedImage(page_num=k + 1, img_index=0, filepath=str(tmp_path / f"p{k}.png"),
                                     array=base, width=base.shape[1], height=base.shape[0],
                                     source_id=f"source:{k}"))

    def run(workers, name):
        monkeypatch.setattr(image_checker, "IMAGE_WORKERS", workers)
        out = image_checker.check_image_duplicates(images, str(tmp_path / name))
        return [(r["page_a"], r["page_b"], r["match_type"], r["severity"], r["region_a"], r["region_b"]) for r in out]

    serial = run(1, "serial")
    assert serial and any(item[:2] == (2, 5) for item in serial)
    assert run(4, "parallel_a") == serial
    assert run(4, "parallel_b") == serial
