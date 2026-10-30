"""Retrieve audit lessons from the project memory for a review shard.

The memory holds ~1300 audit notes (well over 1 MB). Injecting it whole is
impossible, so notes are ranked with BM25 over Latin words and CJK bigrams and
only the most relevant lessons are given to each shard. Sessions can search
on demand with ``pv3 mem``.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from review_v3 import config

_WORD = re.compile(r"[a-z][a-z0-9_.\-]{1,}|\d+(?:\.\d+)?")
_CJK = re.compile(r"[一-鿿]+")
_SKIP_PREFIX = ("MEMORY", "family-")
_DIMENSION_CUES = {
    "image": ("图像", "照片", "显微", "sift", "phash", "拼接", "凝胶", "blot", "面板", "像素", "ncc", "复用"),
    "data": ("源数据", "xlsx", "sheet", "列", "小数", "末位", "复用", "excel", "单元格", "表格"),
    "consistency": ("图注", "正文", "图文", "数字化", "坐标", "定标", "刻度", "读数", "柱", "曲线"),
    "ref": ("参考文献", "doi", "著录", "引用", "crossref", "页码", "期刊"),
    "methodology": ("统计", "样本量", "p 值", "p值", "公式", "回归", "伦理", "显著", "标准差", "复算"),
}
_INDEX_CACHE: dict = {}


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    tokens = _WORD.findall(text)
    for run in _CJK.findall(text):
        if len(run) == 1:
            tokens.append(run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _parse(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    name, description, body = path.stem, "", raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            front, body = raw[3:end], raw[end + 4:]
            match = re.search(r"^name:\s*(.+)$", front, re.M)
            if match:
                name = match.group(1).strip().strip('"')
            match = re.search(r"^description:\s*(.+)$", front, re.M)
            if match:
                description = match.group(1).strip().strip('"')
    body = body.strip()
    apply = ""
    match = re.search(r"\*\*How to apply\*\*[:：]?\s*(.+)", body)
    if match:
        apply = match.group(1).strip()[:400]
    risk = "高风险" if "高风险" in description[-12:] else ("低风险" if "低风险" in description[-12:] else "")
    return {"name": name, "path": str(path), "description": description[:500], "apply": apply,
            "body": body[:1800], "risk": risk}


def _index(memory_dir: Path) -> dict:
    files = sorted(p for p in memory_dir.glob("*.md") if not p.name.startswith(_SKIP_PREFIX))
    signature = (len(files), max((p.stat().st_mtime for p in files), default=0))
    cached = _INDEX_CACHE.get(str(memory_dir))
    if cached and cached["signature"] == signature:
        return cached
    docs, freq = [], Counter()
    for path in files:
        doc = _parse(path)
        if not doc:
            continue
        # The title/description carries the lesson; weight it above the body.
        tokens = tokenize(doc["name"].replace("-", " ")) * 2 + tokenize(doc["description"]) * 3 \
            + tokenize(doc["apply"]) * 2 + tokenize(doc["body"])
        doc["tf"] = Counter(tokens)
        doc["len"] = len(tokens)
        low = (doc["description"] + doc["body"][:600]).lower()
        doc["dims"] = {dim: sum(low.count(cue) for cue in cues) for dim, cues in _DIMENSION_CUES.items()}
        freq.update(set(tokens))
        docs.append(doc)
    avg = sum(d["len"] for d in docs) / max(1, len(docs))
    index = {"signature": signature, "docs": docs, "df": freq, "avg": avg, "n": len(docs), "built": time.time()}
    _INDEX_CACHE[str(memory_dir)] = index
    return index


def doi_markers(doi: str) -> list[str]:
    """Strings identifying one paper inside memory notes (DOI, slug, article number)."""
    doi = (doi or "").strip().lower()
    if not doi:
        return []
    markers = {doi, doi.replace("/", "_"), doi.replace("/", "-")}
    for run in re.findall(r"[a-z0-9]*\d[a-z0-9]*", doi.split("/", 1)[-1]):
        if len(run) >= 5 and not re.fullmatch(r"(19|20)\d\d", run):
            markers.add(run)
    return sorted(markers)


def _mentions(doc: dict, markers: list[str]) -> bool:
    haystack = " ".join((doc["name"], doc["path"], doc["description"], doc["body"])).lower()
    return any(marker in haystack for marker in markers)


def search(query: str, dimension: str = "", k: int = 6, memory_dir: Path | None = None,
           exclude_doi: str = "") -> list[dict]:
    """BM25 search. Notes about ``exclude_doi`` are never returned: a review
    must verify the paper itself, not reuse an earlier verdict on it."""
    memory_dir = Path(memory_dir or config.MEMORY_DIR)
    if not memory_dir.is_dir():
        return []
    index = _index(memory_dir)
    terms = Counter(tokenize(query))
    if not terms or not index["docs"]:
        return []
    k1, b = 1.4, 0.75
    markers = doi_markers(exclude_doi)
    scored = []
    for doc in index["docs"]:
        if markers and _mentions(doc, markers):
            continue
        score = 0.0
        for term, qtf in terms.items():
            tf = doc["tf"].get(term)
            if not tf:
                continue
            df = index["df"][term]
            idf = math.log(1 + (index["n"] - df + 0.5) / (df + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * doc["len"] / index["avg"])) * min(qtf, 3)
        if not score:
            continue
        if dimension:
            dims = doc["dims"]
            total = sum(dims.values()) or 1
            score *= 0.6 + 0.8 * dims.get(dimension, 0) / total
        # General method/false-positive lessons transfer better than one-off case logs.
        if re.search(r"判据|假阳性|方法|坑|不得|须|必须|排除", doc["description"]):
            score *= 1.15
        scored.append((score, doc))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"name": d["name"], "path": d["path"], "description": d["description"], "apply": d["apply"],
             "risk": d["risk"], "score": round(s, 2)} for s, d in scored[:k]]


def hints(query: str, dimension: str, k: int | None = None, max_chars: int | None = None,
          exclude_doi: str = "") -> str:
    """Reader-ready bullet list of the most relevant lessons for a prompt."""
    k = k or config.MEMORY_HINTS_PER_SHARD
    max_chars = max_chars or config.MEMORY_HINT_CHARS
    lines, size = [], 0
    for hit in search(query, dimension=dimension, k=k, exclude_doi=exclude_doi):
        line = f"- {hit['description']}"
        if hit["apply"]:
            line += f"\n  做法：{hit['apply']}"

        if size + len(line) > max_chars:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)


def record_lesson(doi: str, shard: str, item: dict, investigator: dict, final: dict) -> None:
    """Stage a lesson when cross-checking overturned the first conclusion.

    Lessons are written to an inbox for curation; the shared memory is never
    modified automatically by a review session.
    """
    try:
        config.MEMORY_INBOX.mkdir(parents=True, exist_ok=True)
        entry = {"doi": doi, "shard": shard, "item": item.get("id"), "title": item.get("title"),
                 "first": {k: investigator.get(k) for k in ("status", "severity", "finding", "basis")},
                 "final": {k: final.get(k) for k in ("status", "severity", "finding", "basis")},
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        with open(config.MEMORY_INBOX / "lessons.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
