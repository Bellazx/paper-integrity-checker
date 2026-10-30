"""Integrity checks useful for Chinese humanities review papers.

The checks are deliberately conservative. They add evidence to the existing
reference/data issue streams; risk scoring remains owned by the existing
``chinese_report_generator`` rules.
"""
from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher


_REFERENCE_HEADERS = re.compile(
    r"(?:^|\n)\s*(?:References|REFERENCES|Bibliography|Works\s+Cited|"
    r"参考文献|主要参考文献|参考书目|引用文献|注释|脚注|尾注|"
    r"Selected\s+Bibliography)\s*\n",
    re.IGNORECASE,
)
_CITATION_GROUP = re.compile(r"[\[【]([0-9０-９][0-9０-９\s,，;；、\-\u2013~～]*)[\]】]")
_YEAR = re.compile(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)")
_REVIEW_MARKERS = (
    "综述", "文献综述", "系统综述", "系统评价", "研究述评", "文献回顾",
    "literature review", "systematic review", "review article",
)
_METHOD_MARKERS = (
    ("database", ("中国知网", "知网", "CNKI", "万方", "维普", "CSSCI", "Web of Science", "Scopus", "JSTOR", "PubMed", "Google Scholar", "数据库")),
    ("time_range", ("检索时间", "时间范围", "截至", "1990", "2000", "2010", "2020", "search period", "from 20")),
    ("keywords", ("关键词", "检索词", "主题词", "搜索词", "keywords", "search terms")),
    ("criteria", ("纳入标准", "排除标准", "纳入与排除", "inclusion criteria", "exclusion criteria")),
)


def _to_ascii_digits(value: str) -> str:
    return value.translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def _expand_citation_group(raw: str) -> list[int]:
    numbers: list[int] = []
    for part in re.split(r"[,，;；、]", _to_ascii_digits(raw)):
        part = part.strip()
        if not part:
            continue
        range_match = re.fullmatch(r"(\d+)\s*[-\u2013~～]\s*(\d+)", part)
        if range_match:
            start, end = map(int, range_match.groups())
            if 0 < end - start <= 100:
                numbers.extend(range(start, end + 1))
            continue
        if part.isdigit():
            number = int(part)
            if number > 0:
                numbers.append(number)
    return numbers


def _normalized_text(text: str) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text.lower())


def _reference_section_start(text: str) -> int | None:
    match = _REFERENCE_HEADERS.search(text)
    return match.end() if match else None


def _citation_numbers(text: str) -> tuple[set[int], bool]:
    body = text[: _REFERENCE_HEADERS.search(text).start()] if _REFERENCE_HEADERS.search(text) else text
    numbers: set[int] = set()
    found = False
    for match in _CITATION_GROUP.finditer(body):
        values = _expand_citation_group(match.group(1))
        if values:
            found = True
            numbers.update(values)
    return numbers, found


def _issue(ref_number: int | str, ref_text: str, issue_type: str, severity: str,
           description: str, details: dict | None = None) -> dict:
    return {
        "ref_number": ref_number,
        "ref_text": ref_text[:300],
        "issue_type": issue_type,
        "severity": severity,
        "details": details or {},
        "description": description,
    }


def _reference_consistency(text: str, refs: list[dict]) -> list[dict]:
    if not refs:
        return []
    numbers = {int(ref["number"]) for ref in refs if str(ref.get("number", "")).isdigit()}
    cited, has_numeric_citations = _citation_numbers(text)
    if not has_numeric_citations:
        return []

    issues: list[dict] = []
    missing = sorted(cited - numbers)
    for number in missing[:20]:
        issues.append(_issue(
            number, "", "citation_without_reference", "medium",
            f"正文出现编号引用 [{number}]，但参考文献列表中未找到对应条目。",
            {"citation_number": number},
        ))

    uncited = sorted(numbers - cited)
    if uncited and len(uncited) / max(len(numbers), 1) >= 0.35:
        severity = "medium" if len(uncited) / max(len(numbers), 1) >= 0.60 else "low"
        issues.append(_issue(
            "汇总", "", "uncited_references", severity,
            f"参考文献列表中约 {len(uncited)}/{len(numbers)} 个编号未在正文检测到对应引用，需结合脚注、作者-年份引文或版式人工复核。",
            {"uncited_numbers": uncited[:100], "uncited_count": len(uncited), "reference_count": len(numbers)},
        ))
    return issues


