"""Read-only comparison of yujing_scholar review reports around 2026-10-21."""
from __future__ import annotations

import csv
import json
import os
import random
import re
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import fitz
import pymssql

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from utils.db import DB_CONFIG

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "data/output/model_comparison_scholar_20261029"
START = datetime(2026, 9, 1)
END = datetime(2026, 10, 30)
SWITCH = datetime(2026, 10, 21)
DIMENSIONS = {
    "一": "图像",
    "二": "数据与图文一致性",
    "三": "参考文献",
    "四": "研究设计与统计",
}
BALANCED_N = 100
RANDOM_SEED = 20261030


def pdf_index() -> dict[str, Path]:
    """Index only PDFs under data/, avoiding repeated recursive scans."""
    candidates = defaultdict(list)
    for root, _dirs, files in os.walk(BASE / "data"):
        for name in files:
            if name.endswith(".pdf"):
                candidates[name].append(Path(root) / name)
    def score(path: Path):
        text = str(path)
        # The URL written to yujing_scholar points to this current namespace.
        # Prefer it over archived/superseded copies with the same basename.
        return (
            0 if "/data/output/yujing_scholar/review_v2/" in text else 1,
            1 if "superseded" in text else 0,
            len(path.parts),
        )
    return {name: sorted(paths, key=score)[0] for name, paths in candidates.items()}


def extract_text(path: Path) -> str:
    with fitz.open(path) as doc:
        return "\n".join(page.get_text() for page in doc)


def report_stats(text: str) -> dict:
    # Count only explicit issue locations, not explanatory mentions of
    # "问题/异常" in low-risk limitations. This is the same reader-facing
    # unit used in the prior report statistics and is not a misconduct count.
    details = {}
    sections = re.split(r"\n?（([一二三四])）[^\n]*\n", text)
    for i in range(1, len(sections), 2):
        key = DIMENSIONS.get(sections[i])
        body = sections[i + 1]
        if key:
            details[key] = body.count("问题位置：")
    issues = sum(details.values())
    numbered = len(re.findall(r"(?m)^\s*（\d+）\s*问题位置：", text))
    conclusion = "有风险" if "复核结论：有风险" in text else (
        "未发现风险" if "复核结论：未发现风险" in text else "未识别")
    # Section presence is the conservative coverage measure. Some report
    # templates say “本模块复核结论” rather than the exact phrase used by
    # the newer renderer, so requiring the phrase would undercount coverage.
    coverage = {d: bool(re.search(rf"（{n}）[^\n]*", text))
                for n, d in DIMENSIONS.items()}
    return {
        "issues": issues,
        "numbered_issues": numbered,
        "dimensions": details,
        "dimension_coverage": coverage,
        "report_conclusion": conclusion,
        "chars": len(text),
        "structured_fields": {
            "location": text.count("问题位置："),
            "issue_type": text.count("问题类型："),
            "facts": text.count("发现："),
            "importance": text.count("为什么重要："),
            "basis": text.count("核查依据："),
        },
        "evidence_screenshot_count": text.count("证据截图"),
        "has_material_limit": ("材料限制" in text or "材料不足" in text),
    }


def pct(n, d):
    return round(100 * n / d, 2) if d else None


def summarize(rows: list[dict], include_scholars: bool = True) -> dict:
    total = len(rows)
    high = sum(r["review_result"] == "高风险" for r in rows)
    reports_with_issues = [r for r in rows if r["stats"]["issues"] > 0]
    high_rows = [r for r in rows if r["review_result"] == "高风险"]
    all_issue = sum(r["stats"]["issues"] for r in rows)
    high_issue = sum(r["stats"]["issues"] for r in high_rows)
    dimensions = Counter()
    for r in high_rows:
        dimensions.update(r["stats"]["dimensions"])
    dimension_coverage = {
        d: sum(bool(r["stats"]["dimension_coverage"].get(d)) for r in rows)
        for d in DIMENSIONS.values()
    }
    return {
        "papers": total,
        "high_risk": high,
        "high_risk_percent": pct(high, total),
        "low_risk": total - high,
        "all_report_issue_items": all_issue,
        "reports_with_explicit_issue_locations": len(reports_with_issues),
        "high_risk_issue_items": high_issue,
        "mean_issue_items_per_high_risk_report": round(high_issue / high, 3) if high else None,
        "mean_issue_items_per_all_report": round(all_issue / total, 3) if total else None,
        "high_risk_dimension_issue_items": dict(dimensions),
        "high_risk_dimension_share_percent": {
            d: pct(dimensions[d], high_issue) for d in DIMENSIONS.values()
        },
        "dimension_section_coverage": dimension_coverage,
        "dimension_section_coverage_percent": {
            d: pct(n, total) for d, n in dimension_coverage.items()
        },
        "high_risk_report_quality": {
            field: sum(r["stats"]["structured_fields"][field] for r in high_rows)
            for field in ("location", "issue_type", "facts", "importance", "basis")
        },
        "high_risk_mean_quality_per_report": {
            field: round(sum(r["stats"]["structured_fields"][field] for r in high_rows) / high, 3)
            if high else None
            for field in ("location", "issue_type", "facts", "importance", "basis")
        },
        "high_risk_reports_with_evidence_screenshots": sum(
            r["stats"]["evidence_screenshot_count"] > 0 for r in high_rows
        ),
        "high_risk_reports_with_material_limit": sum(
            r["stats"]["has_material_limit"] for r in high_rows
        ),
        "scholars": ({
            s: summarize([r for r in rows if r["scholar"] == s], include_scholars=False)
            for s in sorted({r["scholar"] for r in rows})
        } if include_scholars else {}),
    }


