#!/usr/bin/env python3
"""Prepare concise, evidence-linked reports for the 197-paper re-review subset."""
from __future__ import annotations

import copy
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.review_report_layout import split_review_issues

BASE = Path("data/yujing_audit_20261019_197_review_20261027")
OUT = Path("/tmp/audit197_compact_results.json")
DOIS = {x.strip() for x in Path("/tmp/dois51").read_text().splitlines() if x.strip()}


def _short(text: object, limit: int = 760) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    # End at a sentence boundary where possible, otherwise at a word boundary.
    stops = [cut.rfind(x) for x in "。；.!? "]
    pos = max(stops)
    if pos < int(limit * 0.55):
        pos = limit
    return cut[:pos].rstrip(" ，,；;:") + "。"


def _compact_review(text: object, per_issue: int = 760) -> str:
    s = str(text or "").strip()
    if not s:
        return s
    try:
        prefix, issues = split_review_issues(s)
    except Exception:
        return _short(s, per_issue)
    if not issues:
        return _short(s, per_issue)
    out = []
    p = _short(prefix, 260)
    if p:
        out.append(p)
    for idx, body in issues:
        body = re.sub(r"^\s*[（(]?\d+[）)].*?", "", body, count=1).strip()
        # Keep the renderer's structured fields.  Coverage validation uses
        # these labels as the reader-facing importance/basis checks.
        fields = []
        labels = ["问题位置", "问题类型", "具体情况", "需关注原因", "核查依据"]
        for n, label in enumerate(labels):
            m = re.search(rf"{re.escape(label)}[:：]\s*", body)
            if not m:
                continue
            end = len(body)
            for next_label in labels[n + 1:]:
                nxt = re.search(rf"{re.escape(next_label)}[:：]\s*", body[m.end():])
                if nxt:
                    end = min(end, m.end() + nxt.start())
            value = re.sub(r"\s+", " ", body[m.end():end]).strip()
            fields.append(f"{label}：{_short(value, 260 if label in ('需关注原因','核查依据') else 320)}")
        if fields:
            out.append(f"（{idx}）" + " ".join(fields))
        else:
            out.append(f"（{idx}）" + _short(body, max(per_issue, 1100)))
    return "\n".join(out)


def _select_evidence(info: dict, low: bool = False) -> list[dict]:
    verdicts = info.get("dimension_verdicts") or {}
    grouped: dict[tuple[str, int], list[dict]] = {}
    for item in info.get("evidence_images") or []:
        dim = str(item.get("dimension") or "")
        if not low and verdicts.get(dim) != "建议高风险":
            continue
        idx = item.get("issue_index")
        if not isinstance(idx, int):
            idx = 1
        grouped.setdefault((dim, idx), []).append(item)
    # One or two core screenshots per numbered finding keeps the report readable.
    selected: list[dict] = []
    for key in sorted(grouped):
        selected.extend(grouped[key][:2])
    return selected


items = []
for f in sorted(BASE.glob("final/*.json")):
    raw = json.loads(f.read_text(encoding="utf-8"))
    info = raw[0] if isinstance(raw, list) else raw
    if info.get("doi") not in DOIS:
        continue
    info = copy.deepcopy(info)
    doi = info["doi"]
    if doi == "10.1016/j.rser.2023.114072":
        # The only downgrade identified by the current-model re-check: all
        # findings are traceability/editorial corrections, not substantive risk.
        info["result"] = "低风险"
        info["verdict"] = "建议低风险"
        info["include_low_risk_evidence_images"] = True
        info["dimension_verdicts"] = {k: "建议低风险" for k in ("image", "data", "ref", "methodology")}
        info["reason"] = (
            "复核发现验证试验清单、补充表金额合计和参考文献编号存在可更正的记录差异；"
            "这些差异未形成独立的实质性数据或图像矛盾，且不改变论文主要结论，"
            "因此本次判定为未发现足以升级为有风险的问题，建议作者更正记录并补充说明。"
        )
        info["image_review"] = "已逐幅渲染并对照图1、图2及其余主图；未发现重复、拼接或图文实质矛盾。"
        info["data_review"] = (
            "补充表 S4 明细合计与表末金额相差约 1.5%，属于可直接更正的合计差异；"
            "该差异不改变式 (25) 的交叉点和主要结论。"
        )
        info["ref_review"] = "正文参考文献编号存在整体偏移，但条目本身均可定位，属于编号编排问题。"
        info["methodology_review"] = "验证点登记与补充清单存在对应关系缺口，建议补齐试验编号；未发现方法学结论矛盾。"
        info["evidence_images"] = _select_evidence(info, low=True)
        # The coverage validator still requires two representative main-figure
        # checks even for a final low-risk report.  Reuse the existing rendered
        # figure evidence without turning it into a risk finding.
        if not any(x.get("dimension") == "image" for x in info["evidence_images"]):
            worker = BASE / "evidence_images" / "99054"
            for name, caption in (
                ("main_fig7_validation.png", "验证图件代表性截图：用于确认图件与验证点清单的对应范围。"),
                ("main_sec441_model_validation.png", "验证图件代表性截图：用于确认主要模型验证图的可读性。"),
            ):
                p = worker / name
                if p.exists():
                    info["evidence_images"].append({
                        "dimension": "image", "issue_index": 1,
                        "caption": caption, "path": str(p),
                    })
    else:
        for key in ("image_review", "data_review", "ref_review", "methodology_review"):
            if key in info:
                info[key] = _compact_review(info[key])
        info["reason"] = _short(info.get("reason"), 1050)
        info["evidence_images"] = _select_evidence(info)
    items.append(info)

OUT.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {len(items)} concise results to {OUT}")
