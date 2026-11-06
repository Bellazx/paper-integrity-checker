"""Claude CLI sessions with ledger-driven budgets.

A session runs ``CHUNK_TURNS`` turns at a time. After each chunk the ledger
decides: all items closed -> done; items closed since last chunk -> extend;
no progress for NO_PROGRESS_CHUNKS chunks -> stop. The turn cap therefore
bounds idling, not useful work. State is persisted before every launch so a
restart neither refunds budget nor repeats completed work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from review_v3 import config, ledger, visual_progress

log = logging.getLogger(__name__)

_GATES: dict[int, asyncio.Semaphore] = {}
TRANSPORT_RE = re.compile(r"timeout|timed out|rate.?limit|overload|connection|econnreset|socket|"
                          r"\b(?:429|500|502|503|504|529)\b|unauthorized|authentication", re.I)


def _gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _GATES.get(id(loop))
    if gate is None:
        gate = _GATES[id(loop)] = asyncio.Semaphore(max(1, config.MAX_SESSIONS))
    return gate


def find_cli() -> str:
    explicit = os.environ.get("CLAUDE_CLI")
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    candidates = sorted(Path("/home/library/.vscode-server/extensions").glob(
        "anthropic.claude-code-*/resources/native-binary/claude"), reverse=True)
    for candidate in candidates:
        if os.access(candidate, os.X_OK):
            return str(candidate)
    import shutil
    found = shutil.which("claude")
    if found:
        return found
    raise FileNotFoundError("claude CLI not found")


def runtime_cwd() -> Path:
    """Fixed working directory: no project CLAUDE.md or auto-memory is loaded,
    so each session's context holds only what the engine gives it."""
    path = config.BASE_DIR / "data" / "review_v3_runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


PROFILES = {"default": lambda: (config.MODEL, config.SETTINGS_FILE),
            "judge": lambda: (config.JUDGE_MODEL, config.JUDGE_SETTINGS_FILE)}


def settings_file(profile: str = "default") -> str:
    """The profile's settings file plus the PreToolUse guard that keeps sessions on this paper."""
    source = PROFILES[profile]()[1]
    try:
        base = json.loads(Path(source).read_text(encoding="utf-8")) if source else {}
    except (OSError, ValueError):
        base = {}
    guard = f"{config.PYTHON} -I {config.PACKAGE_DIR / 'tools' / 'guard.py'}"
    base["hooks"] = {"PreToolUse": [{"matcher": "Bash|Read", "hooks": [{"type": "command", "command": guard}]}]}
    read_hook = f"{config.PYTHON} -I {config.PACKAGE_DIR / 'tools' / 'visual_read_hook.py'}"
    base["hooks"]["PostToolUse"] = [{"matcher": "Read", "hooks": [{"type": "command", "command": read_hook}]}]
    # Permissions come only from the CLI flags of each session.
    base.pop("permissions", None)
    path = runtime_cwd() / ("settings_v3.json" if profile == "default" else f"settings_v3_{profile}.json")
    text = json.dumps(base, ensure_ascii=False, indent=1)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return str(path)


TRANSCRIPTS = Path.home() / ".claude" / "projects"


