"""Turn the deterministic evidence into per-dimension review shards.

Every ``coverage_manifest.must_address`` id becomes exactly one ledger item,
so publication coverage can be traced item by item. Synthetic items add the
checks the rules require even without an automated alert (figure-vs-source,
study design, reference list, visual pass over the figures).
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict

from review_v3 import config

# Shard kinds -> default reader-facing dimension of their findings.
SHARD_DIMENSION = {
    "image": "image",
    "image_scan": "image",
    "stats": "methodology",
    "data": "data",
    "consistency": "data",
    "ref": "ref",
    "methodology": "methodology",
    "claims": "methodology",
}

_STRONG_TESTS = {
    "cross_group_duplicate", "linear_dependency", "coefficient_of_variation", "sd_regularity",
    "value_recycling", "rectangular_matrix_reuse", "same_sheet_channel_duplicate",
    "same_sheet_exact_series_reuse", "digit_edit_matrix_reuse", "near_modified_matrix_reuse",
    "figure_n_mismatch", "fixed_ratio_series", "rowwise_value_reuse",
}
_METHODOLOGY_TESTS = {
    "verified_manuscript_content_conflict", "manuscript_consistency", "quantitative_consistency",
    "supplementary_consistency", "citation_content", "humanities",
}


def _short(value, limit: int = 700) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


def _high_item(index: int, finding: dict) -> tuple[str, dict]:
    dim = finding.get("dim") or ""
    test = finding.get("test") or finding.get("match_type") or finding.get("issue_type") or ""
    if dim in ("image", "image_splicing"):
        shard = "image"
    elif dim == "reference":
        shard = "ref"
    elif test in _METHODOLOGY_TESTS or dim in ("methodology", "manuscript"):
        shard = "methodology"
    else:
        shard = "data"
    where = finding.get("location") or finding.get("pages") or finding.get("page") or finding.get("ref_number")
    title = f"初检HIGH发现：{dim}/{test}，位置 {where}"
    location = str(finding.get("location") or "")
    parts = [p.strip() for p in location.split("/")]
    group = (dim, test, parts[0] if parts else "", parts[1] if len(parts) > 1 else "")
    return shard, {"title": title, "kind": "high", "test": test, "strong": test in _STRONG_TESTS or dim == "image_splicing",
                   "hint": _short(finding), "file_key": parts[0] if parts else "", "group": group}


def _find(items: list, predicate):
    for item in items or []:
        if predicate(item):
            return item
    return None


def _manifest_item(item_id: str, evidence: dict) -> tuple[str, dict]:
    det = evidence.get("deterministic_findings") or {}
    parts = item_id.split("::")
    head = parts[0]
    if head == "high":
        try:
            index = int(parts[-1])
            return _high_item(index, (evidence.get("high_findings") or [])[index])
        except (ValueError, IndexError):
            return "data", {"title": f"初检HIGH发现 {item_id}", "kind": "high"}
    if head == "figure":
        return "consistency", {"title": f"随文图件 {parts[1]}：图像内容与图注、正文及源数据核对", "kind": "figure",
                               "hint": parts[1]}
    if head == "dupcol":
        pairs = det.get("duplicate_column_pairs") or []
        try:
            pair = pairs[int(parts[-1])]
        except (ValueError, IndexError):
            pair = {}
        title = (f"重复列对：{pair.get('file', parts[1])} / sheet {pair.get('sheet', parts[2] if len(parts) > 2 else '')} "
                 f"[{pair.get('group_a', '')} {pair.get('col_a_header', '')} ⇄ {pair.get('group_b', '')} {pair.get('col_b_header', '')}]")
        return "data", {"title": title, "kind": "dupcol", "strong": bool(pair.get("cross_group")),
                        "hint": _short(pair), "file_key": str(pair.get("file") or "")}
    if head == "reuse_group":
        group = _find(det.get("cross_sheet_reuse_groups"), lambda g: g.get("group_id") == parts[1]) or {}
        locations = "；".join((group.get("locations_sample") or [])[:3])
        title = f"跨表复用分组 {parts[1]}（{group.get('count', '?')}项）：{locations}"
        compact = {k: group.get(k) for k in ("group_id", "scope", "count", "n_values_bucket", "headers_sample",
                                            "figure_labels", "locations_sample", "requires_expansion")}
        compact["representatives"] = [{k: r.get(k) for k in ("n_values", "locations", "sample")}
                                      for r in (group.get("representatives") or [])[:3]]
        files = (group.get("locations_sample") or [""])[0].split("::")[0]
        return "data", {"title": title, "kind": "reuse_group", "strong": bool(group.get("requires_expansion")),
                        "hint": _short(compact, 900), "file_key": files}
    if head == "decmismatch":
        item = _find(det.get("decimal_precision_mismatch"), lambda d: d.get("header") == parts[1]) or {}
        return "data", {"title": f"小数精度不一致：列 {parts[1]}，精度范围 {item.get('precision_spread')}",
                        "kind": "decimal", "hint": _short(item), "file_key": ""}
    if head == "trailing_digit":
        return "data", {"title": f"末位数字异常：{' / '.join(parts[1:4])}", "kind": "trailing_digit",
                        "strong": "sev=high" in item_id, "hint": item_id, "file_key": parts[1]}
    if head == "passage_offset":
        item = _find(det.get("passage_offset_pattern"),
                     lambda p: p.get("file") == parts[1] and p.get("sheet") == parts[2]) or {}
        return "data", {"title": f"跨行伪重复/固定比例：{parts[1]} / sheet {parts[2]}（{parts[-1]}）",
                        "kind": "passage_offset", "strong": "sev=high" in item_id,
                        "hint": _short(item), "file_key": parts[1]}
    if head == "source_data_audit":
        audit = det.get("source_data_audit") or {}
        try:
            finding = (audit.get("high_findings") or [])[int(parts[-1])]
        except (ValueError, IndexError):
            finding = {}
        compact = {k: v for k, v in finding.items() if k != "details"}
        details = finding.get("details") or {}
        compact["details"] = {k: (v[:12] if isinstance(v, list) else v) for k, v in details.items()}
        loc = [p.strip() for p in str(finding.get("location") or "").split("/")]
        return "data", {"title": f"源数据审计：{finding.get('test', parts[1])}，{finding.get('location', '')}",
                        "kind": "source_data_audit", "strong": True, "hint": _short(compact, 900),
                        "file_key": loc[0], "test": finding.get("test") or parts[1],
                        "group": ("source_data_audit", finding.get("test") or parts[1], loc[0], loc[1] if len(loc) > 1 else "")}
    return "data", {"title": f"必查项 {item_id}", "kind": head, "hint": item_id}


def _pack(items: list[dict], size: int, max_shards: int, key=None) -> list[list[dict]]:
    if not items:
        return []
    shards = max(1, min(max_shards, -(-len(items) // max(1, size))))
    if shards == 1:
        return [items]
    # Keep items of the same file together so a session opens a workbook once.
    if key:
        grouped = OrderedDict()
        for item in items:
            grouped.setdefault(key(item), []).append(item)
        ordered = [item for group in sorted(grouped.values(), key=len, reverse=True) for item in group]
    else:
        ordered = items
    target = -(-len(ordered) // shards)
    return [ordered[i:i + target] for i in range(0, len(ordered), target)]


def build_shards(evidence: dict, dossier: dict) -> list[dict]:
    """Return shard specs: {id, kind, dimension, items:[...]}."""
    by_kind: dict[str, list[dict]] = {kind: [] for kind in SHARD_DIMENSION}
    grouped: "OrderedDict[tuple, list[dict]]" = OrderedDict()
    for item_id in (evidence.get("coverage_manifest") or {}).get("must_address") or []:
        kind, spec = _manifest_item(item_id, evidence)
        item = {"id": item_id, "required": True, "shard_kind": kind, **spec}
        if spec.get("group"):
            grouped.setdefault((kind,) + tuple(spec["group"]), []).append(item)
        else:
            by_kind[kind].append(item)
    # Same detector test on the same sheet is adjudicated as one class (the
    # publication gate also groups HIGH findings by test). Members stay listed
    # so coverage remains traceable to every must_address id.
    for key, members in grouped.items():
        kind = key[0]
        if len(members) == 1:
            members[0].pop("group", None)
            by_kind[kind].append(members[0])
            continue
        _, dim, test, file_name, sheet = key
        by_kind[kind].append({
            "id": f"group::{dim}::{test}::{file_name}::{sheet}"[:200], "required": True, "kind": "group",
            "test": test, "strong": any(m.get("strong") for m in members), "file_key": file_name,
            "members": [m["id"] for m in members],
            "title": f"同类告警 {len(members)} 项：{dim}/{test}，{file_name} / {sheet}（按类核查，代表样例需打开原值）",
            "hint": " ｜ ".join(str(m.get("hint", ""))[:260] for m in members[:4])[:1100],
        })

    image_alerts = dossier.get("image_alerts") or []
    if image_alerts:
        by_kind["image"].append({
            "id": "image::all_alerts", "kind": "image_alerts", "required": True,
            "title": f"全部 {len(image_alerts)} 组图像相似/拼接告警逐组目视核查（pv3 alerts image）",
        })
    by_kind["image_scan"].append({
        "id": "image::visual_pass", "kind": "visual_pass", "required": True,
        "title": "逐页浏览论文主图与关键补充图，检查未被自动标记的面板重复、拼接或异常",
    })

    figures = dossier.get("main_figure_numbers") or []
    if figures:
        for number in figures:
            caption = next((c for c in dossier.get("captions") or []
                            if c["doc"] == "main" and c["kind"] == "figure" and c["number"] == number), {})
            by_kind["consistency"].append({
                "id": f"fig::{number}", "kind": "figure_check", "required": True,
                "title": f"Fig. {number}（第{caption.get('page', '?')}页）：图注声明的样本量/实验设计、"
                         "图中数值与源数据或正文表格是否一致",
                "hint": caption.get("text", ""),
            })
    else:
        by_kind["consistency"].append({
            "id": "fig::overall", "kind": "figure_check", "required": True,
            "title": "论文主要图表：图注声明、图中数值与源数据或正文表格是否一致（未自动识别到图注时逐页查找）",
        })

    ref_alerts = dossier.get("reference_alerts") or []
    by_kind["ref"].append({
        "id": "ref::overall", "kind": "ref_overall", "required": True,
        "title": f"参考文献列表整体核查（初检提示 {len(ref_alerts)} 条，pv3 alerts ref）：区分解析/查询问题与真实著录异常",
    })
    by_kind["methodology"].append({
        "id": "design::overall", "kind": "design", "required": True,
        "title": "研究设计：方法所述样本量/重复数/分组与结果和图注是否一致，时间线与伦理声明是否矛盾",
    })
    by_kind["claims"].append({
        "id": "claims::core", "kind": "claims", "required": True,
        "title": ("核心方法与核心主张：所提方法的关键公式（目标函数、约束、罚项/正则项符号与优化方向、"
                  "变量定义与量纲）和算法步骤是否自洽、能否实现其声称的作用；摘要与结论的主要主张"
                  "（改进幅度、优于基线、某现象被消除或不受影响）是否被本文自身的图表结果支持"),
    })
    by_kind["stats"].append({
        "id": "stats::overall", "kind": "stats", "required": True,
        "title": "统计与数值复算：表格合计/百分比/派生指标、均值-SD-n 可行性、检验统计量与 p 值、摘要-正文-表格同一数值是否一致",
    })
    if dossier.get("data_files"):
        by_kind["data"].append({
            "id": "data::forensic_scan", "kind": "forensic_scan", "required": True,
            "title": "末位数字/SD 规律/小数尾串继承扫描结果复核（pv3 forensic，结果已预先生成）",
        })

    shards = []
    for kind, items in by_kind.items():
        if kind == "data":
            packs = _pack(items, config.DATA_SHARD_SIZE, config.MAX_SHARDS_PER_DIMENSION,
                          key=lambda it: it.get("file_key") or "")
        elif kind == "consistency":
            packs = _pack(items, config.FIGURE_SHARD_SIZE, config.MAX_SHARDS_PER_DIMENSION)
        else:
            packs = [items] if items else []
        for number, pack in enumerate(packs, start=1):
            shard_id = kind if len(packs) == 1 else f"{kind}{number}"
            shards.append({"id": shard_id, "kind": kind, "dimension": SHARD_DIMENSION[kind], "items": pack})
    return shards


def query_terms(shard: dict, dossier: dict) -> str:
    """Words used to retrieve relevant memory rules for one shard."""
    words = [shard["kind"]]
    for item in shard["items"]:
        words.append(item.get("test") or item.get("kind") or "")
        words.append(re.sub(r"[\[\]{}\"'：:，,()（）/]", " ", item.get("title", ""))[:120])
    paper = dossier.get("paper") or {}
    words.append(str(paper.get("title") or ""))
    words.append(str(paper.get("journal") or ""))
    return " ".join(w for w in words if w)
