"""Deterministic consistency checks spanning main and supplementary PDFs."""
from __future__ import annotations

import math
import re
from pathlib import Path

import fitz


_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")
_TABLE_CAPTION = re.compile(r"(?i)Table\s*S?\d+[A-Za-z]?\.")
_MEASUREMENT_CONTEXT = re.compile(
    r"(?i)(?:signal|response|measurement|change|difference|voltage|current|"
    r"intensity|concentration|\u0394|delta)"
)


def _clean_text(value: str) -> str:
    value = str(value or "").replace("\xad", "")
    value = re.sub(r"[\u2000-\u200b\u202f\u00a0]", " ", value)
    return re.sub(r"[ \t]+", " ", value)


def _pdf_pages(path: str | Path) -> list[str]:
    document = fitz.open(str(path))
    try:
        return [_clean_text(page.get_text("text", sort=True)) for page in document]
    finally:
        document.close()


def _table_sections(page_texts: list[str]) -> list[dict]:
    joined = "\n".join(
        f"\n[PAGE {page_number}]\n{text}"
        for page_number, text in enumerate(page_texts, 1)
    )
    matches = list(_TABLE_CAPTION.finditer(joined))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(joined)
        text = joined[match.start():end]
        caption = text.splitlines()[0].strip()
        pages = sorted({int(value) for value in re.findall(r"\[PAGE (\d+)\]", text)})
        sections.append({"caption": caption, "text": text, "pages": pages})
    return sections


def _area_rows(supplement_text: str) -> tuple[str, dict[str, float]]:
    normalized = _clean_text(supplement_text)
    start = re.search(
        r"(?i)Table\s*S?\d+[A-Za-z]?\..{0,180}?\barea\s*\(m(?:2|\u00b2)\)",
        normalized,
        re.DOTALL,
    )
    if not start:
        return "", {}
    following = normalized[start.start():]
    next_table = re.search(r"(?i)\n\s*Table\s*S?\d+[A-Za-z]?\.", following[20:])
    if next_table:
        following = following[:20 + next_table.start()]
    caption = following.splitlines()[0].strip()
    rows = {}
    for line in following.splitlines()[1:]:
        match = re.match(
            r"^\s*([A-Za-z][A-Za-z ()'\u2019&.\-]{1,70}?)\s+"
            r"([-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?)\s*$",
            line,
        )
        if not match:
            continue
        entity = re.sub(r"\s+", " ", match.group(1)).strip()
        if entity.lower() in {"country or region", "area"}:
            continue
        try:
            rows[entity] = float(match.group(2))
        except ValueError:
            continue
    return caption, rows


def _check_capacity_share_consistency(main_text: str, supplement_text: str, source: str) -> list[dict]:
    """Compare reported entity shares with a supplementary area table and density."""
    main = _clean_text(main_text)
    caption, areas = _area_rows(supplement_text)
    if len(areas) < 5:
        return []

    density_match = re.search(
        r"(?i)(?:rated\s+)?power\s+density.{0,100}?(\d+(?:\.\d+)?)\s*W\s*/\s*m(?:2|\u00b2)",
        main,
        re.DOTALL,
    )
    total_matches = list(re.finditer(
        r"(?i)total.{0,180}?(\d+(?:\.\d+)?)\s*GW",
        main,
        re.DOTALL,
    ))
    if not density_match or not total_matches:
        return []
    density = float(density_match.group(1))
    # Repeated abstract/highlight values are expected; the modal value is the paper total.
    totals = [round(float(match.group(1)), 8) for match in total_matches]
    total_gw = max(set(totals), key=totals.count)
    if density <= 0 or total_gw <= 0:
        return []
    share_context = "\n".join(
        main[max(0, match.start() - 200):min(len(main), match.end() + 900)]
        for match in total_matches
        if math.isclose(float(match.group(1)), total_gw, rel_tol=0, abs_tol=1e-8)
    )

    comparisons = []
    for entity, observed_area in areas.items():
        share_match = re.search(
            rf"(?i)\b{re.escape(entity)}\s*\(\s*(\d+(?:\.\d+)?)\s*%\s*\)",
            share_context,
        )
        if not share_match:
            continue
        share = float(share_match.group(1))
        expected_area = total_gw * 1_000_000_000 * (share / 100.0) / density
        if expected_area <= 0:
            continue
        relative_error = abs(observed_area - expected_area) / expected_area
        comparisons.append({
            "entity": entity,
            "share_percent": share,
            "observed_area_m2": observed_area,
            "expected_area_m2": expected_area,
            "relative_error": relative_error,
        })
    consistent = [item for item in comparisons if item["relative_error"] <= 0.20]
    mismatched = [item for item in comparisons if item["relative_error"] >= 0.50]
    if len(comparisons) < 3 or len(consistent) < 2:
        return []

    findings = []
    for item in mismatched:
        duplicate_entities = sorted(
            entity for entity, area in areas.items()
            if entity != item["entity"] and math.isclose(area, item["observed_area_m2"], rel_tol=0, abs_tol=1e-9)
        )
        observed_capacity = item["observed_area_m2"] * density / 1_000_000_000
        expected_capacity = total_gw * item["share_percent"] / 100.0
        duplicate_note = (
            f"；该面积还与 {', '.join(duplicate_entities[:3])} 完全相同"
            if duplicate_entities else ""
        )
        findings.append({
            "test": "cross_document_quantitative_mismatch",
            "severity": "high",
            "location": f"{Path(source).name} / {caption} / {item['entity']}",
            "description": (
                f"正文给出 {item['entity']} 占总装机 {item['share_percent']:.2f}%（总量"
                f" {total_gw:g} GW），按文中 {density:g} W/m\u00b2 换算应约为"
                f" {item['expected_area_m2'] / 1_000_000:.3f} km\u00b2；补充表登记为"
                f" {item['observed_area_m2'] / 1_000_000:.4f} km\u00b2，对应"
                f" {observed_capacity:.4f} GW，而正文份额对应 {expected_capacity:.4f} GW"
                f"{duplicate_note}。"
            ),
            "details": {
                **item,
                "total_capacity_gw": total_gw,
                "power_density_w_m2": density,
                "observed_capacity_gw": observed_capacity,
                "expected_capacity_gw": expected_capacity,
                "duplicate_entities": duplicate_entities,
                "consistent_peer_count": len(consistent),
            },
        })
    return findings


