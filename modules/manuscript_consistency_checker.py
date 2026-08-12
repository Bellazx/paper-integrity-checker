"""Conservative checks for contradictions inside the manuscript PDF itself."""
from __future__ import annotations

from collections import defaultdict
import re

import fitz


_FORMULA_TRANSLATION = str.maketrans({
    "−": "-", "–": "-", "—": "-", "‐": "-",
    "": "<", "": ">", "": "<=", "": ">=",
})
_OPERAND = r"[A-Za-z][A-Za-z0-9_,]*(?:\([A-Za-z0-9_,+\-]+\))?"
_NUMBER = re.compile(r"[-+]?(?:\d+(?:[.,]\d*)?|\.\d+)")
_UNIT = re.compile(
    r"(?i)(?:%|[fpnumkMGT]?W(?:h)?|[fpnumkMGT]?V|[fpnumkMGT]?A|"
    r"[fpnumkMGT]?J|Hz|mol|kg|mg|[µu]g|mm|cm|km|m2|m3|°C|℃|"
    r"Pa|bar|rpm|dB|kcal|¥|CNY|RMB|USD|EUR)"
)
_RESULT_CAPTION = re.compile(
    r"(?i)\b(?:result|configuration|performance|measurement|statistic|outcome|parameter)"
)
_SEASONS = ("spring", "summer", "autumn", "fall", "winter")
_PROPOSED_MODEL = re.compile(
    r"(?i)^(?:ours?|our\s+(?:method|model|approach)|proposed(?:\s+(?:method|model|approach))?|this\s+work)$"
)
_SCENARIO_WITH = re.compile(
    r"\b(?P<entity>[A-Z](?:\s*[A-Z]){1,3})\s+with\b[^\n;]{0,100}?"
    r"(?P<tasks>\d[\d,]*)\s+tasks?\b"
)
_MAP_DESCRIPTOR = re.compile(
    r"(?i)\b(?P<size>largest|smallest|large|middle|medium|small)\b"
    r"(?:\s+and\s+\w+){0,2}\s+(?:map|scenario|dataset)\s+"
    r"[,;:]?\s*(?P<entity>[A-Z](?:\s*[A-Z]){1,3})\b"
)


def _normalize_formula(value: str) -> str:
    value = value.translate(_FORMULA_TRANSLATION)
    value = re.sub(r"[\ue000-\uf8ff]", "", value)
    return re.sub(r"[^0-9A-Za-zΑ-ωα-ω_(),.+\-<>=]", "", value)


def _self_cancelling_operand(expression: str) -> str | None:
    """Return X for a chained constraint containing ``... < X-X < ...``."""
    pattern = re.compile(rf"(?P<operand>{_OPERAND})-(?P=operand)")
    for match in pattern.finditer(expression):
        before = expression[:match.start()]
        after = expression[match.end():]
        if re.search(r"(?:<|>)=?[^<>=]*$", before) and re.match(r"[^<>=]*(?:<|>)=?", after):
            return match.group("operand")
    return None


def _page_spans(page: fitz.Page) -> list[dict]:
    spans = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    spans.append(span)
    return spans


def _check_equations(page: fitz.Page, page_number: int) -> list[dict]:
    spans = _page_spans(page)
    issues = []
    for label in spans:
        label_text = label["text"].strip()
        label_match = re.fullmatch(r"\((\d{1,3})\)", label_text)
        if not label_match:
            continue

        lx0, ly0, _, ly1 = label["bbox"]
        midpoint = page.rect.width / 2
        column_left = page.rect.x0 + 15 if lx0 < midpoint else midpoint
        candidates = []
        for span in spans:
            x0, y0, x1, y1 = span["bbox"]
            if x0 < column_left or x1 >= lx0 - 2:
                continue
            if y1 < ly0 - 5 or y0 > ly1 + 5:
                continue
            candidates.append(span)
        candidates.sort(key=lambda item: (item["bbox"][0], item["bbox"][1]))
        expression = _normalize_formula("".join(item["text"] for item in candidates))
        operand = _self_cancelling_operand(expression)
        if not operand:
            continue
        issues.append({
            "test": "self_cancelling_constraint",
            "severity": "high",
            "location": f"PDF第{page_number}页公式({label_match.group(1)})",
            "description": (
                f"约束公式中同一项 {operand} 自身相减，差值恒为0，"
                "导致该公式无法约束所声明的相邻状态变化，需核对变量下标或时刻。"
            ),
            "details": {
                "equation_number": int(label_match.group(1)),
                "page": page_number,
                "operand": operand,
                "reconstructed_expression": expression[:300],
            },
        })
    return issues


