from __future__ import annotations
#!/usr/bin/env python3
"""Generate batch AI review PDF reports and update the database.

Usage:
    python3 generate_review_report.py --results batch_results.json --output data/output/review_v2/

The results JSON is an array of objects, each with:
    doi, result, trigger, image_review, data_review, verdict, reason
"""
import argparse
import base64
import contextlib
import hashlib
import html
import json
import multiprocessing as mp
import os
import queue
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = os.environ.get(
    "PAPER_INTEGRITY_ROOT",
    "/Users/dingbella/Documents/sjtu/论文预警系统/paper-integrity-checker",
)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz
import pymssql
from modules.chinese_report_generator import (
    FONT_REGULAR, METHODS_HTML, DISCLAIMER_HTML,
    _apply_data_caps, _compute_dimension_risk, _compute_overall_risk,
    _soften_risk_wording, _sanitize_internal_references, doi_to_slug,
    validate_report_render_environment,
)
from modules.doi_utils import normalize_doi

DB_CONFIG = {
    "server": os.environ.get("PAPER_DB_SERVER", "10.119.5.44"),
    "user": os.environ.get("PAPER_DB_USER", "yujing"),
    "password": os.environ.get("PAPER_DB_PASSWORD", ""),
    "database": os.environ.get("PAPER_DB_NAME", "lunwenyujing"),
    "charset": "utf8",
}
PUBLIC_BASE_URL = os.environ.get("PAPER_PUBLIC_BASE_URL", "http://202.120.57.100").rstrip("/")
REVIEW_REPORT_BASE_URL = os.environ.get(
    "PAPER_REVIEW_BASE_URL", f"{PUBLIC_BASE_URL}/review_reports"
).rstrip("/")
REVIEW_V2_DIR = Path(PROJECT_ROOT) / "data" / "output" / "review_v2"
REVIEW_STATE_DIR = Path(PROJECT_ROOT) / "data" / "output" / "review_v2" / "state"
SECOND_REVIEW_QUEUE = Path(PROJECT_ROOT) / "data" / "output" / "review_v2" / "needs_two_reviewer_rereview.jsonl"
COVERAGE_TIMEOUT_SECONDS = int(os.environ.get("REVIEW_COVERAGE_TIMEOUT_SECONDS", "240"))
COVERAGE_MEMORY_MB = int(os.environ.get("REVIEW_COVERAGE_MEMORY_MB", "2048"))
# Reader-facing reports use a compact layout by default.  Set
# REVIEW_REPORT_VERBOSE=1 only for debugging/legacy investigations.
COMPACT_REVIEW_REPORT = os.environ.get("REVIEW_REPORT_VERBOSE", "0") != "1"
# nginx serves /review_reports/<ns>/<file> via per-table alias
#   /home/sjtu/paper-integrity-checker/data/output/<ns>/review_v2/<file>
# (see /etc/nginx/sites-available/knowledge-base, the regex location block).
# The legacy flat alias .../data/output/review_v2/ is shadowed by the per-table
# block, so PDFs written there are NOT served by nginx → 404.
# We must write to <project>/data/output/<namespace>/review_v2/ so local nginx can serve them.
# This is the *base* dir; the per-batch copy path is
#   NGINX_REVIEW_DIR / <namespace> / "review_v2"
NGINX_REVIEW_DIR = REVIEW_V2_DIR.parent
TODAY = date.today().strftime("%Y年%m月%d日")
REVIEW_DISCLAIMER_HTML = DISCLAIMER_HTML.replace("六、免责声明", "五、免责声明", 1)
INCOMPLETE_REVIEW_RESULT = "低风险"
PRIOR_HIGH_LOW_QUARANTINE = (
    Path(PROJECT_ROOT) / "data" / "tmp" / "rereview_20260619" / "fresh_outputs"
    / "report_guard_prior_high_now_low_needs_redo.json"
)
_PRIOR_HIGH_CACHE: dict[str, bool] = {}


def _is_incomplete_material_review(info: dict) -> bool:
    """Return True only for hard upstream failures that cannot be rendered.

    Missing materials are not a review terminal state. Review available material
    and, absent concrete integrity findings, keep the result as low-risk.
    """
    trigger = str(info.get("trigger") or "")
    return trigger == "report_not_found"


