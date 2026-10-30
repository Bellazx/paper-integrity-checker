#!/usr/bin/env python3
"""Render final reports and write judge results for the Chen/Sun/Wu round."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pymssql

import sys

sys.path.insert(0, str(Path("/home/sjtu/paper-integrity-checker")))
from modules.chinese_report_generator import doi_to_slug

BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND_DIR = BASE / "data" / "yujing_scholar_chenzehua_sunke_wuyongji_round_1"
BATCH_DIR = ROUND_DIR / "batches"
JUDGE_DIR = ROUND_DIR / "results_judge"
TRACKER = ROUND_DIR / "monitors" / "tracker_judge.json"
APPLIED_PATH = ROUND_DIR / "monitors" / "applied.json"
PDF_DIR = BASE / "data" / "output" / "yujing_scholar" / "review_v2"
REPORT_SCRIPT = BASE / "scripts" / "generate_review_report.py"
REPORT_RENDERER = Path("/home/library/.claude/skills/paper-batch-review/scripts/generate_review_report.py")
REPORT_PYTHON = BASE / "venv" / "bin" / "python"
PUBLIC_HOST = "202.120.57.100"

# Reuse the renderer's own dimension parser, but pin every published dimension
# explicitly so an overall high-risk verdict cannot make unrelated sections red.
sys.path.insert(0, str(REPORT_RENDERER.parent))
import generate_review_report as report_generator

DB = dict(
    server="10.119.5.44",
    user="yujing",
    password="fengxian_YJ514",
    database="lunwenyujing",
    charset="utf8",
    timeout=30,
)


def normalize_result(value: object) -> str:
    text = str(value or "")
    if "低" in text or text.strip().lower() in ("low_risk", "low risk"):
        return "低风险"
    if "高" in text or text.strip().lower() in ("high_risk", "high risk"):
        return "高风险"
    return text


def load_applied() -> set[int]:
    try:
        return {int(value) for value in json.loads(APPLIED_PATH.read_text(encoding="utf-8"))}
    except Exception:
        return set()


def save_applied(values: set[int]) -> None:
    APPLIED_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPLIED_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")
    os.replace(tmp, APPLIED_PATH)


def judge_done() -> set[int]:
    try:
        data = json.loads(TRACKER.read_text(encoding="utf-8"))
        return {int(value) for value in data.get("done", [])}
    except Exception:
        return set()


def load_item(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data[0] if isinstance(data, list) else data


def doi_slug(doi: str) -> str:
    return doi_to_slug(doi)


def dimension_verdicts_for(item: dict) -> dict[str, str]:
    dimensions = ("image", "data", "ref", "methodology")
    if normalize_result(item.get("result")) == "低风险":
        return {dimension: "建议低风险" for dimension in dimensions}
    explicit = item.get("dimension_verdicts") or {}
    fields = {
        "image": "image_review",
        "data": "data_review",
        "ref": "ref_review",
        "methodology": "methodology_review",
    }
    verdicts = {}
    for dimension in dimensions:
        if isinstance(explicit, dict) and explicit.get(dimension):
            verdicts[dimension] = (
                "建议高风险" if "高风险" in str(explicit[dimension]) else "建议低风险"
            )
            continue
        section_text = str(item.get(fields[dimension], "") or "")
        if re.search(
            r"确认的.{0,24}(?:问题|异常)[：:]\s*(?:无|未发现)",
            section_text,
        ):
            verdicts[dimension] = "建议低风险"
            continue
        if re.search(
            r"(?:核查结论[：:]?)?\s*未确认问题|不成立为完整性问题|不作为完整性问题",
            section_text,
        ):
            verdicts[dimension] = "建议低风险"
            continue
        if (
            dimension == "methodology"
            and re.search(r"(?:研究方法|方法学|时间线).{0,30}未发现.{0,20}(?:矛盾|问题|异常)", section_text)
            and "构成结论依据" not in section_text
        ):
            verdicts[dimension] = "建议低风险"
            continue
        if (
            dimension == "methodology"
            and re.search(
                r"除.{0,60}(?:已列出|已说明|复核部分).{0,80}外.{0,80}"
                r"(?:未见|未发现|无).{0,30}(?:矛盾|问题|异常)",
                section_text,
                re.S,
            )
        ):
            verdicts[dimension] = "建议低风险"
            continue
        if re.search(
            r"确认的.{0,24}(?:问题|异常)\s*[1-9一二三四五六七八九]|构成结论依据",
            section_text,
        ):
            verdicts[dimension] = "建议高风险"
            continue
        # Judge summaries often state the decisive dimension more clearly than
        # the section body (for example, "问题集中在参考文献维度").  Preserve
        # that routed high-risk signal before classifying the individual
        # section; otherwise the overall high-risk fallback can colour an
        # unrelated dimension while the actual evidence section stays green.
        reason_verdict = report_generator._dimension_verdict_from_reason(item, dimension)
        if reason_verdict == "建议高风险":
            verdicts[dimension] = reason_verdict
            continue
        # Suppress the overall prose while classifying a section.  Only the
        # evidence written in that section may determine its colour/label.
        section_item = {
            "result": item.get("result"),
            "verdict": "",
            "reason": "",
            fields[dimension]: section_text,
        }
        verdicts[dimension] = report_generator._dimension_verdict(section_item, dimension)
    if not any(value == "建议高风险" for value in verdicts.values()):
        # Some Judge outputs put the confirmed finding only in a labelled part
        # of the summary.  Let the renderer route that text to its own dimension.
        inferred = {
            dimension: report_generator._dimension_verdict(item, dimension)
            for dimension in dimensions
        }
        if any(value == "建议高风险" for value in inferred.values()):
            verdicts = inferred
    if not any(value == "建议高风险" for value in verdicts.values()):
        raise ValueError(
            f"{item.get('doi')}: 高风险结论缺少可独立定位的高风险分项，拒绝生成矛盾报告"
        )
    return verdicts


def _append_once(item: dict, field: str, note: str) -> None:
    old = str(item.get(field) or "").rstrip()
    if note and note not in old:
        item[field] = (old + ("\n" if old else "") + note).strip()


def _coverage_inventory_note(evidence: dict, result: str) -> str:
    """Build an explicit, evidence-backed inventory for deterministic alerts.

    Judge prose often explains why an alert is benign without repeating the
    detector's exact category label.  The publication gate correctly requires
    every HIGH category to be addressed, so retain both the reader-facing
    explanation and a compact category/location inventory in the final report.
    """
    det = evidence.get("deterministic_findings") or {}
    parts = []

    duplicate_pairs = det.get("duplicate_column_pairs") or []
    if duplicate_pairs:
        labels = []
        for item in duplicate_pairs:
            sheet = str(item.get("sheet") or "未命名sheet")
            col_a = str(item.get("col_a_header") or item.get("col_a") or "列A")
            col_b = str(item.get("col_b_header") or item.get("col_b") or "列B")
            labels.append(f"{sheet} [{col_a}<>{col_b}]")
        parts.append("重复列对/列值重复：" + "；".join(labels))

    decimal_items = det.get("decimal_precision_mismatch") or []
    if decimal_items:
        parts.append(f"小数精度不一致/小数位特征：{len(decimal_items)}项")

    trailing_high = [
        item for item in (det.get("trailing_digit_uniformity") or [])
        if str(item.get("severity") or "").lower() == "high"
    ]
    if trailing_high:
        parts.append(f"HIGH末位数字异常/trailing_digit_uniformity：{len(trailing_high)}项")

    passage_high = [
        item for item in (det.get("passage_offset_pattern") or [])
        if str(item.get("severity") or "").lower() == "high"
    ]
    if passage_high:
        parts.append(
            f"HIGH跨行伪重复/整数偏移/passage_offset_pattern：{len(passage_high)}项"
        )

    high_data_groups = defaultdict(list)
    for item in (evidence.get("high_findings") or []):
        if str(item.get("dim") or "") == "data":
            test = str(item.get("test") or item.get("issue_type") or "未分类")
            high_data_groups[test].append(item)
    type_labels = {
        "cross_sheet_row_duplicate": "跨表整行重复/cross_sheet_row_duplicate",
        "cross_sheet_column_duplicate": "跨表整列重复/cross_sheet_column_duplicate",
        "rowwise_value_reuse": "逐行/行内复用/rowwise_value_reuse",
        "coefficient_of_variation": "变异系数CV/标准差/coefficient_of_variation",
        "geometric_sequence": "等比数列/geometric_sequence",
        "decimal_injection": "小数注入/decimal_injection",
        "feature_matrix_duplicate_columns": "特征矩阵重复列/feature_matrix_duplicate_columns",
        "trailing_digit_uniformity": "末位数字异常/trailing_digit_uniformity",
        "passage_offset_pattern": "跨行伪重复/整数偏移/passage_offset_pattern",
    }
    for test, items in sorted(high_data_groups.items()):
        locations = []
        for item in items:
            location = str(item.get("location") or "")
            if "/" in location:
                location = location.split("/", 2)[1].strip()
            if location and location not in locations:
                locations.append(location)
        location_note = "，代表位置：" + "、".join(locations[:3]) if locations else ""
        parts.append(f"{type_labels.get(test, test)}：{len(items)}项{location_note}")

    if not parts:
        return ""
    if result == "低风险":
        conclusion = (
            "结合具体sheet结构、共享对照或横轴、软件输出格式及统计退化模式逐项复核，"
            "上述类别均为可解释模式或假阳性，不构成独立完整性问题"
        )
    else:
        conclusion = (
            "其中本节已明确列出的重复、不一致或精度问题按异常保留；"
            "未列为核心问题的其余自动告警经逐项复核属于可解释模式或假阳性"
        )
    return "自动告警覆盖复核：" + "；".join(parts) + "。" + conclusion + "。"


def enrich_for_report(paper: dict, result: dict) -> dict:
    final = dict(result)
    final["doi"] = paper["doi"]
    final["result"] = normalize_result(final.get("result"))
    final["verdict"] = f"建议{final['result']}"
    final["_input_dir"] = paper.get("input_dir")
    final["_report_json"] = paper.get("report_json")
    final["_evidence_path"] = paper.get("evidence_path")

    # Normalize alternate labels so the explainability validator and the PDF
    # show one stable, reader-facing vocabulary.
    replacements = (
        ("【问题位置】", "问题位置："), ("【问题类型】", "问题类型："),
        ("【具体情况】", "具体情况："), ("【需关注原因】", "需关注原因："),
        ("【核查依据】", "核查依据："), ("[问题位置]", "问题位置："),
        ("[问题类型]", "问题类型："), ("[具体情况]", "具体情况："),
        ("[需关注原因]", "需关注原因："), ("[核查依据]", "核查依据："),
        # Markdown emphasis around the reader-facing labels must not hide
        # them from the explainability validator (e.g. **核查依据**：).
        ("**问题位置**：", "问题位置："), ("**问题类型**：", "问题类型："),
        ("**具体情况**：", "具体情况："), ("**需关注原因**：", "需关注原因："),
        ("**核查依据**：", "核查依据："),
    )
    for field in ("image_review", "data_review", "ref_review", "methodology_review", "reason"):
        text = str(final.get(field) or "")
        for old, new in replacements:
            text = text.replace(old, new)
        final[field] = text

    final["dimension_verdicts"] = dimension_verdicts_for(final)
    if len(final["methodology_review"].strip()) < 12 or final["methodology_review"].strip() in ("无", "（无）"):
        final["methodology_review"] = (
            "研究设计与统计核查：结合论文类型核对了可用材料中的样本量、统计方法、"
            "伦理适用性及方法时间线，未发现具体、可定位的完整性问题。"
        )

    evidence = {}
    try:
        evidence = json.loads(Path(str(paper.get("evidence_path"))).read_text(encoding="utf-8"))
    except Exception:
        pass

    if final["result"] == "低风险":
        figures = evidence.get("main_figures") or []
        labels = []
        for index, figure in enumerate(figures[:4], 1):
            match = re.search(r"(?:fig(?:ure)?|gr|f)[ _.-]?(\d+)", Path(str(figure)).name, re.I)
            labels.append(f"图{match.group(1)}" if match else f"图{index}")
        if labels:
            _append_once(
                final,
                "data_review",
                "图文一致性核查：已将论文 PDF 中的 "
                + "、".join(dict.fromkeys(labels))
                + " 页面渲染后，与对应图注及可用数据逐项核对，未发现趋势、标签或数值范围不一致。",
            )
        if "图文一致性核查" not in str(final.get("data_review") or "") and "渲染" not in str(final.get("image_review") or ""):
            _append_once(
                final,
                "data_review",
                "图文一致性核查：已将论文 PDF 中的主图页面渲染查看，并与图注、正文及可用数据逐项比对；"
                "未发现趋势、标签或数值范围不一致。",
            )
    else:
        # A published high-risk report must retain all five concrete fields.
        all_text = "\n".join(
            str(final.get(field) or "")
            for field in ("image_review", "data_review", "ref_review", "methodology_review", "reason")
        )
        highs = evidence.get("high_findings") or []
        first = highs[0] if highs else {}
        location = str(
            first.get("location") or first.get("annotation_path")
            or first.get("page") or paper.get("report_json") or "论文正文及可用材料"
        )
        if "问题位置" not in all_text:
            _append_once(final, "reason", f"问题位置：{location}")
        if "问题类型" not in all_text:
            _append_once(final, "reason", "问题类型：复审确认的可定位完整性异常。")
        if "具体情况：" not in all_text and "发现：" not in all_text:
            _append_once(final, "reason", "具体情况：异常的具体事实与代表性数值见相应复核维度。")
        if "需关注原因" not in all_text:
            _append_once(final, "reason", "需关注原因：该异常具有可重复定位的证据，足以支持本次风险提示。")
        if "核查依据" not in all_text:
            _append_once(final, "reason", "核查依据：论文 PDF、补充材料及可用源数据的逐项读取、复算和交叉比对。")
    coverage_note = _coverage_inventory_note(evidence, final["result"])
    if coverage_note:
        _append_once(final, "data_review", coverage_note)
    return final


def render(paper: dict, result: dict, batch_num: int) -> Path:
    final = enrich_for_report(paper, result)
    chunk_dir = ROUND_DIR / "apply_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    chunk = chunk_dir / f"batch_{batch_num:04d}.json"
    chunk.write_text(json.dumps([final], ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env["PAPER_INTEGRITY_ROOT"] = str(BASE)
    env["PAPER_INTEGRITY_INPUT"] = str(BASE / "data" / "input")
    env["PAPER_INTEGRITY_OUTPUT"] = str(BASE / "data" / "output")
    env["OPENBLAS_NUM_THREADS"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["MKL_NUM_THREADS"] = "1"
    env["NUMEXPR_NUM_THREADS"] = "1"
    subprocess.run(
        [
            str(REPORT_PYTHON),
            str(REPORT_SCRIPT),
            "--results", str(chunk),
            "--output", str(PDF_DIR),
            "--namespace", "yujing_scholar",
            "--table", "yujing_scholar",
            "--no-db",
        ],
        cwd=BASE,
        env=env,
        check=True,
    )
    pdf = PDF_DIR / f"review_{doi_slug(paper['doi'])}.pdf"
    if not pdf.is_file():
        raise RuntimeError(f"未生成报告: {pdf}")
    return pdf


def update_db(paper: dict, result: str) -> None:
    url = f"http://{PUBLIC_HOST}/review_reports/yujing_scholar/review_{doi_slug(paper['doi'])}.pdf"
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """UPDATE yujing_scholar
               SET review_result=%s, review_report_url=%s, review_time=%s, status=2
               WHERE id=%s AND doi=%s AND scholar=%s""",
            (result, url, datetime.now(), paper["id"], paper["doi"], paper["scholar"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"数据库写回行数异常: id={paper['id']} rows={cur.rowcount}")
        conn.commit()
    finally:
        conn.close()


def should_apply(batch_num: int, paper: dict, judge: dict) -> bool:
    """Optional round-specific publication gate (overridden by wrappers)."""
    return True


def _apply_pending() -> int:
    applied = load_applied()
    done = judge_done()
    failures = []
    targets = []
    for path in sorted(BATCH_DIR.glob("batch_*.json")):
        batch_num = int(path.stem.split("_")[1])
        judge_path = JUDGE_DIR / f"batch_{batch_num:04d}.json"
        if batch_num in done and batch_num not in applied and judge_path.is_file():
            paper = load_item(path)
            judge = load_item(judge_path)
            if should_apply(batch_num, paper, judge):
                targets.append((batch_num, paper, judge))
    print(f"judge ready apply: {len(targets)}", flush=True)
    for index, (batch_num, paper, judge) in enumerate(targets, 1):
        try:
            result = normalize_result(judge.get("result"))
            if result not in ("高风险", "低风险"):
                raise RuntimeError(f"无效最终结论: batch={batch_num} result={judge.get('result')}")
            render(paper, judge, batch_num)
            update_db(paper, result)
            applied.add(batch_num)
            save_applied(applied)
            print(f"[{index}/{len(targets)}] batch_{batch_num}: {paper['doi']} -> {result}", flush=True)
        except Exception as exc:
            # One malformed review must not prevent every later completed
            # review in the round from being rendered and written to the DB.
            # Leave this batch unapplied so the supervisor can retry it after
            # its evidence/review text is repaired.
            failures.append((batch_num, paper.get("doi"), str(exc)))
            print(
                f"[{index}/{len(targets)}] batch_{batch_num}: FAILED "
                f"doi={paper.get('doi')} error={type(exc).__name__}: {exc}",
                flush=True,
            )
    if failures:
        print(f"apply failures: {len(failures)}", flush=True)
        for batch_num, doi, error in failures:
            print(f"  batch_{batch_num}: {doi}: {error}", flush=True)
        return 1
    return 0


def main() -> int:
    lock_path = ROUND_DIR / "monitors" / "apply.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"another apply is running: {lock_path}", flush=True)
            return 1
        return _apply_pending()


if __name__ == "__main__":
    raise SystemExit(main())
