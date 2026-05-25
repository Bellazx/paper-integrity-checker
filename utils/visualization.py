import cv2
import numpy as np
from pathlib import Path


def draw_duplicate_annotation(
    img_a: np.ndarray,
    img_b: np.ndarray,
    region_a: tuple | None,
    region_b: tuple | None,
    label_a: str,
    label_b: str,
    similarity: float,
    match_type: str,
    output_path: str,
):
    """Create a side-by-side annotated comparison image."""
    h_a, w_a = img_a.shape[:2]
    h_b, w_b = img_b.shape[:2]

    max_h = max(h_a, h_b)
    scale_a = min(1.0, 800 / w_a, 800 / h_a)
    scale_b = min(1.0, 800 / w_b, 800 / h_b)

    disp_a = cv2.resize(img_a, None, fx=scale_a, fy=scale_a)
    disp_b = cv2.resize(img_b, None, fx=scale_b, fy=scale_b)

    dh_a, dw_a = disp_a.shape[:2]
    dh_b, dw_b = disp_b.shape[:2]

    gap = 40
    header = 60
    canvas_w = dw_a + gap + dw_b
    canvas_h = max(dh_a, dh_b) + header
    canvas = np.ones((canvas_h, canvas_w, 3), dtype=np.uint8) * 255

    canvas[header:header + dh_a, :dw_a] = disp_a
    canvas[header:header + dh_b, dw_a + gap:dw_a + gap + dw_b] = disp_b

    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(canvas, label_a, (10, 25), font, 0.6, (0, 0, 0), 1)
    cv2.putText(canvas, label_b, (dw_a + gap + 10, 25), font, 0.6, (0, 0, 0), 1)

    info = f"{match_type} | similarity: {similarity:.3f}"
    cv2.putText(canvas, info, (10, 50), font, 0.5, (0, 0, 200), 1)

    if region_a:
        x, y, w, h = region_a
        sx, sy = int(x * scale_a), int(y * scale_a + header)
        sw, sh = int(w * scale_a), int(h * scale_a)
        cv2.rectangle(canvas, (sx, sy), (sx + sw, sy + sh), (0, 255, 0), 2)

    if region_b:
        x, y, w, h = region_b
        sx = int(x * scale_b + dw_a + gap)
        sy = int(y * scale_b + header)
        sw, sh = int(w * scale_b), int(h * scale_b)
        cv2.rectangle(canvas, (sx, sy), (sx + sw, sy + sh), (0, 255, 0), 2)

    if region_a and region_b:
        cx_a = int(region_a[0] * scale_a + region_a[2] * scale_a / 2)
        cy_a = int(region_a[1] * scale_a + header + region_a[3] * scale_a / 2)
        cx_b = int(region_b[0] * scale_b + dw_a + gap + region_b[2] * scale_b / 2)
        cy_b = int(region_b[1] * scale_b + header + region_b[3] * scale_b / 2)
        cv2.line(canvas, (cx_a, cy_a), (cx_b, cy_b), (0, 200, 0), 1, cv2.LINE_AA)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(output_path, canvas)
    return output_path
