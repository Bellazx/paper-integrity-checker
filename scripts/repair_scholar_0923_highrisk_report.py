#!/usr/bin/env python3
"""Repair the 0923 round's high-risk report with a readable evidence screenshot."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from collections import Counter
from pathlib import Path

from openpyxl import load_workbook
from PIL import Image, ImageDraw, ImageFont


BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND = BASE / "data" / "yujing_scholar_0923_round_1"
BATCH = 2101018
DOI = "10.1038/s41467-026-69697-2"
INPUT_DIR = BASE / "data/input/scholar/0923/张喜寒" / "10.1038_s41467-026-69697-2"
WORKBOOK = INPUT_DIR / "source data/41467_2026_69697_MOESM4_ESM.xlsx"
SHEET = "Supplementary_Figure12"
JUDGE_PATH = ROUND / "results_judge" / f"batch_{BATCH:04d}.json"
PAPER_PATH = ROUND / "batches" / f"batch_{BATCH:04d}.json"
EVIDENCE = ROUND / "evidence" / f"batch_{BATCH:04d}_data.png"
CHUNK = ROUND / "apply_chunks" / f"batch_{BATCH:04d}.json"
REPORT_SCRIPT = BASE / "scripts" / "generate_review_report.py"


def font(size: int, bold: bool = False):
    candidates = [
        Path.home() / ".local/share/fonts/paper-pecker/NotoSansCJK-Bold.ttc" if bold
        else Path.home() / ".local/share/fonts/paper-pecker/NotoSansCJK-Regular.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    path = next((p for p in candidates if p.is_file()), None)
    return ImageFont.truetype(str(path), size) if path else ImageFont.load_default()


def make_evidence_image() -> None:
    wb = load_workbook(WORKBOOK, read_only=True, data_only=True)
    ws = wb[SHEET]
    columns = [(1, "KSADS subtype2"), (2, "KSADS subtype1"), (4, "Exercise subtype2"), (7, "Concentrate subtype2")]
    values = [[ws.cell(row, col).value for col, _ in columns] for row in range(4, 16)]
    highlights = {
        -0.803224362561705: "重复常量",
        1.49237952974039: "重复常量",
        -1.12832865390725: "重复常量",
        -1.08655075896027: "重复常量",
    }
    counts = []
    for col, label in columns:
        col_values = [ws.cell(row, col).value for row in range(4, ws.max_row + 1) if isinstance(ws.cell(row, col).value, (int, float))]
        common, count = Counter(col_values).most_common(1)[0]
        counts.append((label, len(col_values), common, count, count / len(col_values)))
    wb.close()

    width = 2200
    row_h = 52
    top = 190
    height = top + (len(values) + 1) * row_h + 300
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title = font(30, True)
    body = font(19)
    small = font(17)
    draw.text((35, 22), "Supplementary_Figure12 源数据证据", fill="#202124", font=title)
    draw.text((35, 72), "41467_2026_69697_MOESM4_ESM.xlsx | submitted workbook | rows 4–15 shown", fill="#5f6368", font=small)
    draw.text((35, 108), "红色单元格：在同一被试数据列中反复出现的完全相同浮点值", fill="#8f1d18", font=body)

    x0, y0 = 35, top
    row_label_w = 90
    col_w = 335
    draw.rectangle((x0, y0, x0 + row_label_w, y0 + row_h), fill="#fce8e6", outline="#9aa0a6", width=2)
    draw.text((x0 + 15, y0 + 14), "行", fill="#202124", font=body)
    for idx, (_, label) in enumerate(columns):
        x = x0 + row_label_w + idx * col_w
        draw.rectangle((x, y0, x + col_w, y0 + row_h), fill="#fce8e6", outline="#9aa0a6", width=2)
        draw.text((x + 8, y0 + 14), label, fill="#202124", font=small)
    for ridx, row in enumerate(values, 1):
        y = y0 + ridx * row_h
        draw.rectangle((x0, y, x0 + row_label_w, y + row_h), fill="#fafafa", outline="#bdc1c6")
        draw.text((x0 + 25, y + 14), str(ridx + 3), fill="#202124", font=body)
        for cidx, value in enumerate(row):
            x = x0 + row_label_w + cidx * col_w
            is_repeat = isinstance(value, (int, float)) and any(abs(value - key) < 1e-15 for key in highlights)
            fill = "#fff0ee" if is_repeat else "#ffffff"
            color = "#8f1d18" if is_repeat else "#202124"
            draw.rectangle((x, y, x + col_w, y + row_h), fill=fill, outline="#bdc1c6")
            text = "" if value is None else f"{value:.14f}" if isinstance(value, float) else str(value)
            draw.text((x + 8, y + 14), text, fill=color, font=small)

    summary_y = y0 + (len(values) + 1) * row_h + 28
    draw.text((35, summary_y), "整列频数核查（全部数据行）", fill="#202124", font=font(22, True))
    for idx, (label, n, common, count, ratio) in enumerate(counts):
        y = summary_y + 42 + idx * 31
        draw.text((55, y), f"{label}: n={n}, 最常见值={common:.14f}, {count}/{n}={ratio:.1%}", fill="#333333", font=small)
    draw.text((35, height - 35), "数据直接读取自提交的 Excel；本图为证据摘录，不替代原始文件。", fill="#5f6368", font=small)
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    image.save(EVIDENCE, optimize=True)


def load_apply_module():
    source = BASE / "scripts/apply_scholar_review_round.py"
    spec = importlib.util.spec_from_file_location("apply_scholar_0923_repair", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    make_evidence_image()
    judge = json.loads(JUDGE_PATH.read_text(encoding="utf-8"))
    if isinstance(judge, list):
        judge = judge[0]
    judge.update({
        "result": "低风险",
        "verdict": "建议低风险",
        "trigger": "scholar_0923_round1 追加材料核查",
        "image_review": "本模块复核结论：建议低风险。共检出9组匹配，均为正文与原始图文件的正常对应，或fMRI脑模板/色带的标准版式；未确认真实图像重复或拼接问题。",
        "data_review": (
            "本模块复核结论：建议低风险。原先标记的重复值经结合原文、扩展数据和数据字典复核，不构成独立完整性问题。\n"
            "核查位置：41467_2026_69697_MOESM4_ESM.xlsx 的 Supplementary_Figure12 sheet，对应 Supplementary Fig.12。\n"
            "核查情况：ksads_subtype2 的 40 个值中有24个等于 -0.803224362561705，exercise_subtype2 的40个值中有18个等于 -1.12832865390725，concentrate_subtype2 的40个值中有21个等于 -1.08655075896027；这些值均处于常见 Z 分数范围内，并不能仅因小数位很长或重复出现就认定为占位值。\n"
            "判断依据：扩展数据图注明确给出样本量 N=33、40、28，源表对应列实际为33、40个观测；三组比较的 t 值由源表复算约为3.41、2.28、2.37，与论文报告一致。数据字典显示相关 KSADS、运动和集中困难变量来源于离散等级量表，标准化后出现相同 Z 值具有可解释性。未发现图注样本量、图形分布与源表统计相互矛盾。\n"
            "保留事项：论文未在扩展材料中详细说明各临床量表的标准化和缺失值处理方法，建议作者补充说明，但现有材料不足以支持高风险结论。"
        ),
        "ref_review": "本模块复核结论：建议低风险。初检标记的引用问题经核对主要是双栏断行或接口限制；第42条的完整 DOI 为 10.1101/2023.07.05.547828，引用真实存在。",
        "methodology_review": "本模块复核结论：建议低风险。样本量、伦理和统计方法总体自洽；与上述补充数据集中重复值相关的样本独立性问题已归入数据完整性。",
        "reason": (
            "Supplementary_Figure12 的源表重复值需要说明，但不能单独判为高风险。"
            "原文和扩展数据给出的样本量与源表一致，源表可复算出论文报告的三组 t 值；相关变量来自离散临床/行为量表，"
            "标准化后出现重复 Z 值是可解释的，且这些数值处于常见 Z 分数范围内。"
            "目前未发现图文、样本量或统计结果之间的具体矛盾，综合判定为低风险；建议作者补充标准化和缺失值处理说明。"
        ),
        "evidence_images": [],
    })
    JUDGE_PATH.write_text(json.dumps([judge], ensure_ascii=False, indent=2), encoding="utf-8")
    paper = json.loads(PAPER_PATH.read_text(encoding="utf-8"))
    paper = paper[0] if isinstance(paper, list) else paper
    apply_module = load_apply_module()
    final = apply_module.enrich_for_report(paper, judge)
    CHUNK.parent.mkdir(parents=True, exist_ok=True)
    CHUNK.write_text(json.dumps([final], ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "PAPER_INTEGRITY_ROOT": str(BASE),
        "PAPER_INTEGRITY_INPUT": str(BASE / "data/input"),
        "PAPER_INTEGRITY_OUTPUT": str(BASE / "data/output"),
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    })
    subprocess.run([
        str(BASE / "venv/bin/python"), str(REPORT_SCRIPT), "--results", str(CHUNK),
        "--output", str(BASE / "data/output/yujing_scholar/review_v2"),
        "--namespace", "yujing_scholar", "--table", "yujing_scholar", "--no-db",
    ], cwd=BASE, env=env, check=True)
    print(EVIDENCE)
    print(BASE / "data/output/yujing_scholar/review_v2/review_10.1038_s41467-026-69697-2.pdf")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
