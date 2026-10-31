"""Runtime settings for review engine v3. Every value can be overridden by env."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("PAPER_INTEGRITY_ROOT", str(PACKAGE_DIR.parent)))
RULES_DIR = PACKAGE_DIR / "rules"
BIN_DIR = PACKAGE_DIR / "bin"
PYTHON = os.environ.get("PAPER_CHECKER_PYTHON", str(BASE_DIR / "venv" / "bin" / "python"))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# Model: one configuration for API and batch.
MODEL = os.environ.get("REVIEW_V3_MODEL", "MiniMax-M3.1-Flash").strip()
SETTINGS_FILE = os.environ.get("REVIEW_V3_SETTINGS_FILE", "/home/library/.claude/settings-mm.json").strip()
# Final review of substantive findings (user decision 2026-10-31): Opus checks
# every issue that would make a paper high-risk; everything else stays on MODEL.
JUDGE_MODEL = os.environ.get("REVIEW_V3_JUDGE_MODEL", "claude-opus-5-5").strip()
JUDGE_SETTINGS_FILE = os.environ.get("REVIEW_V3_JUDGE_SETTINGS_FILE", "/home/library/.claude/settings-cc.json").strip()
JUDGE_ENABLED = os.environ.get("REVIEW_V3_JUDGE", "1") != "0"
MAX_CHUNKS_JUDGE = _int("REVIEW_V3_MAX_CHUNKS_JUDGE", 4)
JUDGE_CHUNK_TURNS = _int("REVIEW_V3_JUDGE_CHUNK_TURNS", 20)

# Progress-driven budget. A session runs CHUNK_TURNS turns at a time and is
# extended only while its ledger keeps closing items.
CHUNK_TURNS = _int("REVIEW_V3_CHUNK_TURNS", 12)
MAX_CHUNKS_INVESTIGATE = _int("REVIEW_V3_MAX_CHUNKS", 16)
MAX_CHUNKS_CHALLENGE = _int("REVIEW_V3_MAX_CHUNKS_CHALLENGE", 10)
MAX_CHUNKS_ARBITRATE = _int("REVIEW_V3_MAX_CHUNKS_ARBITRATE", 6)
MAX_CHUNKS_CALIBRATE = _int("REVIEW_V3_MAX_CHUNKS_CALIBRATE", 4)
MAX_CHUNKS_EVIDENCE = _int("REVIEW_V3_MAX_CHUNKS_EVIDENCE", 3)
NO_PROGRESS_CHUNKS = _int("REVIEW_V3_NO_PROGRESS_CHUNKS", 2)
WRITER_TURNS = _int("REVIEW_V3_WRITER_TURNS", 3)
WRITER_REPAIRS = _int("REVIEW_V3_WRITER_REPAIRS", 3)
TRANSPORT_RETRIES = _int("REVIEW_V3_TRANSPORT_RETRIES", 3)
RETRY_BACKOFF_SECONDS = _int("REVIEW_V3_RETRY_BACKOFF_SECONDS", 10)
# One CLI chunk may not exceed this wall time (a hung tool or stalled stream).
CHUNK_TIMEOUT_SECONDS = _int("REVIEW_V3_CHUNK_TIMEOUT_SECONDS", 900)

# Phase time boxes, measured from the start of the review (seconds). When a
# phase's box closes, each running session gets one short wrap-up chunk.
INVESTIGATE_SOFT_SECONDS = _int("REVIEW_V3_INVESTIGATE_SOFT_SECONDS", 20 * 60)
CHALLENGE_SOFT_SECONDS = _int("REVIEW_V3_CHALLENGE_SOFT_SECONDS", 32 * 60)
ARBITRATE_SOFT_SECONDS = _int("REVIEW_V3_ARBITRATE_SOFT_SECONDS", 42 * 60)
# Follow-up for must-check findings still open after arbitration: small
# batches in parallel, each with its own time box, at most this many rounds.
COMPLETE_BATCH = _int("REVIEW_V3_COMPLETE_BATCH", 6)
COMPLETE_ROUNDS = _int("REVIEW_V3_COMPLETE_ROUNDS", 2)
COMPLETE_SECONDS = _int("REVIEW_V3_COMPLETE_SECONDS", 15 * 60)
MAX_CHUNKS_COMPLETE = _int("REVIEW_V3_MAX_CHUNKS_COMPLETE", 8)
CALIBRATE_SOFT_SECONDS = _int("REVIEW_V3_CALIBRATE_SOFT_SECONDS", 46 * 60)
EVIDENCE_SOFT_SECONDS = _int("REVIEW_V3_EVIDENCE_SOFT_SECONDS", 50 * 60)
# A late phase still gets this much time from its own start when earlier
# phases ran over (the whole-paper deadline remains the hard limit).
PHASE_FLOOR_SECONDS = {"arbitrate": _int("REVIEW_V3_ARBITRATE_FLOOR_SECONDS", 8 * 60),
                       "calibrate": _int("REVIEW_V3_CALIBRATE_FLOOR_SECONDS", 5 * 60),
                       "evidence": _int("REVIEW_V3_EVIDENCE_FLOOR_SECONDS", 4 * 60)}

# When the rules change, finished reviews can be re-graded from their saved
# ledgers (calibration + writing only) instead of being investigated again.
REGRADE = os.environ.get("REVIEW_V3_REGRADE", "0") == "1"
SOFT_GRACE_SECONDS = _int("REVIEW_V3_SOFT_GRACE_SECONDS", 180)
WRAPUP_TURNS = _int("REVIEW_V3_WRAPUP_TURNS", 8)
WRAPUP_SECONDS = _int("REVIEW_V3_WRAPUP_SECONDS", 300)

# Sharding.
DATA_SHARD_SIZE = _int("REVIEW_V3_DATA_SHARD_SIZE", 12)
FIGURE_SHARD_SIZE = _int("REVIEW_V3_FIGURE_SHARD_SIZE", 2)
MAX_SHARDS_PER_DIMENSION = _int("REVIEW_V3_MAX_SHARDS_PER_DIMENSION", 5)

# Concurrency of model sessions within one process (all papers together).
MAX_SESSIONS = _int("REVIEW_V3_MAX_SESSIONS", 48)

# Whole-paper wall clock (seconds). 0 disables.
PAPER_TIMEOUT_SECONDS = _int("REVIEW_PAPER_TIMEOUT_SECONDS", 90 * 60)
# Investigation-type phases stop this long before the paper deadline so that
# grading, screenshots and writing always run and a report is published.
FINISH_RESERVE_SECONDS = _int("REVIEW_V3_FINISH_RESERVE_SECONDS", 15 * 60)
GRADING_RESERVE_SECONDS = _int("REVIEW_V3_GRADING_RESERVE_SECONDS", 6 * 60)

# Memory retrieval.
MEMORY_DIR = Path(os.environ.get(
    "REVIEW_V3_MEMORY_DIR",
    "/home/library/.claude/projects/-home-sjtu-paper-integrity-checker/memory",
))
MEMORY_HINTS_PER_SHARD = _int("REVIEW_V3_MEMORY_HINTS", 6)
MEMORY_HINT_CHARS = _int("REVIEW_V3_MEMORY_HINT_CHARS", 3200)
MEMORY_INBOX = Path(os.environ.get("REVIEW_V3_MEMORY_INBOX", str(BASE_DIR / "data" / "review_v3_memory_inbox")))

# Dossier rendering.
RENDER_DPI = _int("REVIEW_V3_RENDER_DPI", 110)
RENDER_MAX_SIDE = _int("REVIEW_V3_RENDER_MAX_SIDE", 1600)
TOOL_OUTPUT_CHARS = _int("REVIEW_V3_TOOL_OUTPUT_CHARS", 6000)

# Shared checker / validation (unchanged publication contract).
SKILL_SCRIPTS = Path(os.environ.get(
    "PAPER_REVIEW_SKILL_SCRIPTS", "/home/library/.claude/skills/paper-batch-review/scripts"))
