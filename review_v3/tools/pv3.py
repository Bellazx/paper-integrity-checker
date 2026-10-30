"""pv3: dossier tool used by review sessions through Bash.

Every subcommand prints bounded output. Paths and the active ledger come from
the environment prepared by the session runner (PV3_WORKDIR, PV3_LEDGER,
PV3_ACTOR), so commands stay short and cannot touch other papers' ledgers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_v3 import config, ledger, memory  # noqa: E402

LIMIT = config.TOOL_OUTPUT_CHARS


def _workdir() -> Path:
    value = os.environ.get("PV3_WORKDIR")
    if not value:
        sys.exit("PV3_WORKDIR 未设置")
    return Path(value)


def _index() -> dict:
    return json.loads((_workdir() / "dossier" / "index.json").read_text(encoding="utf-8"))


def _emit(text: str, limit: int = LIMIT, spill_name: str = "") -> None:
    if len(text) <= limit:
        print(text)
        return
    spill = _workdir() / "dossier" / "spill"
    spill.mkdir(exist_ok=True)
    name = spill_name or hashlib.sha1(text.encode()).hexdigest()[:12]
    path = spill / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    print(text[:limit])
    print(f"\n…输出已截断（共 {len(text)} 字符）。完整内容：{path}（可用 grep 定位后再读）")


def _doc(index: dict, doc_id: str) -> dict:
    for doc in index["docs"]:
        if doc["id"] == doc_id or doc["name"] == doc_id:
            return doc
    sys.exit(f"未知文档 {doc_id}；可用：{', '.join(d['id'] for d in index['docs'])}")


def _page_file(doc_id: str, page: int) -> Path:
    return _workdir() / "dossier" / "text" / f"{doc_id}_p{page:03d}.txt"


# ---------------------------------------------------------------- dossier

def cmd_info(args):
    index = _index()
    out = [f"DOI: {index['doi']}", f"题名: {(index.get('paper') or {}).get('title')}",
           f"原始材料目录: {index['input_dir']}", "文档:"]
    for doc in index["docs"]:
        out.append(f"  {doc['id']}: {doc['name']}，{doc.get('pages')} 页，页面尺寸(pt) {doc.get('page_size_pt')}")
    figs = [c for c in index["captions"] if c["kind"] == "figure"]
    tables = [c for c in index["captions"] if c["kind"] == "table"]
    out.append(f"图注 {len(figs)} 条、表题 {len(tables)} 条（pv3 captions 查看）；主图编号：{index.get('main_figure_numbers')}")
    out.append(f"数据文件 {len(index['data_files'])} 个（pv3 files 查看）")
    out.append(f"图像告警 {len(index['image_alerts'])} 组，参考文献提示 {len(index['reference_alerts'])} 条")
    if index.get("warnings"):
        out.append("资料包提示：" + "；".join(index["warnings"]))
    _emit("\n".join(out))


def cmd_page(args):
    index = _index()
    doc = _doc(index, args.doc)
    last = args.to or args.page
    chunks = []
    for page in range(args.page, last + 1):
        path = _page_file(doc["id"], page)
        if not path.is_file():
            chunks.append(f"=== {doc['id']} 第{page}页：无文本（超出页数或抽取失败）")
            continue
        chunks.append(f"=== {doc['id']} 第{page}页 ===\n" + path.read_text(encoding="utf-8"))
    _emit("\n".join(chunks), limit=max(LIMIT, 9000 if last == args.page else LIMIT * 2))


def cmd_find(args):
    index = _index()
    pattern = re.compile(args.regex, re.I)
    hits = []
    for doc in index["docs"]:
        if args.doc and doc["id"] != args.doc:
            continue
        for page in range(1, (doc.get("pages") or 0) + 1):
            path = _page_file(doc["id"], page)
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines):
                if pattern.search(line):
                    lo, hi = max(0, number - args.context), min(len(lines), number + args.context + 1)
                    snippet = " ⏎ ".join(l.strip() for l in lines[lo:hi])
                    hits.append(f"{doc['id']} p{page} L{number + 1}: {snippet[:400]}")
                    if len(hits) >= args.max:
                        break
            if len(hits) >= args.max:
                break
    _emit("\n".join(hits) if hits else "无匹配")


def cmd_captions(args):
    index = _index()
    rows = [f"{c['doc']} p{c['page']} {c['label']}: {c['text'][:220]}" for c in index["captions"]
            if not args.kind or c["kind"] == args.kind]
    _emit("\n".join(rows) if rows else "未识别到图注/表题；用 pv3 find 'Fig' 查找")


def cmd_refs(args):
    text = (_workdir() / "dossier" / "references.txt").read_text(encoding="utf-8")
    if not text:
        print("未定位到参考文献段；用 pv3 find 'References' 查找所在页后 pv3 page 查看")
        return
    if args.numbers:
        out = []
        for number in args.numbers:
            match = re.search(rf"(?m)^\s*(?:\[{number}\]|{number}\.|{number}\s)(.+(?:\n(?!\s*(?:\[\d+\]|\d+\.\s|\d+\s+[A-Z])).+){{0,4}})", text)
            out.append(f"[{number}] " + (match.group(1).replace("\n", " ").strip()[:600] if match else "未定位，用 pv3 find 查找"))
        _emit("\n".join(out))
    else:
        _emit(text, spill_name="references")


def cmd_files(args):
    index = _index()
    rows = []
    for path in index["data_files"]:
        size = Path(path).stat().st_size if Path(path).exists() else 0
        rows.append(f"{path}  ({size // 1024} KB)")
    rows.append("")
    for entry in index.get("sheets") or []:
        rows.append(f"{entry['file']}:")
        for sheet in entry["sheets"]:
            rows.append(f"  sheet {sheet['sheet']}: {sheet.get('rows')} 行 × {sheet.get('cols')} 列；列: {', '.join(sheet['columns'][:12])}")
    _emit("\n".join(rows))


def cmd_alerts(args):
    index = _index()
    key = {"image": "image_alerts", "ref": "reference_alerts"}[args.kind]
    rows = [json.dumps(item, ensure_ascii=False) for item in index.get(key) or []]
    if args.kind == "image" and not args.all:
        rows = [json.dumps({k: v for k, v in item.items() if k not in ("region_a", "region_b")}, ensure_ascii=False)
                for item in index.get(key) or []]
    _emit("\n".join(rows) if rows else "无告警")


def cmd_item(args):
    data = ledger.load(os.environ["PV3_LEDGER"])
    item = (data.get("items") or {}).get(args.id)
    if not item:
        sys.exit(f"台账中无 {args.id}")
    _emit(json.dumps(item, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- images

def _render_out(name: str) -> Path:
    path = _workdir() / "dossier" / "renders" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _downscale(path: Path, max_side: int) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
            im.save(path)
        return im.size


def cmd_render(args):
    import fitz
    index = _index()
    doc = _doc(index, args.doc)
    clip = [float(v) for v in args.clip.split(",")] if args.clip else None
    tag = "full" if not clip else "c" + "_".join(str(int(v)) for v in clip)
    out = _render_out(f"{doc['id']}_p{args.page:03d}_{tag}_d{args.dpi}.png")
    with fitz.open(doc["path"]) as pdf:
        if not 1 <= args.page <= pdf.page_count:
            sys.exit(f"页码超出范围 1..{pdf.page_count}")
        page = pdf[args.page - 1]
        rect = page.rect
        if not out.exists():
            pix = page.get_pixmap(dpi=args.dpi, clip=fitz.Rect(*clip) if clip else None)
            pix.save(str(out))
    size = _downscale(out, args.max_side)
    print(f"{out}  ({size[0]}×{size[1]} px；页面 {rect.width:.0f}×{rect.height:.0f} pt"
          f"{'，裁剪 ' + args.clip if clip else ''}）。用 Read 查看该 PNG。")
    print(f"作为证据截图时可在台账 evidence 中写 {{\"path\": \"{out}\", \"caption\": \"…\"}}")


def cmd_locate(args):
    import fitz
    index = _index()
    doc = _doc(index, args.doc)
    with fitz.open(doc["path"]) as pdf:
        page = pdf[args.page - 1]
        rects = page.search_for(args.text)
        if not rects:
            print("未找到该文字（可能是图片中的文字）；可先渲染整页估计坐标")
        for r in rects[:20]:
            print(f"[{r.x0:.0f},{r.y0:.0f},{r.x1:.0f},{r.y1:.0f}]")
        print(f"页面尺寸 {page.rect.width:.0f}×{page.rect.height:.0f} pt；clip 用 x0,y0,x1,y1（pt）")


def cmd_view(args):
    """Copy an image (e.g. detector annotation) at a readable size, optionally cropped."""
    from PIL import Image
    src = Path(args.path)
    if not src.is_file():
        sys.exit(f"文件不存在 {src}")
    digest = hashlib.sha1(f"{src}|{args.box}|{args.max_side}".encode()).hexdigest()[:10]
    out = _render_out(f"view_{src.stem[:40]}_{digest}.png")
    with Image.open(src) as im:
        original = im.size
        if args.box:
            box = tuple(int(float(v)) for v in args.box.split(","))
            im = im.crop(box)
        im = im.convert("RGB")
        if max(im.size) > args.max_side:
            im.thumbnail((args.max_side, args.max_side))
        im.save(out)
        size = im.size
    print(f"{out}  ({size[0]}×{size[1]} px；原图 {original[0]}×{original[1]} px"
          f"{'，裁剪框 ' + args.box if args.box else ''}）。用 Read 查看。")


# ---------------------------------------------------------------- tables

def _resolve_data(name: str) -> Path:
    path = Path(name)
    if path.is_file():
        return path
    matches = [Path(p) for p in _index()["data_files"] if Path(p).name == name or p.endswith("/" + name)]
    if not matches:
        sys.exit(f"找不到数据文件 {name}；pv3 files 查看可用文件")
    return matches[0]


def _col_index(token: str) -> int:
    token = token.strip()
    if token.isdigit():
        return int(token)
    value = 0
    for ch in token.upper():
        value = value * 26 + (ord(ch) - 64)
    return value


def _col_letter(index: int) -> str:
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


def _parse_cols(spec: str | None) -> list[int] | None:
    if not spec:
        return None
    cols = []
    for part in spec.split(","):
        if ":" in part:
            a, b = part.split(":")
            cols.extend(range(_col_index(a), _col_index(b) + 1))
        else:
            cols.append(_col_index(part))
    return cols


def _iter_rows(path: Path, sheet: str | None):
    """Yield (sheet_names, row_iter) with 1-based row numbers and raw values."""
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        names = wb.sheetnames
        if sheet is None:
            return names, None
        if sheet not in names:
            sys.exit(f"无 sheet {sheet!r}；可用：{names}")
        return names, (row for row in wb[sheet].iter_rows(values_only=True))
    if suffix == ".xls":
        import pandas as pd
        book = pd.read_excel(path, sheet_name=None, header=None)
        names = list(book)
        if sheet is None:
            return names, None
        return names, (tuple(r) for r in book[sheet].itertuples(index=False))
    if suffix in (".csv", ".tsv", ".txt"):
        import csv
        delimiter = "\t" if suffix in (".tsv", ".txt") else ","
        handle = open(path, newline="", encoding="utf-8", errors="replace")
        return ["(text)"], (tuple(r) for r in csv.reader(handle, delimiter=delimiter))
    if suffix == ".docx":
        import docx
        document = docx.Document(str(path))
        names = [f"table{i + 1}" for i in range(len(document.tables))]
        if sheet is None:
            return names, None
        table = document.tables[int(sheet.replace("table", "")) - 1]
        return names, _docx_rows(table)
    sys.exit(f"不支持的文件类型 {suffix}；可直接用 Read 查看文本")


def _docx_rows(table):
    """Rows from the table XML, marking merged cells.

    python-docx repeats a merged cell's text in every grid position it spans,
    which looks exactly like duplicated data. Here each physical cell appears
    once; horizontal spans and vertical continuations are labelled.
    """
    from docx.oxml.ns import qn
    for tr in table._tbl.tr_lst:
        row = []
        for tc in tr.tc_lst:
            text = "".join(t.text or "" for t in tc.iter(qn("w:t"))).strip()
            props = tc.tcPr
            span = props.find(qn("w:gridSpan")) if props is not None else None
            vmerge = props.find(qn("w:vMerge")) if props is not None else None
            if vmerge is not None and vmerge.get(qn("w:val")) != "restart":
                row.append("⟨与上一行合并⟩")
                continue
            if span is not None and int(span.get(qn("w:val"), "1")) > 1:
                text += f"⟨合并{span.get(qn('w:val'))}列⟩"
            row.append(text)
        yield tuple(row)


def cmd_sheet(args):
    path = _resolve_data(args.file)
    names, rows = _iter_rows(path, args.sheet)
    if rows is None:
        print(f"{path.name} 的工作表：{names}")
        return
    start, end = 1, args.limit
    if args.rows:
        a, _, b = args.rows.partition(":")
        start, end = int(a or 1), int(b or (int(a or 1) + args.limit - 1))
    cols = _parse_cols(args.cols)
    out = []
    find = str(args.find) if args.find is not None else None
    for number, row in enumerate(rows, start=1):
        if find is None and number < start:
            continue
        if find is None and number > end:
            break
        values = list(row)
        if cols:
            values = [values[c - 1] if c - 1 < len(values) else None for c in cols]
            labels = [_col_letter(c) for c in cols]
        else:
            labels = [_col_letter(i + 1) for i in range(len(values))]
        if find is not None and not any(find in str(v) for v in values):
            continue
        cells = [f"{lab}={'' if v is None else v}" for lab, v in zip(labels, values) if v not in (None, "")]
        out.append(f"R{number}: " + " | ".join(cells))
        if len(out) >= args.limit:
            break
    if path.suffix.lower() == ".docx":
        out.append("（docx 表格按物理单元格输出：⟨合并N列⟩/⟨与上一行合并⟩ 为合并单元格，不是重复数据）")
    _emit("\n".join(out) if out else "无数据", spill_name=f"sheet_{path.stem}_{args.sheet}")


def _column(path: Path, sheet: str, col: int, start: int) -> list:
    _, rows = _iter_rows(path, sheet)
    values = []
    for number, row in enumerate(rows, start=1):
        if number < start:
            continue
        values.append(row[col - 1] if col - 1 < len(row) else None)
    while values and values[-1] in (None, ""):
        values.pop()
    return values


def _num(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def cmd_colcmp(args):
    """Compare two columns value by value: identity, offsets, constant ratio, correlation."""
    path_a = _resolve_data(args.file)
    path_b = _resolve_data(args.file_b or args.file)
    a = _column(path_a, args.sheet, _col_index(args.col_a), args.start)
    b = _column(path_b, args.sheet_b or args.sheet, _col_index(args.col_b), args.start_b or args.start)
    pairs = [(x, y) for x, y in zip(a, b) if x not in (None, "") or y not in (None, "")]
    numeric = [(_num(x), _num(y)) for x, y in pairs]
    numeric = [(x, y) for x, y in numeric if x is not None and y is not None]
    identical = sum(1 for x, y in pairs if str(x) == str(y))
    out = [f"A: {path_a.name}/{args.sheet}/{args.col_a}（{len(a)} 个）  B: {path_b.name}/{args.sheet_b or args.sheet}/{args.col_b}（{len(b)} 个）",
           f"对齐比较 {len(pairs)} 对，逐值完全相同 {identical} 对（{identical / max(1, len(pairs)):.1%}）"]
    if len(numeric) >= 3:
        xs, ys = zip(*numeric)
        diffs = [y - x for x, y in numeric]
        ratios = [y / x for x, y in numeric if x]
        mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
        sxx = sum((x - mean_x) ** 2 for x in xs)
        syy = sum((y - mean_y) ** 2 for y in ys)
        sxy = sum((x - mean_x) * (y - mean_y) for x, y in numeric)
        r = sxy / math.sqrt(sxx * syy) if sxx and syy else float("nan")
        slope = sxy / sxx if sxx else float("nan")
        out.append(f"数值对 {len(numeric)}；r={r:.6f}；斜率={slope:.6f}；截距={mean_y - slope * mean_x:.6g}")
        out.append(f"差值 B−A：最小 {min(diffs):.6g}，最大 {max(diffs):.6g}，不同差值个数 {len(set(round(d, 9) for d in diffs))}")
        if ratios:
            out.append(f"比值 B/A：最小 {min(ratios):.6g}，最大 {max(ratios):.6g}，不同比值个数 {len(set(round(q, 9) for q in ratios))}")
    out.append("前 8 对样例：" + "; ".join(f"{x}|{y}" for x, y in pairs[:8]))
    _emit("\n".join(out))


# ---------------------------------------------------------------- external

def cmd_forensic(args):
    index = _index()
    cache = _workdir() / "dossier" / f"forensic_{args.mode}.txt"
    if cache.is_file() and not args.refresh:
        _emit(cache.read_text(encoding="utf-8"), spill_name=f"forensic_{args.mode}")
        return
    script = config.SKILL_SCRIPTS / "forensic_checks.py"
    try:
        done = subprocess.run([config.PYTHON, str(script), args.mode, "--excel-dir", index["input_dir"]],
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        text = done.stdout + (("\n[stderr]\n" + done.stderr[-1500:]) if done.returncode else "")
    except subprocess.TimeoutExpired:
        text = "扫描超时（300 s）；请对关键列定向核查，并在台账中说明扫描未完成"
    cache.write_text(text, encoding="utf-8")
    _emit(text, spill_name=f"forensic_{args.mode}")


def cmd_crossref(args):
    import urllib.parse
    import urllib.request
    cache_dir = _workdir() / "dossier" / "crossref"
    cache_dir.mkdir(exist_ok=True)
    if args.doi:
        url = "https://api.crossref.org/works/" + urllib.parse.quote(args.doi.strip())
    else:
        url = "https://api.crossref.org/works?rows=3&query.bibliographic=" + urllib.parse.quote(args.query)
    key = cache_dir / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".json")
    if key.is_file():
        data = json.loads(key.read_text(encoding="utf-8"))
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "paper-integrity-review/3 (mailto:paper-check@example.com)"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            code = getattr(exc, "code", None)
            print(f"CrossRef 查询失败：{code or exc}。404 表示该 DOI 未注册；超时/限流不是引用异常证据。")
            return
        key.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    message = data.get("message") or {}
    works = message.get("items") if "items" in message else [message]
    rows = []
    for work in works[:3]:
        authors = ", ".join(f"{a.get('family', '')} {a.get('given', '')[:1]}" for a in (work.get("author") or [])[:6])
        issued = ((work.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        rows.append(f"DOI {work.get('DOI')} | {issued} | {(work.get('container-title') or [''])[0]} "
                    f"{work.get('volume', '')}:{work.get('page', '')} | {(work.get('title') or [''])[0][:160]} | {authors}")
    _emit("\n".join(rows) if rows else "无结果")


def cmd_refcheck(args):
    path = _workdir() / "dossier" / "refcheck.json"
    if not path.is_file():
        refs = (_workdir() / "dossier" / "references.txt")
        if refs.is_file() and refs.read_text(encoding="utf-8").strip():
            print("参考文献逐条核验仍在后台进行，请先处理其他内容，稍后再运行 pv3 refcheck")
        else:
            print("未定位到参考文献段，无法逐条核验；用 pv3 find 'References' 查找后人工核查")
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    out = [f"共解析 {report['entries']} 条；结果统计 {report['counts']}（ok=题名与权威记录一致）",
           "说明：lookup_failed 是网络/接口问题，不是引用异常；weak_match/no_match 多为无 DOI 条目检索不到，需看原文判断。"]
    for row in report["results"]:
        if not args.all and row["status"] == "ok":
            continue
        record = row.get("record") or {}
        out.append(f"[{row['num']}] {row['status']} 相似度={row.get('title_similarity')} {row.get('note', '')}\n"
                   f"   原文：{row['text'][:260]}\n"
                   f"   权威记录：{record.get('doi')} | {record.get('year')} | {record.get('journal')} | {record.get('title')} | {record.get('authors')}")
    _emit("\n".join(out), spill_name="refcheck")


def cmd_mem(args):
    hits = memory.search(args.query, dimension=args.dim or "", k=args.k,
                         exclude_doi=os.environ.get("PV3_DOI", ""))
    if not hits:
        print("未检索到相关经验")
        return
    rows = []
    for hit in hits:
        rows.append(f"- {hit['description']}" + (f"\n  做法：{hit['apply']}" if hit["apply"] else ""))
    _emit("\n".join(rows))


# ---------------------------------------------------------------- ledger

def cmd_ledger(args):
    path = os.environ.get("PV3_LEDGER")
    if not path:
        sys.exit("PV3_LEDGER 未设置")
    if args.action == "show":
        data = ledger.load(path)
        counts = ledger.counts(data)
        out = [f"台账 {data.get('shard')}（{data.get('role')}）：共 {counts['total']} 项，已关闭 {counts['closed']}，"
               f"未处理 {counts['open']}，确认问题 {counts['issue']}，已排除 {counts['cleared']}，不适用 {counts['not_applicable']}，未能完成 {counts['unresolved']}"]
        for item in (data.get("items") or {}).values():
            if args.open and item.get("status", "open") != "open":
                continue
            line = f"[{item.get('status', 'open')}] {item['id']} — {item.get('title', '')[:160]}"
            if item.get("status") == "issue":
                line += f"（{item.get('severity')}）"
            out.append(line)
        _emit("\n".join(out))
        return
    raw = sys.stdin.read() if not args.file else Path(args.file).read_text(encoding="utf-8")
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        sys.exit(f"JSON 解析失败：{exc}")
    applied, errors = ledger.update(path, entries if isinstance(entries, list) else [entries],
                                    os.environ.get("PV3_ACTOR", "agent"))
    counts = ledger.counts(ledger.load(path))
    print(f"已写入 {applied} 条；台账剩余未处理 {counts['open']} / {counts['total']}")
    if errors:
        print("以下记录被拒绝，请修正后重新提交：\n- " + "\n- ".join(errors))
        sys.exit(2)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pv3", description="论文复核资料包工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info").set_defaults(func=cmd_info)
    p = sub.add_parser("page"); p.add_argument("doc"); p.add_argument("page", type=int); p.add_argument("to", type=int, nargs="?"); p.set_defaults(func=cmd_page)
    p = sub.add_parser("find"); p.add_argument("regex"); p.add_argument("--doc"); p.add_argument("--context", type=int, default=0); p.add_argument("--max", type=int, default=60); p.set_defaults(func=cmd_find)
    p = sub.add_parser("captions"); p.add_argument("--kind", choices=["figure", "table"]); p.set_defaults(func=cmd_captions)
    p = sub.add_parser("refs"); p.add_argument("numbers", nargs="*", type=int); p.set_defaults(func=cmd_refs)
    sub.add_parser("files").set_defaults(func=cmd_files)
    p = sub.add_parser("alerts"); p.add_argument("kind", choices=["image", "ref"]); p.add_argument("--all", action="store_true"); p.set_defaults(func=cmd_alerts)
    p = sub.add_parser("item"); p.add_argument("id"); p.set_defaults(func=cmd_item)
    p = sub.add_parser("render"); p.add_argument("doc"); p.add_argument("page", type=int); p.add_argument("--clip"); p.add_argument("--dpi", type=int, default=config.RENDER_DPI); p.add_argument("--max-side", type=int, default=config.RENDER_MAX_SIDE); p.set_defaults(func=cmd_render)
    p = sub.add_parser("locate"); p.add_argument("doc"); p.add_argument("page", type=int); p.add_argument("text"); p.set_defaults(func=cmd_locate)
    p = sub.add_parser("view"); p.add_argument("path"); p.add_argument("--box"); p.add_argument("--max-side", type=int, default=config.RENDER_MAX_SIDE); p.set_defaults(func=cmd_view)
    p = sub.add_parser("sheet"); p.add_argument("file"); p.add_argument("sheet", nargs="?"); p.add_argument("--rows"); p.add_argument("--cols"); p.add_argument("--find"); p.add_argument("--limit", type=int, default=60); p.set_defaults(func=cmd_sheet)
    p = sub.add_parser("colcmp"); p.add_argument("file"); p.add_argument("sheet"); p.add_argument("col_a"); p.add_argument("col_b")
    p.add_argument("--file-b"); p.add_argument("--sheet-b"); p.add_argument("--start", type=int, default=2); p.add_argument("--start-b", type=int); p.set_defaults(func=cmd_colcmp)
    p = sub.add_parser("forensic"); p.add_argument("mode", choices=["scan-digits", "scan-decimal-tails"]); p.add_argument("--refresh", action="store_true"); p.set_defaults(func=cmd_forensic)
    p = sub.add_parser("crossref"); g = p.add_mutually_exclusive_group(required=True); g.add_argument("--doi"); g.add_argument("--query"); p.set_defaults(func=cmd_crossref)
    p = sub.add_parser("refcheck"); p.add_argument("--all", action="store_true"); p.set_defaults(func=cmd_refcheck)
    p = sub.add_parser("mem"); p.add_argument("query"); p.add_argument("--dim"); p.add_argument("-k", type=int, default=5); p.set_defaults(func=cmd_mem)
    p = sub.add_parser("ledger"); p.add_argument("action", choices=["show", "update"]); p.add_argument("--open", action="store_true"); p.add_argument("--file"); p.set_defaults(func=cmd_ledger)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