def _word_lines(page: fitz.Page) -> list[list[tuple]]:
    lines: list[list[tuple]] = []
    for word in sorted(page.get_text("words", sort=True), key=lambda item: (item[1], item[0])):
        if not lines or abs(word[1] - lines[-1][0][1]) > 2.0:
            lines.append([word])
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda item: item[0])
    return lines


def _cluster_words(words: list[tuple], gap: float = 10.0) -> list[list[tuple]]:
    clusters: list[list[tuple]] = []
    for word in words:
        if not clusters or word[0] - clusters[-1][-1][2] > gap:
            clusters.append([word])
        else:
            clusters[-1].append(word)
    return clusters


def _cluster_text(cluster: list[tuple]) -> str:
    return " ".join(word[4] for word in cluster).strip()


def _plain_text(value: str) -> str:
    value = str(value or "").translate(_FORMULA_TRANSLATION)
    value = re.sub(r"(?<=\w)-\s+(?=\w)", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _cell_lines(value) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _numbers(value) -> list[float]:
    return [float(item.replace(",", "")) for item in _NUMBER.findall(str(value or ""))]


def _entity(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _table_name_from_page(page: fitz.Page, bbox: tuple, fallback: str) -> str:
    x0, y0, x1, _ = bbox
    words = [
        word for word in page.get_text("words", sort=True)
        if y0 - 32 <= word[1] < y0 and word[2] >= x0 - 30 and word[0] <= x1 + 30
    ]
    caption = " ".join(word[4] for word in words)
    match = re.search(r"(?i)\bTABLE\s+([IVXLCDM]+|\d+)\b", caption)
    return f"Table {match.group(1)}" if match else fallback


def _extract_pdf_tables(document: fitz.Document) -> list[dict]:
    tables = []
    for page_index, page in enumerate(document):
        try:
            found = page.find_tables()
        except Exception:
            continue
        for table_index, table in enumerate(found.tables, 1):
            rows = table.extract()
            if not rows:
                continue
            tables.append({
                "page": page_index + 1,
                "name": _table_name_from_page(page, table.bbox, f"结果表{table_index}"),
                "rows": rows,
            })
    return tables


def _page_context(document: fitz.Document, page_number: int, include_previous: bool = False) -> str:
    indexes = [page_number - 1]
    if include_previous and page_number > 1:
        indexes.insert(0, page_number - 2)
    blocks = []
    for index in indexes:
        blocks.extend(block[4] for block in document[index].get_text("blocks", sort=True) if block[4].strip())
    return _plain_text("\n".join(blocks))


def _metric_names(value: str) -> list[str]:
    ignored = {"HORIZON", "CLASSIC", "OURS", "MODEL", "MODELS"}
    result = []
    for name in re.findall(r"\b[A-Za-z][A-Za-z0-9]{1,15}\b", str(value or "")):
        upper = name.upper()
        if upper not in ignored and upper not in result:
            result.append(upper)
    return result


def _model_comparison_values(rows: list[list]) -> dict[int, dict[str, list[dict]]]:
    """Parse wide benchmark tables: dataset/model rows x horizon/metric columns."""
    if len(rows) < 3 or len(rows[0]) < 3:
        return {}
    horizons = {}
    for column, header in enumerate(rows[0]):
        match = re.search(r"(?i)horizon\s*(\d+)", _plain_text(header))
        if match:
            horizons[column] = int(match.group(1))
    if not horizons:
        return {}

    parsed: dict[int, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows[2:]:
        if len(row) < 2:
            continue
        dataset = _plain_text(row[0])
        models = _cell_lines(row[1])
        proposed = next((i for i, model in enumerate(models) if _PROPOSED_MODEL.fullmatch(_plain_text(model))), None)
        if not dataset or proposed is None or len(models) < 2:
            continue
        for column, horizon in horizons.items():
            if column >= len(row) or column >= len(rows[1]):
                continue
            metrics = _metric_names(rows[1][column])
            value_lines = _cell_lines(row[column])
            if not metrics or len(value_lines) != len(models):
                continue
            matrix = [_numbers(line) for line in value_lines]
            if any(len(values) < len(metrics) for values in matrix):
                continue
            for metric_index, metric in enumerate(metrics):
                ours = matrix[proposed][metric_index]
                competitor_values = [
                    (models[i], values[metric_index])
                    for i, values in enumerate(matrix) if i != proposed
                ]
                lower_is_better = "↑" not in str(rows[1][column])
                best_model, baseline = (min if lower_is_better else max)(
                    competitor_values, key=lambda item: item[1]
                )
                if baseline == 0:
                    continue
                improvement = (
                    (baseline - ours) / abs(baseline) * 100
                    if lower_is_better else
                    (ours - baseline) / abs(baseline) * 100
                )
                parsed[horizon][metric].append({
                    "dataset": dataset,
                    "ours": ours,
                    "baseline": baseline,
                    "baseline_model": best_model,
                    "improvement": improvement,
                })
    return {h: dict(metrics) for h, metrics in parsed.items()}


def _check_model_comparison_claims(table: dict, context: str) -> list[dict]:
    parsed = _model_comparison_values(table["rows"])
    if not parsed:
        return []
    context = _plain_text(context)
    issues = []
    for claim in re.finditer(r"(?i)\b(?:improvements?|gains?)\s+of\s+up\s+to\b", context):
        before = context[max(0, claim.start() - 220):claim.start()]
        horizon_mentions = list(re.finditer(
            r"(?i)\bhorizons?\s*(?:of\s*)?(\d+)(?:\s*(?:and|,)\s*(\d+))?", before
        ))
        if not horizon_mentions:
            continue
        mention = horizon_mentions[-1]
        if mention.group(2):
            continue
        horizon = int(mention.group(1))
        metrics = parsed.get(horizon)
        if not metrics:
            continue
        claimed_values = [
            float(value) for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", context[claim.end():claim.end() + 130])
        ]
        metric_order = list(metrics)
        if len(claimed_values) < len(metric_order):
            continue
        mismatches = []
        for metric, claimed in zip(metric_order, claimed_values):
            calculations = metrics[metric]
            actual_max = max(item["improvement"] for item in calculations)
            tolerance = max(0.2, abs(claimed) * 0.10)
            if abs(claimed - actual_max) <= tolerance:
                continue
            mismatches.append({
                "metric": metric,
                "claimed_percent": claimed,
                "actual_max_percent": round(actual_max, 4),
                "calculations": [
                    {
                        **item,
                        "improvement": round(item["improvement"], 4),
                    }
                    for item in calculations
                ],
            })
        if not mismatches:
            continue
        detail = mismatches[0]
        worse = [item for item in detail["calculations"] if item["improvement"] < 0]
        worse_text = ""
        if worse:
            item = min(worse, key=lambda value: value["improvement"])
            worse_text = (
                f"；其中{item['dataset']}数据集本文结果相对最佳对照"
                f"反而变化{item['improvement']:.2f}%"
            )
        issues.append({
            "test": "quantitative_claim_mismatch",
            "severity": "high",
            "location": f"PDF第{table['page']}页{table['name']}及其结果正文",
            "description": (
                f"正文声称Horizon={horizon}时{detail['metric']}最高提升"
                f"{detail['claimed_percent']:.2f}%，但按表中本文方法与各数据集最佳对照逐项复算，"
                f"最大仅为{detail['actual_max_percent']:.2f}%{worse_text}。"
            ),
            "details": {
                "page": table["page"],
                "table": table["name"],
                "horizon": horizon,
                "mismatched_metrics": mismatches,
                "formula": "lower-is-better: (best competitor - proposed) / best competitor * 100%",
            },
        })
    return issues


def _scenario_facts(table: dict) -> tuple[list[dict], list[dict]]:
    facts = []
    descriptors = []
    for row in table["rows"]:
        for cell in row:
            text = _plain_text(cell)
            for match in _SCENARIO_WITH.finditer(text):
                facts.append({
                    "entity": _entity(match.group("entity")),
                    "attribute": "tasks",
                    "value": int(match.group("tasks").replace(",", "")),
                    "table": table["name"],
                    "page": table["page"],
                    "source": text,
                })
            for match in _MAP_DESCRIPTOR.finditer(text):
                descriptors.append({
                    "entity": _entity(match.group("entity")),
                    "size": match.group("size").lower(),
                    "table": table["name"],
                    "page": table["page"],
                    "source": text,
                })

    for row_index, row in enumerate(table["rows"][:-1]):
        for column, cell in enumerate(row):
            attributes = [item.lower() for item in _metric_names(cell)]
            if "tasks" not in attributes:
                continue
            data_row = table["rows"][row_index + 1]
            if column == 0 or column >= len(data_row):
                continue
            label_lines = _cell_lines(data_row[column - 1])
            value_lines = _cell_lines(data_row[column])
            for label, value_line in zip(label_lines, value_lines):
                descriptor = _MAP_DESCRIPTOR.search(_plain_text(label))
                values = _numbers(value_line)
                if not descriptor or len(values) < len(attributes):
                    continue
                entity = _entity(descriptor.group("entity"))
                task_value = int(values[attributes.index("tasks")])
                facts.append({
                    "entity": entity,
                    "attribute": "tasks",
                    "value": task_value,
                    "table": table["name"],
                    "page": table["page"],
                    "source": f"{label}: {value_line}",
                })
                descriptors.append({
                    "entity": entity,
                    "size": descriptor.group("size").lower(),
                    "table": table["name"],
                    "page": table["page"],
                    "source": label,
                })
    return facts, descriptors


def _size_class(value: str) -> str:
    return {"largest": "large", "smallest": "small", "medium": "middle"}.get(value, value)


def _check_scenario_consistency(tables: list[dict], page_contexts: dict[int, str]) -> list[dict]:
    facts = []
    table_descriptors = []
    for table in tables:
        table_facts, descriptors = _scenario_facts(table)
        facts.extend(table_facts)
        table_descriptors.extend(descriptors)

    conflicts = []
    grouped = defaultdict(list)
    for fact in facts:
        grouped[(fact["entity"], fact["attribute"])].append(fact)
    for (entity, attribute), entries in grouped.items():
        if len({entry["value"] for entry in entries}) > 1:
            conflicts.append({"entity": entity, "attribute": attribute, "entries": entries})

    descriptor_conflicts = []
    definitions = defaultdict(list)
    for descriptor in table_descriptors:
        definitions[descriptor["entity"]].append(descriptor)
    for page, context in page_contexts.items():
        for match in _MAP_DESCRIPTOR.finditer(_plain_text(context)):
            entity = _entity(match.group("entity"))
            prose_size = _size_class(match.group("size").lower())
            table_sizes = {_size_class(item["size"]) for item in definitions.get(entity, [])}
            if table_sizes and prose_size not in table_sizes:
                descriptor_conflicts.append({
                    "entity": entity,
                    "prose_size": prose_size,
                    "table_sizes": sorted(table_sizes),
                    "page": page,
                    "source": match.group(0),
                })

    if not conflicts and not descriptor_conflicts:
        return []
    pages = sorted({entry["page"] for conflict in conflicts for entry in conflict["entries"]})
    pages.extend(item["page"] for item in descriptor_conflicts)
    pages = sorted(set(pages))
    conflict_text = []
    for conflict in conflicts:
        values = "/".join(f"{item['table']}={item['value']}" for item in conflict["entries"])
        conflict_text.append(f"{conflict['entity']}的{conflict['attribute']}在{values}")
    for conflict in descriptor_conflicts:
        conflict_text.append(
            f"正文将{conflict['entity']}称为{conflict['prose_size']}，表中定义为"
            f"{'/'.join(conflict['table_sizes'])}"
        )
    return [{
        "test": "scenario_definition_conflict",
        "severity": "high" if conflicts else "medium",
        "location": f"PDF第{'、'.join(map(str, pages))}页场景定义与结果表",
        "description": "同一场景在稿件不同位置的规模定义不一致：" + "；".join(conflict_text) + "。",
        "details": {
            "pages": pages,
            "numeric_conflicts": conflicts,
            "descriptor_conflicts": descriptor_conflicts,
        },
    }]


def _benchmark_values(rows: list[list]) -> dict[str, dict[str, dict]]:
    """Parse paired baseline/proposed scenario columns from operational result tables."""
    if len(rows) < 4:
        return {}
    scenarios = {}
    for column, cell in enumerate(rows[0]):
        match = re.search(r"\b([A-Z](?:\s*[A-Z]){1,3})\s+with\b", _plain_text(cell))
        if not match or column + 1 >= len(rows[0]):
            continue
        scenarios[column] = _entity(match.group(1))
    if not scenarios:
        return {}
    result = defaultdict(dict)
    for row in rows[2:]:
        if len(row) < 2:
            continue
        metric_names = _metric_names(row[1])
        if not metric_names:
            continue
        metric = metric_names[0]
        lower_is_better = "↑" not in str(row[1])
        for column, scenario in scenarios.items():
            if column + 1 >= len(row):
                continue
            baseline_values = _numbers(row[column])
            proposed_values = _numbers(row[column + 1])
            if not proposed_values:
                continue
            baseline = baseline_values[0] if baseline_values else None
            proposed = proposed_values[-1]
            improvement = None
            if baseline not in (None, 0):
                improvement = (
                    (baseline - proposed) / abs(baseline) * 100
                    if lower_is_better else
                    (proposed - baseline) / abs(baseline) * 100
                )
            result[metric][scenario] = {
                "baseline": baseline,
                "longest_horizon_value": proposed,
                "improvement": improvement,
                "all_proposed_values": proposed_values,
            }
    return dict(result)


def _check_benchmark_summary_claims(table: dict, context: str) -> list[dict]:
    values = _benchmark_values(table["rows"])
    if not values:
        return []
    context = _plain_text(context)
    discrepancies = []

    range_pattern = re.compile(
        r"(?i)(?:achiev\w*|show\w*|yield\w*)\s+(\d+(?:\.\d+)?)\s*-\s*"
        r"(\d+(?:\.\d+)?)\s*%\s+improvements?\s+in\s+([^.;]{1,100})"
    )
    for match in range_pattern.finditer(context):
        low, high = float(match.group(1)), float(match.group(2))
        claimed_metrics = [metric for metric in values if re.search(rf"\b{re.escape(metric)}\b", match.group(3), re.I)]
        actual = []
        outside = []
        for metric in claimed_metrics:
            for scenario, item in values[metric].items():
                if item["improvement"] is None:
                    continue
                entry = {"metric": metric, "scenario": scenario, "improvement": round(item["improvement"], 4)}
                actual.append(entry)
                if item["improvement"] < low - 0.25 or item["improvement"] > high + 0.25:
                    outside.append(entry)
        if actual and outside:
            discrepancies.append({
                "type": "improvement_range",
                "claimed_range": [low, high],
                "actual_longest_horizon_improvements": actual,
                "outside_range": outside,
            })

    for metric, scenarios in values.items():
        threshold_pattern = re.compile(
            rf"(?i)\b{re.escape(metric)}\b.{{0,120}}?(?:remains?\s+)?(?:at\s+or\s+)?"
            r"(?:below|under|less\s+than)\s+(?:about|approximately|around)?\s*(\d+(?:\.\d+)?)\s*%"
        )
        for match in threshold_pattern.finditer(context):
            threshold = float(match.group(1))
            observed = [
                {"scenario": scenario, "value": value}
                for scenario, item in scenarios.items()
                for value in item["all_proposed_values"]
            ]
            maximum = max(item["value"] for item in observed)
            tolerance = max(0.05, threshold * 0.10)
            if maximum > threshold + tolerance:
                discrepancies.append({
                    "type": "upper_bound",
                    "metric": metric,
                    "claimed_upper_percent": threshold,
                    "actual_max_percent": maximum,
                    "observed_values": observed,
                })
            break

    if not discrepancies:
        return []
    range_issue = next((item for item in discrepancies if item["type"] == "improvement_range"), None)
    bound_issue = next((item for item in discrepancies if item["type"] == "upper_bound"), None)
    parts = []
    if range_issue:
        actual_text = ", ".join(
            f"{item['metric']}-{item['scenario']}={item['improvement']:.2f}%"
            for item in range_issue["actual_longest_horizon_improvements"]
        )
        parts.append(
            f"正文概括为{range_issue['claimed_range'][0]:g}%–{range_issue['claimed_range'][1]:g}%提升，"
            f"但最长预测步长逐项复算为{actual_text}"
        )
    if bound_issue:
        metric_label = (
            "MTR（Misguided Trip Ratio）"
            if bound_issue["metric"] == "MTR" else bound_issue["metric"]
        )
        parts.append(
            f"正文称{metric_label}不高于约{bound_issue['claimed_upper_percent']:g}%，"
            f"表中最大值为{bound_issue['actual_max_percent']:.2f}%"
        )
    return [{
        "test": "quantitative_summary_mismatch",
        "severity": "high",
        "location": f"PDF第{table['page']}页{table['name']}及结果总结正文",
        "description": "收益与代价的总结超出表中数据支持范围：" + "；".join(parts) + "。",
        "details": {
            "page": table["page"],
            "table": table["name"],
            "discrepancies": discrepancies,
            "formula": "lower-is-better: (baseline - proposed) / baseline * 100%",
        },
    }]


def _check_result_tables(page: fitz.Page, page_number: int) -> list[dict]:
    lines = _word_lines(page)
    issues = []
    for index, raw_header_line in enumerate(lines):
        parameter_word = next(
            (word for word in raw_header_line if word[4].strip().lower() in {"parameter", "parameters"}),
            None,
        )
        if parameter_word is None:
            continue
        left_column = (parameter_word[0] + parameter_word[2]) / 2 < page.rect.width / 2
        header_line = [
            word for word in raw_header_line
            if ((word[0] + word[2]) / 2 < page.rect.width / 2) == left_column
        ]
        header_clusters = _cluster_words(header_line)
        if not header_clusters or _cluster_text(header_clusters[0]).lower() not in {"parameter", "parameters"}:
            continue
        columns = header_clusters[1:]
        if len(columns) < 3:
            continue

        header_y = min(word[1] for word in header_line)
        value_clusters = None
        value_line = None
        for raw_candidate in lines[index + 1:index + 6]:
            candidate = [
                word for word in raw_candidate
                if ((word[0] + word[2]) / 2 < page.rect.width / 2) == left_column
            ]
            if not candidate:
                continue
            candidate_y = min(word[1] for word in candidate)
            if candidate_y - header_y > 45:
                break
            clusters = _cluster_words(candidate)
            if clusters and _cluster_text(clusters[0]).lower() in {"value", "values"}:
                value_clusters = clusters
                value_line = candidate
                break
        if not value_clusters or value_line is None:
            continue

        centers = [sum((word[0] + word[2]) / 2 for word in col) / len(col) for col in columns]
        cells: list[list[tuple]] = [[] for _ in columns]
        first_value_end = value_clusters[0][-1][2]
        for word in value_line:
            if word[0] <= first_value_end:
                continue
            center = (word[0] + word[2]) / 2
            nearest = min(range(len(centers)), key=lambda pos: abs(centers[pos] - center))
            cells[nearest].append(word)

        cell_texts = [_cluster_text(cell) if cell else "" for cell in cells]
        numeric_count = sum(bool(_NUMBER.search(text)) for text in cell_texts)
        if numeric_count < 2:
            continue

        caption_words = [
            word for line in lines for word in line
            if header_y - 35 <= word[1] < header_y
            and (((word[0] + word[2]) / 2 < page.rect.width / 2) == left_column)
        ]
        caption_words.sort(key=lambda word: (round(word[1], 1), word[0]))
        caption = " ".join(word[4] for word in caption_words)
        table_match = re.search(r"(?i)TABLE\s+([IVXLCDM]+|\d+)", caption)
        table_name = f"Table {table_match.group(1)}" if table_match else "结果表"

        for column, cell_text in zip(columns, cell_texts):
            if _NUMBER.search(cell_text) or not _UNIT.search(cell_text):
                continue
            parameter = _cluster_text(column)
            severity = "high" if _RESULT_CAPTION.search(caption) else "medium"
            issues.append({
                "test": "table_numeric_value_missing",
                "severity": severity,
                "location": f"PDF第{page_number}页{table_name}，参数{parameter}",
                "description": (
                    f"结果表中参数 {parameter} 的值单元格仅出现单位“{cell_text}”，"
                    "未检测到数值；同一行其他参数具有完整数值，疑似核心结果漏排或缺失。"
                ),
                "details": {
                    "page": page_number,
                    "table": table_name,
                    "parameter": parameter,
                    "cell_text": cell_text,
                    "caption_context": caption[:300],
                },
            })
    return issues


def _check_figure_condition_order(text: str) -> list[dict]:
    captions: dict[int, str] = {}
    caption_re = re.compile(r"(?im)^\s*Fig(?:ure)?\.?\s*(\d+)\.?\s*([^\n]{0,160})")
    for match in caption_re.finditer(text):
        lowered = match.group(2).lower()
        season = next((item for item in _SEASONS if re.search(rf"\b{item}\b", lowered)), None)
        if season:
            captions[int(match.group(1))] = "autumn" if season == "fall" else season

    issues = []
    pair_re = re.compile(
        r"(?i)Fig(?:ure)?\.?\s*(\d+)\s*(?:,|and|&|以及|和)\s*"
        r"Fig(?:ure)?\.?\s*(\d+)"
    )
    for match in pair_re.finditer(text):
        first, second = int(match.group(1)), int(match.group(2))
        caption_order = [captions.get(first), captions.get(second)]
        if None in caption_order or caption_order[0] == caption_order[1]:
            continue
        context = text[match.end():match.end() + 1400].lower()
        positions = {}
        for season in set(caption_order):
            aliases = (season, "fall") if season == "autumn" else (season,)
            found = [context.find(alias) for alias in aliases if context.find(alias) >= 0]
            if found:
                positions[season] = min(found)
        if len(positions) != 2:
            continue
        prose_order = sorted(positions, key=positions.get)
        if prose_order == caption_order:
            continue
        issues.append({
            "test": "figure_condition_order_mismatch",
            "severity": "medium",
            "location": f"Fig. {first}与Fig. {second}",
            "description": (
                f"图注依次标为 {caption_order[0]}/{caption_order[1]}，但紧随两图引用的正文"
                f"依次描述 {prose_order[0]}/{prose_order[1]} 条件，顺序相反，需核对图号或条件标签。"
            ),
            "details": {
                "figures": [first, second],
                "caption_order": caption_order,
                "prose_order": prose_order,
            },
        })
    return issues


def check_manuscript_consistency(pdf_path: str, text: str = "") -> list[dict]:
    """Check equations, result tables, quantitative claims, and label consistency."""
    document = fitz.open(pdf_path)
    try:
        issues = []
        for page_number, page in enumerate(document, 1):
            issues.extend(_check_equations(page, page_number))
            issues.extend(_check_result_tables(page, page_number))

        tables = _extract_pdf_tables(document)
        page_contexts = {
            table["page"]: _page_context(document, table["page"], include_previous=True)
            for table in tables
        }
        for table in tables:
            context = page_contexts[table["page"]]
            issues.extend(_check_model_comparison_claims(table, context))
            issues.extend(_check_benchmark_summary_claims(table, context))
        issues.extend(_check_scenario_consistency(tables, page_contexts))
    finally:
        document.close()
    if text:
        issues.extend(_check_figure_condition_order(text))
    return issues
