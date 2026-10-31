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
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from review_v3 import config, ledger

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


def _progress_key(spec: SessionSpec) -> tuple[int, int]:
    if not spec.ledger_path:
        return (0, 0)
    data = ledger.load(spec.ledger_path)
    counts = ledger.counts(data)
    updates = sum(1 for item in (data.get("items") or {}).values() if item.get("updated_at"))
    return counts["closed"], updates


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


async def _invoke(spec: SessionSpec, message: str, session_id: str, resume: bool, turns: int,
                  wrapping: bool = False) -> dict:
    model = PROFILES[spec.profile]()[0]
    command = [find_cli(), "-p", message, "--model", model, "--output-format", "json",
               "--max-turns", str(turns), "--disable-slash-commands",
               "--resume" if resume else "--session-id", session_id]
    command += ["--settings", settings_file(spec.profile)]
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
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(runtime_cwd()), env=session_env(spec.workdir, spec.ledger_path, spec.actor, spec.extra_env),
        start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        # A long chunk is slow thinking, not a broken endpoint: keep the
        # session (completed turns are persisted) and continue or wrap up.
        await _kill(proc)
        return {"_error": "chunk timeout", "_transport": False, "_timeout": True,
                "_wall": time.monotonic() - started}
    except asyncio.CancelledError:
        await _kill(proc)
        raise
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
        else:
            raw["_error"] = "malformed CLI output"
            raw["_transport"] = bool(TRANSPORT_RE.search(err)) or proc.returncode != 0
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
    async with _gate():
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
                raw = await _invoke(spec, message, state["session_id"], resume,
                                    config.WRAPUP_TURNS if wrapping else spec.chunk_turns, wrapping)
                if raw.get("_error") == "session not found":
                    state.setdefault("lost_sessions", []).append(state["session_id"])
                    state["session_id"] = str(uuid.uuid4())
                    message = (spec.restart_prompt or (lambda s: spec.prompt))(state)
                    resume = False
                    continue
                if raw.get("_transport") and attempt < config.TRANSPORT_RETRIES and (
                        spec.deadline is None or time.monotonic() < spec.deadline):
                    attempt += 1
                    log.warning("%s transport failure (%s), retry %d", spec.name, raw.get("_error"), attempt)
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
                      "output_tokens": usage.get("output_tokens"), "closed": after[0]}
            state["metrics"].append(metric)
            log.info("%s chunk=%d %s", spec.name, state["chunks"], json.dumps(metric, ensure_ascii=False))
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
