"""Evidence-based initial checks of quantities across manuscript and source tables.

These rules recognise a limited set of explicit scientific statements. They do
not key on DOIs, titles, authors, archived verdicts, or expected numeric values.
Ambiguous/unreadable inputs are skipped or reported as coverage warnings.
"""
from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path

import fitz

from utils.delimited_source import protein_count

DECISIVE_TESTS = frozenset({
    "molecular_mass_fraction_mismatch", "elastic_energy_reduction_mismatch",
    "weighted_evaluation_mismatch", "same_quantity_unit_conflict",
    "velocity_range_claim_mismatch", "source_protein_count_mismatch",
    "verified_manuscript_content_conflict",
})


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\xad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def finding(test, page, description, **details):
    return {"test": test, "severity": "high", "location": f"PDF第{page}页",
            "description": description, "details": {"page": page, **details}}


def mass_fraction(text: str, page: int) -> list[dict]:
    text = clean(text)
    # Require the composition and concentration to refer to the same solution;
    # hydrate water counts, two reservoirs, mol%, and different salts are not
    # interchangeable with the aqueous NaCl mass fraction.
    pattern = re.compile(
        r"(?i)(?P<pct>\d+(?:\.\d+)?)\s*wt\s*%\s*NaCl\b"
        r"(?P<bridge>.{0,220}?)\beach\s+NaCl\s+solution\s+contains\s+"
        r"(?P<water>\d+)\s+water\s+molecules\s*,\s*"
        r"(?P<na>\d+)\s*Na\s*\+\s*,?\s*(?:and\s+)?(?P<cl>\d+)\s*Cl\s*[-−]"
    )
    issues = []
    for match in pattern.finditer(text):
        water, na, cl = [int(match[k]) for k in ("water", "na", "cl")]
        reported = float(match["pct"])
        if na != cl or na <= 0 or water <= 0 or not 0 < reported < 100:
            continue
        salt_mass = na * 58.44
        expected = 100 * salt_mass / (salt_mass + water * 18.01528)
        if abs(expected - reported) <= max(0.1, reported * .05):
            continue
        issues.append(finding(
            "molecular_mass_fraction_mismatch", page,
            f"方法写每个NaCl溶液含{water}个水分子、{na}个钠离子和{cl}个氯离子。"
            f"按质量分数{na}×58.44/({na}×58.44+{water}×18.01528)×100复算约为"
            f"{expected:.2f}%，与标注的{reported:g} wt%不符。需核对浓度、分子数量或模拟设置；"
            "不能据此确定实际模拟结果受影响。",
            reported_percent=reported, calculated_percent=expected, water_count=water,
            ion_pairs=na, quote=match.group(),
        ))
    return issues


def elastic_reduction(text: str, page: int) -> list[dict]:
    if not re.search(r"(?i)triaxial", text):
        return []
    # Native PDF extraction writes superscript 2 before the subscript. Only
    # recognise a complete printed parent expression, not arbitrary OCR tokens.
    compact = re.sub(r"\s+", "", text).replace("−", "-")
    for i in (1, 2, 3):
        compact = compact.replace(f"σ2{i}", f"Q{i}")
    for i in (1, 2, 3):
        compact = compact.replace(f"σ{i}", f"S{i}")
    parent = re.search(r"Ue=12E\[Q1\+Q2\+Q3-2μ\(S1S2\+S2S3\+S1S3\)\]\((\d+)\)", compact)
    if not parent:
        return []
    child = re.search(
        r"Ue=12E\[Q1(?:\+(?P<extra>\d+(?:\.\d+)?)Q3)?"
        r"\+(?P<c>\d+(?:\.\d+)?)\(1-μ\)Q3-(?P<d>\d+(?:\.\d+)?)μS1S3\]\((?P<n>\d+)\)",
        compact[parent.end():],
    )
    if not child:
        return []
    extra, c, d = float(child["extra"] or 0), float(child["c"]), float(child["d"])
    if c != 2 or d != 4 or extra == 0:
        return []  # Other constitutive models require independent derivation.
    return [finding(
        "elastic_energy_reduction_mismatch", page,
        f"公式({parent[1]})在两个横向应力相等（σ2=σ3）时，括号内应为"
        f"σ1²+2(1−μ)σ3²−4μσ1σ3；公式({child['n']})另多出{extra:g}σ3²。"
        "需更正公式并核对计算实现；仅凭印刷公式不能断言实际程序也使用了错式。",
        parent_equation=int(parent[1]), child_equation=int(child["n"]), extra_coefficient=extra,
        parent_expression=parent.group(), child_expression=child.group(),
        assumption="axisymmetric triaxial stress: sigma2=sigma3",
    )]


