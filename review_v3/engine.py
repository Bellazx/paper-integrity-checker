"""Review engine v3 orchestrator.

    evidence (deterministic) -> dossier -> shards
      -> per shard: investigate -> challenge        (all shards in parallel, pipelined)
      -> arbitrate disputed items per dimension      (parallel)
      -> verdict from ledgers (deterministic) -> writer -> publication validation

The returned dict has the same contract as api.services.review.run_review_single.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from review_v3 import checklist, config, dossier as dossier_mod, ledger, memory, prompts, session

log = logging.getLogger(__name__)

DIMENSIONS = ("image", "data", "ref", "methodology")
HIGH, LOW = "建议高风险", "建议低风险"


# ------------------------------------------------------------------ helpers

def _slug(doi: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in doi)[:80] or "paper"


SCRATCH_SUFFIXES = {".png", ".jpg", ".jpeg", ".py", ".txt", ".json", ".npy", ".log", ".html"}


def _identity(doi: str, report_json: str, input_dir: str) -> str:
    """Stable id of the review inputs. Directory mtimes are not used, and
    scratch-type files newer than the initial report are ignored, so a stray
    file written during a review cannot make its resume start over."""
    parts = [doi.lower()]
    report_mtime = None
    path = Path(report_json or "")
    try:
        stat = path.stat()
        report_mtime = stat.st_mtime
        parts.append(f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}")
    except OSError:
        parts.append(str(path))
    parts.append(str(Path(input_dir or "").resolve()) if input_dir else "")
    if input_dir and Path(input_dir).is_dir():
        files = []
        for p in Path(input_dir).rglob("*"):
            if not p.is_file():
                continue
            st = p.stat()
            if report_mtime and st.st_mtime > report_mtime + 60 and p.suffix.lower() in SCRATCH_SUFFIXES:
                continue
            files.append(str(p.relative_to(input_dir)) + f":{st.st_size}")
        files.sort()
        parts.append(hashlib.sha1("\n".join(files).encode()).hexdigest())
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _fallback(doi: str, message: str) -> dict:
    from api.services.review import _fallback_result
    result = _fallback_result(doi, message)
    result["_engine"] = "v3"
    return result


def _is_issue(record: dict, severity: str | None = None) -> bool:
    return record.get("status") == "issue" and (severity is None or record.get("severity") == severity)


def _compact(record: dict) -> dict:
    keep = ("status", "severity", "dimension", "location", "issue_type", "finding", "why", "basis", "note", "evidence")
    return {k: record[k] for k in keep if record.get(k)}


# ------------------------------------------------------------------ phases

class Review:
    def __init__(self, doi: str, report_json: str, input_dir: str, run_dir: Path, deadline: float | None,
                 started: float | None = None):
        self.doi = doi
        self.report_json = report_json
        self.input_dir = input_dir
        self.run_dir = run_dir
        self.deadline = deadline
        self.evidence: dict = {}
        self.dossier: dict = {}
        self.shards: list[dict] = []
        self.hints: dict[str, str] = {}
        self.read_dirs: list[str] = []
        self.background: list[asyncio.Future] = []
        origin = started if started is not None else time.monotonic()
        # Phase time boxes; the whole-paper deadline stays the hard limit.
        self.soft = {"investigate": origin + config.INVESTIGATE_SOFT_SECONDS,
                     "challenge": origin + config.CHALLENGE_SOFT_SECONDS,
                     "arbitrate": origin + config.ARBITRATE_SOFT_SECONDS,
                     "calibrate": origin + config.CALIBRATE_SOFT_SECONDS}
        self.timeline: dict[str, float] = {}

    # -- setup
    async def prepare(self, evidence_path: str) -> None:
        self.evidence = _read_json(Path(evidence_path), {}) or {}
        started = time.monotonic()
        self.dossier = await asyncio.to_thread(dossier_mod.build, self.run_dir, self.doi, self.input_dir,
                                               self.report_json, self.evidence)
        shards_path = self.run_dir / "shards.json"
        self.shards = _read_json(shards_path) or checklist.build_shards(self.evidence, self.dossier)
        _write_json(shards_path, self.shards)
        for shard in self.shards:
            ledger.create(self.ledger_path("inv", shard["id"]),
                          {"shard": shard["id"], "role": "investigator", "dimension": shard["dimension"], "allow_new": True},
                          [self._ledger_item(item, shard) for item in shard["items"]])
        self.read_dirs = self._read_dirs()
        hints = await asyncio.to_thread(self._memory_hints)
        self.hints = hints
        self.timeline["prepare"] = time.monotonic() - started
        if self.dossier.get("data_files"):
            self.background = [asyncio.ensure_future(self._prerun_forensic(mode))
                               for mode in ("scan-digits", "scan-decimal-tails")]
        self.background.append(asyncio.ensure_future(self._prerun_refcheck()))

    async def _prerun_refcheck(self) -> None:
        from review_v3 import refcheck
        target = self.run_dir / "dossier" / "refcheck.json"
        text = (self.run_dir / "dossier" / "references.txt").read_text(encoding="utf-8")
        if target.exists() or not text.strip():
            return
        try:
            await asyncio.to_thread(refcheck.run, text, target)
        except Exception as exc:  # network trouble is reported, not fatal
            log.warning("DOI=%s reference precheck failed: %s", self.doi, exc)

    async def close(self) -> None:
        """Reap background scans so no subprocess outlives the review."""
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)

    def _memory_hints(self) -> dict:
        out = {}
        for shard in self.shards:
            dim = {"consistency": "consistency", "stats": "methodology"}.get(shard["kind"], shard["dimension"])
            out[shard["id"]] = memory.hints(checklist.query_terms(shard, self.dossier), dim, exclude_doi=self.doi)
        return out

    def _read_dirs(self) -> list[str]:
        """Directories a session may read besides its work dir: the paper's
        inputs and the detector's extracted images/annotations for it."""
        dirs = {str(Path(self.input_dir))}
        for alert in self.dossier.get("image_alerts") or []:
            for key in ("annotation", "image_a", "image_b"):
                if alert.get(key):
                    dirs.add(str(Path(alert[key]).parent))
        return sorted(dirs)

    @property
    def extra_env(self) -> dict:
        return {"PV3_INPUT_DIR": str(Path(self.input_dir)), "PV3_DOI": self.doi,
                "PV3_EXTRA_DIRS": os.pathsep.join(self.read_dirs),
                "PAPER_INTEGRITY_ROOT": str(config.BASE_DIR)}

    async def _prerun_forensic(self, mode: str) -> None:
        cache = self.run_dir / "dossier" / f"forensic_{mode}.txt"
        if cache.exists():
            return
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                str(config.BIN_DIR / "pv3"), "forensic", mode,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env=session.session_env(self.run_dir, None, "system"), start_new_session=True)
            await asyncio.wait_for(proc.wait(), 330)
        except (TimeoutError, OSError) as exc:
            log.warning("DOI=%s forensic prerun %s failed: %s", self.doi, mode, exc)
        finally:
            if proc is not None and proc.returncode is None:
                await session._kill(proc)

    @staticmethod
    def _ledger_item(item: dict, shard: dict) -> dict:
        keep = ("id", "title", "hint", "kind", "members", "test", "strong")
        out = {k: item[k] for k in keep if item.get(k) is not None}
        out["dimension"] = shard["dimension"]
        return out

    def ledger_path(self, role: str, name: str) -> Path:
        return self.run_dir / "ledgers" / f"{role}_{name}.json"

    def _spec(self, name: str, prompt: str, ledger_path: Path, max_chunks: int, actor: str,
              phase: str) -> session.SessionSpec:
        def open_ids(_state):
            return [item["id"] for item in ledger.open_items(ledger.load(ledger_path))]
        return session.SessionSpec(
            name=name, workdir=self.run_dir, prompt=prompt, actor=actor, ledger_path=ledger_path,
            max_chunks=max_chunks, deadline=self.deadline, soft_deadline=self.soft.get(phase),
            continue_prompt=lambda state: prompts.investigator_continue(state, open_ids(state)),
            restart_prompt=lambda state: prompt + prompts.restart_note(),
            wrapup_prompt=lambda state: prompts.wrapup(open_ids(state)),
            extra_env=self.extra_env, read_dirs=self.read_dirs,
        )

    @staticmethod
    def _close_leftovers(path: Path, outcome: session.SessionOutcome, actor: str) -> None:
        """Items a session could not reach become explicit 'unresolved'."""
        leftovers = ledger.open_items(ledger.load(path))
        if not leftovers:
            return
        note = {"stalled": "连续多轮没有新进展，核查未完成", "exhausted": "会话预算用尽，核查未完成",
                "wrapped": "阶段时限已到，核查未完成", "timeboxed": "阶段时限已到，核查未开始",
                "deadline": "整篇时限已到，核查未完成", "error": f"模型服务异常（{outcome.error}），核查未完成"}
        ledger.update(path, [{"id": item["id"], "status": "unresolved",
                              "note": note.get(outcome.status, "核查未完成")} for item in leftovers], f"system:{actor}")

    # -- per shard pipeline
    async def investigate(self, shard: dict) -> session.SessionOutcome:
        path = self.ledger_path("inv", shard["id"])
        prompt = prompts.investigator(shard, self.dossier, self.hints.get(shard["id"], ""))
        outcome = await session.run(self._spec(f"inv_{shard['id']}", prompt, path, config.MAX_CHUNKS_INVESTIGATE,
                                               f"investigator:{shard['id']}", "investigate"))
        self._close_leftovers(path, outcome, "investigator")
        return outcome

    @staticmethod
    def needs_challenge(record: dict) -> bool:
        """Confirmed issues (false positives), unfinished items, and cleared
        strong/deterministic findings (misses) get an independent second check.
        Cleared low-signal items keep the investigator's documented basis."""
        if record.get("status") in ("issue", "unresolved", "open"):
            return True
        return bool(record.get("strong")) or record.get("kind") in ("high", "group", "source_data_audit", "added")

    async def challenge(self, shard: dict) -> session.SessionOutcome | None:
        first = ledger.load(self.ledger_path("inv", shard["id"]))
        path = self.ledger_path("chk", shard["id"])
        items = []
        for record in (first.get("items") or {}).values():
            if not self.needs_challenge(record):
                continue
            items.append({"id": record["id"], "title": record.get("title", record["id"]), "hint": record.get("hint"),
                          "kind": record.get("kind"), "members": record.get("members"),
                          "dimension": record.get("dimension") or shard["dimension"], "first": _compact(record)})
        if not items:
            return None
        ledger.create(path, {"shard": shard["id"], "role": "challenger", "dimension": shard["dimension"],
                             "allow_new": True}, items)
        prompt = prompts.challenger(shard, self.dossier, self.hints.get(shard["id"], ""), first,
                                    {item["id"] for item in items})
        outcome = await session.run(self._spec(f"chk_{shard['id']}", prompt, path, config.MAX_CHUNKS_CHALLENGE,
                                               f"challenger:{shard['id']}", "challenge"))
        return outcome

    async def shard_pipeline(self, shard: dict) -> dict:
        started = time.monotonic()
        inv = await self.investigate(shard)
        mid = time.monotonic()
        chk = await self.challenge(shard)
        return {"shard": shard["id"], "investigate": inv.status, "challenge": chk.status if chk else "skipped",
                "investigate_seconds": round(mid - started, 1), "challenge_seconds": round(time.monotonic() - mid, 1)}

    # -- merge
    def merge(self) -> tuple[dict, list[dict]]:
        """Return (final records by id, disputes)."""
        finals, disputes = {}, []
        for shard in self.shards:
            first = ledger.load(self.ledger_path("inv", shard["id"])).get("items") or {}
            second = ledger.load(self.ledger_path("chk", shard["id"])).get("items") or {}
            for item_id, a in first.items():
                b = second.get(item_id, {})
                meta = {"shard": shard["id"], "id": item_id, "title": a.get("title"), "kind": a.get("kind"),
                        "members": a.get("members"), "strong": a.get("strong"),
                        "dimension": a.get("dimension") or shard["dimension"]}
                decision = self._decide(a, b)
                if decision == "first":
                    finals[item_id] = {**meta, **_compact(a), "evidence": a.get("evidence") or b.get("evidence")}
                elif decision == "second":
                    finals[item_id] = {**meta, **_compact(b), "evidence": b.get("evidence") or a.get("evidence")}
                else:
                    disputes.append({**meta, "first": _compact(a), "second": _compact(b)})
            for item_id, b in second.items():
                if item_id in first or not str(item_id).startswith("new:"):
                    continue
                meta = {"shard": shard["id"], "id": item_id, "title": b.get("title"), "kind": "added",
                        "dimension": b.get("dimension") or shard["dimension"]}
                if _is_issue(b, "substantive"):
                    disputes.append({**meta, "first": {}, "second": _compact(b)})
                elif _is_issue(b, "minor"):
                    finals[item_id] = {**meta, **_compact(b)}
        return finals, disputes

    @staticmethod
    def _decide(a: dict, b: dict) -> str:
        sa, sb = a.get("status", "open"), b.get("status", "open")
        non_issue = ("cleared", "not_applicable")
        if sb == "open":  # the cross-check never reached this item
            if sa in non_issue and not a.get("strong"):
                return "first"
            if _is_issue(a, "minor"):
                return "first"
            return "dispute"
        if sa in ("unresolved", "open"):
            # Only the cross-check examined it. Accept a clearance or a minor
            # issue; a substantive finding from one side needs confirmation.
            if sb in non_issue or _is_issue(b, "minor"):
                return "second"
            return "dispute"
        if sb == "unresolved":
            return "dispute"
        if sa in non_issue and sb in non_issue:
            return "first"
        if sa == "issue" and sb == "issue" and a.get("severity") == b.get("severity"):
            return "first"
        return "dispute"

    async def arbitrate(self, disputes: list[dict], round_no: int) -> dict:
        by_dim: dict[str, list[dict]] = {}
        for item in disputes:
            by_dim.setdefault(item["dimension"] if item["dimension"] in DIMENSIONS else "data", []).append(item)

        async def one(dimension: str, items: list[dict]) -> dict:
            path = self.ledger_path(f"arb{round_no}", dimension)
            ledger.create(path, {"shard": f"arb{round_no}_{dimension}", "role": "arbiter", "dimension": dimension,
                                 "allow_new": False},
                          [{"id": i["id"], "title": i.get("title"), "dimension": dimension, "members": i.get("members"),
                            "first": i.get("first"), "second": i.get("second")} for i in items])
            outcome = await session.run(self._spec(f"arb{round_no}_{dimension}", prompts.arbiter(dimension, self.dossier, items),
                                                   path, config.MAX_CHUNKS_ARBITRATE, f"arbiter:{dimension}",
                                                   "arbitrate"))
            self._close_leftovers(path, outcome, "arbiter")
            return ledger.load(path).get("items") or {}

        results = await asyncio.gather(*(one(dim, items) for dim, items in by_dim.items()))
        decided = {}
        for records in results:
            decided.update(records)
        return decided

    async def calibrate(self, finals: dict) -> dict:
        """Second look at the severity of every confirmed issue (both directions)."""
        issues = [r for r in finals.values() if r.get("status") == "issue"]
        if not issues:
            return finals
        path = self.ledger_path("cal", "all")
        ledger.create(path, {"shard": "calibrate", "role": "calibrator", "allow_new": False},
                      [{"id": r["id"], "title": r.get("title") or r["id"], "dimension": r.get("dimension"),
                        "first": _compact(r)} for r in issues])
        outcome = await session.run(self._spec("cal_all", prompts.calibrator(self.dossier, issues), path,
                                               config.MAX_CHUNKS_CALIBRATE, "calibrator", "calibrate"))
        decided = ledger.load(path).get("items") or {}
        out = dict(finals)
        for item_id, record in decided.items():
            if record.get("status") in ("issue", "cleared") and item_id in out:
                before = out[item_id]
                updated = {**before, **_compact(record)}
                if record.get("status") == "cleared":
                    updated.pop("severity", None)
                if (before.get("severity"), before.get("status")) != (updated.get("severity"), updated.get("status")):
                    updated["calibrated_from"] = {"status": before.get("status"), "severity": before.get("severity")}
                out[item_id] = updated
        log.info("DOI=%s calibration %s", self.doi, outcome.status)
        return out

    # -- verdict and writing
    @staticmethod
    def verdicts(finals: dict) -> dict:
        dims = {dim: LOW for dim in DIMENSIONS}
        for record in finals.values():
            if _is_issue(record, "substantive"):
                dim = record.get("dimension") if record.get("dimension") in DIMENSIONS else "data"
                dims[dim] = HIGH
        high = any(v == HIGH for v in dims.values())
        return {"result": "高风险" if high else "低风险", "verdict": HIGH if high else LOW, "dimension_verdicts": dims}

    def facts(self, finals: dict) -> tuple[dict, dict]:
        """Writer input and deterministic evidence binding per dimension."""
        out, evidence = {}, {}
        ordered = sorted(_dedupe_issues(list(finals.values())),
                         key=lambda r: (0 if r.get("severity") == "substantive" else 1))
        for dim in DIMENSIONS:
            issues, cleared, missing, unresolved = [], [], [], []
            evidence[dim] = []
            for record in ordered:
                if (record.get("dimension") if record.get("dimension") in DIMENSIONS else "data") != dim:
                    continue
                status = record.get("status")
                if status == "issue":
                    number = len(issues) + 1
                    issues.append({"编号": number, "严重程度": "实质问题" if record.get("severity") == "substantive" else "规范问题",
                                   **{k: record.get(k, "") for k in ("location", "issue_type", "finding", "why", "basis")}})
                    for ev in record.get("evidence") or []:
                        if isinstance(ev, dict):
                            evidence[dim].append({**ev, "dimension": dim, "issue_index": number,
                                                  "caption": ev.get("caption") or record.get("issue_type", "")})
                elif status == "cleared":
                    cleared.append({"条目": record.get("title", record["id"])[:220], "cite": self._cite(record),
                                    "依据": str(record.get("basis", ""))[:400]})
                elif status == "not_applicable":
                    missing.append({"条目": record.get("title", record["id"])[:200], "依据": str(record.get("basis", ""))[:300]})
                elif status == "unresolved":
                    unresolved.append({"条目": record.get("title", record["id"])[:200], "说明": str(record.get("note", ""))[:300]})
            out[dim] = {"issues": issues, "cleared": cleared, "not_applicable": missing}
            if unresolved:
                out[dim]["未能完成的核查（如实说明为核查范围限制）"] = unresolved
        return out, evidence

    def _cite(self, record: dict) -> str:
        """Words the publication gate looks for when checking coverage of this item."""
        det = self.evidence.get("deterministic_findings") or {}
        item_id = record["id"]
        words = []
        if item_id.startswith("reuse_group::"):
            gid = item_id.split("::")[1]
            group = next((g for g in det.get("cross_sheet_reuse_groups") or [] if g.get("group_id") == gid), {})
            number = gid.rsplit("_", 1)[-1]
            words.append(f"复用组{number}")
            words.extend(str(h) for h in (group.get("headers_sample") or [])[:2])
        elif item_id.startswith("dupcol::"):
            parts = item_id.split("::")
            words.extend([parts[2] if len(parts) > 2 else "", "重复列"])
        elif item_id.startswith("decmismatch::"):
            words.extend([item_id.split("::")[1], "小数精度"])
        elif item_id.startswith("passage_offset::"):
            words.extend([item_id.split("::")[2], "跨行伪重复/固定比例"])
        elif record.get("kind") in ("high", "group", "source_data_audit"):
            words.append(record.get("title", "")[:80])
        return "；".join(w for w in words if w)

    async def write(self, finals: dict, verdicts: dict) -> tuple[dict | None, list[str]]:
        from api.services.review import _clean_internal_references, _extract_json_from_text, _normalize_review_result
        from api.services.review_validation import validate
        facts, evidence = self.facts(finals)
        _write_json(self.run_dir / "facts.json", facts)
        draft, gaps, best = None, [], None
        # Writing is cheap and must reflect the current facts: never replay a
        # cached draft from an earlier call.
        for stale in (self.run_dir / "sessions").glob("writer_*.json"):
            if not stale.name.endswith(".done.json"):
                session.reset(stale.stem, self.run_dir)
        for attempt in range(config.WRITER_REPAIRS + 1):
            name = f"writer_{attempt}"
            spec = session.SessionSpec(name=name, workdir=self.run_dir, actor="writer", tools=False,
                                       prompt=prompts.writer(self.doi, self.dossier, facts, verdicts, gaps or None,
                                                             draft if gaps else None),
                                       max_chunks=1, chunk_turns=config.WRITER_TURNS, deadline=self.deadline)
            outcome = await session.run(spec)
            parsed = _review_json(outcome.text or "") or _extract_json_from_text(outcome.text or "")
            if not isinstance(parsed, dict) or not any(parsed.get(f) for f in REVIEW_FIELDS):
                # Keep the better earlier draft and its gaps for the next attempt.
                gaps = [_writer_gap(g) for g in (best[1] if best else [])]
                gaps.append("上一次没有输出 JSON。本阶段不能调用任何工具，直接输出完整 JSON 对象。")
                draft = draft or (best[2] if best else None)
                continue
            result = _normalize_review_result(parsed)
            result.update(doi=self.doi, **verdicts)
            for field in ("image_review", "data_review", "ref_review", "methodology_review", "reason", "trigger"):
                if not isinstance(result.get(field), str):
                    result[field] = "" if result.get(field) is None else json.dumps(result[field], ensure_ascii=False)
            result["evidence_images"] = _bind_evidence(result, evidence)
            result = _clean_internal_references(result)
            checked, gaps = validate(result, self.evidence)
            _write_json(self.run_dir / f"writer_{attempt}.json", {"result": result, "gaps": gaps})
            if not gaps:
                return checked, []
            draft = {k: result.get(k) for k in ("image_review", "data_review", "ref_review", "methodology_review",
                                                 "reason", "trigger")}
            if best is None or len(gaps) < len(best[1]):
                best = (result, gaps, draft)
            gaps = [_writer_gap(g) for g in gaps]
        return None, (best[1] if best else gaps)