def _bibliography_consistency(refs: list[dict]) -> list[dict]:
    issues: list[dict] = []
    seen: dict[str, int] = {}
    seen_dois: dict[str, int] = {}
    current_year = date.today().year
    for ref in refs:
        number = ref.get("number", "")
        text = ref.get("text", "")
        normalized = _normalized_text(text)
        if len(normalized) >= 12:
            if normalized in seen:
                issues.append(_issue(
                    number, text, "duplicate_reference", "medium",
                    f"参考文献 #{number} 与 #{seen[normalized]} 的书目信息完全重复。",
                    {"duplicate_of": seen[normalized]},
                ))
            else:
                seen[normalized] = number
        doi = re.search(r"10\.\d{4,9}/[^\s,;，；)）]+", text, re.IGNORECASE)
        if doi:
            doi_key = doi.group(0).rstrip(".。;；,，").lower()
            if doi_key in seen_dois:
                issues.append(_issue(
                    number, text, "duplicate_reference_doi", "low",
                    f"参考文献 #{number} 使用了与 #{seen_dois[doi_key]} 相同的 DOI。",
                    {"doi": doi_key, "duplicate_of": seen_dois[doi_key]},
                ))
            else:
                seen_dois[doi_key] = number
        years = [int(value) for value in _YEAR.findall(text)]
        if years and max(years) > current_year + 1:
            issues.append(_issue(
                number, text, "future_reference_year", "medium",
                f"参考文献 #{number} 包含晚于当前年份的发表年份 {max(years)}，需核对原始书目信息。",
                {"years": years},
            ))
    return issues


def _text_repetition(text: str) -> list[dict]:
    """Find substantial repeated paragraphs within one paper, conservatively."""
    start = _reference_section_start(text)
    body = text[:start] if start else text
    paragraphs = []
    for raw in re.split(r"\n\s*\n+", body):
        value = re.sub(r"\s+", " ", raw).strip()
        normalized = _normalized_text(value)
        if len(normalized) >= 160:
            paragraphs.append((value, normalized))
            if len(paragraphs) >= 300:
                break
    issues = []
    reported: set[tuple[int, int]] = set()
    for i, (left, left_norm) in enumerate(paragraphs):
        for j in range(i + 1, len(paragraphs)):
            right, right_norm = paragraphs[j]
            if len(left_norm) < 160 or len(right_norm) < 160:
                continue
            if abs(len(left_norm) - len(right_norm)) / max(len(left_norm), len(right_norm)) > 0.15:
                continue
            if left_norm[:32] != right_norm[:32]:
                continue
            ratio = SequenceMatcher(None, left_norm[:1200], right_norm[:1200]).ratio()
            if ratio >= 0.94 and (i, j) not in reported:
                reported.add((i, j))
                issues.append({
                    "test": "text_repetition",
                    "severity": "medium",
                    "location": f"正文段落 {i + 1} ↔ {j + 1}",
                    "description": "检测到两段较长正文高度重复，可能是章节复用、编辑残留或未充分改写，需人工核对引用和写作来源。",
                    "details": {"similarity": round(ratio, 3), "length_a": len(left_norm), "length_b": len(right_norm)},
                })
                if len(issues) >= 20:
                    return issues
    return issues


def _review_method_check(text: str) -> list[dict]:
    lowered = text.lower()
    if not any(marker.lower() in lowered for marker in _REVIEW_MARKERS):
        return []
    present = [name for name, markers in _METHOD_MARKERS if any(marker.lower() in lowered for marker in markers)]
    if len(present) >= 3:
        return []
    return [{
        "ref_number": "综述方法",
        "ref_text": "",
        "issue_type": "review_method_incomplete",
        "severity": "low",
        "details": {"detected_components": present, "expected_components": [name for name, _ in _METHOD_MARKERS]},
        "description": "论文具有综述特征，但正文中未充分检测到数据库、检索范围、检索词或纳入排除标准等方法信息；叙述性综述可能不适用，建议人工判断。",
    }]


def check_humanities_integrity(text: str, refs: list[dict], *, skip_references: bool = False) -> dict:
    """Return conservative reference and text findings for any language."""
    reference_issues = [] if skip_references else (
        _reference_consistency(text, refs) + _bibliography_consistency(refs) + _review_method_check(text)
    )
    return {
        "reference_issues": reference_issues,
        "text_issues": _text_repetition(text),
    }
