"""Per-shard review ledgers.

A ledger is the durable record of what a session has verified. Progress,
resume and the final verdict are all derived from ledgers, never from the
model's free-text answer. Writes are atomic and serialized with flock so the
agent's tool calls and the orchestrator never see a torn file.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

STATUSES = ("open", "issue", "cleared", "not_applicable", "unresolved")
CLOSED = {"issue", "cleared", "not_applicable", "unresolved"}
SEVERITIES = ("substantive", "minor")
TEXT_FIELDS = ("location", "issue_type", "finding", "why", "basis", "note")
DIMENSIONS = ("image", "data", "ref", "methodology")


class LedgerError(ValueError):
    pass


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def load(path: Path | str) -> dict:
    path = Path(path)
    with _locked(path):
        return _read(path)


def create(path: Path | str, meta: dict, items: list[dict]) -> dict:
    """Create a ledger once. An existing ledger is kept (resume)."""
    path = Path(path)
    with _locked(path):
        existing = _read(path)
        if existing.get("items"):
            return existing
        data = dict(meta)
        data["items"] = {}
        for item in items:
            entry = {"status": "open", **item}
            data["items"][item["id"]] = entry
        data["created_at"] = time.time()
        _write(path, data)
        return data


def _validate_entry(entry: dict, known: dict, allow_new: bool) -> list[str]:
    errors = []
    item_id = str(entry.get("id") or "").strip()
    if not item_id:
        return ["每条记录必须有 id"]
    if item_id not in known and not allow_new:
        errors.append(f"{item_id}: 不在本台账中；新发现请用 id 'new:<简短英文标识>' 登记")
    status = entry.get("status")
    if status not in STATUSES or status == "open":
        errors.append(f"{item_id}: status 必须是 issue/cleared/not_applicable/unresolved 之一")
    for field in TEXT_FIELDS:
        if field in entry and not isinstance(entry[field], str):
            errors.append(f"{item_id}: {field} 必须是字符串")
    if status == "issue":
        if entry.get("severity") not in SEVERITIES:
            errors.append(f"{item_id}: issue 必须给出 severity=substantive 或 minor")
        for field in ("location", "issue_type", "finding", "why", "basis"):
            if len(str(entry.get(field) or "").strip()) < 4:
                errors.append(f"{item_id}: issue 缺少 {field}")
    elif status in ("cleared", "not_applicable"):
        if len(str(entry.get("basis") or "").strip()) < 12:
            errors.append(f"{item_id}: {status} 必须在 basis 中写明具体核查依据（位置、数值或对照）")
    elif status == "unresolved":
        if len(str(entry.get("note") or entry.get("basis") or "").strip()) < 8:
            errors.append(f"{item_id}: unresolved 必须在 note 中写明缺什么、为什么无法完成")
    if item_id.startswith("new:") and entry.get("dimension") not in DIMENSIONS:
        errors.append(f"{item_id}: 新发现必须给出 dimension=image/data/ref/methodology")
    evidence = entry.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list):
            errors.append(f"{item_id}: evidence 必须是列表")
        else:
            for ev in evidence:
                if not isinstance(ev, dict) or not (ev.get("path") or ev.get("pdf_path")):
                    errors.append(f"{item_id}: evidence 每项需含 path（截图文件）或 pdf_path+page+clip")
                elif ev.get("path") and not Path(str(ev["path"])).is_file():
                    errors.append(f"{item_id}: 截图文件不存在 {ev['path']}")
    return errors


def update(path: Path | str, entries: list[dict], actor: str) -> tuple[int, list[str]]:
    """Apply validated entries. Returns (applied_count, errors).

    Invalid entries are rejected individually; valid ones in the same call
    are still applied, so one malformed row does not lose a batch of work.
    """
    path = Path(path)
    if not isinstance(entries, list):
        entries = [entries]
    with _locked(path):
        data = _read(path)
        if not data:
            return 0, ["台账不存在"]
        items = data.setdefault("items", {})
        allow_new = bool(data.get("allow_new", True))
        applied, errors = 0, []
        for entry in entries:
            if not isinstance(entry, dict):
                errors.append("每条记录必须是 JSON 对象")
                continue
            entry_errors = _validate_entry(entry, items, allow_new)
            if entry_errors:
                errors.extend(entry_errors)
                continue
            item_id = str(entry["id"]).strip()
            current = items.get(item_id) or {"id": item_id, "kind": "added",
                                             "dimension": entry.get("dimension"),
                                             "title": entry.get("title") or item_id}
            history = current.pop("history", [])
            if current.get("status") not in (None, "open"):
                history.append({k: current.get(k) for k in ("status", "severity", "finding", "by", "updated_at")})
            for key, value in entry.items():
                if key == "id":
                    continue
                if key in TEXT_FIELDS or key in ("status", "severity", "evidence", "dimension", "title"):
                    current[key] = value
            if current.get("status") != "issue":
                current.pop("severity", None)
            current["by"] = actor
            current["updated_at"] = time.time()
            if history:
                current["history"] = history[-5:]
            items[item_id] = current
            applied += 1
        if applied:
            data["updated_at"] = time.time()
            _write(path, data)
        return applied, errors


def mark(path: Path | str, **fields) -> None:
    path = Path(path)
    with _locked(path):
        data = _read(path)
        data.update(fields)
        _write(path, data)


def counts(data: dict) -> dict:
    out = {status: 0 for status in STATUSES}
    for item in (data.get("items") or {}).values():
        out[item.get("status", "open")] = out.get(item.get("status", "open"), 0) + 1
    out["closed"] = sum(out[s] for s in CLOSED)
    out["total"] = len(data.get("items") or {})
    return out


def open_items(data: dict) -> list[dict]:
    return [item for item in (data.get("items") or {}).values() if item.get("status", "open") == "open"]
