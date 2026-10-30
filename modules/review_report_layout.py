"""Evidence layout shared by report-only repairs, API and batch rendering."""
import base64
from html import escape
from pathlib import Path
import re

from PIL import Image
from modules.review_public_wording import public_text


def evidence_images_html(items: list[dict], *, language: str = 'zh') -> str:
    blocks = []
    for item in items:
        path = Path(item['path'])
        # Story does not implement browser object-fit reliably. Supply both
        # intrinsic, aspect-preserving dimensions instead of width:100% with
        # an unrelated max-height (which can overlap the following caption).
        with Image.open(path) as im:
            width, height = im.size
        scale = min(425 / width, min(370, item.get('max_height', 370)) / height)
        mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}.get(
            path.suffix.lower(), 'image/png')
        uri = f'data:{mime};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')
        page_break = 'page-break-before:always;' if item.get('start_on_new_page', False) else ''
        label = 'Evidence' if language == 'en' else '证据截图'
        blocks.append(
            f'<div style="{page_break} page-break-inside:avoid; margin:10pt 0;">'
            f'<p style="font-size:9pt; font-weight:bold; color:#8f1d18; margin-bottom:6pt;">{label}</p>'
            f'<img src="{uri}" '
            f'style="display:block; width:{width*scale:.2f}pt; height:{height*scale:.2f}pt;">'
            '<p style="font-size:9pt; color:#555; margin-top:8pt; margin-bottom:8pt;">'
            f'{escape(public_text(str(item.get("caption") or "")))}</p></div>'
        )
    return '\n'.join(blocks)


# Match explicit finding numbers, never a decimal value or a figure number.
_ISSUE_START = re.compile(
    r'(?m)(?:^|(?<=\n))[ \t]*(?:[（(](\d+)[）)]|([0-9]+)[.、](?!\d)'
    r'|问题[ \t]*(\d+)[ \t]*[:：])\s*'
)


def split_review_issues(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Keep the reviewer-authored order and all text; no semantic inference."""
    matches = list(_ISSUE_START.finditer(text))
    if not matches:
        return '', [(1, text)] if text.strip() else []
    indices = [int(match.group(1) or match.group(2) or match.group(3)) for match in matches]
    if len(set(indices)) != len(indices):
        raise ValueError('Duplicate issue numbers within a review dimension')
    return text[:matches[0].start()], [
        (index, text[match.start():matches[n + 1].start() if n + 1 < len(matches) else len(text)])
        for n, (index, match) in enumerate(zip(indices, matches))
    ]


def bind_issue_evidence(text: str, items: list[dict]) -> tuple[str, list[tuple[int, str, list[dict]]]]:
    prefix, issues = split_review_issues(text)
    grouped = {index: [] for index, _ in issues}
    for item in items:
        index = item.get('issue_index')
        if index is None and len(issues) == 1:
            index = issues[0][0]
        if type(index) is not int or index not in grouped:
            raise ValueError('Evidence needs a valid issue_index matching a numbered issue '
                             f'in dimension {item.get("dimension", "unknown")}')
        grouped[index].append(item)
    return prefix, [(index, body, grouped[index]) for index, body in issues]


def issue_evidence_html(text: str, items: list[dict], render_text, *, language='zh') -> str:
    """Place only explicitly associated images immediately after their finding."""
    if not items:
        return render_text(text)
    prefix, issues = bind_issue_evidence(text, items)
    parts = [render_text(prefix)] if prefix.strip() else []
    for _, body, images in issues:
        parts.append(render_text(body))
        # Pagination may move the image to the next page, but never past the
        # next finding. Do not force a new page for each screenshot.
        parts.append(evidence_images_html(images, language=language))
    return '\n'.join(parts)
