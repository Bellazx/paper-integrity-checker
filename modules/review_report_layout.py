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
    r'|问题[ \t]*(\d+)[ \t]*[:：]|([一二三四五六七八九十百千万零〇]+)[、.．])\s*'
    r'|(?<=[。；:：])[ \t]*([一二三四五六七八九十百千万零〇]+)[、.．]\s*'
)


def _chinese_issue_number(value: str) -> int:
    digits = {'零': 0, '〇': 0, '一': 1, '二': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
    units = {'十': 10, '百': 100, '千': 1000, '万': 10000}
    total = 0
    section = 0
    current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in units:
            unit = units[char]
            if unit == 10000:
                section += current
                total += section * unit
                section = 0
                current = 0
            else:
                section += (current or 1) * unit
                current = 0
        else:
            raise ValueError(f'Unsupported Chinese issue number: {value}')
    return total + section + current


def split_review_issues(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Keep the reviewer-authored order and all text; no semantic inference."""
    matches = list(_ISSUE_START.finditer(text))
    if not matches:
        return '', [(1, text)] if text.strip() else []
    indices = []
    for match in matches:
        raw = match.group(1) or match.group(2) or match.group(3)
        chinese = match.group(4) or match.group(5)
        indices.append(int(raw) if raw is not None else _chinese_issue_number(chinese))
    if len(set(indices)) != len(indices):
        raise ValueError('Duplicate issue numbers within a review dimension')
    return text[:matches[0].start()], [
        (index, text[match.start():matches[n + 1].start() if n + 1 < len(matches) else len(text)])
        for n, (index, match) in enumerate(zip(indices, matches))
    ]


def bind_issue_evidence(text: str, items: list[dict]) -> tuple[str, list[tuple[int, str, list[dict]]]]:
    prefix, issues = split_review_issues(text)
    grouped = {index: [] for index, _ in issues}
    normalized_items = []
    for item in items:
        index = item.get('issue_index')
        if isinstance(index, str):
            value = index.strip()
            match = re.fullmatch(r'(?:问题\s*)?([0-9]+)', value)
            if match:
                index = int(match.group(1))
            elif re.fullmatch(r'[一二三四五六七八九十百千万零〇]+', value):
                index = _chinese_issue_number(value)
        normalized_items.append((item, index))
    # Repair only unambiguous omissions: a single finding, or an explicit page
    # in the screenshot caption that occurs in exactly one numbered finding.
    # This keeps screenshots adjacent to their actual issue without guessing
    # based on list order when the mapping is ambiguous.
    for position, (item, index) in enumerate(normalized_items):
        if index is not None:
            continue
        if len(issues) == 1:
            normalized_items[position] = (item, issues[0][0])
            continue
        caption = str(item.get('caption') or '')
        page_match = re.search(r'第\s*(\d+)\s*页|\bpage\s*(\d+)\b', caption, re.IGNORECASE)
        page = next((value for value in page_match.groups() if value), None) if page_match else None
        if not page:
            continue
        page_tokens = (f'第{page}页', f'第 {page} 页', f'page {page}')
        matches = [issue_index for issue_index, body in issues
                   if any(token.lower() in body.lower() for token in page_tokens)]
        if len(matches) == 1:
            normalized_items[position] = (item, matches[0])
    if normalized_items and all(index is None for _, index in normalized_items):
        if len(normalized_items) == len(issues):
            normalized_items = [
                (item, issues[position][0])
                for position, (item, _) in enumerate(normalized_items)
            ]
    for item, index in normalized_items:
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
