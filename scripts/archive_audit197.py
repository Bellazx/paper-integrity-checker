#!/usr/bin/env python3
"""Archive source folders and regenerated reports for the 51-paper subset."""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

BASE = Path("data/yujing_audit_20261019_197_review_20261027")
REPORTS = Path("data/output/yujing_audit_20261019_197_review_20261027/review_v2")
OUT = Path("data/archive/yujing_audit_20261019_197_review_20261027_verified")
RESULTS = Path("/tmp/audit197_compact_results.json")

if OUT.exists():
    raise SystemExit(f"archive already exists: {OUT}; remove it explicitly before rebuilding")
OUT.mkdir(parents=True)
items = json.loads(RESULTS.read_text(encoding="utf-8"))
manifest = {
    "batch": "yujing_audit_20261019_197_review_20261027",
    "created_at": datetime.now().isoformat(timespec="seconds"),
    "count": len(items),
    "reports_base_url": "http://202.120.57.100/review_reports/yujing_audit_20261019_197_review_20261027",
    "items": [],
}

for info in sorted(items, key=lambda x: x["doi"]):
    doi = info["doi"]
    slug = doi.replace("/", "_").replace(".", "_")
    dest = OUT / slug
    dest.mkdir()
    source = Path(info["_input_dir"])
    shutil.copytree(source, dest / "source", dirs_exist_ok=True)
    report_name = "review_" + doi.replace("/", "_").lower() + ".pdf"
    report = REPORTS / report_name
    if not report.exists():
        raise FileNotFoundError(report)
    shutil.copy2(report, dest / "review_report.pdf")
    manifest["items"].append({
        "doi": doi,
        "result": info.get("result"),
        "source_folder": f"{slug}/source",
        "review_report": f"{slug}/review_report.pdf",
        "report_url": f"{manifest['reports_base_url']}/{report_name}",
    })

(OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
(OUT / "README.txt").write_text(
    "本目录按 DOI 分文件夹；每个文件夹包含 source（原论文文件夹内容）和 review_report.pdf（重新生成的复核报告）。\n"
    "本批次共 51 篇：50 篇有风险、1 篇未发现风险。\n",
    encoding="utf-8",
)
print(f"archived {len(items)} papers at {OUT}")