def _writer_gap(gap: str) -> str:
    """Restate a validator message as an instruction the tool-less writer can act on."""
    text = str(gap)
    match = re.search(r"issue_index.*dimension (\w+)", text)
    if match:
        return (f"{match.group(1)} 维度：每个确认问题必须单独起一行，以“（编号）问题位置：”开头，"
                "编号按给定事实中的“编号”依次写出，一个都不能合并或省略。")
    if text.startswith("Duplicate issue numbers"):
        return "同一维度内问题编号不能重复，按给定编号依次写。"
    if text.startswith("Verdict must agree"):
        return "verdict 必须与 result 一致（高风险→建议高风险，低风险→建议低风险）。"
    return text


REVIEW_FIELDS = ("image_review", "data_review", "ref_review", "methodology_review", "reason")
DIMENSION_FIELD = {"image": "image_review", "data": "data_review", "ref": "ref_review", "methodology": "methodology_review"}
# Any non-newline whitespace before an issue number (models often emit U+3000).
ISSUE_HEAD = re.compile(r"(?<!\n)[^\S\n]*([（(]\d+[）)][^\S\n]*问题位置[：:])")


def _review_json(text: str) -> dict | None:
    """The JSON object in the writer's reply that actually holds the review."""
    decoder, best, best_score = json.JSONDecoder(), None, 0
    for start in [i for i, ch in enumerate(text) if ch == "{"][:200]:
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict):
            score = sum(1 for f in REVIEW_FIELDS if isinstance(value.get(f), str) and value.get(f))
            if score > best_score:
                best, best_score = value, score
    return best