def tool_waits(session_id: str, since: float, workdir: Path, ledger_path: Path | None) -> dict:
    """Seconds from each tool call to its result in this session since
    ``since``, split at the moment the PreToolUse guard started (it logs to
    the work directory). A command itself runs in well under a second; long
    waits before the hook point at the CLI or the machine, not the review."""
    from datetime import datetime
    calls = []
    for path in TRANSCRIPTS.glob(f"*/{session_id}.jsonl"):
        pending = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and stamp >= since:
                    pending[block.get("id")] = stamp
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                    calls.append((pending.pop(block["tool_use_id"]), stamp))
    if not calls:
        return {}
    hooks = []
    name = ledger_path.name if ledger_path else ""
    try:
        for line in (Path(workdir) / "hook_times.log").read_text(encoding="utf-8").splitlines():
            stamp, ledger_name, _tool = (line.split("\t") + ["", ""])[:3]
            if ledger_name == name and float(stamp) >= since:
                hooks.append(float(stamp))
    except (OSError, ValueError):
        pass
    total, before = [], []
    for start, end in calls:
        total.append(end - start)
        inside = [h for h in hooks if start - 0.5 <= h <= end]
        if inside:
            before.append(min(inside) - start)

    def median(values):
        values = sorted(values)
        return round(values[len(values) // 2], 1) if values else None

    return {"tool_calls": len(total), "tool_wait_s": median(total), "before_hook_s": median(before)}


def session_env(workdir: Path, ledger_path: Path | None, actor: str, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update(extra or {})
    env["PATH"] = f"{config.BIN_DIR}{os.pathsep}{env.get('PATH', '')}"
    env["PV3_WORKDIR"] = str(workdir)
    env["PV3_ACTOR"] = actor
    if ledger_path:
        env["PV3_LEDGER"] = str(ledger_path)
    threads = os.environ.get("REVIEW_NUMERIC_THREADS", "1")
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = threads
    # Guard pathological openpyxl read-only cell() loops in agent scripts.
    runtime = config.BASE_DIR / "api" / "reviewer_runtime"
    if runtime.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(runtime), env.get("PYTHONPATH", "")) if p)
        env.setdefault("PAPER_REVIEW_GUARD_READONLY_CELL", "1")
    env.pop("CLAUDECODE", None)
    # The CLI starts its shell snapshot, and every command while it has no
    # snapshot, as a login shell. From the paper-api service domain a login
    # shell stalls ~25 s in /etc/profile (hostnamectl waits for a D-Bus reply
    # that never comes), so the snapshot (10 s limit) fails and every Bash
    # call pays 25 s. A bash wrapper that drops -l avoids the login shell.
    env.setdefault("CLAUDE_CODE_SHELL", str(config.SHELL))
    return env


@dataclass
class SessionSpec:
    name: str
    workdir: Path
    prompt: str
    actor: str
    ledger_path: Path | None = None
    tools: bool = True
    max_chunks: int = config.MAX_CHUNKS_INVESTIGATE
    chunk_turns: int = config.CHUNK_TURNS
    deadline: float | None = None  # time.monotonic() based
    continue_prompt: Callable[[dict], str] | None = None
    restart_prompt: Callable[[dict], str] | None = None
    extra_env: dict = field(default_factory=dict)
    read_dirs: list = field(default_factory=list)
    # Phase time box: after it passes, one short wrap-up chunk records what is
    # already known and the session ends, so later phases keep their time.
    soft_deadline: float | None = None
    wrapup_prompt: Callable[[dict], str] | None = None
    profile: str = "default"  # "judge": final review model


@dataclass
class SessionOutcome:
    status: str  # done | stalled | exhausted | deadline | error
    text: str = ""
    metrics: list = field(default_factory=list)
    error: str = ""


def _state_path(spec: SessionSpec) -> Path:
    path = spec.workdir / "sessions" / f"{spec.name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_state(spec: SessionSpec) -> dict:
    try:
        return json.loads(_state_path(spec).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(spec: SessionSpec, state: dict) -> None:
    path = _state_path(spec)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _progress_key(spec: SessionSpec) -> tuple[int, int, int]:
    if not spec.ledger_path:
        return (0, 0, 0)
    data = ledger.load(spec.ledger_path)
    counts = ledger.counts(data)
    updates = sum(1 for item in (data.get("items") or {}).values() if item.get("updated_at"))
    visual = visual_progress.progress_count(spec.workdir, spec.actor) if "image::visual_pass" in (data.get("items") or {}) else 0
    return counts["closed"], updates, visual


def _all_closed(spec: SessionSpec) -> bool:
    if not spec.ledger_path:
        return False
    counts = ledger.counts(ledger.load(spec.ledger_path))
    return counts["total"] > 0 and counts["open"] == 0


async def _kill(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), 10)
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


class _TranscriptTail:
    """New lines of the session's CLI transcript since the chunk started."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.path: Path | None = None
        self.offset = 0
        self.partial = b""

    def _locate(self) -> Path | None:
        if self.path is None:
            self.path = next(iter(TRANSCRIPTS.glob(f"*/{self.session_id}.jsonl")), None)
        return self.path

    def start(self) -> None:
        """Skip what earlier chunks of a resumed session wrote."""
        path = self._locate()
        try:
            self.offset = path.stat().st_size if path else 0
        except OSError:
            self.offset = 0

    def turn_limit_events(self) -> list[dict]:
        path = self._locate()
        try:
            size = path.stat().st_size if path else 0
        except OSError:
            return []
        if size <= self.offset:
            return []
        with open(path, "rb") as handle:
            handle.seek(self.offset)
            data = self.partial + handle.read(size - self.offset)
        self.offset = size
        lines = data.split(b"\n")
        self.partial = lines.pop()
        out = []
        for line in lines:
            if b"max_turns_reached" not in line:  # tool results can be megabytes of image data
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (event.get("attachment") or {}).get("type") == "max_turns_reached":
                out.append(event["attachment"])
        return out


async def _exit_wait(tail: _TranscriptTail) -> int | None:
    """Return once the CLI has reached its turn limit and still not exited
    EXIT_WAIT_SECONDS later (it is waiting for background jobs). Never
    raises: a failure to read the transcript only disables the watch."""
    reached, turns = None, None
    try:
        while True:
            await asyncio.sleep(config.WATCH_POLL_SECONDS)
            for attachment in await asyncio.to_thread(tail.turn_limit_events):
                reached = reached or time.monotonic()
                turns = attachment.get("turnCount")
            if reached is not None and time.monotonic() - reached >= config.EXIT_WAIT_SECONDS:
                return turns
    except Exception as exc:  # watching must never end a chunk by itself
        log.debug("transcript watch for %s stopped: %s", tail.session_id, exc)
        await asyncio.Event().wait()


async def _invoke(spec: SessionSpec, message: str, session_id: str, resume: bool, turns: int,
                  wrapping: bool = False, debug: bool = False) -> dict:
    model = PROFILES[spec.profile]()[0]
    # The prompt goes through stdin: a single command-line argument is capped
    # at 128 KB on Linux, and writer/judge prompts can exceed it.
    command = [find_cli(), "-p", "--model", model, "--output-format", "json",
               "--max-turns", str(turns), "--disable-slash-commands",
               "--resume" if resume else "--session-id", session_id]
    command += ["--settings", settings_file(spec.profile)]
    if config.EFFORT and spec.profile == "default":
        command += ["--effort", config.EFFORT]
    if debug:
        # The CLI writes its own timeline (hooks, permission, dispatch) to ~/.claude/debug/<session>.txt.
        command.append("--debug")
    if spec.tools:
        command += ["--tools", "Bash,Read", "--allowedTools", "Bash", "Read"]
        for directory in [spec.workdir, *spec.read_dirs]:
            command += ["--add-dir", str(directory)]
    else:
        command += ["--tools", ""]
    now = time.monotonic()
    timeout = config.WRAPUP_SECONDS if wrapping else config.CHUNK_TIMEOUT_SECONDS
    if spec.soft_deadline is not None and not wrapping:
        timeout = min(timeout, max(60.0, spec.soft_deadline + config.SOFT_GRACE_SECONDS - now))
    if spec.deadline is not None:
        timeout = max(1.0, min(timeout, spec.deadline - now))
    started = time.monotonic()
    tail = _TranscriptTail(session_id)
    tail.start()
    # The prompt is in a file before the CLI starts: the CLI waits only 3 s
    # for stdin, and a pipe written from a busy event loop can miss that.
    prompt_file = tempfile.TemporaryFile()
    prompt_file.write(message.encode("utf-8"))
    prompt_file.seek(0)
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=prompt_file, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(runtime_cwd()), env=session_env(spec.workdir, spec.ledger_path, spec.actor, spec.extra_env),
            start_new_session=True)
    finally:
        prompt_file.close()  # the child holds its own descriptor
    talking = asyncio.ensure_future(proc.communicate())
    watching = asyncio.ensure_future(_exit_wait(tail))
    try:
        await asyncio.wait({talking, watching}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        watching.cancel()
        await _kill(proc)
        await asyncio.gather(talking, return_exceptions=True)
        raise
    watching.cancel()
    if not talking.done():
        stuck_at_exit = watching.done() and not watching.cancelled()
        await _kill(proc)
        await asyncio.gather(talking, return_exceptions=True)
        if stuck_at_exit:
            # The turns of this chunk are persisted; ending the wait for
            # background jobs is the same as the CLI exiting on its own.
            log.info("%s: CLI still running %ds after its turn limit (background jobs); ended",
                     spec.name, config.EXIT_WAIT_SECONDS)
            return {"subtype": "error_max_turns", "num_turns": watching.result(), "_exit_killed": True,
                    "_wall": time.monotonic() - started}
        # A long chunk is slow thinking, not a broken endpoint: keep the
        # session (completed turns are persisted) and continue or wrap up.
        return {"_error": "chunk timeout", "_transport": False, "_timeout": True,
                "_wall": time.monotonic() - started}
    stdout, stderr = talking.result()
    raw: dict = {}
    try:
        parsed = json.loads(stdout.decode(errors="replace") or "{}")
        raw = parsed[-1] if isinstance(parsed, list) and parsed else parsed
        if not isinstance(raw, dict):
            raw = {}
    except ValueError:
        raw = {}
    raw["_wall"] = time.monotonic() - started
    raw["_returncode"] = proc.returncode
    err = stderr.decode(errors="replace")[-1500:]
    if not raw.get("subtype"):
        if re.search(r"no (?:conversation|session)|session.*not found|conversation.*not found", err, re.I):
            raw["_error"] = "session not found"
        elif re.search(r"session id .* is already in use", err, re.I):
            # A chunk interrupted by a restart of the parent process left this
            # id registered; the restart prompt starts a fresh session.
            raw["_error"] = "session id in use"
        else:
            raw["_error"] = "malformed CLI output"
            raw["_transport"] = bool(TRANSPORT_RE.search(err)) or proc.returncode != 0
            raw["_detail"] = f"rc={proc.returncode}, stdout {len(stdout)} B, stderr: {err[-300:].strip()}"
    elif raw.get("is_error") and raw.get("subtype") not in ("error_max_turns",):
        detail = json.dumps(raw.get("errors") or raw.get("result") or "", ensure_ascii=False)
        raw["_error"] = f"cli {raw.get('subtype')}"
        raw["_transport"] = bool(TRANSPORT_RE.search(detail + err))
    return raw


async def run(spec: SessionSpec) -> SessionOutcome:
    state = _load_state(spec)
    if state.get("status") in ("done", "stalled", "exhausted", "wrapped", "timeboxed"):
        return SessionOutcome(state["status"], state.get("text", ""), state.get("metrics", []))
    state.setdefault("session_id", str(uuid.uuid4()))
    state.setdefault("chunks", 0)
    state.setdefault("no_progress", 0)
    state.setdefault("metrics", [])
    state.setdefault("started", False)
    outcome_text = state.get("text", "")
    queued_at = time.monotonic()
    async with _gate():
        state["queue_wait_seconds"] = round(time.monotonic() - queued_at, 3)
        while True:
            if spec.ledger_path and _all_closed(spec) and state["chunks"] > 0:
                state["status"] = "done"
                break
            if spec.deadline is not None and time.monotonic() >= spec.deadline:
                state["status"] = "deadline"
                break
            stop = None
            if state["chunks"] >= spec.max_chunks:
                stop = "exhausted"
            elif state["no_progress"] >= config.NO_PROGRESS_CHUNKS:
                stop = "stalled"
            elif spec.soft_deadline is not None and time.monotonic() >= spec.soft_deadline:
                stop = "wrapped" if state["started"] else "timeboxed"
            wrapping = False
            if stop:
                # Before giving up, one short chunk records what the session
                # already verified; otherwise that work is lost.
                if state.get("wrapped") or not spec.wrapup_prompt or not state["started"] or not spec.ledger_path:
                    state["status"] = state.get("stop_reason") or stop
                    break
                state["stop_reason"] = stop
                wrapping = True
            if wrapping:
                message = spec.wrapup_prompt(state)
                resume = not state.get("restart")
                if not resume:
                    message = (spec.restart_prompt or (lambda s: spec.prompt))(state) + "\n\n" + message
                state["wrapped"] = True
            elif not state["started"]:
                message, resume = spec.prompt, False
            elif state.get("restart"):
                message = (spec.restart_prompt or (lambda s: spec.prompt))(state)
                resume = False
            else:
                message = (spec.continue_prompt or (lambda s: "继续完成任务。"))(state)
                resume = True
            before = _progress_key(spec)
            # Persist the spend before launching; a crash must not refund it.
            state.update(started=True, chunks=state["chunks"] + 1, launched_at=time.time(), restart=False)
            _save_state(spec, state)
            raw, attempt = {}, 0
            while True:
                invoked_with_resume = resume
                raw = await _invoke(spec, message, state["session_id"], resume,
                                    config.WRAPUP_TURNS if wrapping else spec.chunk_turns, wrapping,
                                    debug=bool(state.get("debug")))
                if raw.get("_error") == "session id in use" and not resume:
                    state.setdefault("lost_sessions", []).append(state["session_id"])
                    state["session_id"] = str(uuid.uuid4())
                    continue
                if raw.get("_error") == "session not found":
                    state.setdefault("lost_sessions", []).append(state["session_id"])
                    state["session_id"] = str(uuid.uuid4())
                    message = (spec.restart_prompt or (lambda s: spec.prompt))(state)
                    resume = False
                    continue
                if raw.get("_transport") and attempt < config.TRANSPORT_RETRIES and (
                        spec.deadline is None or time.monotonic() < spec.deadline):
                    attempt += 1
                    log.warning("%s transport failure (%s), retry %d; %s", spec.name, raw.get("_error"), attempt,
                                raw.get("_detail", ""))
                    await asyncio.sleep(min(60, config.RETRY_BACKOFF_SECONDS * attempt))
                    # A session that never registered cannot be resumed.
                    if not resume:
                        state["session_id"] = str(uuid.uuid4())
                    continue
                break
            after = _progress_key(spec)
            usage = raw.get("usage") or {}
            metric = {"chunk": state["chunks"], "subtype": raw.get("subtype"), "turns": raw.get("num_turns"),
                      "wall": round(raw.get("_wall", 0), 1), "api_ms": raw.get("duration_api_ms"),
                      "cost": raw.get("total_cost_usd"), "error": raw.get("_error"),
                      "input_tokens": usage.get("input_tokens"), "cache_read": usage.get("cache_read_input_tokens"),
                      "output_tokens": usage.get("output_tokens"), "closed": after[0], "visual_progress": after[2]}
            metric.update(started_at=state["launched_at"], finished_at=time.time(), wrapping=wrapping,
                          resumed=invoked_with_resume, closed_before=before[0], closed_delta=after[0] - before[0],
                          transport_retries=attempt, api_ms_scope="cli_reported_not_per_chunk")
            if spec.ledger_path:
                metric["last_ledger_update_at"] = ledger.load(spec.ledger_path).get("updated_at")
            if raw.get("_exit_killed"):
                metric["exit_killed"] = True
            if spec.tools:
                try:
                    metric.update(tool_waits(state["session_id"], state["launched_at"], spec.workdir, spec.ledger_path))
                except Exception as exc:  # measuring must never fail a chunk
                    log.debug("%s tool wait measurement failed: %s", spec.name, exc)
            state["metrics"].append(metric)
            log.info("%s chunk=%d %s", spec.name, state["chunks"], json.dumps(metric, ensure_ascii=False))
            if (metric.get("tool_wait_s") or 0) > config.TOOL_WAIT_WARN_SECONDS:
                log.warning("%s chunk=%d: tool calls waited %.1f s (median of %d), %s s of it before the "
                            "PreToolUse hook started; commands themselves take under a second",
                            spec.name, state["chunks"], metric["tool_wait_s"], metric["tool_calls"],
                            metric.get("before_hook_s"))
                if not state.get("debug"):
                    # Such stalls have lasted hours and are gone by the time anyone looks:
                    # the session's next chunks record where the CLI spends the time.
                    state["debug"] = True
                    log.warning("%s: next chunks run with CLI debug logging: %s", spec.name,
                                Path.home() / ".claude" / "debug" / f"{state['session_id']}.txt")
            if isinstance(raw.get("result"), str):
                outcome_text = raw["result"]
                state["text"] = outcome_text
            if raw.get("_error") and raw.get("_transport"):
                state["last_error"] = raw["_error"]
                state["status"] = "error"
                _save_state(spec, state)
                return SessionOutcome("error", outcome_text, state["metrics"], raw["_error"])
            if raw.get("_error") and not raw.get("_timeout"):
                # A malformed run with no new session state: next chunk restarts from the ledger.
                state["restart"] = True
            if not spec.ledger_path:
                state["status"] = "done" if not raw.get("_error") else "error"
                break
            # The first chunk is orientation (reading the paper); it may close nothing.
            if after != before:
                state["no_progress"] = 0
            elif state["chunks"] > 1:
                state["no_progress"] += 1
            _save_state(spec, state)
    state["finished_at"] = time.time()
    _save_state(spec, state)
    return SessionOutcome(state["status"], outcome_text, state["metrics"], state.get("last_error", ""))


def reset(spec_name: str, workdir: Path) -> None:
    """Forget a finished session so it can be run again (used by gap filling)."""
    path = workdir / "sessions" / f"{spec_name}.json"
    if path.exists():
        path.rename(path.with_name(f"{spec_name}.{int(time.time())}.done.json"))


def continue_as(source: str, target: str, workdir: Path) -> dict:
    """Hand off a finished session's context once, retaining its spent chunks.

    The caller must have awaited the source. The source audit record remains
    unchanged; the target keeps only its own metrics. Existing targets are
    never reopened here, so process restarts cannot refund recovery budgets.
    Unresponsive/failed sessions start fresh from the caller's ledger handoff.
    """
    target_spec = SessionSpec(target, workdir, "", "calibrator")
    path = _state_path(target_spec)
    if path.exists():
        state = _load_state(target_spec)
        # Older runs have no handoff metadata; retain their existing budget.
        state.setdefault("continuation_mode", "existing")
        state.setdefault("continuation_reason", "接续已保存的补评级进度")
        state.setdefault("continuation_base_chunks", 0)
        _save_state(target_spec, state)
        return state
    source_spec = SessionSpec(source, workdir, "", "calibrator")
    previous = _load_state(source_spec)
    metrics = previous.get("metrics") or []
    before_last_two = metrics[-3].get("closed", 0) if len(metrics) > 2 else 0
    no_progress_timeouts = (len(metrics) >= 2 and
                           all(m.get("error") == "chunk timeout" for m in metrics[-2:]) and
                           metrics[-1].get("closed", 0) <= before_last_two)
    eligible = (previous.get("finished_at") and previous.get("session_id") and
                previous.get("status") in ("done", "wrapped", "exhausted") and
                not previous.get("restart") and not no_progress_timeouts)
    state = {"continuation_source": source, "continuation_mode": "resume" if eligible else "restart",
             "continuation_reason": ("保留前一会话上下文" if eligible else
                                     "前一会话无可用上下文、执行失败或连续超时无进展"),
             "continuation_base_chunks": int(previous.get("chunks", 0)) if eligible else 0,
             "metrics": [], "no_progress": 0, "started": bool(eligible)}
    if eligible:
        state.update(session_id=previous["session_id"], chunks=int(previous.get("chunks", 0)))
    _save_state(target_spec, state)
    return state


def reopen(spec_name: str, workdir: Path) -> int:
    """Let a finished session take new work: it resumes with its context and
    its earlier chunks still count. Returns the chunks used so far."""
    path = workdir / "sessions" / f"{spec_name}.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if state.get("status"):
        for key in ("status", "stop_reason", "wrapped", "finished_at", "last_error"):
            state.pop(key, None)
        state["no_progress"] = 0
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    return int(state.get("chunks", 0))
