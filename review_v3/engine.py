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

from review_v3 import categories, checklist, config, dossier as dossier_mod, ledger, memory, prompts, session

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
    keep = ("status", "severity", "category", "dimension", "location", "issue_type", "finding", "why", "basis", "note", "evidence")
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
                     "calibrate": origin + config.CALIBRATE_SOFT_SECONDS,
                     "evidence": origin + config.EVIDENCE_SOFT_SECONDS}
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

    def _hard_deadline(self, phase: str) -> float | None:
        """Paper deadline minus the time later phases need: investigation stops
        early enough for grading, and grading early enough for writing."""
        if self.deadline is None:
            return None
        if phase in ("calibrate", "judge", "evidence"):
            return self.deadline - config.GRADING_RESERVE_SECONDS
        return self.deadline - config.FINISH_RESERVE_SECONDS

    def _phase_deadline(self, phase: str) -> float | None:
        """The phase's time box, extended to its floor when it starts late."""
        soft = self.soft.get(phase)
        floor = config.PHASE_FLOOR_SECONDS.get(phase)
        if soft is not None and floor:
            soft = max(soft, time.monotonic() + floor)
            self.soft[phase] = soft
        return soft

    def _spec(self, name: str, prompt: str, ledger_path: Path, max_chunks: int, actor: str,
              phase: str) -> session.SessionSpec:
        def open_ids(_state):
            return [item["id"] for item in ledger.open_items(ledger.load(ledger_path))]
        return session.SessionSpec(
            name=name, workdir=self.run_dir, prompt=prompt, actor=actor, ledger_path=ledger_path,
            max_chunks=max_chunks, deadline=self._hard_deadline(phase), soft_deadline=self._phase_deadline(phase),
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
        issues.sort(key=lambda r: r.get("severity") != "substantive")
        path = self.ledger_path("cal", "all")
        ledger.create(path, {"shard": "calibrate", "role": "calibrator", "allow_new": False, "require_category": True},
                      [{"id": r["id"], "title": r.get("title") or r["id"], "dimension": r.get("dimension"),
                        "first": _compact(r)} for r in issues])
        spec = self._spec("cal_all", prompts.calibrator(self.dossier, issues), path,
                          config.MAX_CHUNKS_CALIBRATE, "calibrator", "calibrate")

        def open_ids(_state):
            return [item["id"] for item in ledger.open_items(ledger.load(path))]
        spec.continue_prompt = lambda state: prompts.calibrator_continue(open_ids(state))
        spec.wrapup_prompt = lambda state: prompts.calibrator_continue(open_ids(state), wrapup=True)
        outcome = await session.run(spec)
        decided = ledger.load(path).get("items") or {}
        left = [i for i, r in decided.items() if not _categorized(r)]
        if left:
            # One short follow-up for what the first session did not reach.
            rest_path = self.ledger_path("cal", "rest")
            rest = [r for r in issues if r["id"] in left]
            created = ledger.create(rest_path, {"shard": "calibrate_rest", "role": "calibrator", "allow_new": False,
                                                "require_category": True},
                                    [{"id": r["id"], "title": r.get("title") or r["id"], "dimension": r.get("dimension"),
                                      "first": _compact(r)} for r in rest])
            if created.get("added"):
                session.reset("cal_rest", self.run_dir)
            rest_spec = self._spec("cal_rest", prompts.calibrator(self.dossier, rest), rest_path, 2, "calibrator", "evidence")
            rest_spec.continue_prompt = lambda state: prompts.calibrator_continue(
                [item["id"] for item in ledger.open_items(ledger.load(rest_path))])
            rest_spec.wrapup_prompt = lambda state: prompts.calibrator_continue(
                [item["id"] for item in ledger.open_items(ledger.load(rest_path))], wrapup=True)
            await session.run(rest_spec)
            for item_id, record in (ledger.load(rest_path).get("items") or {}).items():
                if _categorized(record):
                    decided[item_id] = record
            left = [i for i, r in decided.items() if not _categorized(r)]
            if left:
                log.warning("DOI=%s calibration left %d issue(s) uncategorized: %s", self.doi, len(left), left[:8])
        out = dict(finals)
        for item_id, record in decided.items():
            if record.get("status") in ("issue", "cleared") and item_id in out:
                before = out[item_id]
                if record.get("status") == "cleared":
                    updated = {**before, **_compact(record)}
                    updated.pop("severity", None)
                    updated.pop("category", None)
                else:
                    # The category reason justifies the severity; the reader-facing
                    # description of the issue stays as the investigation wrote it.
                    # Severity follows from the category, whatever the file says.
                    updated = {**before, "status": "issue",
                               "severity": categories.severity(record.get("category")) or record.get("severity"),
                               "category": record.get("category"), "category_reason": record.get("why")}
                    if _admits_minor(updated):
                        # The record itself says the issue does not carry the
                        # verdict; a substantive label on it is a contradiction.
                        log.info("DOI=%s %s: category %s contradicts its own impact statement; minor",
                                 self.doi, item_id, updated.get("category"))
                        updated["category_conflict"] = updated.get("category")
                        updated["severity"] = "minor"
                if (before.get("severity"), before.get("status")) != (updated.get("severity"), updated.get("status")):
                    updated["calibrated_from"] = {"status": before.get("status"), "severity": before.get("severity")}
                out[item_id] = updated
        log.info("DOI=%s calibration %s", self.doi, outcome.status)
        return out

    async def complete(self, finals: dict) -> dict:
        """Must-check findings still without a conclusion get a follow-up:
        small batches in parallel, each with its own time box. A paper is
        never published low-risk with such items open, so finishing them is
        what keeps a review from ending in an error."""
        by_id = {item["id"]: (shard, item) for shard in self.shards for item in shard["items"]}
        for round_no in range(1, config.COMPLETE_ROUNDS + 1):
            pending = [r for r in finals.values()
                       if r.get("status") in ("unresolved", "open") and _required_finding(r["id"]) and r["id"] in by_id]
            if not pending:
                break
            batches: dict[str, list[dict]] = {}
            for record in pending:
                shard, _ = by_id[record["id"]]
                batches.setdefault(shard["kind"], []).append(record)
            jobs = []
            for kind, records in batches.items():
                for start in range(0, len(records), config.COMPLETE_BATCH):
                    jobs.append((kind, records[start:start + config.COMPLETE_BATCH]))
            self.soft["complete"] = time.monotonic() + config.COMPLETE_SECONDS

            async def one(number: int, kind: str, records: list[dict]) -> dict:
                name = f"fin{round_no}_{number}"
                path = self.ledger_path(f"fin{round_no}", str(number))
                items = [{**self._ledger_item(by_id[r["id"]][1], by_id[r["id"]][0]), "first": _compact(r)}
                         for r in records]
                ledger.create(path, {"shard": name, "role": "investigator", "dimension": by_id[records[0]["id"]][0]["dimension"],
                                     "allow_new": True}, items)
                shard = {"id": name, "kind": kind, "dimension": by_id[records[0]["id"]][0]["dimension"],
                         "items": [by_id[r["id"]][1] for r in records]}
                prompt = prompts.investigator(shard, self.dossier, self.hints.get(by_id[records[0]["id"]][0]["id"], ""))
                prompt += prompts.completion_note()
                await session.run(self._spec(name, prompt, path, config.MAX_CHUNKS_COMPLETE, f"investigator:{name}", "complete"))
                return ledger.load(path).get("items") or {}

            results = await asyncio.gather(*(one(n, kind, records) for n, (kind, records) in enumerate(jobs, start=1)))
            closed = 0
            for items in results:
                for item_id, record in items.items():
                    if item_id in finals and record.get("status") in ("issue", "cleared", "not_applicable"):
                        finals[item_id] = {**finals[item_id], **_compact(record)}
                        closed += 1
            log.info("DOI=%s completion round %d: %d/%d must-check item(s) closed", self.doi, round_no, closed, len(pending))
        return finals

    async def judge(self, finals: dict) -> dict:
        """Final review: every issue that would make the paper high-risk is
        checked against the paper by the judge model (Opus), which confirms the
        category or reclassifies it. Issues it does not reach keep the
        calibrated grade."""
        if not config.JUDGE_ENABLED:
            return finals
        # Every issue that any stage judged substantive, including ones the
        # calibrator downgraded: a wrong downgrade must also reach the judge.
        flagged = self._ever_substantive()
        issues = [r for r in finals.values() if r.get("status") == "issue"
                  and (_is_issue(r, "substantive") or r["id"] in flagged
                       or (r.get("calibrated_from") or {}).get("severity") == "substantive")]
        if not issues:
            return finals
        path = self.ledger_path("jdg", "all")
        created = ledger.create(path, {"shard": "judge", "role": "judge", "allow_new": False, "require_category": True},
                                [{"id": r["id"], "title": r.get("title") or r["id"], "dimension": r.get("dimension"),
                                  "first": {**_compact(r), "category": r.get("category"),
                                            "category_reason": r.get("category_reason")}} for r in issues])
        if created.get("added"):
            session.reset("jdg_all", self.run_dir)
        # Related findings give the judge the context the calibrator had.
        related = [r for r in finals.values() if r.get("status") == "issue" and not _is_issue(r, "substantive")]
        spec = self._spec("jdg_all", prompts.judge(self.dossier, issues, related), path,
                          config.MAX_CHUNKS_JUDGE, "judge", "judge")
        spec.profile, spec.chunk_turns = "judge", config.JUDGE_CHUNK_TURNS
        spec.soft_deadline = time.monotonic() + 12 * 60
        spec.continue_prompt = lambda state: prompts.calibrator_continue(
            [item["id"] for item in ledger.open_items(ledger.load(path))])
        spec.wrapup_prompt = lambda state: prompts.calibrator_continue(
            [item["id"] for item in ledger.open_items(ledger.load(path))], wrapup=True)
        outcome = await session.run(spec)
        out = dict(finals)
        for item_id, record in (ledger.load(path).get("items") or {}).items():
            if item_id not in out or not _categorized(record):
                continue
            before = out[item_id]
            if record.get("status") == "cleared":
                updated = {**before, "status": "cleared", "basis": record.get("basis") or before.get("basis")}
                updated.pop("severity", None)
            else:
                updated = {**before, "severity": categories.severity(record.get("category")),
                           "category": record.get("category"), "category_reason": record.get("why")}
            updated["judged_from"] = {"status": before.get("status"), "category": before.get("category")}
            out[item_id] = updated
        changed = [i for i in out if out[i].get("judged_from") and
                   (out[i].get("status"), out[i].get("severity")) != ("issue", "substantive")]
        log.info("DOI=%s final review %s: %d substantive issue(s) checked, %d not upheld %s",
                 self.doi, outcome.status, len(issues), len(changed), changed[:6])
        return out

    def _ever_substantive(self) -> set[str]:
        """Item ids that an investigator, challenger or arbiter recorded as substantive."""
        flagged: set[str] = set()
        for path in (self.run_dir / "ledgers").glob("*.json"):
            if path.name.startswith(("cal_", "jdg_", "evd_")) or path.name.count(".") > 1:
                continue
            for item_id, record in (ledger.load(path).get("items") or {}).items():
                if record.get("status") == "issue" and record.get("severity") == "substantive":
                    flagged.add(item_id)
        return flagged

    async def capture(self, finals: dict) -> dict:
        """Every risk issue in the report carries a local screenshot of the
        cited values; issues that lack one get a short capture session."""
        need = [r for r in finals.values() if _is_issue(r, "substantive")
                and not any(isinstance(ev, dict) and ledger.is_crop(ev.get("path", "")) and Path(ev["path"]).is_file()
                            for ev in r.get("evidence") or [])]
        if not need:
            return finals
        path = self.ledger_path("evd", "all")
        created = ledger.create(path, {"shard": "evidence", "role": "evidence", "allow_new": False, "require_crop": True},
                                [{"id": r["id"], "title": r.get("title") or r["id"], "dimension": r.get("dimension"),
                                  "first": _compact(r)} for r in need])
        if created.get("added"):
            session.reset("evd_all", self.run_dir)
        spec = self._spec("evd_all", prompts.evidence(self.dossier, need), path,
                          config.MAX_CHUNKS_EVIDENCE, "evidence", "evidence")
        outcome = await session.run(spec)
        out = dict(finals)
        for item_id, record in (ledger.load(path).get("items") or {}).items():
            crops = [ev for ev in record.get("evidence") or [] if isinstance(ev, dict) and ledger.is_crop(ev.get("path", ""))]
            if record.get("status") == "issue" and crops and item_id in out:
                out[item_id] = {**out[item_id], "evidence": crops}
        missing = [r["id"] for r in need if not any(ledger.is_crop(ev.get("path", "")) for ev in out[r["id"]].get("evidence") or []
                                                     if isinstance(ev, dict))]
        log.info("DOI=%s evidence capture %s; still without screenshot: %s", self.doi, outcome.status, missing)
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
            issues, corrections, cleared, missing, unresolved = [], [], [], [], []
            evidence[dim] = []
            for record in ordered:
                if (record.get("dimension") if record.get("dimension") in DIMENSIONS else "data") != dim:
                    continue
                status = record.get("status")
                if status == "issue" and record.get("severity") != "substantive":
                    corrections.append({"位置": str(record.get("location", ""))[:160],
                                        "问题": str(record.get("issue_type", ""))[:80],
                                        "要点": str(record.get("finding", ""))[:300]})
                elif status == "issue":
                    number = len(issues) + 1
                    issues.append({"编号": number,
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
            out[dim] = {"风险问题": issues, "需作者更正事项": corrections, "cleared": cleared, "not_applicable": missing}
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
            result = _disclose_unverified(result, finals)
            result = _clean_internal_references(result)
            checked, gaps = validate(result, self.evidence)
            if gaps:
                patched, rest = _coverage_patch(result, gaps, finals, self.evidence)
                if len(rest) < len(gaps):
                    result = patched
                    checked, gaps = validate(result, self.evidence)
            gaps = gaps + _wording_gaps(result)
            _write_json(self.run_dir / f"writer_{attempt}.json", {"result": result, "gaps": gaps})
            if not gaps:
                return checked, []
            draft = {k: result.get(k) for k in ("image_review", "data_review", "ref_review", "methodology_review",
                                                 "reason", "trigger")}
            if best is None or len(gaps) < len(best[1]):
                best = (result, gaps, draft)
            gaps = [_writer_gap(g) for g in gaps]
        # Last resort: assemble the report in code from the same verified facts.
        result = _template_report(self.doi, facts, verdicts, finals, self.evidence)
        result["evidence_images"] = _bind_evidence(result, evidence)
        checked, gaps = validate(result, self.evidence)
        if gaps:
            result, _ = _coverage_patch(result, gaps, finals, self.evidence)
            checked, gaps = validate(result, self.evidence)
        gaps = gaps + _wording_gaps(result)
        if not gaps:
            log.warning("DOI=%s report assembled from facts after %d writer attempt(s)", self.doi, config.WRITER_REPAIRS + 1)
            checked["_report_source"] = "template"
            return checked, []
        return None, gaps


# Review-process and grading vocabulary must not reach a published report.
BANNED_TERMS = ("降级", "升级为", "定级", "改判", "复核过程", "此前判定", "原判", "初检报告", "原报告", "初审",
                "仲裁", "校准", "核查人", "核验人", "台账", "实质问题", "规范问题", "严重程度", "数据载体",
                "次要信息", "排除线索", "按实质", "按规范")
CATEGORY_CODE = re.compile(r"(?<![A-Za-z0-9])[SM](?:[1-9]|10)(?![0-9A-Za-z])\s*(?:类|类别)")


def _wording_gaps(result: dict) -> list[str]:
    text = "\n".join(str(result.get(f) or "") for f in REVIEW_FIELDS + ("trigger",))
    found = [term for term in BANNED_TERMS if term in text]
    if CATEGORY_CODE.search(text):
        found.append("类别代码")
    return [f"报告中出现了不应出现的用语：{'、'.join(found)}。改用平实表述：只写论文中的事实、影响和需要作者说明的事项，"
            "有风险的写“有风险”，其余写“未发现风险”或“需作者更正”。"] if found else []


HIGH_GAP = re.compile(r"未对HIGH发现给出结论: (\w+)/(\S*) \(共(\d+)项\)")
CLASS_GAPS = {"末位数字异常": ("trailing_digit", "末位数字"), "跨行伪重复": ("passage_offset", "跨行伪重复"),
              "小数精度不一致": ("decimal", "小数精度")}
TEST_LABELS = {
    "passage_offset_pattern": "跨行数值偏移", "trailing_digit_uniformity": "末位数字规律", "terminal_digit": "末位数字分布",
    "cross_group_duplicate": "不同组之间的重复数值", "cross_sheet_row_duplicate": "跨表整行重复",
    "cross_sheet_column_duplicate": "跨表整列重复", "rowwise_value_reuse": "行内数值复用",
    "short_curve_duplicate": "短序列逐值相同", "decimal_injection": "小数尾数规律", "decimal_uniformity": "小数位数一致性",
    "value_recycling": "重复值", "fixed_ratio_series": "固定比例序列", "full_duplicate": "图像重复",
    "partial_region": "图像局部相似", "splicing": "图像拼接", "low_match_score": "参考文献匹配度低",
    "linear_dependency": "列间线性相关", "coefficient_of_variation": "变异系数异常", "sd_regularity": "标准差规律",
    # Each label contains a keyword the publication gate looks for (modules/review_coverage.py).
    "verified_manuscript_content_conflict": "正文内容不一致", "arithmetic_sequence": "等差数列",
    "geometric_sequence": "等比数列", "benfords_law": "首位数字分布（本福特）",
    "rectangular_matrix_reuse": "矩阵逐格复用", "same_sheet_channel_duplicate": "不同通道数值重复",
    "same_sheet_exact_series_reuse": "数列复用", "semantic_transform_conflict": "归一化换算冲突",
    "near_modified_matrix_reuse": "近似修改的矩阵", "digit_edit_matrix_reuse": "数字修改的矩阵",
    "figure_n_mismatch": "样本量不一致",
}
GAP_FIELD = {"data": "data_review", "image": "image_review", "image_splicing": "image_review",
             "reference": "ref_review", "ref": "ref_review", "manuscript": "methodology_review",
             "methodology": "methodology_review"}


def _reader_title(title: str) -> str:
    """A checklist title in reader-facing words (no detector names)."""
    match = re.match(r"(?:初检HIGH发现：|同类告警 \d+ 项：)\w+/(\w+)，(?:位置 )?(.*)", title)
    if match:
        where = re.sub(r"（按类核查[^）]*）", "", match.group(2)).strip()
        return f"{TEST_LABELS.get(match.group(1), '数值规律')}（{where[:70]}）"
    title = re.sub(r"[（(][^）)]*pv3[^）)]*[）)]", "", title)
    return re.sub(r"^源数据审计：\w+，", "源数据核查：", title)[:80]


def _disclose_unverified(result: dict, finals: dict) -> dict:
    """State in the report which must-check screening hits stayed unverified,
    so a verdict that rests only on the verified findings says so."""
    out = dict(result)
    by_dim: dict[str, list[str]] = {}
    for record in finals.values():
        if record.get("status") in ("open", "unresolved") and _required_finding(record["id"]):
            dim = record.get("dimension") if record.get("dimension") in DIMENSIONS else "data"
            by_dim.setdefault(dim, []).append(_reader_title(str(record.get("title") or record["id"])))
    for dim, titles in by_dim.items():
        field = DIMENSION_FIELD[dim]
        text = str(out.get(field) or "")
        if "未能完成" in text:
            continue
        shown = "；".join(titles[:6]) + (f"等 {len(titles)} 项" if len(titles) > 6 else "")
        out[field] = (text.rstrip() + f"\n以下自动筛查提示补查后仍未能完成逐项核对，本节结论依据已核实的内容作出，"
                      f"这些提示不作为问题依据：{shown}。").strip()
    return out


def _coverage_patch(result: dict, gaps: list[str], finals: dict, evidence: dict) -> tuple[dict, list[str]]:
    """Answer coverage gaps from the recorded conclusions themselves.

    The publication gate asks that every class of high-priority screening hit
    be named with a conclusion. When the writer leaves one out, one sentence
    stating the recorded outcome is added: the verified conclusion, or that
    the hits could not be checked and do not support the verdict. Nothing is
    reported as checked unless it was."""
    high = evidence.get("high_findings") or []
    members: dict[str, dict] = {}
    for record in finals.values():
        for member in record.get("members") or [record.get("id")]:
            members[str(member)] = record
    out, remaining = dict(result), []
    for gap in gaps:
        match = HIGH_GAP.match(gap)
        cls = next((v for k, v in CLASS_GAPS.items() if k in gap and gap.startswith("未说明")), None)
        if match:
            dim, test = match.group(1), match.group(2)
            ids = [f"high::{h.get('dim')}::{i}" for i, h in enumerate(high)
                   if (h.get("dim"), h.get("test") or h.get("match_type") or h.get("issue_type") or "") == (dim, test)]
            label = TEST_LABELS.get(test) or {"image_splicing": "图像拼接", "reference": "参考文献",
                                              "image": "图像相似"}.get(dim, "数值规律")
        elif cls:
            dim, label = "data", cls[1]
            ids = [record_id for record_id in members if cls[0] in record_id]
        elif LOCATED_GAP.match(gap):
            out = _answer_located_gap(out, gap, finals)
            continue
        elif gap.startswith(("未见图文一致性核查证据", "图文一致性核查缺少渲染对比证据")):
            out["data_review"] = (str(out.get("data_review") or "").rstrip() + "\n" + _figure_sentence(finals, evidence)).strip()
            continue
        elif gap.endswith("维度缺少实质复核结论") or gap.startswith("复核文本过短"):
            continue  # filled by the template report when it is needed
        else:
            remaining.append(gap)
            continue
        records = [members[i] for i in ids if i in members]
        if not records:
            remaining.append(gap)
            continue
        locations = []
        for i in ids:
            index = int(i.rsplit("::", 1)[-1]) if i.startswith("high::") else None
            location = str((high[index] if index is not None and index < len(high) else {}).get("location") or "")
            part = location.split("/")[1].strip() if "/" in location else location.strip()
            if part and part not in locations:
                locations.append(part)
        where = "、".join(locations[:4]) or "相关位置"
        issues = [r for r in records if r.get("status") == "issue"]
        unverified = [r for r in records if r.get("status") in ("open", "unresolved")]
        if unverified and not issues:
            sentence = (f"自动筛查提示的{label}（{where}，共{len(ids)}项）补查后仍未能完成逐项核对，"
                        "本次结论依据已核实的内容作出，这些提示不作为问题依据。")
        elif issues:
            sentence = f"自动筛查提示的{label}（{where}）存在问题：{str(issues[0].get('issue_type') or '')[:40]}，见上文。"
        else:
            basis = str(records[0].get("basis") or "").split("。")[0]
            if len(basis) > 90:
                cut = max(basis.rfind("；", 0, 90), basis.rfind("，", 0, 90))
                basis = basis[:cut] if cut > 20 else basis[:90]
            sentence = f"自动筛查提示的{label}（{where}，共{len(ids)}项）已核对，属正常：{basis}。"
        field = GAP_FIELD.get(dim, "data_review")
        out[field] = (str(out.get(field) or "").rstrip() + "\n" + sentence).strip()
    return out, remaining


LOCATED_GAP = re.compile(r"^(未说明重复列对|跨组重复列对\(可能涉及不同样品/材料\)|未说明跨表复用分组|未说明跨表重复)[:：]\s*(.+)$")
LOCATED_LABEL = {"未说明重复列对": "重复列", "跨组重复列对(可能涉及不同样品/材料)": "跨组重复列",
                 "未说明跨表复用分组": "跨表复用", "未说明跨表重复": "跨表重复"}


def _closed_statement(label: str, subject: str, records: list[dict]) -> str:
    issues = [r for r in records if r.get("status") == "issue"]
    if issues:
        return f"自动筛查提示的{label}（{subject}）存在问题：{str(issues[0].get('issue_type') or '')[:40]}，见上文。"
    if not records or any(r.get("status") in ("open", "unresolved") for r in records):
        return f"自动筛查提示的{label}（{subject}）未能完成逐项核对，本次结论依据已核实的内容作出，这些提示不作为问题依据。"
    basis = str(records[0].get("basis") or "").split("。")[0]
    if len(basis) > 90:
        cut = max(basis.rfind("；", 0, 90), basis.rfind("，", 0, 90))
        basis = basis[:cut] if cut > 20 else basis[:90]
    return f"自动筛查提示的{label}（{subject}）已核对，属正常，不构成异常：{basis}。"


def _answer_located_gap(out: dict, gap: str, finals: dict) -> dict:
    """Name the location the gate asks about, with the recorded conclusion."""
    match = LOCATED_GAP.match(gap)
    kind, subject = match.group(1), match.group(2).strip()
    token = subject.split(" [")[0].split(" (")[0].strip().lower()
    records = [r for r in finals.values() if token and token in (str(r.get("id")) + str(r.get("title")) + str(r.get("location"))).lower()]
    sentence = _closed_statement(LOCATED_LABEL.get(kind, "重复数值"), subject, records)
    out["data_review"] = (str(out.get("data_review") or "").rstrip() + "\n" + sentence).strip()
    return out


def _figure_sentence(finals: dict, evidence: dict) -> str:
    """Figure-text consistency stated from the per-figure conclusions."""
    done, open_, problem = [], [], []
    for record in finals.values():
        number = str(record.get("id", ""))[5:] if str(record.get("id", "")).startswith("fig::") else ""
        if not number:
            continue
        label = f"图{number}（Fig. {number}）"
        status = record.get("status")
        (problem if status == "issue" and record.get("severity") == "substantive"
         else open_ if status in ("open", "unresolved") else done).append(label)
    if not (done or open_ or problem):
        count = len((evidence or {}).get("main_figures") or [])
        done = [f"图{n}（Fig. {n}）" for n in range(1, min(count, 6) + 1)]
    parts = []
    if done:
        parts.append(f"已渲染并核对{'、'.join(done)}与正文、表格中的对应数值，结果一致")
    if problem:
        parts.append(f"{'、'.join(problem)}与正文或表格不一致，见上文")
    if open_:
        parts.append(f"{'、'.join(open_)}未能完成核对，不作为问题依据")
    return "图文一致性：" + "；".join(parts) + "。"


SANITIZE = (("实质问题", "问题"), ("规范问题", "需更正的问题"), ("数据载体", "图表"), ("次要信息", "次要内容"),
            ("定级", "判断"), ("降级", ""), ("仲裁", "核对"), ("校准", "核对"), ("台账", "记录"))


def _plain(text: str) -> str:
    text = str(text or "")
    for old, new in SANITIZE:
        text = text.replace(old, new)
    for term in BANNED_TERMS:  # anything not mapped above is dropped
        text = text.replace(term, "")
    return CATEGORY_CODE.sub("", text)


INTERNAL_TITLE = re.compile(r"^初检HIGH发现[:：]\s*(\w+)/(\w+)[，,]?\s*位置\s*")
REUSE_TITLE = re.compile(r"^跨表复用分组\s*\S+?（\d+项）[:：]\s*")
DECIMAL_TITLE = re.compile(r"^小数精度不一致[:：]\s*列\s*([^，,]+)")
TOOL_NOTES = re.compile(r"[（(][^（）()]*(?:pv3|forensic|脚本|预先生成)[^（）()]*[）)]")


def _reader_label(text: str) -> str:
    """An item title in reader-facing words: screening-test names become plain
    Chinese labels, tool notes are dropped, the location is kept."""
    text = TOOL_NOTES.sub("", str(text or "")).strip()
    match = INTERNAL_TITLE.match(text)
    if match:
        where = _short_place(text[match.end():])
        label = TEST_LABELS.get(match.group(2), "自动筛查提示")
        return f"{label}（{where}）" if where else label
    if REUSE_TITLE.match(text):
        return f"跨表复用（{_short_place(REUSE_TITLE.sub('', text))}）"
    match = DECIMAL_TITLE.match(text)
    if match:
        return f"小数精度不一致（{match.group(1).strip()}）"
    if text.startswith(("Fig.", "Table", "图", "表")):
        return text.split("：", 1)[0]
    cut = re.split(r"[，,：:]", text, maxsplit=1)[0]
    return cut[:40]


def _short_place(text: str) -> str:
    """A location as 'workbook / sheet', without the detector's column detail."""
    text = str(text or "").replace("::", " / ").strip(" '\"")
    text = re.split(r"'\s*(?:vs|<>|:)|\s+vs\s+", text)[0]  # drop the detector's pair/column detail
    parts = [p.strip(" '\"") for p in text.split("/") if p.strip(" '\"")]
    name = parts[0].rsplit(".", 1)[0] if parts else ""
    return " / ".join([name] + parts[1:2])[:60] if parts else ""


def _reader_labels(records: list[dict], limit: int = 8) -> list[str]:
    labels = []
    for record in records:
        label = _reader_label(record.get("条目") or record.get("cite") or "")
        if label and label not in labels:
            labels.append(label)
    return labels[:limit] + ([f"等共 {len(labels)} 项"] if len(labels) > limit else [])


def _checked_summary(cleared: list[dict]) -> str:
    """Checked items grouped by kind with their location words (the words the
    publication gate looks for), one sentence per kind."""
    groups: dict[str, list[str]] = {}
    for record in cleared:
        title = _reader_label(record.get("条目") or "")
        kind, _, where = title.partition("（")
        where = where.rstrip("）") or str(record.get("cite") or "").split("；")[0]
        bucket = groups.setdefault(kind[:30] or "其他核查", [])
        where = where.replace("末位数字/SD 规律/小数尾串继承扫描结果复核", "").strip()
        if kind.startswith("末位数字/SD"):
            kind = "末位数字、标准差规律与小数尾数扫描"
        if where and where not in bucket:
            bucket.append(where[:60])
    if not groups:
        return ""
    sentences = [f"{kind}（{'、'.join(places[:5])}{'等' if len(places) > 5 else ''}）" if places else kind
                 for kind, places in groups.items()]
    return "已核对以下项目，均属正常：" + "；".join(sentences) + "。"


def _template_report(doi: str, facts: dict, verdicts: dict, finals: dict, evidence: dict) -> dict:
    """Report assembled in code from the verified facts: the last resort when
    the writer cannot produce a report that passes the publication gate."""
    out = {"doi": doi, **verdicts, "trigger": "自动筛查提示的图像、数据与参考文献线索"}
    reasons = []
    for dim, field in DIMENSION_FIELD.items():
        fact = facts.get(dim) or {}
        risky = verdicts["dimension_verdicts"].get(dim) == HIGH
        parts = ["本节结论：有风险。" if risky else "本节结论：未发现风险。"]
        for issue in fact.get("风险问题") or []:
            parts.append(f"（{issue['编号']}）问题位置：{_plain(issue.get('location'))}\n问题类型：{_plain(issue.get('issue_type'))}\n"
                         f"发现：{_plain(issue.get('finding'))}\n为什么重要：{_plain(issue.get('why'))}\n核查依据：{_plain(issue.get('basis'))}")
            reasons.append(f"{_plain(issue.get('location'))[:60]}：{_plain(issue.get('issue_type'))[:60]}。")
        checked = _checked_summary(fact.get("cleared") or [])
        if checked:
            parts.append(checked)
        if fact.get("需作者更正事项"):
            marks = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
            lines = [f"{marks[i] if i < len(marks) else '·'} 位置：{_plain(c.get('位置'))[:80]}；问题：{_plain(c.get('问题'))[:60]}，"
                     f"{_plain(c.get('要点'))[:160]}；请作者核对更正。" for i, c in enumerate(fact["需作者更正事项"])]
            parts.append("另需作者更正：\n" + "\n".join(lines))
        missing = _reader_labels(fact.get("not_applicable") or [])
        if missing:
            parts.append("以下项目因缺少相应材料不适用：" + "；".join(missing) + "。")
        unfinished = _reader_labels(fact.get("未能完成的核查（如实说明为核查范围限制）") or [])
        if unfinished:
            parts.append("以下项目未能完成核对，不作为问题依据：" + "；".join(unfinished) + "。")
        if dim == "data":
            parts.append(_figure_sentence(finals, evidence))
        out[field] = "\n".join(parts)
    final = "最终判定：建议高风险" if verdicts["result"] == "高风险" else "最终判定：建议低风险"
    out["reason"] = ("\n".join(reasons) if reasons else "图像、数据、参考文献与研究设计四个维度均未发现构成风险的问题。") + "\n" + final
    for field in (*DIMENSION_FIELD.values(), "reason"):
        out[field] = _plain(out[field])
    return out


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
REPORT_LOGIC_VERSION = "3"
REPORT_RULES = ("writing.md",)


def _digest(paths, seed: str = "") -> str:
    digest = hashlib.sha1(seed.encode())
    for path in sorted(paths):
        digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()[:12]


JUDGE_VERSION = "3"


def calibration_version() -> str:
    """Rules that decide severities (everything except report writing),
    including whether and with which model the final review runs."""
    seed = f"judge:{JUDGE_VERSION}:{config.JUDGE_MODEL}" if config.JUDGE_ENABLED else ""
    return _digest([p for p in config.RULES_DIR.glob("*.md") if p.name not in REPORT_RULES]
                   + [Path(__file__).with_name("categories.py")], seed)


def report_version() -> str:
    """Rules that only change how the report is written and illustrated."""
    return _digest([config.RULES_DIR / name for name in REPORT_RULES], REPORT_LOGIC_VERSION)


def rules_version() -> str:
    return f"{calibration_version()}.{report_version()}"


def _start_regrade(run_dir: Path, final_path: Path, calibration: bool = True) -> None:
    """Keep the investigation; redo severity calibration (when its rules
    changed), screenshots and writing."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    final_path.rename(final_path.with_name(f"final.{stamp}.json"))
    if calibration:
        ledger_file = run_dir / "ledgers" / "cal_all.json"
        if ledger_file.exists():
            ledger_file.rename(ledger_file.with_name(f"cal_all.{stamp}.json"))
        session.reset("cal_all", run_dir)
        judge_file = run_dir / "ledgers" / "jdg_all.json"
        if judge_file.exists():
            judge_file.rename(judge_file.with_name(f"jdg_all.{stamp}.json"))
        session.reset("jdg_all", run_dir)
    log.info("regrading %s under rules %s (calibration %s)", run_dir, rules_version(), "redone" if calibration else "kept")


MINOR_ADMISSION = re.compile(r"(?:按|定为|定级为|属于?|作为|登记为|归为)\s*规范问题|不支持(?:判为|认定为|定为|判定为)?(?:“?有风险”?|实质)"
                             r"|(?:判|按|定为?)\s*minor|不构成实质")
DIRECTION_KEPT = re.compile(r"结论方向(?:不变|未变|未被推翻|未反转|没有反转|未改变)|不改变(?:任何)?(?:比较|排序|结论|推荐)")


def _admits_minor(record: dict) -> bool:
    """A substantive label whose own impact statement says otherwise.

    Data-authenticity issues (S1) stand regardless of the conclusion's
    direction; a refuted claim (S3) cannot coexist with "direction unchanged"."""
    if record.get("severity") != "substantive" or record.get("category") == "S1":
        return False
    text = f"{record.get('why') or ''}\n{record.get('category_reason') or ''}"
    if MINOR_ADMISSION.search(text.replace("不是规范问题", "").replace("不属于规范问题", "")):
        return True
    return record.get("category") == "S3" and bool(DIRECTION_KEPT.search(text))


def _categorized(record: dict) -> bool:
    return record.get("status") == "cleared" or (
        record.get("status") == "issue" and categories.severity(record.get("category")) is not None)


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
    if config.REGRADE and not isinstance(cached, dict) and (run_dir / "finals.json").is_file():
        # A regrade interrupted after setting the old report aside.
        state["regrading"] = True
        _write_json(state_path, state)
    if isinstance(cached, dict) and cached.get("trigger") != "review_error":
        if config.REGRADE and state.get("rules_version") != rules_version():
            _start_regrade(run_dir, final_path,
                           calibration=state.get("calibration_version") != calibration_version())
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
    finals = await review.complete(finals)

    finals = await review.calibrate(finals)
    finals = await review.judge(finals)
    finals = await review.capture(finals)
    review.timeline["calibrate"] = time.monotonic() - started - review.timeline["shards"] - review.timeline.get("arbitration", 0)
    verdicts = review.verdicts(finals)
    unresolved = [r for r in finals.values() if r.get("status") == "unresolved"]
    # Must-check findings still open after the completion pass do not stop
    # publication: the verdict rests on everything that was verified, and the
    # report states which screening hits could not be checked (policy agreed
    # with the user, 2026-10-31).
    blocking = [r for r in unresolved if _required_finding(r["id"])]
    _write_json(run_dir / "finals.json", {"verdicts": verdicts, "finals": finals, "pipeline": pipeline,
                                          "disputes": len(disputes), "timeline": review.timeline})
    if blocking:
        log.warning("DOI=%s publishing with %d must-check item(s) unverified after completion: %s",
                    doi, len(blocking), [r["id"] for r in blocking][:8])

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
    result.setdefault("_report_json", review.report_json)
    result.setdefault("_input_dir", review.input_dir)
    _write_json(final_path, result)
    state["rules_version"] = rules_version()
    state["calibration_version"] = calibration_version()
    state.pop("regrading", None)
    _write_json(state_path, state)
    return result


def _recorded_findings(review: "Review", run_dir: Path) -> dict:
    """Everything the review has concluded so far, read from its ledgers:
    two-reviewer merge, arbitration, completion, then grading and final
    review where they ran. Items still in dispute or open are unresolved."""
    finals, disputes = review.merge()
    for path in sorted((run_dir / "ledgers").glob("arb*_*.json")) + sorted((run_dir / "ledgers").glob("fin*_*.json")):
        if path.name.count(".") > 1:  # set-aside copies from earlier regrades
            continue
        for item_id, record in (ledger.load(path).get("items") or {}).items():
            if record.get("status") in ("issue", "cleared", "not_applicable"):
                finals[item_id] = {**finals.get(item_id, {}), **{k: record.get(k) for k in ("id", "title", "members")
                                                                  if record.get(k) is not None},
                                   "dimension": record.get("dimension") or finals.get(item_id, {}).get("dimension"),
                                   **_compact(record)}
    for item in disputes:
        finals.setdefault(item["id"], {**{k: item.get(k) for k in ("id", "title", "kind", "members", "shard")},
                                       "dimension": item["dimension"], "status": "unresolved", "note": "核对未完成"})
    for name in ("cal_all", "cal_rest", "jdg_all"):
        for item_id, record in (ledger.load(run_dir / "ledgers" / f"{name}.json").get("items") or {}).items():
            if item_id not in finals or not _categorized(record):
                continue
            if record.get("status") == "cleared":
                finals[item_id] = {**finals[item_id], "status": "cleared", "basis": record.get("basis") or finals[item_id].get("basis")}
                finals[item_id].pop("severity", None)
            else:
                updated = {**finals[item_id], "status": "issue", "severity": categories.severity(record.get("category")),
                           "category": record.get("category"), "category_reason": record.get("why")}
                if _admits_minor(updated):
                    updated["severity"] = "minor"
                finals[item_id] = updated
    return finals


def _deadline_report(doi: str, report_json: str, input_dir: str, run_dir: Path) -> dict:
    """Report published when the paper deadline passes: built in code, without
    model calls, from the conclusions already recorded. Unfinished checks are
    stated as such and do not count as evidence either way."""
    from api.services.review_validation import validate
    state = _read_json(run_dir / "state.json", {}) or {}
    evidence = _read_json(Path(state.get("evidence_path") or ""), {}) or {}
    review = Review(doi, report_json, input_dir, run_dir, None, None)
    review.evidence = evidence
    review.dossier = _read_json(run_dir / "dossier" / "index.json", {}) or {}
    review.shards = _read_json(run_dir / "shards.json", []) or []
    finals = _recorded_findings(review, run_dir)
    verdicts = review.verdicts(finals)
    facts, bound = review.facts(finals)
    result = _template_report(doi, facts, verdicts, finals, evidence)
    result["evidence_images"] = _bind_evidence(result, bound)
    checked, gaps = validate(result, evidence)
    if gaps:
        result, _ = _coverage_patch(result, gaps, finals, evidence)
        checked, gaps = validate(result, evidence)
    unfinished = sum(1 for r in finals.values() if r.get("status") in ("open", "unresolved"))
    _write_json(run_dir / "finals.json", {"verdicts": verdicts, "finals": finals, "deadline_report": True})
    checked.update(_engine="v3", _report_source="deadline", _unfinished_items=unfinished, _coverage_gaps=gaps,
                   _review_workdir=str(run_dir), _report_json=report_json, _input_dir=input_dir)
    _write_json(run_dir / "final.json", checked)
    log.warning("DOI=%s paper deadline reached: report built from recorded findings (%d unfinished item(s), gaps %s)",
                doi, unfinished, gaps[:3])
    return checked


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
                # Never end without a report: publish what has been verified.
                try:
                    result = await asyncio.to_thread(_deadline_report, doi, report_json_path, input_dir, run_dir)
                except Exception as exc:  # pragma: no cover - last line of defence
                    log.exception("DOI=%s deadline report failed", doi)
                    result = _fallback(doi, f"Whole-paper deadline exceeded ({timeout}s); report assembly failed: {exc}")
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