def _bind_evidence(result: dict, evidence: dict) -> list[dict]:
    """Put every numbered issue on its own line, then keep only screenshots
    whose issue number exists in that dimension's text (the layout binds by
    number; a dangling number would block publication)."""
    from modules.review_report_layout import split_review_issues
    images = []
    for dim, field in DIMENSION_FIELD.items():
        text = ISSUE_HEAD.sub(lambda m: "\n\n" + m.group(1), result.get(field) or "")
        result[field] = text
        try:
            numbers = {index for index, _ in split_review_issues(text)[1]}
        except ValueError:
            numbers = set()
        kept = [ev for ev in evidence.get(dim, []) if ev.get("issue_index") in numbers]
        if len(kept) < len(evidence.get(dim, [])):
            log.warning("writer numbering left %d %s screenshots unbound; dropped",
                        len(evidence.get(dim, [])) - len(kept), dim)
        images.extend(kept)
    return images


# ------------------------------------------------------------------ entry

# Bump when writer-side logic changes what a report says (e.g. issue merging),
# so a regrade pass rewrites reports produced before the change.
REPORT_LOGIC_VERSION = "2"


def rules_version() -> str:
    digest = hashlib.sha1(REPORT_LOGIC_VERSION.encode())
    for path in sorted(config.RULES_DIR.glob("*.md")):
        digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:12]