def _numeric_rows(section_text: str) -> list[dict]:
    rows = []
    page_number = None
    group_label = ""
    for line in section_text.splitlines():
        page = re.fullmatch(r"\[PAGE (\d+)\]", line.strip())
        if page:
            page_number = int(page.group(1))
            continue
        values = [float(value) for value in _NUMBER.findall(line)]
        if len(values) < 3:
            if re.search(r"(?i)\bNO\.?\s*\d+\b", line):
                group_label = re.sub(r"\s+", " ", line).strip()
            continue
        values = values[-3:]
        if not all(math.isfinite(value) for value in values):
            continue
        rows.append({
            "page": page_number,
            "values": values,
            "line": line.strip(),
            "group": group_label,
        })
    return rows


def _check_sign_polarity_blocks(page_texts: list[str], source: str) -> list[dict]:
    findings = []
    for section in _table_sections(page_texts):
        if not _MEASUREMENT_CONTEXT.search(section["text"]):
            continue
        rows = _numeric_rows(section["text"])
        if len(rows) < 20:
            continue
        flat = [value for row in rows for value in row["values"] if abs(value) > 1e-12]
        if len(flat) < 60:
            continue
        positive = sum(value > 0 for value in flat)
        negative = sum(value < 0 for value in flat)
        dominant_positive = positive >= negative
        dominant_count = max(positive, negative)
        if dominant_count / len(flat) < 0.85:
            continue

        def is_opposite(row: dict) -> bool:
            values = row["values"]
            return all(value < 0 for value in values) if dominant_positive else all(value > 0 for value in values)

        runs = []
        start = None
        for index, row in enumerate(rows + [{"values": []}]):
            opposite = index < len(rows) and is_opposite(row)
            if opposite and start is None:
                start = index
            elif not opposite and start is not None:
                runs.append((start, index - 1))
                start = None
        for start, end in runs:
            if end - start + 1 < 3:
                continue
            block = rows[start:end + 1]
            block_values = [value for row in block for value in row["values"]]
            pages = sorted({row["page"] for row in block if row["page"]})
            dominant_label = "positive" if dominant_positive else "negative"
            minority_label = "negative" if dominant_positive else "positive"
            dominant_cn = "正值" if dominant_positive else "负值"
            minority_cn = "负值" if dominant_positive else "正值"
            groups = sorted({row.get("group", "") for row in block if row.get("group")})
            group_note = f"（{groups[0]}）" if len(groups) == 1 else ""
            findings.append({
                "test": "group_sign_polarity_outlier",
                "severity": "high",
                "location": (
                    f"{Path(source).name} / {section['caption']} / "
                    f"PDF pages {','.join(map(str, pages)) or '?'}"
                ),
                "description": (
                    f"该测量表共提取 {len(flat)} 个非零值，其中 {dominant_count}/{len(flat)} 为"
                    f"{dominant_cn}；但连续 {len(block)} 行{group_note}共 {len(block_values)} 个值全部为"
                    f"{minority_cn}，范围 {min(block_values):g} 至 {max(block_values):g}，"
                    "形成孤立的整组符号反转。"
                ),
                "details": {
                    "total_values": len(flat),
                    "dominant_sign": dominant_label,
                    "dominant_count": dominant_count,
                    "block_sign": minority_label,
                    "block_rows": len(block),
                    "block_values": len(block_values),
                    "block_min": min(block_values),
                    "block_max": max(block_values),
                    "sample": block_values[:9],
                    "groups": groups,
                    "row_text_sample": [row["line"] for row in block[:5]],
                    "pages": pages,
                },
            })
    return findings


def check_supplementary_consistency(
    main_pdf: str | Path, supplementary_pdfs: list[str | Path]
) -> list[dict]:
    main_text = "\n".join(_pdf_pages(main_pdf))
    findings = []
    for source in supplementary_pdfs:
        try:
            pages = _pdf_pages(source)
        except Exception:
            continue
        supplement_text = "\n".join(pages)
        findings.extend(_check_capacity_share_consistency(main_text, supplement_text, str(source)))
        findings.extend(_check_sign_polarity_blocks(pages, str(source)))
    # Some deliveries contain both a supplement-only PDF and a combined article
    # package with the same table.  Report the scientific contradiction once.
    unique = []
    signatures = set()
    for finding in findings:
        details = finding.get("details", {})
        signature = (
            finding.get("test"),
            details.get("entity"),
            details.get("observed_area_m2"),
            details.get("expected_area_m2"),
            details.get("total_values"),
            details.get("dominant_count"),
            details.get("block_sign"),
            details.get("block_rows"),
            tuple(details.get("sample", [])),
        )
        if signature in signatures:
            continue
        signatures.add(signature)
        unique.append(finding)
    return unique
