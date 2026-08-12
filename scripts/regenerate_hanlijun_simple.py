#!/usr/bin/env python3
"""Regenerate a concise, reader-oriented review PDF for one Han Lijun paper."""
from __future__ import annotations

import json
import os
from pathlib import Path

import fitz
from PIL import Image, ImageDraw
from PIL import ImageFont


BASE = Path(os.environ.get("PAPER_INTEGRITY_ROOT", Path(__file__).resolve().parents[1]))
DOI = "10.1109/ICRA55743.2025.11127751"
PAPER_DIR = BASE / "data/input/scholar/韩莉钧" / DOI.replace("/", "_")
PDF = PAPER_DIR / "Foresee_and_Act_Ahead_Task_Prediction_and_Pre-Scheduling_Enabled_Efficient_Robotic_Warehousing.pdf"
OUT = BASE / "data/output/yujing_scholar/review_v2"
EVIDENCE = BASE / "work/hanlijun_simple"
RESULTS = EVIDENCE / "results.json"


def crop_page(doc: fitz.Document, page_no: int, rect: tuple[float, float, float, float], name: str) -> Path:
    page = doc[page_no - 1]
    clip = fitz.Rect(*rect)
    pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), clip=clip, alpha=False)
    path = EVIDENCE / name
    pix.save(str(path))
    return path


def composite(*paths: Path, name: str, labels: list[str]) -> Path:
    images = [Image.open(path).convert("RGB") for path in paths]
    width = max(im.width for im in images)
    label_h = 54
    height = sum(im.height + label_h for im in images)
    canvas = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(canvas)
    font_path = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    font = ImageFont.truetype(str(font_path), 28) if font_path.is_file() else None
    y = 0
    for im, label in zip(images, labels):
        draw.rectangle((0, y, width, y + label_h), fill="#fff1f0")
        draw.text((18, y + 12), label, fill="#8f1d18", font=font)
        y += label_h
        canvas.paste(im, (0, y))
        y += im.height
    out = EVIDENCE / name
    canvas.save(out, optimize=True)
    return out


def build_evidence() -> list[dict]:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for old in EVIDENCE.glob("evidence_*"):
        old.unlink()
    with fitz.open(PDF) as doc:
        # The coordinates are the exact text/table regions in the published PDF.
        claim = crop_page(doc, 5, (300, 475, 580, 635), "evidence_01_claim.png")
        table_i = crop_page(doc, 6, (55, 42, 565, 255), "evidence_02_table_i.png")
        table_ii = crop_page(doc, 6, (55, 250, 565, 365), "evidence_03_table_ii.png")
        conclusion = crop_page(doc, 6, (305, 365, 580, 590), "evidence_04_conclusion.png")
        table_iv = crop_page(doc, 6, (55, 610, 300, 725), "evidence_05_table_iv.png")
    # Combine the two locations needed for the MM/MS mapping and the 50–60% claim.
    mapping = composite(
        table_ii,
        table_iv,
        name="evidence_04_mapping_and_conclusion.png",
        labels=["TABLE II: MM/MS task counts in the original header", "TABLE IV: MM/MS map sizes in the original table"],
    )
    conclusion_combo = composite(
        conclusion,
        table_ii,
        name="evidence_05_conclusion_and_values.png",
        labels=["MAIN TEXT: 50-60% gain and about 1% MTR claim", "TABLE II: reported ERR/MPT/MTR values"],
    )
    # Keep four focused screenshots in the report: claim, Table I, mapping, conclusion.
    return [
        {"dimension": "data", "path": str(claim), "caption": "证据 1｜第 5 页：正文声称 Horizon 10 的 RMSE 提升为 5.3%。"},
        {"dimension": "data", "path": str(table_i), "caption": "证据 2｜第 6 页 Table I：RMSE 数值可复算的最大提升仅约 1.18%，BB 上本文方法反而较差。"},
        {"dimension": "data", "path": str(mapping), "caption": "证据 3｜第 6 页 Table II 与 Table IV：MM/MS 的任务数和地图规模定义相互对调。"},
        {"dimension": "data", "path": str(conclusion_combo), "caption": "证据 4｜第 6 页正文与 Table II：50–60%/约 1% 的概括与实际数值不完全一致。"},
    ]


def main() -> int:
    evidence = build_evidence()
    result = {
        "doi": DOI,
        "result": "高风险",
        "verdict": "建议高风险",
        "trigger": "韩莉钧批量复审（简明版）",
        "_input_dir": str(PAPER_DIR),
        "_report_json": str(BASE / "data/output/yujing_scholar/init" / DOI.replace("/", "_") / "report.json"),
        "evidence_images": evidence,
        "image_review": "未确认图像重复或拼接问题。",
        "data_review": (
            "问题位置：第 5–6 页的正文、Table I、Table II 和 Table IV。\n"
            "问题类型：正文量化结论与论文自身表格不一致。\n"
            "具体情况：确认 3 个可复核的问题。\n"
            "1. 第 5 页称 Horizon 10 的 RMSE 提升 5.3%；但按第 6 页 Table I，最大实际提升仅为 "
            "(1.2754−1.2603)/1.2754=1.18%，BB 数据集本文方法还比 DDSTGCN 差 4.67%。\n"
            "2. 第 6 页 Table II 把 MM/MS 任务数写成 3601/6398，而 Table IV 定义为 6398/3601；正文还把 MM 称为最小地图，和 Table IV 冲突。\n"
            "3. 正文称 ERR/MPT 提升 50–60%、MTR 约不超过 1%；但 Table II 在最长步长下实际为 ML 43.7%、MM 46.7%、MS 55.9%，MTR 最高 1.25%。\n"
            "需关注原因：这些数字直接支撑论文关于预测优势和大规模可扩展性的结论，读者无法按表格复现或确定场景对应关系。\n"
            "核查依据：逐项读取原始 PDF 第 5–6 页，按表 I 的模型数值和表 II 的 classic 基线重新计算提升率，并对照表 IV 的地图规模定义。"
        ),
        "ref_review": "本模块复核结论：建议低风险。参考文献维度未发现实质性问题，不构成高风险。",
        "methodology_review": "未发现需要单独升级风险等级的研究设计问题；本次高风险结论仅基于上面的可复核数字与场景定义矛盾。",
        "reason": (
            "简明结论：论文正文的关键量化结论有 3 处无法与论文自己的表格稳定对应，影响读者复核其性能优势和可扩展性。"
            "这说明论文需要作者解释或更正，不等同于已经证明存在数据造假。"
        ),
    }
    RESULTS.write_text(json.dumps([result], ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