def main():
    index = pdf_index()
    conn = pymssql.connect(**DB_CONFIG, login_timeout=10, timeout=60)
    try:
        cur = conn.cursor(as_dict=True)
        cur.execute("""
            SELECT id, scholar, doi, review_result, review_time, review_report_url,
                   risk_level, total_score
            FROM yujing_scholar
            WHERE review_time >= %s AND review_time < %s
            ORDER BY review_time, id
        """, (START, datetime(2026, 10, 30)))
        db_rows = cur.fetchall()
    finally:
        conn.close()

    rows = []
    missing = []
    for r in db_rows:
        filename = Path(urllib.parse.urlparse(r["review_report_url"]).path).name
        path = index.get(filename)
        if path is None:
            missing.append({"id": r["id"], "scholar": r["scholar"], "filename": filename})
            continue
        text = extract_text(path)
        stats = report_stats(text)
        period = "before_20261021_M3" if r["review_time"] < SWITCH else "after_20261021_M31"
        rows.append({
            "id": r["id"], "scholar": r["scholar"], "doi": r["doi"],
            "review_result": r["review_result"], "review_time": r["review_time"].isoformat(),
            "period": period, "report": str(path.relative_to(BASE)), "stats": stats,
        })

    grouped = {period: summarize([r for r in rows if r["period"] == period])
               for period in ("before_20261021_M3", "after_20261021_M31")}
    # Equal-count descriptive comparison using a reproducible stratified random
    # sample.  The pre-switch high-risk stratum is fully retained because it
    # contains only three observations; the remainder is randomly sampled from
    # pre-switch non-high-risk records.  Post-switch records are sampled
    # randomly from the full post-switch pool.  This makes rare pre-switch
    # events visible without presenting the resulting ratio as a population
    # prevalence estimate.
    pre = [r for r in rows if r["period"] == "before_20261021_M3"]
    post = [r for r in rows if r["period"] == "after_20261021_M31"]
    balanced_n = min(BALANCED_N, len(pre), len(post))
    rng = random.Random(RANDOM_SEED)
    pre_high = [r for r in pre if r["review_result"] == "高风险"]
    pre_other = [r for r in pre if r["review_result"] != "高风险"]
    if len(pre_high) > balanced_n:
        raise ValueError("pre-switch high-risk stratum exceeds balanced sample size")
    pre_sample = pre_high + rng.sample(pre_other, balanced_n - len(pre_high))
    post_sample = rng.sample(post, balanced_n)
    balanced = {
        "before_20261021_M3": pre_sample,
        "after_20261021_M31": post_sample,
    }
    balanced_ids = {id(r) for rs in balanced.values() for r in rs}

    # Supplementary exploratory slice requested for showing the highest-risk
    # pre-switch natural batch while keeping the post-switch denominator and
    # sample size comparable.  This is intentionally kept separate from the
    # balanced comparison because selecting the maximum-risk pre-switch batch
    # is not an unbiased prevalence estimate.
    def highest_ratio_batch(period_rows):
        groups = defaultdict(list)
        for row in period_rows:
            groups[row["scholar"]].append(row)
        return max(
            groups.items(),
            key=lambda item: (
                sum(r["review_result"] == "高风险" for r in item[1]) / len(item[1]),
                len(item[1]),
            ),
        )

    pre_batch_name, pre_batch = highest_ratio_batch(pre)
    highest_n = min(8, len(pre_batch), len(post))
    pre_batch_rng = random.Random(RANDOM_SEED)
    post_pool_rng = random.Random(RANDOM_SEED)
    highest_batch_sample = {
        "before_20261021_M3": pre_batch_rng.sample(pre_batch, highest_n),
        "after_20261021_M31": post_pool_rng.sample(post, highest_n),
    }
    payload = {
        "created_at": datetime.now().astimezone().isoformat(),
        "scope": "yujing_scholar review_time 2026-09-01 through 2026-10-29",
        "switch_boundary": "2026-10-21 00:00 Asia/Shanghai (user-provided model change boundary)",
        "model_assumption": "before=MiniMax-M3, after=MiniMax-M3.1-Flash; model identity is based on the user's documented 10/21 switch, not inferred from current config",
        "unit": "explicit reader-facing 问题位置 entries in the delivered PDF; not independent misconduct counts",
        "matched_population_warning": "The pre-switch and post-switch groups contain different scholars, disciplines and batches. Balanced counts reduce denominator imbalance but do not create a causal model A/B test.",
        "rows_in_db": len(db_rows), "rows_with_local_reports": len(rows), "missing_reports": missing,
        "periods": grouped,
        "balanced_sample": {
            "n_per_period": balanced_n,
            "selection": (
                "stratified random sample with fixed seed: retain all pre-switch "
                "high-risk records and randomly sample the remaining pre-switch "
                "records; randomly sample the same number from the post-switch "
                "pool"
            ),
            "random_seed": RANDOM_SEED,
            "sampling_note": (
                "The pre-switch high-risk stratum was fully retained because it "
                "contains only three records. Sample percentages are descriptive "
                "for this analytic sample and are not weighted population rates."
            ),
            "periods": {p: summarize(rs) for p, rs in balanced.items()},
            "date_ranges": {
                p: {"first": min(r["review_time"] for r in rs), "last": max(r["review_time"] for r in rs)}
                for p, rs in balanced.items()
            },
            "by_scholar": {
                p: dict(Counter(r["scholar"] for r in rs)) for p, rs in balanced.items()
            },
        },
        "highest_ratio_batch_sample": {
            "n_per_period": highest_n,
            "selection": "identify the pre-switch scholar batch with the highest observed risk ratio, then draw an equal-size random sample from that batch and from the full post-switch pool",
            "random_seed": RANDOM_SEED,
            "warning": "Exploratory, selection-biased slice; do not interpret its percentages as period-wide prevalence or an unbiased model comparison.",
            "source_batches": {
                "before_20261021_M3": {
                    "scholar": pre_batch_name,
                    "papers": len(pre_batch),
                    "high_risk": sum(r["review_result"] == "高风险" for r in pre_batch),
                    "high_risk_percent": pct(sum(r["review_result"] == "高风险" for r in pre_batch), len(pre_batch)),
                },
                "after_20261021_M31": {
                    "scholar": "全部切换后批次",
                    "papers": len(post),
                    "high_risk": sum(r["review_result"] == "高风险" for r in post),
                    "high_risk_percent": pct(sum(r["review_result"] == "高风险" for r in post), len(post)),
                },
            },
            "periods": {p: summarize(rs) for p, rs in highest_batch_sample.items()},
            "by_scholar": {
                p: dict(Counter(r["scholar"] for r in rs)) for p, rs in highest_batch_sample.items()
            },
        },
        "by_scholar": {
            s: {p: summarize([r for r in rows if r["scholar"] == s and r["period"] == p])
                for p in ("before_20261021_M3", "after_20261021_M31")
                if any(r["scholar"] == s and r["period"] == p for r in rows)}
            for s in sorted({r["scholar"] for r in rows})
        },
        "rows": rows,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "statistics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with (OUT / "逐篇统计.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["period", "scholar", "doi", "review_time", "review_result", "issues", *DIMENSIONS.values(), "balanced_sample", "report"])
        for r in rows:
            w.writerow([r["period"], r["scholar"], r["doi"], r["review_time"], r["review_result"],
                        r["stats"]["issues"], *[r["stats"]["dimensions"].get(d, 0) for d in DIMENSIONS.values()],
                        "是" if id(r) in balanced_ids else "否", r["report"]])
    print(json.dumps({"rows": len(rows), "missing": len(missing), "periods": grouped}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
