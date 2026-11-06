"""Durable page-level visual coverage, separate from scientific verdicts.

A render is not a review. A page is complete only after a successful Read
hook and an explicit observation. All writers use the same file lock.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from review_v3 import ledger

FILENAME = "visual_progress.json"


def initialize(run_dir: Path, dossier: dict) -> dict:
    pages, documents, unavailable = {}, {}, []
    if not dossier.get("docs"):
        unavailable.append("未提供可读取的 PDF")
    scope = dossier.get("scope") or {}
    for doc in dossier.get("docs") or []:
        doc_id = doc["id"]
        try:
            with open(doc["path"], "rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except (OSError, KeyError):
            digest = "unavailable"
        documents[doc_id] = digest
        count = int(doc.get("pages") or 0)
        if not count or digest == "unavailable":
            unavailable.append(doc_id)
        first, last = 1, count
        if scope and doc_id == scope.get("doc", "main"):
            first, last = scope["first"], scope["last"]
        for number in range(first, last + 1):
            labels = [c["label"] for c in dossier.get("captions") or []
                      if c["doc"] == doc_id and c["page"] == number and c.get("kind") == "figure"]
            key = f"{doc_id}:{number}"
            pages[key] = {"doc": doc_id, "page": number, "labels": labels,
                          "source_hash": digest, "status": "pending", "observations": []}
    data_sources = {}
    for source in dossier.get("data_files") or []:
        try:
            with open(source, "rb") as handle:
                data_sources[str(source)] = hashlib.file_digest(handle, "sha256").hexdigest()
        except OSError:
            data_sources[str(source)] = "unavailable"
    path = Path(run_dir) / FILENAME
    with ledger._locked(path):
        old = ledger._read(path)
        for key, page in pages.items():
            previous = (old.get("pages") or {}).get(key, {})
            if previous.get("source_hash") == page["source_hash"]:
                pages[key] = {**page, **previous, "labels": page["labels"]}
        same = old.get("documents") == documents
        data = {"version": 1, "documents": documents, "data_sources": data_sources, "pages": pages, "unavailable": unavailable,
                "renders": old.get("renders", {}) if same else {}}
        ledger._write(path, data)
    return data


def load(run_dir: Path) -> dict:
    path = Path(run_dir) / FILENAME
    return ledger.load(path) if path.exists() else {}


def source_version(run_dir: Path) -> str:
    data = load(run_dir)
    if not data:
        return ""
    return hashlib.sha256(json.dumps([data.get("documents"), data.get("data_sources")],
                                     sort_keys=True).encode()).hexdigest()[:24]


def pending(data: dict) -> list[dict]:
    # Figure pages first; every unclassified page remains on the list, including
    # scanned supplements and pages beyond the dossier's text-extraction cap.
    return sorted((p for p in (data.get("pages") or {}).values() if p["status"] != "reviewed"),
                  key=lambda p: (not bool(p.get("labels")), p["doc"] != "main", p["doc"], p["page"]))


def summary(run_dir: Path) -> dict:
    data = load(run_dir)
    if not data:
        return {}
    rows = list(data["pages"].values())
    return {"total": len(rows), "reviewed": sum(p["status"] == "reviewed" for p in rows),
            "remaining": [{k: p.get(k) for k in ("doc", "page", "labels", "status", "note")}
                          for p in pending(data)], "unavailable": data.get("unavailable", []),
            "read_events": sum(len(p.get("observations", [])) for p in rows)}


def pending_note(run_dir: Path, limit: int = 30) -> str:
    data = load(run_dir)
    if not data:
        return ""
    rows = pending(data)
    text = "；".join(f"{p['doc']} p{p['page']}" + (f"（{p['note']}）" if p.get("note") else "")
                    for p in rows[:limit])
    if len(rows) > limit:
        text += f"；另 {len(rows) - limit} 页（完整清单见 pv3 visual pending）"
    if data.get("unavailable"):
        text += "；无法读取的文档：" + "、".join(data["unavailable"])
    return "逐页浏览尚未完成：" + text if text else ""


def register_render(run_dir: Path, doc: str, page: int, image: Path, *, full: bool) -> None:
    path = Path(run_dir) / FILENAME
    if not path.exists() or not full:
        return
    with ledger._locked(path):
        data = ledger._read(path)
        key = f"{doc}:{page}"
        if key not in data.get("pages", {}):
            return
        data.setdefault("renders", {})[str(image.resolve())] = {
            "page_key": key, "source_hash": data["pages"][key]["source_hash"],
            "reads": data.get("renders", {}).get(str(image.resolve()), {}).get("reads", {})}
        ledger._write(path, data)


def record_read(run_dir: Path, image: Path, actor: str) -> None:
    path = Path(run_dir) / FILENAME
    if not path.exists() or not image.is_file():
        return
    with ledger._locked(path):
        data = ledger._read(path)
        render = (data.get("renders") or {}).get(str(image.resolve()))
        if not render:
            return
        render.setdefault("reads", {})[actor] = time.time()
        ledger._write(path, data)


def update(run_dir: Path, entries: list[dict], actor: str) -> tuple[int, list[str]]:
    path = Path(run_dir) / FILENAME
    applied, errors = 0, []
    with ledger._locked(path):
        data = ledger._read(path)
        if not data:
            return 0, ["本次复审尚未建立页面进度"]
        for entry in entries:
            if not isinstance(entry, dict):
                errors.append("页面记录必须是对象")
                continue
            key = f"{entry.get('doc')}:{entry.get('page')}"
            page = data.get("pages", {}).get(key)
            status, note = entry.get("status"), str(entry.get("note") or "").strip()
            if page is None or status not in {"reviewed", "needs_detail", "blocked"} or len(note) < 8:
                errors.append(f"{key}: 需有效页面、status=reviewed/needs_detail/blocked 和具体 note（至少 8 字）")
                continue
            if status != "blocked":
                viewed = any(r["page_key"] == key and r.get("source_hash") == page["source_hash"]
                             and actor in r.get("reads", {}) for r in data.get("renders", {}).values())
                if not viewed:
                    errors.append(f"{key}: 请先 pv3 render 渲染整页并用 Read 实际查看，再写浏览记录")
                    continue
            result = entry.get("result")
            if status == "reviewed" and result not in {"clear", "no_figure", "issue"}:
                errors.append(f"{key}: reviewed 需 result=clear/no_figure/issue")
                continue
            if result == "issue" and not entry.get("issue_ids"):
                errors.append(f"{key}: 已确认问题须关联已写入问题台账的 issue_ids")
                continue
            observation = {"actor": actor, "status": status, "result": result, "note": note,
                           "issue_ids": entry.get("issue_ids") or [], "at": time.time()}
            if page.get("status") == "reviewed" and status == "blocked":
                # A later render failure cannot erase an already verified page.
                page.setdefault("observations", []).append(observation)
            else:
                page.update({k: observation[k] for k in ("status", "result", "note", "issue_ids")})
                page.setdefault("observations", []).append(observation)
            applied += 1
        if applied:
            ledger._write(path, data)
    return applied, errors


def progress_count(run_dir: Path, actor: str) -> int:
    data = load(run_dir)
    # Rewriting the same note does not keep a stalled session alive. Each actor
    # can advance each page through browsing and detailed checking only once.
    return sum(len({o["status"] for o in p.get("observations", [])
                    if o["actor"] == actor and o["status"] in {"reviewed", "needs_detail"}})
               for p in (data.get("pages") or {}).values())
