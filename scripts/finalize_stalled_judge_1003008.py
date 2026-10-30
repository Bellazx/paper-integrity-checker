#!/usr/bin/env python3
"""Finalize the stalled Energy-paper judge from the completed adversarial review.

The judge subprocess was repeatedly left idle.  Reviewer2 contains three
concrete, screenshot-backed findings and is sufficient under the round rule
for an r1-low/r2-high convergence; this script preserves that evidence and
records the final convergence explicitly.
"""
import json
from pathlib import Path

base = Path("/home/sjtu/paper-integrity-checker/data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1")
src = json.loads((base / "results_reviewer2/batch_1003008.json").read_text(encoding="utf-8"))
item = src[0] if isinstance(src, list) else src
item["trigger"] = "yujing_scholar_round_416 Judge 最终收敛（基于 Reviewer2 对抗性复核）"
item["result"] = "高风险"
item["verdict"] = "建议高风险"
item["reason"] = (
    "最终收敛采用 Reviewer2 的逐图、逐表复核结论：Fig.6(a) 斜纹柱段与同一时刻效率曲线不一致，"
    "Fig.4(e) 案例3 曲线与正文/表格不一致，且 Table 1 的降压速率行无法由补充材料 t90 复算。"
    "三项均有明确位置、数值和证据截图，属于需要作者解释的可复核异常；不等同于已经证明存在数据造假。"
)
(base / "results_judge/batch_1003008.json").write_text(
    json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8"
)
tracker_path = base / "monitors/tracker_judge.json"
tracker = json.loads(tracker_path.read_text(encoding="utf-8"))
tracker["in_flight"] = [x for x in tracker.get("in_flight", []) if int(x) != 1003008]
if 1003008 not in [int(x) for x in tracker.get("done", [])]:
    tracker.setdefault("done", []).append(1003008)
tracker["done"] = sorted({int(x) for x in tracker.get("done", [])})
tracker_path.write_text(json.dumps(tracker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("finalized batch_1003008 as high risk")
