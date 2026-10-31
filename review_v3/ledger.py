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
import re
import time
from pathlib import Path

STATUSES = ("open", "issue", "cleared", "not_applicable", "unresolved")
CLOSED = {"issue", "cleared", "not_applicable", "unresolved"}
SEVERITIES = ("substantive", "minor")
TEXT_FIELDS = ("location", "issue_type", "finding", "why", "basis", "note", "category")
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
    """Create a ledger once. An existing ledger is kept (resume); items it does
    not have yet are added as open, so later findings still get handled.
    ``added`` in the returned dict counts those (not stored)."""
    path = Path(path)
    with _locked(path):
        existing = _read(path)
        if existing.get("items"):
            new = [item for item in items if item["id"] not in existing["items"]]
            for item in new:
                existing["items"][item["id"]] = {"status": "open", **item}
            if new:
                _write(path, existing)
            return {**existing, "added": len(new)}
        data = dict(meta)
        data["items"] = {}
        for item in items:
            entry = {"status": "open", **item}
            data["items"][item["id"]] = entry
        data["created_at"] = time.time()
        _write(path, data)
        return data


EVIDENCE_PATH_KEYS = ("path", "file", "png", "image", "screenshot")


def _normalize(entry: dict, run_dir: Path) -> list[str]:
    """Accept the shapes agents actually write; never reject a conclusion
    because of its attachments. Returns warnings for what was dropped."""
    warnings = []
    item_id = str(entry.get("id") or "").strip()
    for field in ("status", "severity", "dimension"):
        if isinstance(entry.get(field), str):
            entry[field] = entry[field].strip().lower()
    for field in TEXT_FIELDS:
        value = entry.get(field)
        if isinstance(value, list):
            entry[field] = "；".join(str(v) for v in value)
        elif isinstance(value, dict):
            entry[field] = json.dumps(value, ensure_ascii=False)
        elif value is not None and not isinstance(value, str):
            entry[field] = str(value)
    evidence = entry.get("evidence")
    if evidence is None:
        return warnings
    if isinstance(evidence, (str, dict)):
        evidence = [evidence]
    if not isinstance(evidence, list):
        entry.pop("evidence")
        return [f"{item_id}: evidence 无法识别，已忽略（结论已写入）"]
    kept, dropped = [], []
    for ev in evidence:
        if isinstance(ev, str):
            ev = {"path": ev}
        if not isinstance(ev, dict):
            dropped.append(str(ev)[:80])
            continue
        if ev.get("pdf_path"):
            kept.append(ev)
            continue
        raw = next((ev[k] for k in EVIDENCE_PATH_KEYS if isinstance(ev.get(k), str) and ev[k].strip()), "")
        candidates = [Path(raw.strip())] if raw else []
        if raw and not Path(raw).is_absolute():
            candidates += [run_dir / raw, run_dir / "dossier" / "renders" / Path(raw).name]
        found = next((p for p in candidates if p.is_file()), None)
        if found:
            kept.append({**{k: v for k, v in ev.items() if k not in EVIDENCE_PATH_KEYS}, "path": str(found)})
        else:
            dropped.append(raw[:120] or json.dumps(ev, ensure_ascii=False)[:80])
    if kept:
        entry["evidence"] = kept
    else:
        entry.pop("evidence")
    if dropped:
        warnings.append(f"{item_id}: 以下截图不存在，已忽略（结论已写入，可用 pv3 render 生成后再补写）：" + "，".join(dropped[:5]))
    return warnings


CROP_NAME = re.compile(r"_c\d+_\d+_\d+_\d+_d\d+\.png$|^view_.*\.png$")


def is_crop(path: str) -> bool:
    """A local screenshot (pv3 render --clip or pv3 view), not a whole page."""
    return bool(CROP_NAME.search(Path(str(path)).name))


def _validate_entry(entry: dict, known: dict, allow_new: bool, require_category: bool = False,
                    require_crop: bool = False) -> list[str]:
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
    if status == "issue" and require_category:
        from review_v3.categories import CATEGORIES, severity
        code = str(entry.get("category") or "").strip().upper()
        if code not in CATEGORIES:
            errors.append(f"{item_id}: issue 必须给出 category（{'/'.join(CATEGORIES)} 之一），定级由类别决定")
        else:
            entry["category"], entry["severity"] = code, severity(code)
    if status == "issue" and require_crop:
        crops = [ev for ev in entry.get("evidence") or [] if isinstance(ev, dict) and is_crop(ev.get("path", ""))]
        if not crops:
            errors.append(f"{item_id}: 需要至少一张局部截图（pv3 render <doc> <页> --clip x0,y0,x1,y1 生成，"
                          "整页截图不算），在 evidence 中给出 path 与 caption")
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
    return errors


def update(path: Path | str, entries: list[dict], actor: str,
           warnings: list[str] | None = None) -> tuple[int, list[str]]:
    """Apply validated entries. Returns (applied_count, errors).

    Invalid entries are rejected individually; valid ones in the same call
    are still applied, so one malformed row does not lose a batch of work.
    Attachments that cannot be used are dropped (reported in ``warnings``)
    rather than rejecting the conclusion.
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
            notes = _normalize(entry, path.parent.parent)
            if warnings is not None:
                warnings.extend(notes)
            known = items.get(str(entry.get("id") or "").strip()) or {}
            # A re-check (calibration, challenge) may restate only what it
            # changes; the rest of the issue comes from the recorded finding.
            if entry.get("status") == "issue" and isinstance(known.get("first"), dict):
                for field in ("location", "issue_type", "finding", "why", "basis"):
                    if len(str(entry.get(field) or "").strip()) < 4 and known["first"].get(field):
                        entry[field] = known["first"][field]
                if not data.get("require_category") and not entry.get("severity") and known["first"].get("severity"):
                    entry["severity"] = known["first"]["severity"]
            if entry.get("status") == "issue" and len(str(entry.get("location") or "").strip()) < 4 and known.get("title"):
                # Checklist titles start with the location ("Fig. 3（第4页）：…").
                entry["location"] = str(known["title"]).split("：")[0][:120]
            entry_errors = _validate_entry(entry, items, allow_new, bool(data.get("require_category")),
                                           bool(data.get("require_crop")))
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
                current.pop("category", None)
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