def _start_regrade(run_dir: Path, final_path: Path) -> None:
    """Keep the investigation; redo only severity calibration and writing."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    final_path.rename(final_path.with_name(f"final.{stamp}.json"))
    calibration = run_dir / "ledgers" / "cal_all.json"
    if calibration.exists():
        calibration.rename(calibration.with_name(f"cal_all.{stamp}.json"))
    session.reset("cal_all", run_dir)
    log.info("regrading %s under rules %s", run_dir, rules_version())


ANCHOR_RE = re.compile(r"(?:Fig(?:ure)?\.?|Table|表|图|条目|文献|Ref\.?)\s*(S?\d+)", re.I)
NUMBER_RE = re.compile(r"[−-]?\d+(?:[.,:;]\d+)+|\d{3,}")


def _issue_signature(record: dict) -> tuple[set, set]:
    location = str(record.get("location") or "")
    anchors = {m.group(0)[:1].lower() + m.group(1) for m in ANCHOR_RE.finditer(location)}
    numbers = set(NUMBER_RE.findall(str(record.get("finding") or "").replace("−", "-")))
    return anchors, numbers


def _dedupe_issues(records: list[dict]) -> list[dict]:
    """Merge issues that several shards recorded for the same problem (same
    figure/table/reference anchor and the smaller finding's key numbers
    contained in the other), so the report states each problem once. The
    verdict is computed from finals and is unaffected."""
    issues = [r for r in records if r.get("status") == "issue"]
    rank = sorted(issues, key=lambda r: (r.get("severity") != "substantive", -len(str(r.get("finding") or ""))))
    kept: list[dict] = []
    for record in rank:
        anchors, numbers = _issue_signature(record)
        target = None
        for other in kept:
            other_anchors, other_numbers = _issue_signature(other)
            if not anchors & other_anchors or len(numbers) < 3:
                continue
            if len(numbers & other_numbers) / len(numbers) >= 0.7:
                target = other
                break
        if target is None:
            kept.append(dict(record))
        else:
            target["evidence"] = (target.get("evidence") or []) + [
                ev for ev in record.get("evidence") or [] if ev not in (target.get("evidence") or [])]
            log.info("merged duplicate issue %s into %s", record.get("id"), target.get("id"))
    return kept + [r for r in records if r.get("status") != "issue"]


SYNTHETIC_PREFIXES = ("fig::", "image::", "ref::", "design::", "stats::", "claims::", "data::forensic_scan", "new:")


def _required_finding(item_id: str) -> bool:
    return not str(item_id).startswith(SYNTHETIC_PREFIXES)


def _run_dir(output_dir: str, doi: str, report_json: str, input_dir: str, resume: bool) -> Path:
    base = Path(output_dir) if output_dir else config.BASE_DIR / "data" / "output" / "review_v3"
    paper_dir = base / "review_v3" / f"{_slug(doi)}_{_identity(doi, report_json, input_dir)}"
    if config.REGRADE and not (paper_dir / "current").is_file():
        # Regrading keeps an existing investigation: use this paper's latest
        # investigated run even if it was recorded under another input id.
        runs = [run for run in (base / "review_v3").glob(f"{_slug(doi)}_*/run_*") if (run / "finals.json").is_file()]
        if runs:
            return max(runs, key=lambda run: (run / "finals.json").stat().st_mtime)
    paper_dir.mkdir(parents=True, exist_ok=True)
    pointer = paper_dir / "current"
    if resume and pointer.is_file():
        existing = paper_dir / pointer.read_text().strip()
        if existing.is_dir():
            return existing
    run_dir = paper_dir / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    run_dir.mkdir()
    pointer.write_text(run_dir.name)
    return run_dir


async def _evidence(doi: str, report_json: str, input_dir: str, output_dir: str) -> str:
    from api.services.review import _ensure_evidence_bundle, _preprocess_review_input_dir, _review_artifacts_dir
    from api.services.review_runtime import limited_evidence
    await asyncio.to_thread(_preprocess_review_input_dir, input_dir)
    return await limited_evidence(_ensure_evidence_bundle, doi, report_json, input_dir, _review_artifacts_dir(output_dir))


async def _review(doi: str, report_json: str, input_dir: str, output_dir: str, run_dir: Path,
                  deadline: float | None) -> dict:
    from api.services.review_validation import validate
    started = time.monotonic()
    state_path = run_dir / "state.json"
    state = _read_json(state_path, {}) or {}
    final_path = run_dir / "final.json"

    evidence_path = state.get("evidence_path")
    if not evidence_path or not Path(evidence_path).is_file() or (_read_json(Path(evidence_path), {}) or {}).get("_fallback_reason"):
        evidence_path = await _evidence(doi, report_json, input_dir, output_dir)
        state["evidence_path"] = evidence_path
        _write_json(state_path, state)
    evidence = _read_json(Path(evidence_path), {}) or {}
    if not evidence or evidence.get("_fallback_reason"):
        failed = _fallback(doi, "Evidence preparation incomplete; saved diagnostics retained; "
                                "do not start model investigation from a partial inventory")
        failed["_retry_exhausted"] = True
        return failed

    cached = _read_json(final_path)
    if isinstance(cached, dict) and cached.get("trigger") != "review_error":
        if config.REGRADE and state.get("rules_version") != rules_version():
            _start_regrade(run_dir, final_path)
            state["regrading"] = True
            _write_json(state_path, state)
        else:
            checked, gaps = validate(cached, evidence)
            if not gaps:
                return checked

    review = Review(doi, report_json, input_dir, run_dir, deadline, started)
    try:
        return await _review_phases(review, doi, evidence, evidence_path, state, state_path, final_path, started)
    finally:
        await review.close()


async def _review_phases(review: "Review", doi: str, evidence: dict, evidence_path: str, state: dict,
                         state_path: Path, final_path: Path, started: float) -> dict:
    run_dir = review.run_dir
    await review.prepare(evidence_path)
    pipeline = await asyncio.gather(*(review.shard_pipeline(shard) for shard in review.shards))
    review.timeline["shards"] = time.monotonic() - started

    finals, disputes = review.merge()
    round_no = int(state.get("arbitration_rounds", 0)) + 1
    # Earlier calls' arbitration results still count on resume.
    # A regrade keeps the investigation as it was: the last arbitration
    # outcome stands (including "unresolved") and no new round is started.
    regrading = bool(state.get("regrading"))
    for previous in range(1, round_no):
        for dim in DIMENSIONS:
            for item_id, record in (ledger.load(review.ledger_path(f"arb{previous}", dim)).get("items") or {}).items():
                if record.get("status") not in ("open", "unresolved") or (regrading and record.get("status") == "unresolved"):
                    finals[item_id] = {**{k: record.get(k) for k in ("id", "title", "members")},
                                       "dimension": record.get("dimension") or dim, **_compact(record)}
    disputes = [d for d in disputes if d["id"] not in finals]
    if disputes and not regrading:
        state["arbitration_rounds"] = round_no
        _write_json(state_path, state)
        decided = await review.arbitrate(disputes, round_no)
        for item in disputes:
            record = decided.get(item["id"], {"status": "unresolved", "note": "仲裁未完成"})
            finals[item["id"]] = {**{k: item.get(k) for k in ("id", "title", "kind", "members", "shard")},
                                  "dimension": record.get("dimension") or item["dimension"], **_compact(record)}
            first = item.get("first") or {}
            if first and (first.get("status") == "issue") != (record.get("status") == "issue"):
                memory.record_lesson(doi, item.get("shard", ""), item, first, record)
    for item in disputes if regrading else []:
        finals[item["id"]] = {**{k: item.get(k) for k in ("id", "title", "kind", "members", "shard")},
                              "dimension": item["dimension"], "status": "unresolved", "note": "仲裁未完成"}
    review.timeline["arbitration"] = time.monotonic() - started - review.timeline["shards"]

    finals = await review.calibrate(finals)
    review.timeline["calibrate"] = time.monotonic() - started - review.timeline["shards"] - review.timeline.get("arbitration", 0)
    verdicts = review.verdicts(finals)
    unresolved = [r for r in finals.values() if r.get("status") == "unresolved"]
    # Deterministic must-check findings may not stay open under a "no risk"
    # verdict; open synthetic checks are disclosed as scope limitations.
    blocking = [r for r in unresolved if _required_finding(r["id"])]
    _write_json(run_dir / "finals.json", {"verdicts": verdicts, "finals": finals, "pipeline": pipeline,
                                          "disputes": len(disputes), "timeline": review.timeline})
    if blocking and verdicts["result"] == "低风险":
        names = "；".join(str(r.get("title") or r["id"])[:80] for r in blocking[:8])
        failed = _fallback(doi, f"{len(blocking)} 项必查发现未能完成核查（{names}）；检查点已保留，可续跑")
        failed["_unresolved_items"] = [r["id"] for r in blocking]
        return failed

    result, gaps = await review.write(finals, verdicts)
    review.timeline["write"] = (time.monotonic() - started - review.timeline["shards"]
                                - review.timeline.get("arbitration", 0) - review.timeline.get("calibrate", 0))
    if result is None:
        failed = _fallback(doi, "报告写作未通过发布校验：" + "；".join(gaps[:6]))
        failed["_coverage_gaps"] = gaps
        return failed
    result["_engine"] = "v3"
    result["_review_metrics"] = {"timeline": {k: round(v, 1) for k, v in review.timeline.items()},
                                 "shards": pipeline, "disputes": len(disputes), "unresolved": len(unresolved)}
    _write_json(final_path, result)
    state["rules_version"] = rules_version()
    state.pop("regrading", None)
    _write_json(state_path, state)
    return result


async def run_review(doi: str, report_json_path: str, input_dir: str, output_dir: str, resume: bool = False) -> dict:
    """Drop-in replacement for api.services.review.run_review_single."""
    from api.services.review_lock import exclusive_review
    from api.services.review_runtime import paper_deadline
    from modules.doi_utils import normalize_doi
    doi = normalize_doi(doi) or doi
    # Sessions run in their own working directory; every path must be absolute.
    output_dir = str(Path(output_dir).resolve()) if output_dir else output_dir
    input_dir = str(Path(input_dir).resolve())
    report_json_path = str(Path(report_json_path).resolve()) if report_json_path else report_json_path
    run_dir = _run_dir(output_dir, doi, report_json_path, input_dir, resume)
    timeout = config.PAPER_TIMEOUT_SECONDS or None
    deadline = time.monotonic() + timeout if timeout else None
    token = paper_deadline.set(deadline)
    started = time.monotonic()
    try:
        with exclusive_review(run_dir.parent / "review.lock"):
            try:
                result = await asyncio.wait_for(
                    _review(doi, report_json_path, input_dir, output_dir, run_dir, deadline), timeout)
            except TimeoutError:
                result = _fallback(doi, f"Whole-paper deadline exceeded ({timeout}s); checkpoints retained")
                result["_retry_exhausted"] = True
    finally:
        paper_deadline.reset(token)
        log.info("DOI=%s v3 review wall_seconds=%.1f run=%s", doi, time.monotonic() - started, run_dir)
    result.setdefault("_input_dir", input_dir)
    result.setdefault("_report_json", report_json_path)
    state = _read_json(run_dir / "state.json", {}) or {}
    if state.get("evidence_path"):
        result.setdefault("_evidence_path", state["evidence_path"])
    result["_review_workdir"] = str(run_dir)
    return result