def _old_4050_review_result_is_high(doi: str) -> bool:
    """Return True when the original yujing_4050 review_result is 高风险.

    This guards the final render/write path: old-high/new-low cases must be
    pulled out for an independent redo instead of being written as low-risk.
    """
    norm = normalize_doi(doi) or doi
    if norm in _PRIOR_HIGH_CACHE:
        return _PRIOR_HIGH_CACHE[norm]
    try:
        conn = pymssql.connect(**DB_CONFIG)
        try:
            cur = conn.cursor(as_dict=True)
            cur.execute(
                "SELECT TOP 1 review_result FROM dbo.yujing_4050 WHERE doi=%s ORDER BY id DESC",
                (norm,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        is_high = bool(row and row.get("review_result") == "高风险")
    except Exception as e:
        print(f"[PRIOR_GUARD] {doi}: could not query old yujing_4050 review_result ({e})", file=sys.stderr)
        is_high = False
    _PRIOR_HIGH_CACHE[norm] = is_high
    return is_high


def _quarantine_old_high_new_low(info: dict, table_name: str) -> None:
    PRIOR_HIGH_LOW_QUARANTINE.parent.mkdir(parents=True, exist_ok=True)
    old = []
    if PRIOR_HIGH_LOW_QUARANTINE.exists():
        try:
            old = json.loads(PRIOR_HIGH_LOW_QUARANTINE.read_text(encoding="utf-8"))
            if not isinstance(old, list):
                old = []
        except Exception:
            old = []
    row = dict(info)
    row["_target_table"] = table_name
    row["_needs_independent_redo"] = True
    row["_redo_reason"] = (
        "旧表yujing_4050.review_result为高风险，但本轮准备写入低风险；"
        "按规则单独拎出重做，不生成正式PDF/DB写入。"
    )
    row["_quarantined_at"] = datetime.now(timezone.utc).isoformat()
    by_doi = {str(x.get("doi")): x for x in old if isinstance(x, dict) and x.get("doi")}
    by_doi[str(row.get("doi"))] = row
    PRIOR_HIGH_LOW_QUARANTINE.write_text(
        json.dumps(list(by_doi.values()), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _html_escape(value) -> str:
    return html.escape(str(value or ""), quote=False)


def _safe_evidence_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "paper"))
    return value.strip("._-")[:120] or "paper"


def _copy_evidence_image(source: str, scratch_dir: Path, index: int) -> Path | None:
    """Copy one already-created evidence image into this paper's scratch dir."""
    if not source:
        return None
    try:
        src = Path(source).expanduser().resolve()
        if not src.is_file() or src.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            return None
        scratch_dir.mkdir(parents=True, exist_ok=True)
        dst = scratch_dir / f"evidence_{index:02d}{src.suffix.lower()}"
        shutil.copy2(src, dst)
        return dst
    except (OSError, ValueError):
        return None


def _render_page_evidence(pdf_path: str, page_number: int, scratch_dir: Path, index: int) -> Path | None:
    """Render a cited PDF page as a bounded evidence image.

    The page-level image is used only when the review text gives a concrete page
    but no machine-readable bounding box. The caption tells the reader to use
    the nearby textual location (table/figure/formula) for the exact target.
    """
    try:
        pdf = Path(pdf_path).expanduser().resolve()
        page_idx = int(page_number) - 1
        if not pdf.is_file() or page_idx < 0:
            return None
        with fitz.open(pdf) as doc:
            if page_idx >= len(doc):
                return None
            page = doc[page_idx]
            pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            scratch_dir.mkdir(parents=True, exist_ok=True)
            out = scratch_dir / f"page_evidence_{index:02d}.png"
            pix.save(str(out))
            return out
    except (OSError, ValueError, RuntimeError):
        return None


def _resolve_excel_evidence_file(input_dir: str, relative_file: str) -> Path | None:
    """Resolve an audit workbook without allowing paths outside this paper's input."""
    if not input_dir or not relative_file or "!/" in relative_file:
        return None
    try:
        root = Path(input_dir).expanduser().resolve()
        requested = Path(str(relative_file).replace("\\", "/"))
        candidate = (root / requested).resolve()
        if root not in candidate.parents or candidate.suffix.lower() not in {".xlsx", ".xlsm"}:
            return None
        if candidate.is_file():
            return candidate
        matches = [
            path.resolve() for path in root.rglob(requested.name)
            if path.is_file() and path.suffix.lower() in {".xlsx", ".xlsm"}
        ]
        return matches[0] if len(matches) == 1 else None
    except (OSError, RuntimeError, ValueError):
        return None


def _excel_column_name(index: int) -> str:
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _evidence_raster_font(size: int):
    from PIL import ImageFont

    candidates = (
        Path.home() / ".local/share/fonts/paper-pecker/NotoSansCJK-Regular.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    return ImageFont.truetype(str(path), size) if path else ImageFont.load_default()


def _render_excel_region_evidence(
    item: dict, input_dir: str, scratch_dir: Path, index: int
) -> Path | None:
    """Render exact workbook ranges as a readable, coordinate-preserving image."""
    try:
        from openpyxl import load_workbook
        from PIL import Image, ImageDraw

        rendered_regions = []
        for region in (item.get("regions") or [])[:2]:
            workbook_path = _resolve_excel_evidence_file(input_dir, region.get("file") or "")
            row_range = region.get("row_range") or []
            col_range = region.get("col_range") or []
            if not workbook_path or len(row_range) != 2 or len(col_range) != 2:
                continue
            row_start, row_end = sorted(int(value) for value in row_range)
            col_start, col_end = sorted(int(value) for value in col_range)
            if row_start < 1 or col_start < 1 or row_end - row_start > 199 or col_end - col_start > 19:
                continue
            with contextlib.closing(load_workbook(workbook_path, read_only=True, data_only=True)) as workbook:
                sheet_name = str(region.get("sheet") or "")
                if sheet_name not in workbook.sheetnames:
                    continue
                sheet = workbook[sheet_name]
                shown_end = min(row_end, row_start + 11)
                values = [
                    [sheet.cell(row, column).value for column in range(col_start, col_end + 1)]
                    for row in range(row_start, shown_end + 1)
                ]
            rendered_regions.append({
                "label": str(region.get("label") or f"区域{len(rendered_regions) + 1}"),
                "file": workbook_path.name,
                "sheet": sheet_name,
                "row_start": row_start,
                "row_end": row_end,
                "shown_end": shown_end,
                "col_start": col_start,
                "col_end": col_end,
                "values": values,
            })
        if not rendered_regions:
            return None

        title_font = _evidence_raster_font(24)
        text_font = _evidence_raster_font(17)
        small_font = _evidence_raster_font(15)
        margin = 30
        row_label_width = 78
        cell_width = 155
        row_height = 48
        header_height = 62
        title_height = 88
        gap = 28
        block_widths = [
            row_label_width + len(region["values"][0]) * cell_width
            for region in rendered_regions
        ]
        block_heights = [header_height + len(region["values"]) * row_height + 58 for region in rendered_regions]
        horizontal = len(rendered_regions) == 2 and sum(block_widths) + gap <= 1900
        width = (
            sum(block_widths) + gap + margin * 2
            if horizontal
            else max(block_widths) + margin * 2
        )
        height = (
            title_height + max(block_heights) + margin
            if horizontal
            else title_height + sum(block_heights) + gap * (len(rendered_regions) - 1) + margin
        )
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        draw.text((margin, 16), "Excel evidence ranges", fill="#202124", font=title_font)
        draw.text(
            (margin, 52),
            "Values are read directly from the submitted workbook; coordinates are preserved.",
            fill="#5f6368",
            font=small_font,
        )

        next_y = title_height
        next_x = margin
        for region, block_width in zip(rendered_regions, block_widths):
            origin_x = next_x if horizontal else margin
            y = title_height if horizontal else next_y
            range_text = (
                f"{region['label']} | {region['sheet']} | "
                f"R{region['row_start']}:R{region['row_end']} "
                f"C{region['col_start']}:C{region['col_end']}"
            )
            draw.text((origin_x, y), range_text, fill="#8f1d18", font=text_font)
            y += 36
            x = origin_x
            headers = ["Row"] + [
                _excel_column_name(column) for column in range(region["col_start"], region["col_end"] + 1)
            ]
            widths = [row_label_width] + [cell_width] * (len(headers) - 1)
            for header, cell_w in zip(headers, widths):
                draw.rectangle((x, y, x + cell_w, y + header_height), fill="#fce8e6", outline="#9aa0a6", width=2)
                draw.text((x + 8, y + 20), header, fill="#202124", font=text_font)
                x += cell_w
            y += header_height
            for offset, row_values in enumerate(region["values"]):
                x = origin_x
                display_values = [str(region["row_start"] + offset)] + [
                    "" if value is None else str(value) for value in row_values
                ]
                for value, cell_w in zip(display_values, widths):
                    draw.rectangle((x, y, x + cell_w, y + row_height), fill="#fffafa", outline="#bdc1c6")
                    shown = value if len(value) <= 17 else value[:14] + "..."
                    draw.text((x + 8, y + 13), shown, fill="#202124", font=small_font)
                    x += cell_w
                y += row_height
            if region["shown_end"] < region["row_end"]:
                omitted = region["row_end"] - region["shown_end"]
                draw.text((origin_x, y + 8), f"... {omitted} additional rows in the cited range", fill="#5f6368", font=small_font)
            if horizontal:
                next_x += block_width + gap
            else:
                next_y = y + 58 + gap

        scratch_dir.mkdir(parents=True, exist_ok=True)
        output = scratch_dir / f"excel_evidence_{index:02d}.png"
        image.save(output, optimize=True)
        return output
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _evidence_image_data_uri(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(suffix, "image/png")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _review_evidence_images(info: dict, findings: dict | None, scratch_dir: Path) -> list[dict]:
    """Collect only confirmed high-risk evidence images for the final PDF.

    Existing annotation paths are preferred. An optional ``evidence_images``
    list is also supported for future agents to return explicitly confirmed
    evidence. The function deliberately does not expose low-risk findings.
    """
    if "高风险" not in f"{info.get('result', '')}{info.get('verdict', '')}":
        return []

    high_dims = {
        dim for dim in ("image", "data", "ref", "methodology")
        if _dimension_verdict(info, dim) == "建议高风险"
    }
    if not high_dims:
        return []

    items: list[dict] = []
    next_index = 1
    explicit = info.get("evidence_images") or []
    if isinstance(explicit, list):
        for item in explicit:
            if not isinstance(item, dict) or item.get("dimension") not in high_dims:
                continue
            copied = _copy_evidence_image(item.get("path") or item.get("image_path"), scratch_dir, next_index)
            if not copied:
                continue
            items.append({
                **{key: item[key] for key in ("issue_index", "max_height", "start_on_new_page") if key in item},
                "dimension": item.get("dimension"),
                "path": copied,
                "caption": str(item.get("caption") or "复核确认的证据截图"),
            })
            next_index += 1

    excel_regions = info.get("excel_evidence_regions") or []
    if "data" in high_dims and isinstance(excel_regions, list):
        for item in excel_regions:
            if not isinstance(item, dict) or item.get("dimension", "data") != "data":
                continue
            rendered = _render_excel_region_evidence(
                item, str(info.get("_input_dir") or ""), scratch_dir, next_index
            )
            if not rendered:
                continue
            items.append({
                **{key: item[key] for key in ("issue_index", "max_height", "start_on_new_page") if key in item},
                "dimension": "data",
                "path": rendered,
                "caption": str(item.get("caption") or "Excel源数据证据截图"),
            })
            next_index += 1

    if items:
        return items

    report = findings or {}
    if "image" in high_dims:
        for finding in (report.get("image_duplicates") or []):
            if finding.get("severity") != "high":
                continue
            copied = _copy_evidence_image(finding.get("annotation_path"), scratch_dir, next_index)
            if not copied:
                continue
            pages = [p for p in (finding.get("page_a"), finding.get("page_b")) if p is not None and int(p) > 0]
            page_text = "、".join(f"第{int(p)}页" for p in pages) if pages else "论文图像位置"
            if any(p is not None and int(p) <= 0 for p in (finding.get("page_a"), finding.get("page_b"))):
                page_text += "与原始图文件"
            items.append({
                "dimension": "image",
                "path": copied,
                "caption": f"图像证据：{page_text}；红色标注为复核关注区域。",
            })
            next_index += 1
            if len(items) >= 4:
                break

        if len(items) < 4:
            for finding in (report.get("image_splicing") or []):
                copied = _copy_evidence_image(finding.get("annotation_path"), scratch_dir, next_index)
                if not copied:
                    continue
                page = finding.get("page")
                page_text = f"第{int(page)}页" if page is not None else "论文图像位置"
                items.append({
                    "dimension": "image",
                    "path": copied,
                    "caption": f"图像证据：{page_text}；红色标注为复核关注区域。",
                })
                next_index += 1
                if len(items) >= 4:
                    break

    # For confirmed non-image problems, include up to four distinct cited pages.
    # Page screenshots are preferable to invented bounding boxes when the agent
    # did not provide exact coordinates, and make the finding auditable.
    if high_dims & {"data", "ref", "methodology"}:
        paper = (report.get("paper") or {}) if isinstance(report, dict) else {}
        pdf_path = paper.get("filepath") or info.get("_pdf_path") or ""
        seen_pages = set()
        for dimension in ("data", "ref", "methodology"):
            if dimension not in high_dims:
                continue
            key = _DIMENSION_REVIEW_KEY[dimension]
            text = str(info.get(key) or "")
            for pattern, flags in ((r"第\s*(\d+)\s*页", 0), (r"\bpage\s*(\d+)\b", re.I)):
                for match in re.finditer(pattern, text, flags):
                    page = int(match.group(1))
                    if page in seen_pages or len(items) >= 4 or not pdf_path:
                        continue
                    copied = _render_page_evidence(pdf_path, page, scratch_dir, next_index)
                    if copied:
                        seen_pages.add(page)
                        items.append({
                            "dimension": dimension,
                            "path": copied,
                            "caption": f"页面证据：论文第{page}页；具体表格、图或公式位置见问题说明。",
                        })
                        next_index += 1
    return items[:4]


def _evidence_images_html(items: list[dict]) -> str:
    if not items:
        return ""
    blocks = []
    for item in items:
        try:
            uri = _evidence_image_data_uri(Path(item["path"]))
        except (OSError, ValueError):
            continue
        blocks.append(
            '<div style="border-left:3pt solid #b3261e; background:#fffafa; '
            'padding:6pt 8pt; margin:6pt 0 9pt 0; page-break-inside:avoid;">'
            '<p style="font-size:9pt; font-weight:bold; color:#8f1d18; margin-bottom:5pt;">证据截图</p>'
            f'<img src="{uri}" style="display:block; width:100%; max-height:360pt; object-fit:contain; margin-bottom:5pt;">'
            f'<p style="font-size:8.5pt; color:#555; margin-bottom:0;">{_html_escape(item.get("caption"))}</p>'
            '</div>'
        )
    return "\n".join(blocks)


def _header_html(paper: dict) -> str:
    title = _html_escape(paper.get("title", "未知"))
    authors_full = [_html_escape(x) for x in paper.get("authors_full", [])]
    affiliations = [_html_escape(x) for x in paper.get("affiliations", [])]
    journal = _html_escape(paper.get("journal", "未知"))
    doi = _html_escape(paper.get("doi", "未知"))
    total_images = paper.get("total_images", 0)
    total_refs = paper.get("total_references", 0)

    all_author_line = ""
    if authors_full:
        all_author_line = f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>全部作者：</b>{", ".join(authors_full)}</p>'

    all_aff_html = ""
    if affiliations:
        all_aff_html = '<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>作者单位：</b></p>'
        for aff in affiliations:
            all_aff_html += f'<p style="font-size:8.5pt; color:#555; margin-bottom:1pt; margin-left:12pt;">{aff}</p>'

    stats = []
    if total_images:
        stats.append(f'<b>提取图片数：</b>{total_images}')
    if total_refs:
        stats.append(f'<b>参考文献数：</b>{total_refs}')
    stats_line = f'<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;">{"　　".join(stats)}</p>' if stats else ""

    return f"""
<h1 style="font-size:18pt; color:#1a1a1a; text-align:center; margin-bottom:4pt;">Paper Pecker论文预警报告（AI复核版）</h1>
<p style="font-size:10.5pt; color:#555; text-align:center; margin-top:0; margin-bottom:16pt;">Paper Pecker Risk Alert Report — AI Review</p>
<hr style="border:none; border-top:1px solid #ccc; margin:10pt 0;">
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">一、论文基本信息</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>论文标题：</b>{title}</p>
{all_author_line}
{all_aff_html}
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>发表期刊：</b>{journal}</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>DOI：</b>{doi}</p>
{stats_line}
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>复核日期：</b>{TODAY}</p>
"""


_NO_ISSUE_MARKERS = ("未发现异常", "未发现问题", "未检出", "无异常发现", "无参考文献异常")


def _has_issues(text: str) -> bool:
    """Return True if the review text contains actual issues (not just 'no issues found')."""
    text = text.strip()
    if not text:
        return False
    for marker in _NO_ISSUE_MARKERS:
        if marker in text and len(text) < 50:
            return False
    return True


def _text_to_html(text: str) -> str:
    """Convert plain text with newlines to HTML paragraphs.
    Lines starting with 【 get extra top margin for visual separation.
    Lines flagged as confirmed issues get a red-left-border highlight.
    Lines flagged as false-positive summary get a grey-left-border."""
    text = (text or "").replace("\\n", "\n")
    text = text.replace("为什么需要关注", "需关注原因")
    text = re.sub(r"(公式|式)\s*[（(]\s*\n\s*(\d+)\s*[）)]", r"\1(\2)", text)
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    parts = []

    _issue_markers = re.compile(
        r"^(?:核心发现|确认问题|真实问题|关键发现|"
        r"\d+处真实问题|1处真实问题|"
        r"①|②|③|④|⑤|⑥|⑦|⑧|⑨|⑩)"
    )
    _fp_markers = re.compile(
        r"^(?:假阳性|经核查为假阳性|均为假阳性|其余.*?假阳性|"
        r"其余.*?告警|假阳性来源|自动检测.*?假阳性)"
    )

    in_issue_block = False
    for p in paragraphs:
        safe_p = _highlight_explanation_labels(p)
        safe_p = re.sub(
            r"(?<![A-Za-z0-9])((?:公式|式)\s*[（(]\s*\d+\s*[）)])",
            r'<span style="white-space:nowrap;">\1</span>',
            safe_p,
        )
        if p.startswith("【"):
            parts.append(
                f'<p style="font-size:9.5pt; color:#333; margin-top:14pt; '
                f'margin-bottom:4pt; font-weight:bold;">{safe_p}</p>'
            )
            in_issue_block = False
        elif _issue_markers.search(p):
            in_issue_block = True
            parts.append(
                f'<p style="font-size:9.5pt; color:#333; margin-bottom:6pt; '
                f'border-left:3px solid #c00; padding-left:8pt;">{safe_p}</p>'
            )
        elif _fp_markers.search(p):
            in_issue_block = False
            parts.append(
                f'<p style="font-size:9.5pt; color:#666; margin-bottom:6pt; '
                f'border-left:3px solid #ccc; padding-left:8pt;">{safe_p}</p>'
            )
        elif in_issue_block and p and not p.startswith("本模块"):
            parts.append(
                f'<p style="font-size:9.5pt; color:#333; margin-bottom:6pt; '
                f'border-left:3px solid #c00; padding-left:8pt;">{safe_p}</p>'
            )
        else:
            in_issue_block = False
            parts.append(f'<p style="font-size:9.5pt; color:#333; margin-bottom:6pt;">{safe_p}</p>')
    return "\n".join(parts)


def _highlight_explanation_labels(text: str) -> str:
    safe = _html_escape(text)
    labels = (
        "问题位置", "问题类型", "具体情况", "需关注原因", "核查依据",
        "Location", "Issue type", "What was found", "Why it matters", "Verification basis",
    )
    for label in labels:
        safe = re.sub(
            rf"(?i)(?<![A-Za-z])({re.escape(label)}\s*[：:])",
            r"<b>\1</b>",
            safe,
        )
    return safe


_CONFIRMED_MARKERS = (
    "核心发现", "确认问题", "真实问题", "值得关注", "数据完整性问题",
    "确认为数据复制", "属于数据复制", "数据准备错误", "复制粘贴",
    "属数据复制", "属数据呈现异常", "数据呈现异常", "实质性",
    "逐位相同", "逐位完全相同", "完全一致", "100%重叠",
    "物理上不可能", "不可能等于", "矛盾",
)

_FP_MARKERS = (
    "假阳性", "属正常", "为正常", "可解释", "不构成", "正常现象",
    "正常特征", "正常模式", "属解析", "解析问题", "格式伪影",
    "不构成数据异常", "不构成数据完整性", "非数据重复", "非异常",
    "收录滞后", "数据库限制",
    "非测量数据异常", "非数据伪造", "非测量异常",
    "属于仪器", "仪器预设", "仪器采集特征",
    "标准数据结构", "数据处理规范",
    "天然存在线性关系", "天然存在",
    "重复标记",
    "未发现跨表", "未发现可疑",
)

_VERDICT_LINE_RE = re.compile(r"^本模块复核结论：")
_STRONG_CONFIRMED_RE = re.compile(
    r"(?:确认|确认为|锁定|构成|存在|发现|显示|提示|无法|不能|不可|难以).{0,40}"
    r"(?:完整性问题|异常|矛盾|不一致|复用|重复|完全一致|完全相同)"
    r"|(?:完整性问题|异常|矛盾|不一致).{0,40}(?:无法|不能|不可|难以).{0,20}(?:解释|排除)"
)
_INLINE_SECTION_HEADER_RE = re.compile(
    r"^(?:确认的问题|已排除的告警|假阳性来源[（(].*?[）)]?)[：:]\s*"
)


def _split_review_sentences(line: str) -> list[str]:
    """Split dense reviewer text into sentence-like pieces for safer styling."""
    pieces = re.findall(r"[^。；;]+[。；;]?", line or "")
    return [p.strip() for p in pieces if p.strip()] or ([line.strip()] if line.strip() else [])


def _ensure_terminal_punctuation(text: str) -> str:
    text = (text or "").strip()
    if text and text[-1] not in "。！？!?；;":
        return text + "。"
    return text


def _normalize_highrisk_reason_piece(piece: str) -> str:
    """Avoid misleading dimension labels in the high-risk summary.

    Some reviewer reasons label a data-integrity explanation as
    "研究设计/统计核查" because it cites sample/method context. If the same
    sentence confirms source-data inconsistency, render it as data evidence so
    it does not contradict the low-risk methodology detail module.
    """
    piece = (piece or "").strip()
    if re.match(r"^(?:方法学|研究设计)(?:/统计核查|与统计核查)?[：:]", piece) and re.search(
        r"(?:源数据|数据点|数值).{0,80}(?:匹配|一致|无.*变异|不符合|完整性问题)",
        piece,
    ):
        return re.sub(r"^(?:方法学|研究设计)(?:/统计核查|与统计核查)?[：:]", "数据维度补充说明：", piece, count=1)
    return piece


def _strip_internal_review_process_terms(text: str) -> str:
    """Remove internal pipeline wording from user-facing PDF prose.

    The PDF is an external review report. It should present evidence and the
    final conclusion, not mention r1/reviewer2/judge orchestration or phrases
    such as "对抗性复核/推翻". Those terms are useful for operators but read as
    noisy and adversarial to report users.
    """
    text = text or ""
    replacements = (
        (r"data_review", "数据复核部分"),
        (r"image_review", "图像复核部分"),
        (r"ref_review", "参考文献复核部分"),
        (r"methodology_review", "研究设计复核部分"),
        (r"\bReviewer\s*2\b", "本次复核"),
        (r"\breviewer\s*2\b", "本次复核"),
        (r"Reviewer2", "本次复核"),
        (r"reviewer2", "本次复核"),
        (r"(?<![A-Za-z0-9_])R2(?![A-Za-z0-9_])", "本次复核"),
        (r"(?<![A-Za-z0-9_])r2(?![A-Za-z0-9_])", "本次复核"),
        (r"(?<![A-Za-z0-9_])R1(?![A-Za-z0-9_])", "初步复核"),
        (r"(?<![A-Za-z0-9_])r1(?![A-Za-z0-9_])", "初步复核"),
        (r"Judge", "最终复核"),
        (r"judge", "最终复核"),
        (r"round8\s*Judge\s*最终收敛", "最终复核"),
        (r"对抗性复核", "进一步复核"),
        (r"对抗复核", "进一步复核"),
        (r"收敛判定依据", "综合评估"),
        (r"机器结果", "结论"),
    )
    for old, new in replacements:
        text = re.sub(old, new, text)

    text = re.sub(r"(?:前期核查|初步复核)\s*(?:复核)?(?:仅|只)[^。；;]*[。；;]?", "", text)
    text = re.sub(r"初步复核复核", "前期核查", text)
    text = re.sub(r"并非(?:初步复核|前期核查)?(?:判定|判断)的?[\"“]?", "并非\"", text)
    text = re.sub(r"非(?:初步复核|前期核查)?(?:判定|判断)的?[\"“]?", "非\"", text)
    text = re.sub(r"(?:初步复核|前期核查)(?:判定|判断)的?", "", text)
    text = re.sub(r"(?:足以)?推翻(?:了)?(?:初步复核|初检|前次复核)?(?:低风险)?结论", "支持本次高风险结论", text)
    text = text.replace("被矩阵自身推翻", "与矩阵自身计算结果相冲突")
    text = text.replace("推翻", "不支持")
    text = text.replace("进一步复核新发现", "本次复核确认")
    text = text.replace("进一步复核发现", "本次复核确认")
    text = text.replace("进一步复核经", "本次复核经")
    text = re.sub(r"(?:初步复核|本次复核|最终复核)\s*(?:新发现|发现)", "本次复核发现", text)
    text = re.sub(r"(?:综合|结合)\s*初步复核\s*与\s*本次复核[，,]?", "综合核查结果，", text)
    text = re.sub(r"初步复核\s*(?:仅|只)", "前期核查仅", text)
    text = re.sub(r"本次复核\s*进一步复核", "进一步复核", text)
    text = re.sub(r"进一步复核中重点重读", "复核中重点核查", text)
    text = re.sub(r"进一步复核基于", "本次复核基于", text)
    text = re.sub(r"进一步复核后", "本次复核后", text)
    text = re.sub(r"本次复核本次复核", "本次复核", text)
    text = re.sub(r"本次最终复核最终收敛[^，。；;]*[，,]?", "", text)
    text = re.sub(r"最终复核直接读取", "本次复核读取", text)
    text = re.sub(r"本最终复核已", "本次复核已", text)
    text = re.sub(r"详见\s+([^，。；;]*?复核部分)\s+详述", r"详见\1", text)
    return text


def _format_external_reason_text(text: str) -> str:
    """Readable, external-facing 综合评估 text."""
    text = (text or "").replace("为什么需要关注", "需关注原因")
    text = _strip_internal_review_process_terms(text or "")
    text = _format_reason_text(text)

    # Break before common evidence bullets when agents emitted a dense paragraph.
    text = re.sub(r"(?<!\n)(?=-\s*(?:图像|数据|方法学|参考文献|文献)问题[：:])", "\n", text)
    text = re.sub(r"(?<!\n)(?=\d+[）)]\s*)", "\n", text)
    text = re.sub(r"(?<!\n)(?=[①②③④⑤⑥⑦⑧⑨⑩])", "\n", text)
    text = re.sub(r"([：:])\s*-\s*", r"\1\n- ", text)
    text = re.sub(r"(。)(?=(?:该问题|上述异常|综合判断|最终判定|本次复核|核心问题|按|通过|只有))", r"\1\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _structure_section_html(text: str, section_verdict: str = "") -> str:
    """Split review section text into confirmed-issue and false-positive blocks
    with distinct visual styling, so readers can immediately see what's real."""
    text = _format_section_text(_strip_internal_review_process_terms(text))
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if not lines:
        return ""

    # Headers we'll add ourselves — strip agent-generated duplicates
    _header_noise = re.compile(
        r"^(?:确认的问题[：:]?\s*$|已排除的告警[：:]?\s*$|假阳性来源[（(].*?[）)][：:]?\s*$)"
    )

    verdict_lines = []
    confirmed_lines = []
    fp_lines = []
    neutral_lines = []

    # Negation prefixes that flip a confirmed marker into a false-positive context.
    _NEGATION_RE = re.compile(r"(?:不构成|无|未发现|未见|未检出|不存在|没有|排除).{0,6}")

    for line in lines:
        if _VERDICT_LINE_RE.match(line):
            verdict_lines.append(line)
            continue
        if _header_noise.match(line):
            continue  # skip — we add our own headers

        for piece in _split_review_sentences(line):
            piece = _INLINE_SECTION_HEADER_RE.sub("", piece).strip()
            if not piece:
                continue
            fp_hit = any(m in piece for m in _FP_MARKERS)
            structured_issue_line = bool(re.match(
                r"^(?:问题位置|问题类型|具体情况|需关注原因|核查依据|"
                r"Location|Issue type|What was found|Why it matters|Verification basis)\s*[：:]",
                piece,
                re.I,
            ))
            hits = [m for m in _CONFIRMED_MARKERS if m in piece]
            if structured_issue_line and "低风险" not in (section_verdict or ""):
                confirmed_lines.append(_ensure_terminal_punctuation(piece))
                continue
            if hits and "低风险" not in (section_verdict or ""):
                # Confirmed evidence wins over false-positive wording only when
                # the sentence has a strong confirmation phrase. This preserves
                # "完全一致 ... 属假阳性" as dismissed, while keeping
                # "无法用假阳性解释，构成完整性问题" as confirmed.
                all_negated = all(
                    _NEGATION_RE.search(piece[:piece.index(h)].split("，")[-1].split("；")[-1])
                    for h in hits
                )
                if not all_negated and (not fp_hit or _STRONG_CONFIRMED_RE.search(piece)):
                    confirmed_lines.append(_ensure_terminal_punctuation(piece))
                    continue
            if fp_hit:
                fp_lines.append(_ensure_terminal_punctuation(piece))
                continue
            neutral_lines.append(_ensure_terminal_punctuation(piece))

    parts = []

    # Verdict line at top
    for line in verdict_lines:
        verdict_color = "#c00" if "高风险" in line else "#080"
        parts.append(
            f'<p style="font-size:9.5pt; color:{verdict_color}; margin-bottom:8pt; '
            f'font-weight:bold;">{_html_escape(line)}</p>'
        )

    # Confirmed issues block (red accent)
    if confirmed_lines:
        inner = "\n".join(
            f'<p style="font-size:9.5pt; color:#333; margin-bottom:5pt;">{_highlight_explanation_labels(l)}</p>'
            for l in confirmed_lines
        )
        parts.append(
            f'<div style="border-left:3pt solid #c00; background:#fff5f5; padding:6pt 8pt; margin:6pt 0;">'
            f'<p style="font-size:9.5pt; font-weight:bold; color:#c00; margin-bottom:4pt;">确认的问题：</p>'
            f'{inner}</div>'
        )

    # Neutral context lines
    for line in neutral_lines:
        parts.append(f'<p style="font-size:9.5pt; color:#333; margin-bottom:5pt;">{_highlight_explanation_labels(line)}</p>')

    # False positive block (green accent, smaller text)
    if fp_lines:
        inner = "\n".join(
            f'<p style="font-size:8.5pt; color:#666; margin-bottom:4pt;">{_highlight_explanation_labels(l)}</p>'
            for l in fp_lines
        )
        parts.append(
            f'<div style="border-left:3pt solid #0a0; background:#f5fff5; padding:6pt 8pt; margin:6pt 0;">'
            f'<p style="font-size:8.5pt; font-weight:bold; color:#080; margin-bottom:4pt;">已排除的告警：</p>'
            f'{inner}</div>'
        )

    return "\n".join(parts)


def _humanize_internal_terms(text: str) -> str:
    """Replace internal detector/rule names with reviewer-facing labels."""
    text = text or ""
    replacements = (
        ("方法学与统计核查", "研究设计与统计核查"),
        ("方法学与时间线", "研究设计与时间线"),
        ("方法学/统计核查", "研究设计/统计核查"),
        ("方法学维度", "研究设计维度"),
        ("方法学", "研究设计"),
        ("cross_sheet_row_duplicate", "跨表整行重复告警"),
        ("cross_sheet_column_duplicate", "跨表整列重复告警"),
        ("cross_sheet_reuse", "跨表数据复用告警"),
        ("cross_group_duplicate", "跨组重复告警"),
        ("duplicate_column_pairs", "重复数据列告警"),
        ("decimal_uniformity", "小数位一致告警"),
        ("benfords_law", "首位数字分布"),
        ("value_recycling", "数值重复使用告警"),
        ("image_duplicates", "图像重复告警"),
        ("image_splicing", "图像拼接告警"),
        ("template_score", "模板匹配分"),
        ("PHash=0", "感知哈希完全一致"),
        ("PHash", "感知哈希"),
        ("data_anomalies", "数据异常"),
        ("reference_issues", "参考文献问题"),
        ("terminal_digit", "末位数字分布"),
        ("sd_regularity", "标准差规律性"),
        ("arithmetic_sequence", "等差数列"),
        ("geometric_sequence", "等比数列"),
        ("coefficient_of_variation", "变异系数"),
        ("not_found", "未收录"),
        ("doi_mismatch", "DOI不匹配"),
        # Internal review-orchestration terms that the agent may have used to
        # describe deterministic findings; replace with reviewer-facing wording.
        ("evidence bundle", "检测发现"),
        ("evidence bundle中", "检测发现"),
        ("evidence bundle层", "检测环节"),
        ("evidence bundle里的", "检测发现中的"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    # "Benford定律" → "首位数字分布定律" as a unit; bare "Benford" → "首位数字分布"
    # But avoid "首位数Benford定律" → "首位数首位数字分布定律"
    text = re.sub(r"首位数字?Benford定律", "首位数字分布定律", text)
    text = re.sub(r"Benford定律", "首位数字分布定律", text)
    text = text.replace("Benford", "首位数字分布")
    text = re.sub(r"report\.json(?:中|的|里|无|有|未|显示|标记|记录|检出|包含)?", _report_json_replace, text)
    return text


def _report_json_replace(m: re.Match) -> str:
    """Replace 'report.json' + optional Chinese suffix with natural language."""
    full = m.group(0)
    suffix = full[len("report.json"):]
    if suffix:
        return "结构化检测报告" + suffix
    return "结构化检测报告"


def _normalize_verdict_phrases(text: str) -> str:
    text = text or ""
    text = text.replace("确认疑似高风险", "建议高风险")
    text = text.replace("确认高风险", "建议高风险")
    text = text.replace("维持疑似高风险", "建议高风险")
    text = text.replace("建议高风险", "建议高风险")
    text = text.replace("疑似高风险", "建议高风险")
    text = re.sub(r"判定为([^。；，,]*?)建议高风险", r"判定为\1建议高风险", text)
    text = re.sub(r"[，,；;]\s*触发维度[为：:][^。；;]*", "", text)
    text = re.sub(r"触发维度[为：:][^。；;]*[。；;]?", "", text)
    # BUG2 fix: strip the "自动校准 override 升级" template that appears when
    # the reviewer marked the paper 未审 and the batch machinery auto-promoted
    # the verdict.  When the human reviewer explicitly says 未审, the
    # downstream prose MUST NOT carry any audit conclusion — it is a known
    # anti-pattern that used to leave orphan "建议高风险" PDFs behind.
    text = re.sub(
        r"自动校准[^。；;，,]*(?:override|Override|OVERRIDE)[^。；;，,]*升级[^。；;，,]*建议高风险",
        "",
        text,
    )
    text = re.sub(r"自动校准[^。；;，,]*override[^。；;，,]*升级", "", text, flags=re.I)
    # BUG3 fix: strip empty "复核确认：xxx层面存在异常" placeholders.  These are
    # produced when a section verdict was synthesized without any underlying
    # evidence sentence.  If a paper's prose does not cite any specific
    # finding, the PDF must not show the placeholder sentence.
    text = re.sub(
        r"复核确认[：:][^。；;]*?层面存在异常[^。；;]*?[。；;]?",
        "",
        text,
    )
    return text


def _format_section_text(text: str) -> str:
    """Add readable paragraph breaks to dense review section text.

    Review agents sometimes return a wall of text without line breaks. Insert
    breaks at natural boundaries so the rendered PDF has clear visual structure.

    Step 1: join hard-wrapped lines (Claude CLI JSON output wraps mid-sentence).
    Step 2: insert breaks at semantic boundaries.
    """
    text = (text or "").replace("\\n", "\n").strip()
    if not text:
        return ""

    # --- Step 1: join broken lines ---
    # Claude CLI JSON output hard-wraps at ~90 chars, producing mid-sentence
    # breaks like "FeMoO4-1至\n5, XOR".  Join a line to the next when it does
    # NOT end with sentence-ending punctuation AND the next line does NOT start
    # with a recognised section / numbered-item marker.
    _section_start = re.compile(
        r"^(?:[①②③④⑤⑥⑦⑧⑨⑩]|[（(][一二三四五六七八九十1-9][）)]|[1-9]\.\s|"
        r"核心发现|确认问题|确认的问题|真实问题|关键发现|假阳性|已排除|"
        r"问题位置|问题类型|具体情况|需关注原因|核查依据|"
        r"Location|Issue type|What was found|Why it matters|Verification basis|"
        r"数据维度|图像维度|参考文献|引用维度|方法学|独立扫描|"
        r"样本量|小数位|首位数|统计自洽|其余|综上|最终判定|本模块)"
    )
    _sentence_end = re.compile(r"[。；;！？!?\n]$")

    raw_lines = text.split("\n")
    joined = []
    buf = ""
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            if buf:
                joined.append(buf)
                buf = ""
            continue
        if buf and (_section_start.match(stripped) or _sentence_end.search(buf)):
            joined.append(buf)
            buf = stripped
        elif buf:
            buf += stripped
        else:
            buf = stripped
    if buf:
        joined.append(buf)
    text = "\n".join(joined)

    # --- Step 2: insert breaks at semantic boundaries ---
    break_cues = (
        "数据维度", "图像维度", "引用维度", "参考文献", "方法学",
        "样本量", "其余告警", "最终判定", "综上",
        "核心发现", "值得关注", "假阳性来源", "独立扫描",
        "小数位核查", "首位数", "统计自洽", "方法学维度",
        "问题位置", "问题类型", "具体情况", "需关注原因", "核查依据",
        "Location", "Issue type", "What was found", "Why it matters", "Verification basis",
    )
    for cue in break_cues:
        text = re.sub(rf"([。；])(?={cue})", r"\1\n", text)

    # Break before circled numbers only after sentence-ending punctuation
    text = re.sub(r"([。；:：])(?=[①②③④⑤⑥⑦⑧⑨⑩])", r"\1\n", text)
    text = re.sub(r"(?<=[—）\)。；])(?=[①②③④⑤⑥⑦⑧⑨⑩])", "\n", text)
    # Break before (1) (2) or （1）（2） after sentence-ending punctuation
    text = re.sub(r"([。；])(?=\([1-9]\))", r"\1\n", text)
    text = re.sub(r"([。；])(?=（[1-9]）)", r"\1\n", text)
    # Break before Chinese numbered items such as （一）（二）
    text = re.sub(r"(?<=.)(?=（[一二三四五六七八九十]+）)", "\n", text)
    # Break before Western-style 1. 2. after sentence-ending punctuation
    text = re.sub(r"([。；])(?=[1-9]\.\s)", r"\1\n", text)
    # Break before "分析：" / "综合：" / "结论：" that start a new logical block
    text = re.sub(r"([。；])(?=分析：|综合：|结论：)", r"\1\n", text)

    return re.sub(r"\n{3,}", "\n\n", text)


def _format_reason_text(text: str) -> str:
    """Add readable paragraph breaks to the 综合评估 text (delegates to shared formatter)."""
    return _format_section_text(text)


def _is_lowrisk_reason_piece(piece: str) -> bool:
    """Return True only for a self-contained low-risk / false-positive sentence.

    If a sentence contains a contrastive turn followed by confirmed high-risk
    evidence ("但...确认/存在/完全一致"), keep it. This prevents the summary
    filter from deleting mixed sentences such as "未发现A，但独立核查确认B".
    """
    piece = (piece or "").strip()
    if not piece:
        return False
    core = re.sub(r"^[（(]?[一二三四五六七八九十1-9]+[）)]\s*", "", piece)
    core = re.sub(r"[。；;，,\s]+$", "", core).strip()
    if not core:
        return False

    high_turn = re.search(
        r"(?:但|然而|不过|同时|仍|且|并且).{0,120}"
        r"(?:确认|发现|存在|构成|显示|提示|完全一致|完全相同|复用|完整性问题|异常|矛盾|不一致)",
        core,
    )
    if high_turn:
        return False

    low_patterns = (
        r"^本次复核认为[^。；;]*主要问题集中在[^。；;]*$",
        r"^(?:其余|其他|剩余)[^。；;]*(?:假阳性|合理解释|正常模式|正常复用|已排除|不构成)[^。；;]*$",
        r"^假阳性说明[：:]?[^。；;]*$",
        r"^(?:图像|参考文献|引用|文献|方法学|图像和参考文献|图像与参考文献|参考文献与方法学)(?:维度)?[：:]?[^。；;]*(?:未发现|未检出|未见|无异常|无问题|低风险|不构成|假阳性|解析伪影|无法核查|匹配失败|无引用异常)[^。；;]*$",
        r"^数据维度[：:]?[^。；;]*(?:均为LOW|告警均为LOW|未发现独立|主要为Prism|样本量与论文声明一致|低风险|假阳性|不构成)[^。；;]*$",
        r"^(?:文献告警|参考文献)[^。；;]*(?:解析伪影|假阳性|不构成|无引用异常|无法核查|匹配失败)[^。；;]*$",
        r"^方法学(?:与统计核查|/统计核查)?[：:]?[^。；;]*(?:均与论文声明一致|末位数字和SD规律性扫描|未发现异常|正常精度差异|不同测量类型产生)[^。；;]*$",
        r"^(?:[0-9]+项|[0-9]+条|[0-9]+个)?[^。；;]*(?:正常精度差异|检测系统固有假阳性)[^。；;]*$",
    )
    return any(re.search(pattern, core) for pattern in low_patterns)


def _highrisk_reason_html(info: dict, reason: str) -> str:
    """Render high-risk 综合评估: keep the reviewer's original reason text but
    remove sentences that only discuss low-risk / false-positive material.

    Priority:
    1. Never drop confirmed integrity evidence.
    2. Never create sentence fragments.
    3. It is acceptable to leave some false-positive context if removal is not
       clearly safe.

    Therefore this is sentence-level subtraction only: we do not remove text
    inside a sentence, reorganize evidence, or truncate confirmed descriptions.
    """
    text = _format_external_reason_text(reason or "")
    if not text.strip():
        return _text_to_html("确认存在建议高风险问题，请查看复核详情。")

    # Agent text often contains hard wraps copied from terminal/PDF output.
    # Join those soft wraps first; otherwise one low-risk sentence can be split
    # into several lines and only be partly removed.
    text = re.sub(
        r"\n(?!(?:[0-9]+\.|[①②③④⑤⑥⑦⑧⑨⑩]|[（(]?[一二三四五六七八九十]+[）)]|"
        r"数据维度|图像维度|参考文献|引用维度|文献维度|方法学|统计核查|综合判定|最终判定))",
        " ",
        text,
    )
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Lines to drop: pure false-positive / low-risk / template boilerplate.
    # Keep this conservative: only remove lines that are explicitly about
    # dismissed findings or dimensions with no independent problem.
    _drop_line = re.compile(
        r"^(?:"
        r"其余.{0,20}(?:假阳性|不构成|已排除|低风险)|"
        r"其他.{0,20}(?:假阳性|不构成|已排除|低风险)|"
        r"剩余.{0,20}假阳性|"
        r"假阳性(?:来源|说明)|"
        r"经核查为假阳性|"
        r"自动检测.*假阳性|"
        r"已排除的告警|"
        r"确认的问题[：:]?\s*$|"
        r"本模块复核结论|"
        r"本次复核未确认"
        r")"
    )

    kept = []
    for line in lines:
        if _drop_line.search(line):
            continue
        # Remove low-risk clauses from mixed lines. Work sentence-by-sentence
        # so an early low-risk clause does not swallow a later confirmed issue.
        pieces = re.findall(r"[^。；;]+[。；;]?", line)
        cleaned_pieces = []
        for piece in pieces or [line]:
            cleaned = piece.strip()
            if not cleaned:
                continue
            if _is_lowrisk_reason_piece(cleaned):
                continue
            cleaned = re.sub(r"^[。；;，,\s]+", "", cleaned)
            cleaned = re.sub(r"[，,\s]+$", "", cleaned).strip()
            if _is_lowrisk_reason_piece(cleaned):
                continue
            if cleaned and len(cleaned) > 5:
                cleaned = _normalize_highrisk_reason_piece(cleaned)
                cleaned_pieces.append(_ensure_terminal_punctuation(cleaned))
        if cleaned_pieces:
            kept.append("".join(cleaned_pieces))

    if not kept:
        # All lines were filtered — fall back to original
        return _text_to_html(_format_external_reason_text(reason))

    return _text_to_html(_format_external_reason_text("\n".join(kept)))


def _trigger_dimensions(info: dict) -> str:
    dims = []
    if _dimension_verdict(info, "data") == "建议高风险":
        dims.append("数据")
    if _dimension_verdict(info, "image") == "建议高风险":
        dims.append("图像")
    if _dimension_verdict(info, "ref") == "建议高风险":
        dims.append("参考文献")
    if _dimension_verdict(info, "methodology") == "建议高风险":
        dims.append("研究设计/统计")

    if not dims:
        trigger = str(info.get("trigger") or "")
        if "data" in trigger or "数据" in trigger:
            dims.append("数据")
        if "pic" in trigger or "image" in trigger or "图像" in trigger:
            dims.append("图像")
        if "ref" in trigger or "引用" in trigger or "参考文献" in trigger:
            dims.append("参考文献")
    return "、".join(dict.fromkeys(dims)) or "未明确"


def _normalize_review_info(info: dict) -> dict:
    out = dict(info)
    result = out.get("result")
    verdict = out.get("verdict")

    if result not in ("高风险", "低风险"):
        result = "高风险" if "高风险" in str(verdict or "") else "低风险"
        out["result"] = result

    expected_verdict = "建议高风险" if result == "高风险" else "建议低风险"
    if verdict not in ("建议高风险", "建议低风险") or (
        result == "低风险" and "高风险" in str(verdict)
    ) or (
        result == "高风险" and "低风险" in str(verdict)
    ):
        out["verdict"] = expected_verdict

    # Coerce review fields to strings. Some agents emit dicts like
    # {"detail": "..."} or {"notes": "..."} instead of a plain string. The
    # downstream render path uses .strip() and string concatenation, which
    # would otherwise raise AttributeError.
    _STRING_FIELDS = (
        "image_review", "data_review", "ref_review", "methodology_review",
        "reason", "trigger", "verdict", "result",
    )
    for key in _STRING_FIELDS:
        if key in out and not isinstance(out[key], str):
            val = out[key]
            if isinstance(val, dict):
                # Prefer common text keys; fall back to JSON dump
                out[key] = (
                    val.get("detail")
                    or val.get("text")
                    or val.get("summary")
                    or val.get("notes")
                    or json.dumps(val, ensure_ascii=False)
                )
            elif val is None:
                out[key] = ""
            else:
                out[key] = str(val)

    return out


def _has_lowrisk_final_conclusion(text: str) -> bool:
    text = text or ""
    # 必须匹配"建议低风险"才能算最终低风险结论. 之前的版本对"综合.{0,100}低风险"
    # 过于宽松, 会误判 "综合评估: ... 图像维度低风险 ... 建议高风险" 这种"维度层面
    # 低风险" 提及. 只匹配 "建议低风险" 出现在综合判定附近才算低风险.
    # Require the conclusion cue to start a sentence/clause. Without this
    # boundary, a normal dimension sentence such as “故该维度无已确认风险；
    # 参考文献维度（建议低风险）” is mistaken for the paper-level verdict.
    return bool(re.search(
        r"(?:^|[。；;\n])\s*(?:综上|综合(?:评估|判断|判定|结论)?|因此|故(?!该(?:维度|项|问题)|此维度)|最终).{0,100}建议低风险(?!\s*[）)])"
        r"|(?:^|[。；;\n])\s*图像/数据/参考文献.{0,100}均未发现.{0,100}(?:完整性问题|问题|异常).{0,60}建议低风险(?!\s*[）)])"
        r"|建议低风险\s*$",
        text,
        re.S,
    ))


def _has_highrisk_final_conclusion(text: str) -> bool:
    text = text or ""
    # NOTE: removed "建议" from the alternation — per feedback memory
    # `highrisk-negation-validator.md`, "建议高风险" is a soft verb that appears inside
    # negation patterns like "不满足建议高风险的标准" / "未达到建议高风险的程度", and
    # the previous regex caught those as a final high-risk conclusion. The remaining
    # patterns (判定/判断/结论/归为/列为/定为 + 高风险) are still strong commitments
    # that the prose is concluding high risk, and never appear inside common negations.
    judgement = (
        r"(?:判定|判断|结论|归为|列为|定为)\s*"
        r"(?:疑似)?高风险"
    )
    pattern = re.compile(
        rf"(?:综上|综合(?:评估|判断|判定|结论)?|因此|故|最终).{{0,100}}{judgement}"
        rf"|{judgement}\s*$",
        re.S,
    )
    for match in pattern.finditer(text):
        piece_start = max(
            text.rfind("。", 0, match.start()),
            text.rfind("；", 0, match.start()),
            text.rfind(";", 0, match.start()),
            text.rfind("\n", 0, match.start()),
        ) + 1
        piece_end_candidates = [
            pos for pos in (
                text.find("。", match.end()),
                text.find("；", match.end()),
                text.find(";", match.end()),
                text.find("\n", match.end()),
            )
            if pos != -1
        ]
        piece_end = min(piece_end_candidates) if piece_end_candidates else len(text)
        piece = text[piece_start:piece_end]
        if re.search(
            r"(?:未发现|未见|无|不存在|不构成|不支持|未确认|排除|假阳性|"
            r"无法支持|不能支持|难以支持|并非|不是|非真实|未达到|不满足|无需|不需)"
            r".{0,80}(?:判定|判断|结论|归为|列为|定为|高风险)",
            piece,
            re.S,
        ) or re.search(r"不(?:单独)?(?:判定|判断|归为|列为|定为)\s*(?:疑似)?高风险", piece):
            continue
        return True
    return False


_EXPLAINABLE_DETAIL_FIELDS = {
    "location": (r"问题位置\s*[：:]", r"\bLocation\s*[:：]"),
    "issue_type": (r"问题类型\s*[：:]", r"\bIssue\s+type\s*[:：]"),
    "facts": (
        r"(?:具体情况|发现)\s*[：:]",
        r"\b(?:What\s+was\s+found|Finding)\s*[:：]",
    ),
    "importance": (
        r"(?:需关注原因|为什么重要|为什么需要关注)\s*[：:]",
        r"\bWhy\s+it\s+matters\s*[:：]",
    ),
    "basis": (r"核查依据\s*[：:]", r"\bVerification\s+basis\s*[:：]"),
}


def _review_identity(info: dict) -> str:
    return str(info.get("doi") or info.get("_report_slug") or "<unknown>")


def _has_legacy_deterministic_issue_type(info: dict, text: str) -> bool:
    """Recognize detailed audit findings produced before labels were standardized."""
    trigger = str(info.get("trigger") or "")
    if "确定性审计" not in trigger and "deterministic" not in trigger.lower():
        return False
    if not re.search(r"发现\s*[：:]", text):
        return False
    return bool(re.search(
        r"(?:逐格(?:完全)?相同|精确复用|数值矩阵复用|样本量.{0,30}不一致|"
        r"固定比例|共享比例|字节级完全一致)",
        text,
        re.I | re.S,
    ))


def _validate_explainable_highrisk_detail(info: dict) -> None:
    """Reject a high-risk result that cannot be understood without guesswork.

    The final PDF is read by reviewers who may not know the detector's internal
    terminology. A high-risk conclusion therefore needs a reproducible,
    reader-facing explanation before it can be rendered or written to the DB.
    """
    if info.get("result") != "高风险":
        return

    text = "\n".join(str(info.get(key) or "") for key in (
        "image_review", "data_review", "ref_review", "methodology_review", "reason",
    ))
    missing = [
        name for name, patterns in _EXPLAINABLE_DETAIL_FIELDS.items()
        if not any(re.search(pattern, text, re.I) for pattern in patterns)
    ]
    if "issue_type" in missing and _has_legacy_deterministic_issue_type(info, text):
        missing.remove("issue_type")
    if missing:
        raise ValueError(
            f"{_review_identity(info)}: high-risk review is not reader-explainable; "
            f"missing structured fields: {', '.join(missing)}"
        )

    location_re = re.compile(
        r"(?:第\s*\d+\s*页|page\s*\d+|Fig(?:ure)?\.?\s*[A-Za-z0-9.-]+|"
        r"Table\s*[A-Za-z0-9.-]+|图\s*[A-Za-z0-9.-]+|表\s*[A-Za-z0-9.-]+|"
        r"公式?\s*[（(]?\d+|式\s*[（(]?\d+|sheet\s*['\"“”]?[^，,。;；\n]+|"
        r"参考文献\s*(?:第)?\s*\d+\s*(?:条|项|号)?|Reference\s*#?\s*\d+|"
        r"(?:\.xlsx?|\.xlsm|\.csv).{0,160}(?:R\d+:R\d+\s*C\d+:C\d+|"
        r"[A-Z]{1,3}\d+(?::[A-Z]{1,3}\d+)?))",
        re.I,
    )
    if not location_re.search(text):
        raise ValueError(
            f"{_review_identity(info)}: high-risk review has labels but no "
            "specific page/figure/table/formula/sheet/reference location"
        )


def _validate_review_info_consistency(info: dict) -> None:
    """Reject upstream JSON whose machine verdict and final prose conclusion disagree.

    This catches adjudication mergers that overwrite result/verdict but leave the
    previous reviewer's reason tail in place. Failing here prevents contradictory
    PDFs and DB writes.
    """
    doi = info.get("doi", "<unknown>")
    result = info.get("result", "")
    verdict = str(info.get("verdict", "") or "")
    reason = str(info.get("reason", "") or "")
    if result == "高风险":
        if "低风险" in verdict or _has_lowrisk_final_conclusion(reason):
            raise ValueError(
                f"{doi}: inconsistent review JSON, result=高风险 but reason/verdict "
                "contains a final low-risk conclusion"
            )
        _validate_explainable_highrisk_detail(info)
    elif result == "低风险":
        if "高风险" in verdict or _has_highrisk_final_conclusion(reason):
            raise ValueError(
                f"{doi}: inconsistent review JSON, result=低风险 but reason/verdict "
                "contains a final high-risk conclusion"
            )


def _display_review_verdict(info: dict, include_dimensions: bool = True) -> str:
    if _is_incomplete_material_review(info):
        return "建议低风险"
    verdict_text = f"{info.get('verdict', '')}{info.get('result', '')}"
    if "高风险" in verdict_text:
        return "建议高风险"
    return "建议低风险"


def _level_span(level: str, color: str) -> str:
    return f'<span style="color:{color}; font-weight:bold;">{_html_escape(level)}</span>'


def _module_verdict_color(verdict: str) -> str:
    if "无法复核" in verdict:
        return "#b36b00"
    return "#c00" if "高风险" in verdict else "#080"


def _piece_negates_highrisk_match(piece: str, match_start: int, match_end: int) -> bool:
    """Return True when a high-risk cue is locally dismissed in the same clause."""
    piece = piece or ""
    before = piece[max(0, match_start - 45):match_start]
    match_text = piece[match_start:match_end]
    after = piece[match_end:min(len(piece), match_end + 60)]
    window = before + match_text + after
    return bool(re.search(
        r"(?:不构成|未发现|未见|无|不存在|排除|假阳性|非真实|并非|不是|"
        r"不支持|未确认|低风险|无独立.{0,10}高风险).{0,70}"
        r"(?:高风险|完整性问题|完整性异常|异常|不一致|不匹配|错误|不存在|指向不相关|矛盾|复用|重复|完全一致|完全相同|逐值)"
        r"|(?:高风险|完整性问题|完整性异常|异常|不一致|不匹配|错误|不存在|指向不相关|矛盾|复用|重复|完全一致|完全相同|逐值)"
        r".{0,55}(?:假阳性|不构成|排除|可解释|低风险|正常|规范问题|轻微|无独立.{0,10}高风险)",
        window,
    ))


def _has_unnegated_strong_highrisk_evidence(text: str, dimension: str = "") -> bool:
    """Detect explicit high-risk evidence before broad low-risk prose can mask it.

    Reviewer sections often start by dismissing false-positive alerts and later
    introduce "核心高风险发现/证据".  The older negation scan looked across too much
    text, so early "无/假阳性" clauses could incorrectly cancel the later finding.
    Keep this detector intentionally local and evidence-oriented.
    """
    text = _format_section_text(text or "")
    if not text:
        return False

    common_patterns = [
        r"核心高风险(?:发现|证据|问题)",
        r"核心(?:发现|问题|异常)[：:].{0,80}(?:完整性问题|完整性异常|不一致|不符|矛盾|复用|重复|完全相同|完全一致|逐值相同|逐位相同|样本量不符|小数尾串)",
        r"(?:确认|确证|锁定|构成|属于|属|支持|提示).{0,60}(?:高风险|完整性问题|完整性异常|实质性问题)",
        r"(?:建议|判定|判断|结论|列为|归为).{0,25}高风险",
        r"(?:具体|可定位|独立成立|实质性).{0,45}(?:完整性问题|高风险证据|问题证据)",
    ]
    dimension_patterns = {
        "data": [
            r"(?:数据完整性问题|数据完整性异常|真实数值不一致|客观不一致|内在矛盾)",
            r"(?:正文|表格|Table|源数据|上传数据|数值|样本|字段).{0,90}(?:不一致|不匹配|矛盾|错配)",
            r"(?:图注|矩阵|混淆矩阵|精度|accuracy|Accuracy|Overall Accuracy).{0,90}(?:不一致|不匹配|矛盾|不自洽|相差)",
            r"(?:对角和|总样本|测试集|列分布|行分布).{0,90}(?:不一致|不匹配|矛盾|缺失|违反)",
            r"(?:Fig\.?\s*\d+|Figure\s*\d+|图\s*\d+).{0,60}(?:矩阵|数值|精度|样本|图注).{0,90}(?:不一致|不自洽|矛盾|相差)",
            r"(?:共|发现)\s*\d+\s*处.{0,20}(?:数值不一致|不一致|矛盾)",
            r"(?:两套|多套).{0,20}(?:不同|互不一致).{0,20}(?:数据|数值|结果)",
            r"(?:独立|不同).{0,45}(?:样本|实验|测量|条件).{0,90}(?:完全一致|完全相同|逐值相同|精确一致)",
            r"(?:复制粘贴错误|列级或表级数据复制|错列复用|整块复用)",
            r"(?:小数尾串|尾串|值回收|平台期|测量值).{0,80}(?:重复|全同|完全相同|完全一致|系统性|异常)",
            r"(?:样本量|n\s*=|N\s*=).{0,80}(?:不符|不一致|矛盾|虚高|相差)",
            r"(?:末两位|末三位|尾数|尾串).{0,80}(?:100%|全为|占\s*\d|系统性)",
            r"(?:\\d+\\s*/\\s*\\d+|100%).{0,60}(?:完全相同|完全一致|逐值相同|逐位相同|重合|相同)",
            r"(?:同一组|同一批|相同).{0,50}(?:数值|数据|测量).{0,50}(?:套用|复用|复制|赋值)",
        ],
        "image": [
            r"(?:图像完整性问题|真实图像重复|真实.{0,20}图像复用)",
            r"(?:同一张图像|同一张.{0,20}标注为.{0,30}不同)",
            r"(?:像素|逐像素).{0,40}(?:完全|高度)?(?:相同|一致)",
            r"(?:跨图|跨样本|跨实验|跨条件).{0,45}(?:图像复用|重复|同一张)",
            r"(?:图像|图谱|面板|子图).{0,60}(?:错用|复用|逐像素一致|同一张)",
        ],
        "ref": [
            r"(?:参考文献|引用|著录|DOI|题名|标题|作者|期刊).{0,90}(?:实质)?(?:不一致|不匹配|错误|指向不相关|不存在)",
            r"(?:引用异常|文献捏造|参考文献完整性问题)",
            r"(?:参考文献|引用|文献).{0,80}(?:无法定位|查无|不存在|疑似捏造)",
        ],
        "methodology": [
            r"(?:方法学|统计|样本量|时间线|伦理).{0,90}(?:矛盾|不一致|不匹配|虚高|无法解释|完整性问题)",
            r"(?:样本量|n\s*=|N\s*=).{0,60}(?:矛盾|虚高|不一致)",
            r"(?:图注|正文|Methods|方法).{0,80}(?:样本量|n\s*=|N\s*=).{0,80}(?:不符|不一致|矛盾|相差)",
        ],
    }
    patterns = common_patterns + dimension_patterns.get(dimension, [])
    compiled = [re.compile(p, re.I) for p in patterns]

    for line in text.splitlines():
        for piece in _split_review_sentences(line):
            if not piece:
                continue
            if dimension == "methodology" and re.search(
                r"(?:轻微|规范问题|展示不一致|表述与展示不一致|无独立高风险)",
                piece,
            ) and not re.search(r"(?:核心高风险|完整性问题|完整性异常|建议高风险|判定.{0,10}高风险)", piece):
                continue
            for pat in compiled:
                for match in pat.finditer(piece):
                    if not _piece_negates_highrisk_match(piece, match.start(), match.end()):
                        return True
    return False


def _ensure_overview_consistency(info: dict, module_rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    # Do not add a synthetic high-risk row to hide a contradiction. The caller
    # reconciles the paper-level verdict before rendering and DB update.
    return module_rows


def _reconcile_review_verdict(info: dict) -> dict:
    """Keep the final review verdict and module verdicts logically consistent."""
    out = dict(info)
    overall = f"{out.get('verdict', '')}{out.get('result', '')}"
    if "低风险" in overall and "高风险" not in overall:
        out["result"] = "低风险"
        out["verdict"] = "建议低风险"
        return out
    if "高风险" not in overall or _is_incomplete_material_review(out):
        return out

    module_verdicts = [
        _dimension_verdict(out, dimension)
        for dimension in ("image", "data", "ref", "methodology")
    ]
    if not any("高风险" in verdict for verdict in module_verdicts):
        # A high final result without an independently high module is not a
        # supported convergence result. Downgrade instead of inventing a row.
        out["result"] = "低风险"
        out["verdict"] = "建议低风险"
        out["_risk_reconciliation_downgraded"] = True
    return out


def _review_overview_html(findings: dict | None, info: dict) -> str:
    info = _reconcile_review_verdict(info)
    display_verdict = _display_review_verdict(info, include_dimensions=False)
    result_color = "#b36b00" if _is_incomplete_material_review(info) else (
        "#c00" if info.get("result") == "高风险" else "#080"
    )
    module_rows = [
        ("图像问题复核", _dimension_verdict(info, "image")),
        ("数据异常检测复核", _dimension_verdict(info, "data")),
        ("参考文献检测复核", _dimension_verdict(info, "ref")),
    ]
    module_rows.append(("研究设计与统计核查", _dimension_verdict(info, "methodology")))
    module_rows = _ensure_overview_consistency(info, module_rows)

    module_html = "\n".join(
        '<p style="font-size:9.3pt; color:#333; margin-bottom:3pt;">'
        f'<b>{_html_escape(label)}：</b>{_level_span(verdict, _module_verdict_color(verdict))}'
        '</p>'
        for label, verdict in module_rows
    )

    return f"""
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">二、复核风险结论</h2>

<p style="font-size:16pt; font-weight:bold; color:{result_color}; text-align:center; margin:12pt 0 6pt 0;">
复核结论：{display_verdict}
</p>

<p style="font-size:10pt; font-weight:bold; color:#1a1a1a; margin-top:10pt; margin-bottom:5pt;">复核后维度结论：</p>
{module_html}
<hr style="border:none; border-top:1px solid #eee; margin:10pt 0;">
"""


def _summary_lead(info: dict) -> str:
    verdict_text = f"{info.get('verdict', '')}{info.get('result', '')}"
    if "高风险" not in verdict_text:
        return "本次复核未发现具体的论文完整性问题，现有证据支持低风险结论。"

    dims = _trigger_dimensions(info)
    low_notes = []
    if _dimension_verdict(info, "image") == "建议低风险":
        low_notes.append("图像")
    if _dimension_verdict(info, "ref") == "建议低风险":
        low_notes.append("参考文献")

    tail = ""
    if low_notes:
        tail = f"{'和'.join(low_notes)}维度未发现支持该结论的独立问题。"
    return f"本次复核认为，该论文的主要问题集中在{dims}维度。{tail}"


def _section_verdict(text: str, fallback_verdict: str, dimension: str = "") -> str:
    """Determine the per-section verdict from review text.

    ``dimension`` controls fallback behaviour when no patterns match:
    only "data" falls back to the paper-level verdict; image / ref /
    methodology default to 建议低风险 so the paper-level verdict does
    not contaminate sections that have no independent findings.
    """
    text = (text or "").strip()
    if not text:
        # A missing dimension has no independent evidence. The paper may still
        # be high-risk because another dimension carries the finding.
        return "建议低风险"
    text_for_verdict = text
    if dimension == "methodology":
        # Methodology sections often mention a confirmed image/data problem as
        # context. Do not let those cross-dimension statements turn the
        # methodology module itself high-risk.
        text_for_verdict = "\n".join(
            sentence for sentence in _split_review_sentences(text)
            if not re.search(r"(?:图像|图注|源数据|数据).{0,80}(?:完整性问题|不匹配|复用|完全一致|重复)", sentence)
        ) or text
    if dimension == "ref":
        ref_low = re.search(
            r"(?:解析|提取|换行|断字|截断|CrossRef|数据库|检索|接口).{0,80}"
            r"(?:伪影|问题|失败|限制|滞后|导致|所致|低分|低置信|误匹配|未命中|未收录|索引)"
            r"|(?:DataCite|Figshare|figshare|10\.6084).{0,80}(?:CrossRef|索引|收录|检索|未命中)"
            r"|(?:真实存在|正确引用|著录完整|完整可读|对应文献真实存在).{0,80}"
            r"(?:不构成|无|未发现).{0,30}(?:引用异常|参考文献异常|完整性问题|问题)"
            r"|(?:不构成|无|未发现).{0,30}(?:引用异常|参考文献异常|完整性问题|问题|实质不一致)"
            r"|(?:未列出|未被列出|未发现|未见).{0,60}(?:DOI|题名|标题|作者|期刊).{0,60}(?:实质)?(?:不一致|不匹配|错误)",
            text_for_verdict,
        )
        ref_high_re = re.compile(
            r"(?:完整可读|原文|引用|参考文献).{0,80}"
            r"(?:DOI|题名|标题|作者|期刊).{0,40}(?:实质)?(?:不一致|不匹配|错误|指向不相关)"
        )
        ref_high = False
        for match in ref_high_re.finditer(text_for_verdict):
            window = text_for_verdict[max(0, match.start() - 30):match.end() + 20]
            # negation 只用"未发现/未见/不构成/不存在"这种"未确认"信号,
            # 不要把"非PDF解析碎片" (描述"非+类型")误判为否定
            if re.search(
                r"(?:未发现|未见|未列出|未被列出|不构成|不存在).{0,90}"
                r"(?:不一致|不匹配|错误|指向不相关|异常|问题|实质)",
                window,
            ):
                continue
            ref_high = True
            break
        if ref_low and not ref_high:
            return "建议低风险"

    explicit = re.search(r"本模块复核结论：\s*(建议高风险|确认高风险|建议低风险|高风险|低风险)", text)
    if explicit:
        explicit_verdict = "建议高风险" if "高风险" in explicit.group(1) else "建议低风险"
        if explicit_verdict == "建议低风险" and "高风险" in (fallback_verdict or "") and dimension == "data":
            pass
        else:
            return explicit_verdict

    final_low_patterns = (
        r"(?:结论|最终判定|总体结论)\s*[:：].{0,80}(?:建议低风险|低风险)",
        r"(?:结论|最终判定|总体结论)\s*[:：].{0,80}(?:未发现|未见|无).{0,30}(?:完整性问题|异常|问题)",
        r"(?:图像|引用|文献|参考文献).{0,10}维度.{0,20}(?:未发现|未见|无).{0,30}(?:完整性问题|异常|问题)",
        r"(?:图像|引用|文献|参考文献).{0,10}维度.{0,20}不(?:单独)?构成.{0,20}(?:高风险|建议高风险|完整性问题)",
        r"(?:图像|引用|文献|参考文献).{0,30}(?:未构成|不构成).{0,30}(?:高风险|建议高风险|结论)",
        r"(?:真实存在文献|文献真实存在).{0,30}无.{0,10}完整性问题",
    )
    low_override_patterns = (
        r"(?:所有|全部|均).{0,30}(?:HIGH|高风险|异常|告警|问题).{0,80}(?:可解释|属正常|为正常|假阳性|不构成)",
        r"(?:HIGH|高风险|异常|告警|问题).{0,80}(?:均|全部|均可|全部可).{0,30}(?:解释|可解释|属正常|为正常|假阳性)",
        r"(?:结合|经).{0,40}(?:核查|核验|分析).{0,80}(?:可解释|属正常|为正常|假阳性|不构成)",
        r"(?:可解释为|解释为).{0,30}(?:正常|模型|建模|假阳性)",
    )

    high_patterns = (
        r"严重数据复用",
        r"数据复用问题",
        r"错列复用",
        r"列级或表级数据复制",
        r"整块(?:相同|重复|错配|复用)",
        r"(?:整行|整列|整段|三元组).{0,20}(?:完全|精确|逐一).{0,20}(?:重复|相同|一致)",
        r"逐格.{0,20}(?:100%|完全).{0,20}(?:一致|相同)",
        r"逐(?:一|位|值)(?:完全|精确)?(?:相同|一致|重复)",
        r"表型.{0,20}(?:错配|归属错配)",
        r"工具变量.{0,30}(?:错配|重复|整块相同)",
        r"同一输入数据.{0,30}(?:运行|使用)",
        r"图文.{0,20}(?:硬证据|印证)",
        r"图[-－—]?数据.{0,30}(?:不匹配|不一致|矛盾)",
        r"(?:图注|矩阵|混淆矩阵|精度|accuracy|Accuracy|Overall Accuracy).{0,90}(?:不一致|不匹配|矛盾|不自洽|相差)",
        r"(?:对角和|总样本|测试集|列分布|行分布).{0,90}(?:不一致|不匹配|矛盾|缺失|违反)",
        r"(?:Fig\.?\s*\d+|Figure\s*\d+|图\s*\d+).{0,60}(?:矩阵|数值|精度|样本|图注).{0,90}(?:不一致|不自洽|矛盾|相差)",
        r"(?:发表图|论文图|Panel|面板).{0,40}(?:源数据|上传数据).{0,40}(?:不匹配|不一致|矛盾)",
        r"(?:源数据|上传.{0,10}源数据).{0,40}(?:不匹配|不一致|错误|复制粘贴错误)",
        r"(?:源数据|上传.{0,10}源数据).{0,80}(?:完全一致|完全相同|逐一相同)",
        r"(?:D0|D6|0 min|15 min|30 min|60 min|120 min).{0,80}(?:完全相同|完全一致|逐值匹配|100%相同)",
        r"(?:正确数值|正确.{0,10}值).{0,30}(?:不可能|绝不可能)",
        r"不可能等于",
        r"复制粘贴错误",
        r"实质性矛盾",
        r"可验证的完整性问题",
        r"(?<!未)(?<!无)(?<!不)(?:确认|确证|锁定|存在|发现).{0,30}(?:异常|完整性问题|完整性异常|实质性问题)",
        r"(?:核心|关键|确认).{0,10}(?:异常|问题)",
        r"(?:面板|子图|柱状图).{0,30}(?:重复|错用|逐像素一致|完全一致|同一张)",
        r"同一张柱状图",
        r"图像完整性问题",
        r"图注.{0,40}(?:矛盾|不一致|不匹配)",
        r"(?:phyla|genera|门|属).{0,80}(?:矛盾|不一致)",
        r"图形内容.{0,20}(?:完全一致|逐像素一致)",
        r"实质性.{0,20}(?:图文|数值).{0,20}(?:不一致|不符)",
        r"样本量.{0,30}(?:矛盾|不符|虚高)",
        r"真实数据完整性问题",
        r"(?<!未)(?<!无)(?:发现|检出).{0,40}完整性问题",
        r"(?<!真实)(?<!文献)(?<!不)(?:存在|构成).{0,40}完整性问题",
        r"(?<!未)(?:确认|锁定).{0,40}完整性问题",
        r"完整性问题.{0,80}(?:无法|不能|不可|难以).{0,30}(?:解释|排除)",
        r"(?:无法|不能|不可|难以).{0,30}(?:解释|排除)",
        r"平移复用",
        r"跨(?:样品|实验|模型|工作表|表|组|图).{0,30}(?:复用|重复)",
        r"不同(?:电压|年龄|时间点|条件|分类层级|物理量).{0,80}(?:完全相同|完全一致|不应相同|不应产生相同)",
        r"(?:Raman shift|Intensity|血糖|GTT|光谱|计数).{0,80}(?:完全相同|完全一致|逐值)",
        r"独立测量.{0,40}(?:共享|相同|全同)",
        r"复制后.{0,20}(?:加|减|平移|偏移).{0,20}常数",
        r"小数(?:尾数|部分).{0,30}(?:全同|完全一致|雷同)",
        r"概率.{0,20}(?:极低|近乎为零)",
    )
    image_high_patterns = ()
    if dimension == "image":
        image_high_patterns = (
            r"真实图像重复",
            r"真实.{0,20}图像复用",
            r"同一张图像",
            r"同一张.{0,20}(?:被标注为|标注为).{0,30}不同",
            r"(?:两图|二者|两张图).{0,80}(?:像素|逐像素).{0,30}(?:相同|一致)",
            r"(?:像素|逐像素).{0,20}(?:完全|高度)?(?:相同|一致)",
            r"(?:图像|图谱).{0,20}(?:本质上是)?同一张",
            r"(?:图像|图谱).{0,40}(?:逐像素|像素).{0,40}(?:相同|一致)",
            r"(?:不同|独立).{0,30}(?:催化剂|样本|样品|实验|条件).{0,80}(?:同一张|图像复用|图谱复用|逐像素相同)",
        )

    # Negation-aware: if a high pattern match falls inside a negated sentence
    # ("未发现真实跨图图像重复"), suppress it.
    _NEGATION_WINDOW = re.compile(
        r"(?:未发现|未见|无|不存在|未检出|未确认|不构成|不支持|排除|假阳性|非真实|不是).{0,30}"
    )

    def _match_is_negated(match, text):
        start = match.start()
        end = match.end()
        # 1) Look 25 chars BEFORE the match for a negation cue.
        window_before = text[max(0, start - 25):start]
        if _NEGATION_WINDOW.search(window_before):
            return True  # match is inside a negation context
        # 2) Look at the match itself + 1 char before for direct negation
        #    (e.g. "非真实图像重复" where "非" is the 1 char preceding the
        #    "真实图像重复" match, and the negation window 25 chars is too
        #    narrow to capture the cue).
        matched_text = match.group(0)
        boundary_text = text[max(0, start - 1):end]
        if re.search(
            r"(?:非|不|未|无)[" + re.escape(matched_text[:2]) + r"]",
            boundary_text,
        ):
            return True
        # 2.5) Post-match negation: opus agents often phrase findings as
        #    "样本量n=1078...无内部n矛盾" / "确认问题:无" / "未发现真实重复"
        #    where the negation word comes AFTER the matched word. The 25-char
        #    before window misses these.
        _POST_NEG_WINDOW = re.compile(
            r"(?:无|未发现|未见|未检出|不存在|排除|均不|均非|均属|并未|并未发现|"
            r":\s*无[。；;,，\s]|\s*[:：]\s*0\s*项|\s*0\s*条|\s*告警\s*0)"
        )
        window_after = text[end:min(len(text), end + 30)]
        if _POST_NEG_WINDOW.search(window_after):
            return True
        # 3) Sentence-level check: extract the sentence (or section) containing
        #    the match and look for any negation cue in the surrounding 200
        #    chars before/after. Catches opus patterns like:
        #      "**跨图数据复用**：Fig.4-10分别展示...数据矩阵完全不同"
        #      "提取的多张子图均为同一张大图的留白/背景区域...PHash=0...均属PDF提取伪影"
        #      "**不构成建议高风险**" / "**复核结论：建议低风险**"
        _SENTENCE_NEG = re.compile(
            r"未发现|未见|不存在|未检出|未确认|不构成|不支持|排除|假阳性|非真实|"
            r"并非|不是|均不|均属|均非|无[^一-鿿]{0,2}(?:完整性问题|异常|问题|真实)|"
            r"分别展示|独立(?:实验|测量|重复)|数据矩阵完全不同|"
            r"PDF提取伪影|解析.{0,10}(?:伪影|问题|失败|限制)|"
            r"无xlsx源数据|不涉及.{0,8}实验数据|无源数据|无定量数据|"
            r"无(?:xlsx|csv|docx|结构化)?源数据|目录为空|不适配本类论文|"
            r"无定量源数据|无结构化源数据|无独立源数据|"
            r"数值型.{0,10}无法|数值型复核无法|无法执行|不适用|不提供.*?源数据|"
            r"建议低风险|维持低风险|判定为低风险|"
            r"互非复制|数值范围不同|趋势均符合|无独立.{0,15}(?:高风险|证据|异常|问题)|"
            r"未发现.{0,30}(?:独立|真实|高风险|异常|证据|支持)|"
            r"未发现.{0,30}(?:图像|数据|引用|文献|参考文献|拼接|重复|复用|完整性|矛盾|异常)|"
            r"告警\s*0\s*条|0\s*项|返回\s*0\s*项|"
            r"整本论文共|整篇论文|全文.{0,15}无|核查.{0,10}均|经核验.{0,10}均|"
            r"未发现任何|无任何|均不构成|均不(?:单独)?构成|不(?:单独)?构成|"
            r"未提供|没有提供|不提供"
        )
        context = text[max(0, start - 1200):min(len(text), end + 1200)]
        if _SENTENCE_NEG.search(context):
            return True
        # 3.5) Wider context (full text) check for section-header false
        #    positives like "**跨图数据复用**：" where the body explains
        #    "无 xlsx/csv 源数据" / "无源数据 sheet 可供 X" / "无独立源数据".
        if _SENTENCE_NEG.search(text):
            return True
        # 4) Existing check for "完整性问题" inside the match.
        if "完整性问题" in matched_text and re.search(
            r"(?:无|未发现|未见|不构成|不存在).{0,12}完整性问题",
            matched_text,
        ):
            return True
        # 5) In-match negation: opus often writes "无...矛盾" / "无...不一致" /
        #    "无内部n矛盾" / "属 n=3 常见四舍五入差异...无矛盾" where the "无"
        #    comes INSIDE the matched text (between the high-pattern anchor and
        #    its keyword). E.g. pattern `属.{0,80}(?:矛盾|不一致)` matches
        #    "属 n=3 常见四舍五入差异；图与数据定量趋势一致；方法学、...无矛盾" —
        #    the "无" is inside the matched span and signals negation.
        # Use \w + 一-鿿 to allow CJK + ASCII letters/digits (e.g. "无内部n矛盾"
        #    where the literal "n" between "内部" and "矛盾" must be matched).
        if re.search(
            r"无[\w一-鿿]{0,20}(?:矛盾|不一致|不符|虚高|问题|异常|复用|重复|拼接)|"
            r"无[\w一-鿿]{0,5}内部[\w一-鿿]{0,5}(?:n|N)?[\w一-鿿]{0,5}(?:矛盾|不一致)|"
            r"(?:未发现|未见|不存在|未检出).{0,30}(?:矛盾|不一致|不符|虚高|问题|异常)|"
            r"无(?:内部|定量|结构化|独立)?(?:源数据|sheet|xlsx|csv|docx|JPG|PDF|数据|文件)|"
            r"目录为空|不适配本类论文|无法执行|不适用|数值型复核无法|数值型.{0,10}无法|"
            r"前后矛盾|前后不一致|无任何.{0,10}复用|无任何.{0,10}重复|无任何.{0,10}拼接|"
            r"无样本量|内部无\s*[nN]|内部无.{0,5}矛盾|"
            r"未发现.{0,30}(?:高风险|问题|异常|证据|支持|矛盾|不一致|复用|重复|拼接|独立.{0,10}证据)",
            matched_text,
        ):
            return True
        return False

    # A high match in the IMAGE dimension is "explained away" when its
    # surrounding sentence describes the match as a SIFT false positive (e.g.
    # "页9 dbRDA 与同一张 docx 地图" — the agent is listing SIFT false positives
    # that share a docx reference map, not asserting duplicate figures). Only
    # apply this to the image dimension; data/ref/methodology have no such
    # "false-positive listing" idiom and applying it would suppress real
    # evidence (e.g. "21 组 RPKM 完全相同... 属于典型的数据完整性问题... 后续
    # (三)其他数据异常：未发现" — a cross-section "未发现" is not a negation of
    # the prior high finding).
    _FP_FORWARD_WINDOW = re.compile(
        r"假阳性|非真实|非重复|不是.{0,4}重复|经视觉核查|经核查|模板.{0,4}特征|偶然.{0,4}重合|过敏感.{0,4}匹配|非.{0,4}真实.{0,4}复用|不构成.{0,4}真实.{0,4}复用|色块|轴框|色.{0,4}块|轴.{0,4}框|补充.{0,4}材料|docx|节点名|站位图|采样站|配色|模板图|版式"
    )

    def _match_is_explained_fp(match, text):
        if dimension != "image":
            return False
        start = match.start()
        end = match.end()
        # Look 40 chars before AND 50 chars after for a false-positive descriptor.
        # The match context should be a single clause; cross-section
        # explanations (e.g. "(三)其他数据异常" appearing 100+ chars away) are
        # not relevant.
        before = text[max(0, start - 40):start]
        after = text[end:min(len(text), end + 50)]
        window = before + after
        return bool(_FP_FORWARD_WINDOW.search(window))

    high_hits = [
        p for p in high_patterns
        + image_high_patterns
        if any(
            (not _match_is_negated(m, text_for_verdict))
            and (not _match_is_explained_fp(m, text_for_verdict))
            for m in re.finditer(p, text_for_verdict)
        )
    ]

    low_patterns = (
        r"本模块复核结论：(?:建议低风险|低风险)",
        r"(?:均为|全部为|判为|确认为|系|属|所致的).{0,20}假阳性",
        r"(?:均|全部|全部经核验|经核验均|经核对均).{0,40}假阳性",
        r"假阳性.{0,20}(?:非真实|不构成|无)",
        r"非真实.{0,20}问题",
        r"(?:非|不是).{0,20}(?:真实|文献捏造|完整性问题|异常)",
        r"(?:未发现|未见|无).{0,20}(?:异常|问题|完整性问题)",
        r"不构成.{0,20}(?:完整性问题|问题|异常)",
        r"不(?:单独)?构成.{0,20}(?:高风险|建议高风险)",
        r"(?:未发现|未见|无).{0,30}(?:图像|引用|文献|参考文献).{0,30}(?:异常|问题|完整性问题)",
        r"(?:图像|引用|文献|参考文献).{0,20}(?:维度)?(?:未发现|无).{0,20}(?:异常|问题|完整性问题)",
        r"(?:均为|全部为|系|属).{0,20}(?:解析|格式|识别|收录|检索).{0,20}(?:问题|错误|失败|限制)",
        r"(?:DOI|doi).{0,30}(?:收录滞后|暂时不可用|数据库限制|检索失败|无DOI)",
        r"(?:CrossRef|crossref).{0,30}(?:解析失败|无DOI|检索失败|限制)",
        r"(?:著录|引用).{0,20}(?:完整|信息完整).{0,20}(?:可读|无乱码)",
        r"未发现真实.{0,20}(?:重复|拼接|复用)",
        r"(?:经视觉核查|经核查).{0,40}未发现.{0,20}(?:重复|拼接|复用|异常)",
    )
    # 每个 dimension 独立判断: section 实际 evidence (high_patterns / low_patterns)
    # 决定结论, 主结论不应强制覆盖.
    # 实际场景: 高风险论文中, 通常 1-2 个 dimension 有强证据, 其他 dimension
    # 详情是"未发现/假阳性" — 后者应如实显示"建议低风险", 不被主结论拉高.
    _fallback_low_safe = (
        "低风险" in (fallback_verdict or "")
        and "高风险" not in (fallback_verdict or "")
    )
    low_hits = (
        any(re.search(pattern, text_for_verdict) for pattern in final_low_patterns)
        or (_fallback_low_safe and any(re.search(pattern, text_for_verdict) for pattern in low_override_patterns))
        or any(re.search(p, text_for_verdict) for p in low_patterns)
    )

    if high_hits and not low_hits:
        return "建议高风险"
    if low_hits and not high_hits:
        return "建议低风险"
    if high_hits and low_hits:
        # Both signals present — the text mentions both confirmed issues and
        # false positives. Prefer high if there are non-negated high matches.
        return "建议高风险"
    # Neither signal — fall through to cue words

    high_cues = (
        "建议高风险", "判定为高风险", "确认为高风险", "无法排除",
        "证据不足", "存在完整性问题", "发现完整性问题", "实质性问题",
        "真实数值不一致", "客观不一致", "内在矛盾", "数学上不可能",
        "平均值不可能", "数据归属矛盾", "标签层面的内在矛盾",
        "存在不一致", "异常无法解释", "图-数据不匹配", "源数据不匹配",
        "上传了错误的源数据", "复制粘贴错误", "实质性矛盾",
    )
    low_cues = (
        "建议低风险", "判定为低风险", "确认为假阳性", "均为假阳性",
        "全部为假阳性", "属假阳性", "系假阳性", "未发现", "未见真实问题",
        "无异常", "不构成", "可解释", "属正常", "为正常", "正常范围",
        "正常图像", "低风险", "均为解析问题", "解析失败", "收录滞后",
        "数据库限制", "非引用异常", "无文本乱码",
    )
    if any(cue in text_for_verdict for cue in high_cues):
        return "建议高风险"
    # cue 阶段同样按 section 自身 evidence 独立判断.
    if any(cue in text_for_verdict for cue in low_cues):
        return "建议低风险"
    # No clear signal either way — default to low risk.
    # Section 独立判断: 主结论不再默认覆盖 section.
    return "建议低风险"


_DIMENSION_REVIEW_KEY = {
    "image": "image_review",
    "data": "data_review",
    "ref": "ref_review",
    "methodology": "methodology_review",
}

_DIMENSION_REASON_LABELS = {
    "image": ("图像维度", "图像重复", "图像拼接"),
    "data": ("数据维度", "数据异常", "数据核查", "源数据", "数据方面", "数据层面"),
    "ref": ("参考文献", "引用维度", "文献维度"),
    "methodology": ("研究设计", "研究设计/统计", "方法学", "统计核查", "样本量"),
}


def _reason_chunks_for_dimension(reason: str, dimension: str) -> list[str]:
    """Return 综合评估 chunks that explicitly belong to one dimension.

    Older review results sometimes put the final high-risk evidence only in
    ``reason`` while leaving the corresponding module text prefixed as low-risk.
    Use only labelled chunks so a data finding does not contaminate image/ref
    module verdicts.
    """
    text = _format_section_text(reason or "")
    if not text:
        return []
    chunks = re.split(
        r"(?=(?:图像维度|图像重复|图像拼接|数据维度|数据异常|数据核查|源数据|数据方面|数据层面|"
        r"参考文献|引用维度|文献维度|方法学|统计核查|样本量|最终判定|综合评估|"
        r"核心高风险(?:发现|证据|问题)|高风险(?:发现|证据|问题))[^：:\n]{0,30}[：:])",
        text,
    )
    labels = _DIMENSION_REASON_LABELS.get(dimension, ())
    out = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if any(chunk.startswith(label) for label in labels):
            out.append(chunk)

    # Fallback: numbered ①/②/③/④ markers in 综合评估 can carry per-dimension
    # evidence without an explicit prefix. Route by heuristic content match so
    # the dimension verdict reflects the actual evidence.
    if not out:
        # Re-split on ① ② ③ ④ markers.  If no markers exist, route sentence by
        # sentence instead of handing the whole 综合评估 paragraph to one dimension;
        # otherwise a low-risk reference sentence can accidentally inherit a later
        # data high-risk finding from the same paragraph.
        marker_chunks = re.split(r"(?=[①②③④])", text)
        if len(marker_chunks) > 1:
            expanded_chunks = []
            for chunk in marker_chunks:
                for line in chunk.splitlines():
                    expanded_chunks.extend(_split_review_sentences(line))
            marker_chunks = expanded_chunks
        else:
            marker_chunks = []
            for line in text.splitlines():
                marker_chunks.extend(_split_review_sentences(line))
        dim_route = {
            "data": (r"(?:数据维度|数据异常|数据核查|数据方面|数据层面|源数据)|独立.{0,30}样品.{0,80}(?:完全相同|完全一致)|(?:RPKM|数值|数据).{0,80}(?:完全相同|完全一致)|数据完整性问题|精确.{0,4}复用|典型.{0,4}数据完整性|Table.{0,4}S\d|21.{0,4}组|M002|M000|M001|M003|M005|M007|M008|KEGG|(?:图注|矩阵|混淆矩阵|精度|accuracy|Accuracy|Overall Accuracy|对角和|总样本|测试集|列分布|行分布).{0,90}(?:不一致|不自洽|矛盾|相差|缺失|违反)", "data"),
            "image": (r"图像.{0,20}(?:重复|拼接|复用)|图.{0,4}与图.{0,4}|(?:IHC|柱状图|子图|面板|像素|逐像素|全文匹配|局部.{0,4}匹配|相似度)", "image"),
            "ref": (r"参考文献|CrossRef|DeLong|ref_number|引用.{0,8}异常|未收录|著录|引文", "ref"),
            "methodology": (r"方法学|统计.{0,4}自洽|样本量|测序.{0,4}方法|建库|时间线|试剂与方法|引物|FastDNA|PRJNA", "methodology"),
        }
        if dimension in dim_route:
            pattern, _ = dim_route[dimension]
            for chunk in marker_chunks:
                chunk = chunk.strip()
                if not chunk:
                    continue
                if re.search(pattern, chunk):
                    out.append(chunk)
                    break  # only first matching chunk
    return out


def _dimension_verdict_from_reason(info: dict, dimension: str) -> str | None:
    chunks = _reason_chunks_for_dimension(info.get("reason", ""), dimension)
    if not chunks:
        return None
    joined = "\n".join(chunks)
    if dimension == "image" and re.search(
        r"(?:字节级完全一致|MD5.{0,20}(?:相同|一致)|逐像素.{0,12}(?:相同|一致))"
        r".{0,500}(?:图像重复嵌入|非标准PDF构造|图像完整性问题|不同实验.{0,20}复用)",
        joined,
        re.S | re.I,
    ):
        return "建议高风险"
    if _has_unnegated_strong_highrisk_evidence(joined, dimension):
        return "建议高风险"
    if any(
        re.search(pattern, joined)
        for pattern in (
            r"核心.{0,20}(?:高风险|建议高风险).{0,20}(?:发现|问题|异常)",
            r"(?:支持|构成).{0,20}(?:高风险|建议高风险)(?:判定|结论|异常)?",
            r"(?:独立|不同).{0,30}(?:基因|样本|实验|条件|测量).{0,80}(?:完全一致|完全相同|精确一致|逐值相同)",
            r"(?:完全一致|完全相同|精确一致|逐值相同).{0,80}(?:统计学上|几乎|极不|近乎).{0,20}不可能",
            r"(?:数据完整性问题|真实完整性问题|完整性异常)",
            r"(?:小数尾串|值回收|样本量不符|图-源数据|图[-－—]?数据).{0,80}(?:完整性问题|异常|矛盾|不一致|不符)",
            r"(?:\\d+\\s*/\\s*\\d+|100%).{0,60}(?:完全相同|完全一致|逐值相同|逐位相同|重合)",
        )
    ):
        # Negation must be local to the same evidence phrase. A 综合评估 paragraph
        # often says "图像是假阳性；数据存在完整性问题"; the former must not
        # cancel the latter.
        if not re.search(r"(?:不构成|无|未发现|未见|不存在|排除|并非|非真实|假阳性|不支持|未确认).{0,25}(?:高风险|建议高风险|完整性问题|完整性异常|异常)$", joined):
            return "建议高风险"
    return _section_verdict(joined, info.get("verdict", ""), dimension)


def _dimension_verdict(info: dict, dimension: str) -> str:
    """返回单维度结论 ("建议高风险" / "建议低风险").

    独立判断: 优先用 dimension 实际 evidence 决定, 不被主结论强制覆盖.
    实际场景: 主结论=高风险通常基于 1-2 个 dimension 的强证据, 其他 dimension
    可能是"未发现/假阳性", 这时单项应真实反映"建议低风险"而不是被强制为高.
    """
    overall = f"{info.get('result', '')}{info.get('verdict', '')}"
    # A final low-risk judgement cannot coexist with a high-risk dimension in
    # the published report.  Judge has already converged the evidence; older
    # report inference sometimes re-promoted an initial alert mentioned only
    # as a false positive and produced a visibly contradictory PDF.
    if "低风险" in overall and "高风险" not in overall:
        return "建议低风险"

    explicit = info.get("dimension_verdicts") or info.get("_dimension_verdicts") or {}
    if isinstance(explicit, dict):
        value = explicit.get(dimension)
        if value:
            return "建议高风险" if "高风险" in str(value) else "建议低风险"

    key = _DIMENSION_REVIEW_KEY.get(dimension, "")
    fallback = info.get("verdict", "")
    reason_verdict = _dimension_verdict_from_reason(info, dimension)
    if reason_verdict == "建议高风险":
        return reason_verdict

    # 直接从 dimension 实际 evidence 决定: 该 dimension 详情是否真有高风险证据
    dim_text = info.get(key, "")
    if not str(dim_text or "").strip():
        return "建议低风险"

    if dimension == "data" and re.search(
        r"(?:核心发现|核心问题|关键发现|独立发现).{0,500}"
        r"(?:byte-identical|逐值完全相同|逐位完全相同|100%.*(?:相同|一致)|"
        r"常量偏移|整数偏移|小数尾串(?:继承|插入)|数据复用|跨子表|跨基因|"
        r"系统性.{0,40}(?:偏离|不一致|不符)|完整性问题)",
        str(dim_text),
        re.I | re.S,
    ):
        return "建议高风险"

    # Image/reference sections have a stable reviewer idiom: enumerate every
    # candidate, then explicitly conclude that all are false positives or that
    # no real issue remains. Honor that section-level conclusion before broad
    # words such as "重复", "DOI不匹配", or "完全相同" can trigger red labels.
    if dimension == "image" and re.search(
        r"(?:全部|所有|均).{0,100}(?:假阳性|正常(?:复用|匹配|模式)|不构成).{0,80}"
        r"|(?:未发现|未见|无).{0,45}(?:真实|独立|可定位)?.{0,30}"
        r"(?:图像重复|图像复用|拼接|图像完整性问题|图像异常)"
        r"|图像(?:重复|拼接)?(?:维度)?.{0,45}(?:低风险|无独立高风险证据|"
        r"不构成(?:完整性问题|风险)|未发现(?:完整性问题|异常|真实问题))",
        str(dim_text),
        re.S,
    ):
        return "建议低风险"
    if dimension == "ref" and re.search(
        r"(?:参考文献|引用|文献)(?:维度)?.{0,80}(?:低风险|无高风险发现|"
        r"无真实异常|不构成(?:引用异常|完整性问题|风险)|未发现.{0,35}"
        r"(?:实质不一致|完整性问题|引用异常|异常))"
        r"|(?:全部|所有|均).{0,100}(?:解析|匹配|CrossRef|数据库).{0,50}"
        r"(?:假阳性|问题|限制|误报|误匹配)"
        r"|(?:未发现|未见|无).{0,50}(?:完整可读条目|DOI|题名|作者|期刊).{0,45}"
        r"(?:实质不一致|不匹配|异常)",
        str(dim_text),
        re.S,
    ):
        return "建议低风险"
    if dimension == "ref" and re.search(
        r"(?:属|判为|确认为|系).{0,25}(?:解析|数据库|CrossRef|匹配).{0,35}"
        r"(?:假阳性|误报|误匹配|问题|限制)"
        r"|(?:并非|非|不构成).{0,30}(?:作者引用|真实引用|引用异常|著录异常|完整性问题)"
        r"|(?:无|未发现|未见).{0,55}(?:真实|可定位|具体)?.{0,20}"
        r"(?:引用异常|参考文献异常|著录异常|完整性问题)"
        r"|(?:未单独计入|不单独计入).{0,20}高风险",
        str(dim_text),
        re.S,
    ):
        return "建议低风险"

    # Data sections commonly end by dismissing the *remaining* automated alerts
    # after presenting a concrete core finding. Preserve that finding before
    # considering any trailing low-risk summary.
    if dimension == "data" and _has_unnegated_strong_highrisk_evidence(dim_text, dimension):
        return "建议高风险"

    # Reviewer prose often cites a confirmed finding from another section for
    # context (for example, methodology says "见数据复核") and then explicitly
    # concludes that this dimension has no independent high-risk evidence. That
    # dimension-level convergence must win over cross-dimension cue words.
    tail = _format_section_text(str(dim_text))[-900:]
    dimension_low_patterns = {
        "image": (
            r"(?:综上[，,：:]?)?(?:图像(?:重复|拼接)?(?:维度)?|本维度).{0,55}"
            r"(?:未发现|未见|无独立|不构成|未构成).{0,40}"
            r"(?:完整性问题|高风险|问题|证据|异常)",
        ),
        "data": (
            r"(?:综上[，,：:]?)?(?:数据(?:异常)?(?:维度)?|本维度).{0,55}"
            r"(?:未发现|未见|无独立|不构成|未构成).{0,40}"
            r"(?:完整性问题|高风险|问题|证据|异常)",
        ),
        "ref": (
            r"(?:综上[，,：:]?)?(?:参考文献|引用|文献)(?:维度)?.{0,55}"
            r"(?:未发现|未见|无独立|不构成|未构成).{0,40}"
            r"(?:完整性问题|高风险|问题|证据|异常|不一致)",
        ),
        "methodology": (
            r"(?:综上[，,：:]?)?(?:方法学|方法学与时间线|方法学/时间线|本节|本维度)"
            r"(?:维度|层面)?.{0,65}(?:未发现|未见|无独立|不构成|未构成).{0,40}"
            r"(?:完整性问题|高风险|问题|证据|异常|矛盾)",
            r"(?:未发现|未见).{0,30}方法学(?:层面|维度)?.{0,35}"
            r"(?:完整性问题|独立问题|内部矛盾|异常|高风险证据)",
            r"(?:方法学|方法学与时间线)(?:层面|维度)?.{0,35}"
            r"(?:无|未发现|未见).{0,30}(?:内部矛盾|独立问题|完整性问题|异常)",
            r"(?:与方法学描述无关|归入数据(?:复核|维度)|归数据维度|"
            r"问题主要集中在数据完整性|已在数据复核(?:部分)?(?:详述|记录))",
            r"(?:问题主要集中在|归入|已在).{0,30}(?:数据|图像|参考文献)(?:复核|维度|部分)"
            r".{0,80}(?:方法学|本节).{0,35}(?:无独立|不构成|未发现)",
        ),
    }
    for pattern in dimension_low_patterns.get(dimension, ()):
        for match in re.finditer(pattern, tail, re.S):
            sentence_start = max(
                tail.rfind("。", 0, match.start()),
                tail.rfind("；", 0, match.start()),
                tail.rfind("\n", 0, match.start()),
            ) + 1
            sentence_end_candidates = [
                pos for pos in (tail.find("。", match.end()), tail.find("；", match.end()), tail.find("\n", match.end()))
                if pos != -1
            ]
            sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(tail)
            sentence = tail[sentence_start:sentence_end]
            # "除 Fig.X 异常外未发现其他问题" confirms the named exception;
            # it is not a low-risk conclusion for the whole dimension.
            if re.search(
                r"(?:除|但|唯一|其余|其它).{0,120}(?:外|之外|异常|问题|矛盾).{0,55}"
                r"(?:未发现|未见|无|正常)",
                sentence,
                re.S,
            ):
                continue
            return "建议低风险"

    if _has_unnegated_strong_highrisk_evidence(dim_text, dimension):
        return "建议高风险"

    # dimension 文本未明确高风险时, 再回退到 reason (综合评估). 这一步必须在
    # section verdict 的低风险返回前执行, 否则模块开头的"低风险/假阳性"会挡住
    # 综合评估里明确归属到该维度的核心高风险证据。
    base = _section_verdict(dim_text, fallback, dimension)
    if base in ("建议高风险", "建议低风险"):
        return base

    if reason_verdict in ("建议高风险", "建议低风险"):
        return reason_verdict

    # 最后: 用主结论 (但仅在 dimension 真的"无独立证据"时)
    if "高风险" in f"{fallback}{info.get('result', '')}":
        return "建议高风险"
    return "建议低风险"


def _prepend_section_verdict(text: str, fallback_verdict: str) -> str:
    verdict = _section_verdict(text, fallback_verdict)
    prefix = f"本模块复核结论：{verdict}。"
    stripped = (text or "").strip()
    if stripped.startswith("本模块复核结论："):
        return stripped
    return prefix + ("\n" + stripped if stripped else "")


def _prepend_section_verdict_for_dimension(
    text: str,
    fallback_verdict: str,
    dimension: str,
    verdict_override: str | None = None,
) -> str:
    verdict = verdict_override or _section_verdict(text, fallback_verdict, dimension)
    prefix = f"本模块复核结论：{verdict}。"
    stripped = (text or "").strip()
    if stripped.startswith("本模块复核结论："):
        return re.sub(
            r"^本模块复核结论：\s*(?:建议高风险|建议低风险|确认高风险|高风险|低风险)[。；;]?",
            prefix,
            stripped,
            count=1,
        )
    return prefix + ("\n" + stripped if stripped else "")


def _enforce_overall_consistency_in_detail(text: str, info: dict) -> str:
    """已废弃: 之前为主结论=高时强制把 detail header 改成"建议高风险".
    但这与 dimension 实际证据矛盾 (例如 4 维度详情都写"假阳性/未发现").
    现在 _dimension_verdict 已独立判断, 此函数保留为 no-op 兼容旧调用.
    """
    return text


def _compact_section_text(text: str, verdict: str) -> str:
    """Keep published review prose short without deleting confirmed evidence.

    High-risk sections retain the five reader-explainability labels and numbered
    findings, while blocks devoted only to dismissed alerts are removed. Low-risk
    sections collapse to their conclusion plus one plain-language explanation.
    The unabridged JSON remains available for audit; this function affects PDF
    presentation only.
    """
    if not COMPACT_REVIEW_REPORT:
        return text
    formatted = _format_section_text(str(text or ""))
    if not formatted:
        return formatted

    # Delete explicitly dismissed/diagnostic blocks from public high-risk PDFs.
    formatted = re.sub(
        r"(?:^|\n)(?:【?已排除(?:的项|项|告警)?】?|【?假阳性(?:说明|来源)?】?)"
        r".*?(?=\n(?:【?(?:确认的问题|确认问题|核心问题|问题位置|问题类型)】?|"
        r"[①②③④⑤⑥⑦⑧⑨⑩]|\d+\.\s)|\Z)",
        "\n",
        formatted,
        flags=re.S | re.M,
    )
    lines = [line.strip() for line in formatted.splitlines() if line.strip()]

    if "低风险" in verdict:
        conclusion = next((line for line in lines if line.startswith("本模块复核结论：")), "")
        useful = []
        for line in lines:
            if line == conclusion or re.search(r"(?:已排除|假阳性|初检|初审|内部|流程|逐项|扫描)", line):
                continue
            for sentence in _split_review_sentences(line):
                sentence = sentence.strip()
                if sentence and sentence not in useful:
                    useful.append(sentence)
                if "实验或研究设计" in sentence:
                    useful = [sentence]
                    break
            if useful and "实验或研究设计" in useful[0]:
                break
        return "\n".join(part for part in (conclusion, useful[0] if useful else "未发现实质性问题。") if part)

    # Keep all structured evidence labels, but cap runaway prose after the
    # finding is already reader-explainable. This is intentionally generous
    # enough for several numbered findings and their calculations.
    compact = "\n".join(lines)
    if len(compact) > 2600:
        cut = max(compact.rfind("。", 0, 2600), compact.rfind("；", 0, 2600))
        compact = compact[:cut + 1 if cut > 2100 else 2600].rstrip() + "\n其余细节请结合原始材料复核。"
    return compact


def _compact_structure_section_html(text: str, section_verdict: str) -> str:
    """Render compact prose in source order so calculations stay with labels."""
    text = _format_section_text(_strip_internal_review_process_terms(text))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    parts = []
    for line in lines:
        if _VERDICT_LINE_RE.match(line):
            color = "#c00" if "高风险" in line else "#080"
            parts.append(
                f'<p style="font-size:9.5pt; color:{color}; margin-bottom:7pt; '
                f'font-weight:bold;">{_html_escape(line)}</p>'
            )
            continue
        if re.match(r"^(?:【?已排除|【?假阳性)", line):
            continue
        parts.append(
            '<p style="font-size:9.3pt; color:#333; margin-bottom:4pt;">'
            f'{_highlight_explanation_labels(line)}</p>'
        )
    return "\n".join(parts)


def _compact_overall_reason(info: dict, reason: str) -> str:
    """Use a short decision statement in the public PDF summary."""
    if not COMPACT_REVIEW_REPORT:
        return reason
    if "高风险" not in f"{info.get('result', '')}{info.get('verdict', '')}":
        return "本次复核未发现会改变整体风险判断的实质性问题，现有证据支持低风险结论。"
    high_labels = []
    labels = {
        "image": "图像",
        "data": "数据与结果表述",
        "ref": "参考文献",
        "methodology": "研究设计与统计",
    }
    for dimension, label in labels.items():
        if _dimension_verdict(info, dimension) == "建议高风险":
            high_labels.append(label)
    focus = "、".join(high_labels) or "论文完整性"
    return (
        f"本次复核在{focus}方面确认了可定位、可复核的问题，具体证据见上文。"
        "建议作者解释或更正；该风险结论不等同于已经证明存在学术不端。"
    )


def _analysis_html(info: dict, evidence_images: list[dict] | None = None) -> str:
    from modules.review_report_layout import issue_evidence_html
    evidence_by_dimension = {}
    for item in evidence_images or []:
        evidence_by_dimension.setdefault(item.get("dimension"), []).append(item)

    def compact(text, verdict, dimension):
        # Never truncate numbered issues that carry attached evidence.
        return text if evidence_by_dimension.get(dimension) else _compact_section_text(text, verdict)

    verdict = info["verdict"]
    display_verdict = _display_review_verdict(info)
    image_section_verdict = _dimension_verdict(info, "image")
    data_section_verdict = _dimension_verdict(info, "data")
    ref_section_verdict = _dimension_verdict(info, "ref")
    image_review = _humanize_internal_terms(_prepend_section_verdict_for_dimension(
        info.get("image_review", "无图像异常发现。"), verdict, "image", image_section_verdict
    ))
    data_review = _humanize_internal_terms(_prepend_section_verdict_for_dimension(
        info.get("data_review", "无数据异常发现。"), verdict, "data", data_section_verdict
    ))
    ref_review = _humanize_internal_terms(_prepend_section_verdict_for_dimension(
        info.get("ref_review", "无参考文献异常发现。"), verdict, "ref", ref_section_verdict
    ))
    image_review = compact(image_review, image_section_verdict, "image")
    data_review = compact(data_review, data_section_verdict, "data")
    ref_review = compact(ref_review, ref_section_verdict, "ref")
    # 主结论=高风险时, 把单项 detail header 里的"建议低风险"强制改成"建议高风险"
    image_review = _enforce_overall_consistency_in_detail(image_review, info)
    data_review = _enforce_overall_consistency_in_detail(data_review, info)
    ref_review = _enforce_overall_consistency_in_detail(ref_review, info)
    reason_text = info.get("reason") or info.get("verdict") or info.get("result") or info.get("trigger") or ""
    reason = _format_external_reason_text(_normalize_verdict_phrases(_humanize_internal_terms(reason_text)))
    reason = _compact_overall_reason(info, reason)
    methodology_review = info.get("methodology_review", "")

    # Verdict vocabulary is binary: 建议高风险 / 建议低风险.
    if "高风险" in f"{verdict}{info.get('result', '')}":
        verdict_color = "#c00"
    else:
        verdict_color = "#080"

    manual_note = ""

    structure = _compact_structure_section_html if COMPACT_REVIEW_REPORT else _structure_section_html
    def section(text, verdict, dimension):
        return issue_evidence_html(text, evidence_by_dimension.get(dimension, []),
                                   lambda body: structure(body, verdict))

    image_html = section(image_review, image_section_verdict, "image")
    data_html = section(data_review, data_section_verdict, "data")
    ref_html = section(ref_review, ref_section_verdict, "ref")
    if "高风险" in f"{verdict}{info.get('result', '')}":
        summary_lead_html = ""
        reason_html = _highrisk_reason_html(info, reason)
        # Note: methodology findings are shown in the (四) section of 复核详情 only.
        # We previously injected a one-liner into 综合评估, but that duplicated the
        # section heading and confused readers — the (四) section is the canonical
        # place for methodology. If the agent's reason text omits methodology
        # findings, that is the agent's responsibility, not the renderer's.
    else:
        summary_lead_html = _text_to_html(_summary_lead(info))
        reason_html = _text_to_html(_format_external_reason_text(reason))

    # Optional （四）研究设计与统计核查 — rendered for every report so the
    # reader can see what was checked, even when no independent concern was found.
    # Some agents emit methodology_review as a dict (e.g. {"detail": "..."}) instead of a
    # plain string. Coerce to a string to keep .strip() and downstream string ops safe.
    if isinstance(methodology_review, dict):
        methodology_review = (
            methodology_review.get("detail")
            or methodology_review.get("text")
            or methodology_review.get("summary")
            or json.dumps(methodology_review, ensure_ascii=False)
        )
    methodology_section_verdict = _dimension_verdict(info, "methodology")
    if not methodology_review or not str(methodology_review).strip():
        methodology_review = (
            "研究设计与统计核查未发现独立支持高风险结论的问题；"
            "本报告的最终风险结论由上文已定位的图像、数据或参考文献证据承载。"
            "已根据可用材料核对实验或研究设计、样本量、统计方法、伦理适用性、时间线以及关键试剂/仪器描述，"
            "未见可单独升级风险等级的具体研究设计矛盾。"
        )
    methodology_review = _humanize_internal_terms(_prepend_section_verdict_for_dimension(
        methodology_review, verdict, "methodology", methodology_section_verdict
    ))
    methodology_review = compact(methodology_review, methodology_section_verdict, "methodology")
    methodology_review = _enforce_overall_consistency_in_detail(methodology_review, info)
    methodology_block = (
        '\n<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; '
        'margin-top:14pt; margin-bottom:6pt;">（四）研究设计与统计核查</p>\n'
        + section(methodology_review, methodology_section_verdict, "methodology") + "\n"
    )

    return f"""
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">三、复核详情</h2>

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（一）图像问题复核</p>
{image_html}

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（二）数据异常检测复核</p>
{data_html}

<p style="font-size:11pt; font-weight:bold; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">（三）参考文献检测复核</p>
{ref_html}
{methodology_block}
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:18pt; margin-bottom:6pt;">四、综合评估</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:3pt;"><b>复核结论：</b><span style="color:{verdict_color}; font-weight:bold;">{display_verdict}</span></p>
{summary_lead_html}
{reason_html}
{manual_note}
"""


def _sanitize_paper_text_for_render(s: str) -> str:
    """删 control chars (除 \\t\\n\\r) + 修特定已知错 (e.g. \\x01d -> δ).

    用于 PDF 渲染前 sanitize report.json paper 字段, 避免乱码.
    """
    if not s:
        return s
    fix_map = {
        '\x01d': 'δ', '\x01D': 'Δ',
        '\x01a': 'α', '\x01A': 'Α',
        '\x01b': 'β', '\x01B': 'Β',
        '\x01g': 'γ', '\x01G': 'Γ',
        '\x01s': 'σ', '\x01S': 'Σ',
        '\x01m': 'μ', '\x01M': 'Μ',
        '\x01p': 'π', '\x01P': 'Π',
        '\x01l': 'λ', '\x01L': 'Λ',
        '\x01t': 'τ', '\x01T': 'Τ',
    }
    for bad, good in fix_map.items():
        s = s.replace(bad, good)
    # 修 PDF 提取时 U+FFFD (efbfbd) 替换西文重音 + 串位补字符
    # 真案例: djaf230 中 "Maye�n" / "Jose�-Maria" / "Epidemiolog�ıa y Salud Pu�blica"
    # "Epidemiología" 错为 "Epidemiolog�ıa" (FFFD替换ó, ı替换í)
    # "Pública" 错为 "Pu�blica" (FFFD替换ú)
    fffd_map = {
        'Maye�n': 'Mayén',
        'Mayén'.replace('é', '�'): 'Mayén',  # 兜底
        'Jose�-Maria': 'José-Maria',
        'Epidemiolog�ıa': 'Epidemiología',
        'Salud Pu�blica': 'Salud Pública',
    }
    for bad, good in fffd_map.items():
        if bad in s:
            s = s.replace(bad, good)
    s = ''.join(c for c in s if (c >= ' ' or c in '\t\n\r'))
    return s


def _sanitize_paper_dict_for_render(paper: dict) -> dict:
    if not paper:
        return paper
    out = dict(paper)
    for k in ('title', 'author', 'author_type', 'department',
              'author_all', 'department_all', 'journal',
              'doi', 'abstract'):
        if k in out and isinstance(out[k], str):
            out[k] = _sanitize_paper_text_for_render(out[k])
    # list 字段 (作者/单位)
    for k in ('authors_full', 'authors', 'affiliations', 'departments'):
        if k in out and isinstance(out[k], list):
            out[k] = [_sanitize_paper_text_for_render(x) for x in out[k] if isinstance(x, str)]
    return out


def _render_pdf(html: str, output_path: str):
    validate_report_render_environment()
    # Display-only: render 高风险/建议高风险 as 建议高风险 in the final PDF. Risk *values*
    # (result/verdict/DB) are unchanged — softening happens here at the render boundary.
    html = _soften_risk_wording(html)
    html = _sanitize_internal_references(html)
    story = fitz.Story(html)
    writer = fitz.DocumentWriter(output_path)
    mediabox = fitz.paper_rect("a4")
    margin = 72
    where = mediabox + (margin, margin, -margin, -margin)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def _find_report_json(doi: str, preferred: str | None = None) -> str | None:
    doi = str(doi or "").strip()
    if not doi:
        return None
    if preferred:
        p = Path(preferred)
        if p.exists():
            return str(p)
    slug1 = doi.replace("/", "_")
    slug2 = doi.replace("/", "__")
    base = Path(PROJECT_ROOT) / "data" / "output"

    def matches_report(path: Path) -> bool:
        try:
            with path.open(encoding="utf-8") as f:
                paper = (json.load(f).get("paper") or {})
            return (paper.get("doi") or "").strip().lower() == (doi or "").strip().lower()
        except Exception:
            return False

    candidates: list[Path] = []
    flat_fallback: Path | None = None
    for namespace in ("yujing_4050", "yujing_quanliang"):
        root = base / namespace
        if root.exists():
            for slug in (slug2, slug1):
                candidates.extend(root.glob(f"*{slug}*/report.json"))
    for slug in [slug1, slug2]:
        p = base / slug / "report.json"
        if p.exists() and flat_fallback is None:
            flat_fallback = p
        candidates.append(p)
        candidates.extend(base.glob(f"*/*{slug}*/report.json"))

    seen = set()
    matches = []
    for p in candidates:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        if matches_report(p):
            matches.append(p)
    if matches:
        matches.sort(key=lambda p: (0 if "yujing_4050" in p.parts else 1, -p.stat().st_mtime, str(p)))
        return str(matches[0])
    if flat_fallback:
        return str(flat_fallback)
    return None


def _evidence_cache_path(doi: str, input_dir: str = "", report_json: str = "") -> Path:
    slug = (doi or "").replace("/", "_")
    if input_dir or report_json:
        identity = {
            "doi": doi,
            "input_dir": str(Path(input_dir).resolve()) if input_dir else "",
            "report_json": str(Path(report_json).resolve()) if report_json else "",
        }
        digest = __import__("hashlib").sha1(
            json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:12]
        return REVIEW_V2_DIR / f"{slug}_{digest}_evidence.json"
    return REVIEW_V2_DIR / f"{slug}_evidence.json"


def _safe_namespace(namespace: str) -> str:
    parts = []
    for part in (namespace or "").strip().split("/"):
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", part).strip("._-")
        if cleaned:
            parts.append(cleaned[:80])
    return "/".join(parts)


def _load_or_build_evidence(doi: str, input_dir: str = "", report_json: str = ""):
    import review_evidence

    input_dir = input_dir or review_evidence._find_paper_dir(doi)
    report_json = report_json or _find_report_json(doi) or review_evidence._find_report_json(doi)
    cached = _evidence_cache_path(doi, input_dir or "", report_json or "")

    cache_stale = False
    if cached.exists():
        cache_mtime = cached.stat().st_mtime
        for src in (report_json, input_dir):
            if src and os.path.exists(src):
                try:
                    if os.path.getmtime(src) > cache_mtime:
                        cache_stale = True
                        break
                except OSError:
                    pass
        if not cache_stale:
            with open(cached, encoding="utf-8") as f:
                return json.load(f), str(cached), True
        print(f"[evidence] cache stale for {doi}: rebuilding", file=sys.stderr)

    if not input_dir:
        raise RuntimeError("paper input dir not found")

    bundle = review_evidence.build_bundle(input_dir, report_json, doi)
    cached.parent.mkdir(parents=True, exist_ok=True)
    with open(cached, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=1)
    return bundle, str(cached), False


def _limit_child_memory():
    """Keep a pathological evidence build from taking down the batch process."""
    if COVERAGE_MEMORY_MB <= 0:
        return
    try:
        import resource
        limit = COVERAGE_MEMORY_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass


def _coverage_worker(info: dict, q):
    try:
        _limit_child_memory()
        import coverage_validator
        doi = info.get("doi", "")
        bundle, evidence_path, from_cache = _load_or_build_evidence(
            doi,
            info.get("_input_dir") or "",
            info.get("_report_json") or "",
        )
        validated, gaps = coverage_validator.validate(info, bundle)
        validated["_coverage_evidence_path"] = evidence_path
        q.put({
            "ok": True,
            "validated": validated,
            "gaps": gaps,
            "source": "cache" if from_cache else "rebuilt",
        })
    except BaseException as e:
        q.put({"ok": False, "error": f"{type(e).__name__}: {e}"})


def _run_coverage_in_child(info: dict) -> dict:
    """Run evidence loading/building and validation outside the PDF/DB process.

    OOM killer exit code 137 or a hung workbook scan should mark coverage as
    unavailable, not kill the whole batch run.
    """
    ctx = mp.get_context("fork")
    q = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_coverage_worker, args=(info, q), daemon=True)
    proc.start()
    proc.join(COVERAGE_TIMEOUT_SECONDS)

    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
        raise TimeoutError(f"coverage timed out after {COVERAGE_TIMEOUT_SECONDS}s")

    try:
        payload = q.get_nowait()
    except queue.Empty:
        code = proc.exitcode
        if code == -9 or code == 137:
            raise RuntimeError(f"coverage worker killed by OOM/signal (exitcode={code})")
        raise RuntimeError(f"coverage worker exited without result (exitcode={code})")

    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "coverage worker failed")
    return payload


def _safe_doi_slug(doi: str) -> str:
    doi = normalize_doi(doi) or doi
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", doi or "unknown").strip("_")
    return slug[:120].lower() or "unknown"


def _find_api_state_final(doi: str) -> dict | None:
    """Scan REVIEW_STATE_DIR and review_artifacts/ for any completed state files matching this DOI.

    Returns the newest high-risk final if any state file has final.result='高风险',
    otherwise the newest low-risk final, or None if no completed states are found.
    """
    REVIEW_STATE_DIR.mkdir(parents=True, exist_ok=True)
    slug = _safe_doi_slug(doi)

    # Collect candidate paths from both state/ (flat) and review_artifacts/ (nested)
    candidate_files: list[Path] = []
    candidate_files.extend(REVIEW_STATE_DIR.glob(f"{slug}_*.json"))
    # Also scan review_artifacts/ subdirectories under review_v2/
    for artifacts_dir in REVIEW_V2_DIR.glob("*/review_artifacts"):
        candidate_files.extend(artifacts_dir.glob(f"{slug}_*_state.json"))

    # Exclude .tmp files (partial writes)
    candidate_files = [p for p in candidate_files if not p.name.endswith(".tmp")]

    if not candidate_files:
        return None

    high_final = None
    high_updated = ""
    low_final = None
    low_updated = ""
    for path in candidate_files:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            final = (state.get("stages") or {}).get("final")
            if isinstance(final, dict) and final.get("result") in ("高风险", "低风险"):
                updated = state.get("updated_at", "")
                if final["result"] == "高风险":
                    if updated >= high_updated:
                        high_updated = updated
                        high_final = dict(final)
                else:
                    if updated >= low_updated:
                        low_updated = updated
                        low_final = dict(final)
        except Exception:
            continue

    return high_final or low_final


def _find_api_state_verdict(doi: str) -> str | None:
    final = _find_api_state_final(doi)
    if isinstance(final, dict):
        return final.get("result")
    return None


def _reconcile_review_info_with_state(info: dict) -> dict:
    """Keep review text untouched before rendering.

    A high-risk historical state is not evidence for a new report by itself.
    Conflicts are handled before DB write by refusing the write and requiring
    an adversarial re-review, rather than substituting old state prose.
    """
    return info


def _reconcile_before_write(
    db_updates: list[dict], table_name: str
) -> list[dict]:
    """Return DB updates unchanged.

    Historical API state or another table's old verdict is not evidence for the
    current report. The DB value must match the freshly rendered review result.
    """
    return db_updates


def _write_minimal_state(
    doi: str, info: dict, report_json_path: str | None, input_dir: str | None
) -> None:
    """Write a minimal review state file so Skill-path reviews are traceable.

    Only writes when no state file already exists for this DOI — avoids
    creating duplicate stubs alongside existing API-produced state files.
    """
    REVIEW_STATE_DIR.mkdir(parents=True, exist_ok=True)
    slug = _safe_doi_slug(doi)

    # Skip if any state file already exists for this DOI
    existing = list(REVIEW_STATE_DIR.glob(f"{slug}_*.json"))
    existing = [p for p in existing if not p.name.endswith(".tmp")]
    if existing:
        return
    try:
        report_path = str(Path(report_json_path).resolve()) if report_json_path else ""
        input_path = str(Path(input_dir).resolve()) if input_dir else ""
    except Exception:
        report_path = report_json_path or ""
        input_path = input_dir or ""

    identity = {
        "version": 2,
        "doi": doi,
        "report": {
            "path": report_path,
            "mtime_ns": 0,
        },
        "input": {
            "path": input_path,
            "mtime_ns": 0,
        },
    }
    digest = hashlib.sha1(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]

    state = {
        "identity": identity,
        "stages": {
            "decision": {"flow": "skill_batch", "reason": "Skill批量复核路径产出，非API流水线。"},
            "final": {
                "doi": doi,
                "result": info.get("result", "低风险"),
                "trigger": info.get("trigger", ""),
                "image_review": info.get("image_review", ""),
                "data_review": info.get("data_review", ""),
                "ref_review": info.get("ref_review", ""),
                "methodology_review": info.get("methodology_review", ""),
                "verdict": info.get("verdict", "建议低风险"),
                "reason": info.get("reason", ""),
            },
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    state_path = REVIEW_STATE_DIR / f"{slug}_{digest}.json"
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp_path, state_path)


def _update_db(updates: list[dict], table_name: str = "yujing_quanliang", scholar: str = ""):
    conn = pymssql.connect(**DB_CONFIG)
    cursor = conn.cursor()
    now = datetime.now()
    for item in updates:
        where_sql = "WHERE doi=%(doi)s"
        params = {
            "doi": item["doi"],
            "review_result": item["review_result"],
            "review_report_url": item["review_report_url"],
            "review_time": now,
        }
        if scholar:
            where_sql += " AND scholar=%(scholar)s"
            params["scholar"] = scholar
        cursor.execute(
            f"""UPDATE {table_name} SET
                review_result=%(review_result)s,
                review_report_url=%(review_report_url)s,
                review_time=%(review_time)s,
                status=2
            {where_sql}""",
            params,
        )
        status = "OK" if cursor.rowcount > 0 else "NOT FOUND"
        print(f"[{status}] {item['doi']} -> {item['review_result']}")
    conn.commit()
    conn.close()


def _coverage_check(info):
    """Run the deterministic coverage validator on one review result, regardless of how
    the review was produced (API 2+1 path OR conversation/Skill agents). This is the
    single shared chokepoint: both paths call this script to write PDF+DB.

    Coverage gaps are audit warnings, not risk evidence by themselves. Do not turn a
    low-risk review into high-risk only because the prose omitted validator-friendly
    tokens such as "已核查 Fig.X"; that caused false high-risk DB writes."""
    if os.environ.get("REVIEW_SKIP_REPORT_COVERAGE", "0") == "1":
        out = dict(info)
        out["_coverage_status"] = "skipped"
        out["_coverage_error"] = "skipped by REVIEW_SKIP_REPORT_COVERAGE"
        return out
    doi = info.get("doi", "")
    verdict = (info.get("verdict") or "") + (info.get("result") or "")
    is_lowrisk = ("低风险" in verdict) and ("高风险" not in (info.get("result") or ""))
    if not is_lowrisk:
        return info
    try:
        checked_info = dict(info)
        if not checked_info.get("_input_dir") or not checked_info.get("_report_json"):
            try:
                import review_evidence
                if not checked_info.get("_input_dir"):
                    input_dir = review_evidence._find_paper_dir(doi)
                    if input_dir:
                        checked_info["_input_dir"] = input_dir
                if not checked_info.get("_report_json"):
                    report_json = _find_report_json(doi) or review_evidence._find_report_json(doi)
                    if report_json:
                        checked_info["_report_json"] = report_json
            except Exception:
                pass
        cached = _evidence_cache_path(
            doi,
            checked_info.get("_input_dir") or "",
            checked_info.get("_report_json") or "",
        )
        if cached.exists():
            import coverage_validator
            with open(cached, encoding="utf-8") as f:
                bundle = json.load(f)
            validated, gaps = coverage_validator.validate(checked_info, bundle)
            validated["_coverage_evidence_path"] = str(cached)
            src = "cache"
        else:
            payload = _run_coverage_in_child(checked_info)
            validated = payload["validated"]
            gaps = payload.get("gaps") or []
            src = payload.get("source", "unknown")
        if gaps:
            print(f"[COVERAGE] {doi}: warning only ({src}; gaps: {'; '.join(gaps)})", file=sys.stderr)
        return validated
    except Exception as e:
        print(f"[COVERAGE] {doi}: warning only, coverage unavailable ({e})", file=sys.stderr)
        out = dict(info)
        out["_coverage_status"] = "unavailable"
        out["_coverage_error"] = str(e)
        return out


def main():
    parser = argparse.ArgumentParser(description="Generate batch AI review PDF reports")
    parser.add_argument("--results", required=True, help="Path to batch results JSON")
    parser.add_argument("--output", default="data/output/review_v2/", help="Output directory")
    parser.add_argument("--table", default="yujing_quanliang", help="Target DB table name")
    parser.add_argument("--namespace", default="", help="Report URL/output namespace, normally the target table name")
    parser.add_argument("--scholar", default="", help="Optional scholar value used to scope DB review updates")
    parser.add_argument("--no-db", dest="no_db", action="store_true",
                        help="Render PDFs + coverage check + nginx copy only; skip the DB review-result UPDATE "
                             "(used by the detection_reports flow, which writes the DB itself)")
    args = parser.parse_args()

    with open(args.results) as f:
        results = json.load(f)

    namespace = _safe_namespace(args.namespace)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    review_base_url = f"{REVIEW_REPORT_BASE_URL}/{namespace}" if namespace else REVIEW_REPORT_BASE_URL

    db_updates = []
    generated_pdfs = []
    skipped_review_errors = []

    for info in results:
        info = _reconcile_review_info_with_state(info)
        info = _coverage_check(info)
        info = _normalize_review_info(info)
        info = _reconcile_review_verdict(info)
        _validate_review_info_consistency(info)
        doi = info["doi"]
        # Coerce DOI to string (some inputs have float/NaN DOIs)
        if doi is None or (isinstance(doi, float) and doi != doi):  # NaN check
            doi = ""
        else:
            doi = str(doi).strip()
        # Fail loud: if the upstream review produced a `review_error` fallback
        # (placeholder text like "复核过程中出现错误"), refuse to render a PDF
        # or write to DB. The user must never see an internal-error placeholder
        # inside a finished report PDF.
        if str(info.get("trigger") or "").strip() == "review_error":
            doi = info.get("doi", "<unknown>")
            print(
                f"[REVIEW_ERROR] {doi}: refusing to generate PDF — upstream review "
                f"fell back to review_error placeholder. Re-review required.",
                file=sys.stderr,
            )
            skipped_review_errors.append(doi)
            continue
        report_path = _find_report_json(doi, info.get("_report_json"))
        findings = None

        if report_path:
            with open(report_path) as f:
                findings = json.load(f)
            # 修 source 错编码: e.g. EuBa2Cu3O7\x01d -> EuBa2Cu3O7δ
            # + 删 control chars (除 \t\n\r) 避免 PDF 渲染乱码
            paper = _sanitize_paper_dict_for_render(findings.get("paper", {}))
            if info.get("_verified_paper_title"):
                paper["title"] = _sanitize_paper_text_for_render(str(info["_verified_paper_title"]))
            findings["paper"] = paper
            header = _header_html(paper)
            risk_score = _review_overview_html(findings, info)
        else:
            safe_doi = _html_escape(doi)
            header = f"""
<h1 style="font-size:18pt; color:#1a1a1a; text-align:center; margin-bottom:4pt;">Paper Pecker论文预警报告（AI复核版）</h1>
<p style="font-size:10.5pt; color:#555; text-align:center; margin-top:0; margin-bottom:16pt;">Paper Pecker Risk Alert Report — AI Review</p>
<hr style="border:none; border-top:1px solid #ccc; margin:10pt 0;">
<h2 style="font-size:13pt; color:#1a1a1a; margin-top:14pt; margin-bottom:6pt;">一、论文基本信息</h2>
<p style="font-size:9.5pt; color:#333; margin-bottom:2pt;"><b>DOI：</b>{safe_doi}</p>
<p style="font-size:9.5pt; color:#333; margin-bottom:8pt;"><b>复核日期：</b>{TODAY}</p>
"""
            risk_score = _review_overview_html(None, info)

        doi_slug = doi_to_slug(doi) or doi_to_slug(info.get("_report_slug", "")) or "unknown"
        evidence_scratch = out_dir / ".evidence_scratch" / _safe_evidence_slug(doi_slug)
        evidence_images = _review_evidence_images(info, findings, evidence_scratch)
        analysis = _analysis_html(info, evidence_images)
        disclaimer = REVIEW_DISCLAIMER_HTML.format(date=TODAY)

        full_html = f"""
<body style='font-family: {FONT_REGULAR};'>
{header}
{risk_score}
{analysis}
{disclaimer}
</body>
"""
        filename = f"review_{doi_slug}.pdf"
        output_path = str(out_dir / filename)

        try:
            _render_pdf(full_html, output_path)
            print(f"[PDF] {filename}")
            generated_pdfs.append(Path(output_path))
            if evidence_scratch.exists():
                shutil.rmtree(evidence_scratch, ignore_errors=True)
            with contextlib.suppress(OSError):
                evidence_scratch.parent.rmdir()
        except Exception as e:
            print(f"[ERR] {doi}: {e}", file=sys.stderr)
            continue

        review_result = info["result"]
        review_url = f"{review_base_url}/{filename}"
        write_only_low = (
            os.environ.get("REVIEW_WRITE_ONLY_LOW", "0") == "1"
            or args.table == "yujing_4050_202606"
        )
        if write_only_low and review_result != "低风险":
            print(f"[SKIP_DB] {doi} -> {review_result}; write_only_low=1")
            if review_result == "高风险":
                try:
                    SECOND_REVIEW_QUEUE.parent.mkdir(parents=True, exist_ok=True)
                    with SECOND_REVIEW_QUEUE.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "doi": doi,
                            "result": review_result,
                            "review_report_url": review_url,
                            "table": args.table,
                            "namespace": namespace,
                            "report_json": str(info.get("_report_json") or report_path or ""),
                            "input_dir": str(info.get("_input_dir") or ""),
                            "queued_at": datetime.now().isoformat(timespec="seconds"),
                        }, ensure_ascii=False) + "\n")
                except Exception as e:
                    print(f"[SECOND_REVIEW_QUEUE] {doi}: could not append queue: {e}", file=sys.stderr)
        else:
            db_updates.append({
                "doi": doi,
                "review_result": review_result,
                "review_report_url": review_url,
            })

        # Write a minimal state file so this review is traceable
        # regardless of whether it was produced via API or Skill path.
        try:
            _input_dir = info.get("_input_dir") or ""
            _report_json = info.get("_report_json") or report_path or ""
            _write_minimal_state(doi, info, _report_json, _input_dir)
        except Exception as e:
            print(f"[STATE] {doi}: could not write state file: {e}", file=sys.stderr)

    copy_root = NGINX_REVIEW_DIR / namespace / "review_v2" if namespace else NGINX_REVIEW_DIR
    if not copy_root.exists() and not copy_root.is_symlink():
        copy_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    if copy_root.resolve() == out_dir.resolve():
        print(f"\nNginx report path already points to output dir; no duplicate PDF copy needed: {copy_root}")
    else:
        for pdf in generated_pdfs:
            dst = copy_root / pdf.name
            if pdf.resolve() == dst.resolve():
                continue
            try:
                shutil.copy2(pdf, dst)
                copied += 1
            except shutil.SameFileError:
                continue
        print(f"\nCopied {copied} PDFs to {copy_root}")

    if db_updates and not args.no_db:
        print(f"\nReconciling {len(db_updates)} papers before DB write...")
        db_updates = _reconcile_before_write(db_updates, table_name=args.table)
        print(f"\nUpdating DB ({len(db_updates)} papers) in table '{args.table}'...")
        _update_db(db_updates, table_name=args.table, scholar=args.scholar)
    elif db_updates and args.no_db:
        print(f"\n--no-db set: skipping DB update for {len(db_updates)} papers (caller writes DB)")

    high = sum(1 for u in db_updates if u["review_result"] == "高风险")
    low = sum(1 for u in db_updates if u["review_result"] == "低风险")
    print(f"\nDone. 高风险: {high}, 低风险: {low}, Total: {len(db_updates)}")
    if skipped_review_errors:
        print(
            f"\n[REVIEW_ERROR] {len(skipped_review_errors)} paper(s) skipped (no PDF, no DB write) "
            f"because upstream review returned review_error placeholder. Re-review required:",
            file=sys.stderr,
        )
        for d in skipped_review_errors:
            print(f"  - {d}", file=sys.stderr)
        # Non-zero exit so CI / callers can detect this and refuse to mark the run as done.
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