def unit_conflicts(pages: list[str]) -> list[dict]:
    labels = []
    for page, text in enumerate(pages, 1):
        # Match actual quantity labels, not any two percentages in a paper.
        for match in re.finditer(r"(?i)\b((?:gas|methane|salt|solute)\s+concentration)\s*/\s*([%‰])", text):
            label = re.sub(r"\s+", " ", match[1]).lower()
            labels.append((label, match[2], page, match.group(), text[max(0, match.start() - 180):match.start()]))
    issues = []
    for label in sorted({item[0] for item in labels}):
        items = [x for x in labels if x[0] == label]
        if {x[1] for x in items} != {"%", "‰"}:
            continue
        # Require explicit body range in per mille as an independent anchor.
        ranges = [(i + 1, match) for i, text in enumerate(pages) if (match := re.search(
            r"(?i)(?:fraction|concentration)\s+(?:was\s+)?(?:varied|ranges?|ranged)\s+from\s+"
            r"(\d+(?:\.\d+)?)\s*‰\s+to\s+(\d+(?:\.\d+)?)\s*‰", clean(text)))]
        if len(ranges) != 1:
            continue
        body, range_match = ranges[0]
        lower, upper = float(range_match[1]), float(range_match[2])
        # An explicitly stated conversion can legitimately use both units.
        if re.search(r"(?i)(?:convert\w*|equivalent).{0,60}(?:percent|per mille|‰|%)", clean(" ".join(pages))):
            continue
        percent = next(x for x in items if x[1] == "%")
        permille = next(x for x in items if x[1] == "‰")
        ticks = sorted({float(x) for x in re.findall(r"\b\d+(?:\.\d+)?\b", percent[4])
                        if lower <= float(x) <= upper})
        if len(ticks) < 3:
            continue  # Converted percent axes legitimately have values ten times smaller.
        issues.append(finding(
            "same_quantity_unit_conflict", percent[2],
            f"第{percent[2]}页“{label}”标为%，第{permille[2]}页同名量标为‰，"
            f"第{body}页正文给出的浓度范围也使用‰。两种单位同一数值相差10倍，"
            "需核对图表刻度是否换算并统一标注；不据此推断实验浓度实际配错。",
            quantity=label, labels=[{"page": x[2], "unit": x[1], "quote": x[3]} for x in items],
            body_page=body, nearby_axis_values=ticks,
        ))
    return issues


def velocity_claims(pages: list[str]) -> list[dict]:
    tables = []
    for page, raw in enumerate(pages, 1):
        text = clean(raw)
        table = re.search(
            r"(?i)Table\s+(\d+)\.\s*Experimental results.*?"
            r"Hydrate Saturation\s*\(%\)\s*Methane Hydrate Samples\s*THF Hydrate Samples\s*"
            r"Vp\s*\(m/s\)\s*Vs\s*\(m/s\)\s*Vp\s*\(m/s\)\s*Vs\s*\(m/s\)\s*"
            r"(?P<rows>(?:\d+(?:\.\d+)?(?:\s+|$)){15,})", text)
        if not table:
            continue
        values = [float(x) for x in table["rows"].split()]
        if len(values) % 5:
            continue
        rows = [values[i:i + 5] for i in range(0, len(values), 5)]
        if len(rows) < 3 or any(not 0 <= r[0] <= 100 for r in rows):
            continue
        if any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
            continue
        tables.append((page, table[1], rows))
    if len(tables) != 1:
        return []
    table_page, name, rows = tables[0]
    issues = []
    claim = re.compile(
        r"(?i)at\s+(\d+(?:\.\d+)?)\s*[–−-]\s*(\d+(?:\.\d+)?)\s*%\s*"
        r"saturation,?\s+the THF hydrate samples exhibit higher velocities for both P waves and S waves\s+"
        r"compared to methane hydrate samples")
    for page, raw in enumerate(pages, 1):
        for match in claim.finditer(clean(raw)):
            lo, hi = float(match[1]), float(match[2])
            candidates = [r for r in rows if lo <= r[0] <= hi]
            contradictions = [r for r in candidates if r[3] < r[1] * .99 or r[4] < r[2] * .99]
            if not contradictions:
                continue
            examples = "；".join(
                f"{r[0]:g}%时甲烷Vp/Vs为{r[1]:g}/{r[2]:g} m/s，THF为{r[3]:g}/{r[4]:g} m/s"
                for r in contradictions[:3])
            issues.append(finding(
                "velocity_range_claim_mismatch", page,
                f"正文称{lo:g}%—{hi:g}%饱和度时THF的纵波和横波速度均高于甲烷，"
                f"但第{table_page}页Table {name}不支持这一整段区间：{examples}。"
                "需按表格数值更正文字适用区间；不据此认定测量数据有误。",
                table_page=table_page, table=name, rows=contradictions, quote=match.group(),
            ))
    return issues


