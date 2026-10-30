"""Summarize where review v3 sessions spent their turns and time.

    venv/bin/python -m review_v3.tools.analyze_run <run_dir> [<run_dir> ...]
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SESSIONS = Path.home() / ".claude" / "projects"


def _transcript(session_id: str) -> Path | None:
    for path in SESSIONS.glob(f"*/{session_id}.jsonl"):
        return path
    return None


def _ts(event: dict) -> float | None:
    value = event.get("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def classify(name: str, payload: dict) -> str:
    if name == "Read":
        return "Read:image" if re.search(r"\.(png|jpe?g)$", str(payload.get("file_path", ""))) else "Read:text"
    command = str(payload.get("command", ""))
    match = re.match(r"\s*(?:cd [^&;]+&&\s*)?pv3\s+(\w+)", command)
    if match:
        return f"pv3 {match.group(1)}"
    if "pv3 ledger" in command:
        return "pv3 ledger"
    if re.search(r"python3?|<<", command):
        return "script"
    return "shell:" + (command.split()[0] if command.split() else "?")


def summarize(session_id: str) -> dict:
    path = _transcript(session_id)
    if not path:
        return {}
    kinds, blocked, big = Counter(), 0, 0
    model, tools, last_user = 0.0, 0.0, None
    pending, turns, max_context = {}, 0, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        stamp = _ts(event)
        message = event.get("message") or {}
        if event.get("type") == "assistant":
            turns += 1
            usage = message.get("usage") or {}
            max_context = max(max_context, sum(usage.get(k) or 0 for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
            if last_user and stamp:
                model += stamp - last_user
                last_user = None
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    kinds[classify(block["name"], block.get("input") or {})] += 1
                    pending[block["id"]] = stamp
        elif event.get("type") == "user":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    start = pending.pop(block.get("tool_use_id"), None)
                    if start and stamp:
                        tools += stamp - start
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    blocked += "hook error" in text or "PreToolUse" in text
                    big += len(text) > 20000
            last_user = stamp
    return {"turns": turns, "model_s": round(model), "tool_s": round(tools), "max_context": max_context,
            "blocked": blocked, "big_outputs": big, "tools": dict(kinds.most_common(8))}


def main() -> None:
    for run_dir in map(Path, sys.argv[1:]):
        print(f"== {run_dir}")
        for state_path in sorted((run_dir / "sessions").glob("*.json")):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            ids = [state.get("session_id")] + list(state.get("lost_sessions") or [])
            for session_id in filter(None, ids):
                info = summarize(session_id)
                if info:
                    print(f"{state_path.stem:22s} chunks={state.get('chunks')} status={state.get('status')} "
                          f"{json.dumps(info, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
