#!/usr/bin/env python3
"""Create reader-facing evidence screenshots for the PNAS Reactome finding."""
from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import openpyxl
from PIL import Image, ImageDraw, ImageFont

BASE = Path("/home/sjtu/paper-integrity-checker")
ROOT = BASE / "data/input/9月-八刊 - 通讯/10.1073_pnas.2535342123/source data"
OUT = BASE / "data/output/yujing_4050_202607/review_evidence"
FONT = "/home/library/.local/share/fonts/paper-pecker/NotoSansCJK-Regular.ttc"
BOLD = "/home/library/.local/share/fonts/paper-pecker/NotoSansCJK-Bold.ttc"


def fonts():
    return (
        ImageFont.truetype(BOLD, 34, index=2),
        ImageFont.truetype(FONT, 24, index=2),
        ImageFont.truetype(FONT, 21, index=2),
        ImageFont.truetype(FONT, 18, index=2),
    )


def norm_gene(value: object) -> str:
    return str(value or "").strip()


def read_rows(filename: str, rows: list[int]):
    path = ROOT / filename
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    headers = [c.value for c in next(ws.iter_rows(min_row=2, max_row=2))]
    index = {str(v): i for i, v in enumerate(headers)}
    output = []
    for row_no in rows:
        values = [c.value for c in next(ws.iter_rows(min_row=row_no, max_row=row_no))]
        gene = norm_gene(values[index["geneID"]])
        output.append({
            "row": row_no,
            "id": values[index["ID"]],
            "description": str(values[index["Description"]] or "").replace("  ", " "),
            "bg": values[index["BgRatio"]],
            "count": values[index["Count"]],
            "gene": gene,
            "gene_count": len([x for x in gene.split("/") if x]),
            "gene_prefix": gene[:74] + ("…" if len(gene) > 74 else ""),
            "gene_hash": hashlib.sha256(gene.encode("utf-8")).hexdigest()[:12],
            "file": filename,
        })
    return output


def wrap(value: object, width: int) -> str:
    return "\n".join(textwrap.wrap(str(value or ""), width=width, break_long_words=True, break_on_hyphens=False)) or "—"


def draw_table(path: Path, title: str, subtitle: str, rows: list[dict]):
    title_font, head_font, body_font, small_font = fonts()
    columns = [
        ("Excel行", 100, lambda r: str(r["row"])),
        ("Reactome通路ID", 245, lambda r: str(r["id"])),
        ("通路名称", 520, lambda r: wrap(r["description"], 28)),
        ("BgRatio", 180, lambda r: str(r["bg"])),
        ("Count", 130, lambda r: str(r["count"])),
        ("geneID（前缀）", 760, lambda r: wrap(f"{r['gene_prefix']}\n共{r['gene_count']}个；摘要校验值 {r['gene_hash']}", 46)),
    ]
    margin = 46
    header_h = 62
    row_h = 122
    title_h = 150
    width = margin * 2 + sum(w for _, w, _ in columns)
    height = title_h + header_h + row_h * len(rows) + 110
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((margin, 24), title, fill="#8f1d18", font=title_font)
    draw.multiline_text((margin, 76), subtitle, fill="#444", font=small_font, spacing=6)
    x0, y0 = margin, title_h
    x = x0
    for name, w, _ in columns:
        draw.rectangle((x, y0, x + w, y0 + header_h), fill="#fce8e6", outline="#9b3a32", width=2)
        draw.multiline_text((x + 10, y0 + 12), wrap(name, 16), fill="#202124", font=head_font, spacing=2)
        x += w
    for idx, row in enumerate(rows):
        y = y0 + header_h + idx * row_h
        x = x0
        fill = "#fff3f1" if idx else "#fffafa"
        for _, w, value_fn in columns:
            draw.rectangle((x, y, x + w, y + row_h), fill=fill, outline="#bdc1c6", width=2)
            draw.multiline_text((x + 10, y + 12), value_fn(row), fill="#202124", font=body_font, spacing=4)
            x += w
    draw.text((margin, height - 65), "红框区域：不同通路行的 BgRatio、Count 和 geneID 摘要校验值相同；数据直接读取提交的 Excel 源文件。", fill="#8f1d18", font=small_font)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)


def main():
    sd01 = read_rows("pnas.2535342123.sd01.xlsx", [6, 7, 8])
    sd02 = read_rows("pnas.2535342123.sd02.xlsx", [44, 45, 47, 48])
    sd03 = read_rows("pnas.2535342123.sd03.xlsx", [9, 10, 33, 34])
    draw_table(
        OUT / "reactome_duplicate_sd01.png",
        "证据截图 1｜sd01 中三条不同通路的结果完全相同",
        "文件：pnas.2535342123.sd01.xlsx；Excel 第6–8行。三条通路名称不同，但 BgRatio、Count 和 geneID 摘要校验值一致。",
        sd01,
    )
    draw_table(
        OUT / "reactome_duplicate_sd02_sd03.png",
        "证据截图 2｜sd02、sd03 中不同通路成对重复",
        "文件：pnas.2535342123.sd02.xlsx（第44–48行）与 sd03.xlsx（第9–10、33–34行）。每对通路的基因数、BgRatio、Count 和 geneID 摘要校验值一致。",
        sd02 + sd03,
    )
    print(OUT / "reactome_duplicate_sd01.png")
    print(OUT / "reactome_duplicate_sd02_sd03.png")


if __name__ == "__main__":
    main()