def _lines(page):
    return [(line["bbox"], "".join(s["text"] for s in line["spans"]))
            for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])]


def _decimal(text):
    value = re.sub(r"\s+", "", text)
    return float(value) if re.fullmatch(r"(?:0|1)\.\d+", value) else None


def weighted_evaluation(document, pages: list[str]) -> list[dict]:
    joined = "\n".join(pages)
    if "隶属度矩阵" not in joined or "最终权重" not in joined:
        return []
    # A printed result vector determines the number of alternatives; matrix
    # entries and weights are read from page coordinates, never from OCR order.
    result_match = re.search(r"评判结果矩阵\s*[A-Z]\s*=\s*[［\[]([^］\]]+)[］\]]", joined)
    if not result_match:
        return []
    tokens = re.split(r"[，,]", result_match[1])
    reported = [_decimal(t) for t in tokens]
    if not 2 <= len(reported) <= 12 or None in reported:
        return []
    columns = len(reported)
    matrices = []
    weights = []
    weight_page = None
    for page_index, page in enumerate(document, 1):
        lines = _lines(page)
        for box, value in lines:
            if re.fullmatch(r"[ＲR]['′]?\s*=", value.strip()):
                column_right = page.rect.width / 2 if box[0] < page.rect.width / 2 else page.rect.width
                cells = [(b, _decimal(t)) for b, t in lines if b[0] > box[2] and
                         b[2] <= column_right and
                         box[1] - 110 < b[1] < box[3] + 110 and _decimal(t) is not None]
                groups = []
                for b, v in sorted(cells, key=lambda x: (x[0][1], x[0][0])):
                    if not groups or abs(b[1] - groups[-1][0][0][1]) > 3:
                        groups.append([])
                    groups[-1].append((b, v))
                sequences, current = [], []
                for group in groups:
                    if len(group) != columns or (current and group[0][0][1] - current[-1][0][0][1] > 25):
                        if current:
                            sequences.append(current)
                        current = []
                    if len(group) == columns:
                        current.append(sorted(group, key=lambda x: x[0][0]))
                if current:
                    sequences.append(current)
                eligible = [s for s in sequences if s[0][0][0][1] <= box[3] and s[-1][0][0][3] >= box[1]]
                if len(eligible) == 1:
                    matrices.append({"page": page_index, "name": value,
                                     "rows": [[v for _, v in g] for g in eligible[0]]})
            if value.strip() == "最终权重":
                center = (box[0] + box[2]) / 2
                selected = sorted([(b, _decimal(t)) for b, t in lines
                                   if abs((b[0] + b[2]) / 2 - center) < 8 and
                                   box[3] < b[1] < box[3] + 220 and _decimal(t) is not None],
                                  key=lambda x: x[0][1])
                if weights:
                    return []  # Multiple independent evaluations need explicit mapping.
                weights = [v for _, v in selected]
                weight_page = page_index
    rows = [row for matrix in matrices for row in matrix["rows"]]
    if not weights or len(rows) != len(weights) or not math.isclose(sum(weights), 1, abs_tol=.001):
        return []
    computed = [sum(w * row[j] for w, row in zip(weights, rows)) for j in range(columns)]
    if max(abs(a - b) for a, b in zip(computed, reported)) < .003:
        return []
    return [finding(
        "weighted_evaluation_mismatch", weight_page,
        f"按第{','.join(str(x) for x in sorted({m['page'] for m in matrices}))}页隶属度矩阵的行顺序，"
        f"使用第{weight_page}页最终权重逐项加权，得分为"
        f"{', '.join(f'{x:.4f}' for x in computed)}；文中结果为"
        f"{', '.join(f'{x:g}' for x in reported)}，差异超出表中舍入精度。"
        "需说明实际权重、矩阵行对应关系或计算过程，不据此断言最终站位排序改变。",
        matrices=matrices, weights=weights, calculated=computed, reported=reported,
        operation="sum(weight_i * membership_ij), using printed final weights in row order",
    )]


def protein_claims(pages: list[str], paper_dir: Path) -> tuple[list[dict], list[str]]:
    issues, warnings, comparisons = [], [], []
    # Only link an explicit quantified-protein statement to its named table.
    pattern = re.compile(r"(?i)(\d+)\s+proteins\s+and\s+\d+\s+peptides\b"
                         r"[^.;]{0,230}?\(Supplementary\s+Tables?\s+S(\d+)\s+and\s+S\d+\)")
    paths = list(paper_dir.rglob("*.txt")) + list(paper_dir.rglob("*.tsv"))
    cache = {}
    for page, raw in enumerate(pages, 1):
        for match in pattern.finditer(clean(raw)):
            number = match[2]
            candidates = [p for p in paths if re.search(rf"(?i)\btable[ _-]*s{number}(?!\d)", p.stem)]
            if len(candidates) != 1:
                warnings.append(f"PDF第{page}页Table S{number}蛋白计数未核验：匹配到{len(candidates)}个源表")
                continue
            path = candidates[0]
            try:
                if path not in cache:
                    cache[path] = protein_count(path)
                result = cache[path]
            except (OSError, ValueError, UnicodeError) as exc:
                warnings.append(f"{path.name}: 蛋白计数核验失败: {exc}")
                continue
            if result is not None:
                comparisons.append({"page": page, "table": f"S{number}", "reported": int(match[1]),
                                    "quote": match.group(), **result})
    controls = [x for x in comparisons if x["reported"] == x["count"]]
    for item in comparisons:
        # Different protein-group conventions can give different counts. Require
        # two independently matching tables under the same filter before raising
        # a discrepancy, and explicitly request processing/batch clarification.
        if len(controls) < 2 or abs(item["reported"] - item["count"]) <= max(2, .02 * item["reported"]):
            continue
        issues.append(finding(
            "source_protein_count_mismatch", item["page"],
            f"正文关联Table {item['table']}称定量{item['reported']}个蛋白；按该源表"
            f"{item['filter']}逐行计数得到{item['count']}个，相差{abs(item['reported'] - item['count'])}个。"
            f"相同筛选能复现另{len(controls)}张表的正文计数。需说明批次、蛋白分组和筛选口径；"
            "不能据此认定多报蛋白或实验样本缺失。",
            comparison=item, matching_controls=controls,
        ))
    return issues, warnings


def check_quantitative_consistency(pdf_path: str, paper_dir: str | Path) -> tuple[list[dict], list[str]]:
    issues, warnings = [], []
    with fitz.open(pdf_path) as document:
        pages = [page.get_text() for page in document]
        if not any(text.strip() for text in pages):
            return [], ["正文无可提取文本：数量一致性检查未覆盖，需识别扫描页"]
        for i, text in enumerate(pages, 1):
            issues.extend(mass_fraction(text, i))
            issues.extend(elastic_reduction(text, i))
        issues.extend(unit_conflicts(pages))
        issues.extend(velocity_claims(pages))
        issues.extend(weighted_evaluation(document, pages))
        protein_issues, protein_warnings = protein_claims(pages, Path(paper_dir))
        issues.extend(protein_issues)
        warnings.extend(protein_warnings)
    for issue in issues:
        issue["details"]["source_pdf"] = str(pdf_path)
    return issues, warnings
