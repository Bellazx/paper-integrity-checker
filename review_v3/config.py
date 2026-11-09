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
# Shell the CLI runs tool commands with (see review_v3/bin/bash-nologin).
SHELL = Path(os.environ.get("REVIEW_V3_SHELL") or str(PACKAGE_DIR / "bin" / "bash-nologin"))
SETTINGS_FILE = os.environ.get("REVIEW_V3_SETTINGS_FILE", "/home/library/.claude/settings-mm.json").strip()
# Reasoning effort passed to the CLI for review sessions (low/medium/high/
# xhigh/max). Empty: the CLI's default, which for this model is "high" (the
# CLAUDE_EFFORT environment variable does not change it; checked 2026-11-05).
EFFORT = os.environ.get("REVIEW_V3_EFFORT", "").strip()
# Final review of substantive findings (user decision 2026-10-31): Opus checks
# every issue that would make a paper high-risk; everything else stays on MODEL.
JUDGE_MODEL = os.environ.get("REVIEW_V3_JUDGE_MODEL", "claude-opus-5-5").strip()
JUDGE_SETTINGS_FILE = os.environ.get("REVIEW_V3_JUDGE_SETTINGS_FILE", "/home/library/.claude/settings-cc.json").strip()
JUDGE_ENABLED = os.environ.get("REVIEW_V3_JUDGE", "1") != "0"
MAX_CHUNKS_JUDGE = _int("REVIEW_V3_MAX_CHUNKS_JUDGE", 4)
JUDGE_CHUNK_TURNS = _int("REVIEW_V3_JUDGE_CHUNK_TURNS", 20)
# The judge's own time box from its start: it must open the paper and source
# data for every substantive issue, which takes longer than confirming a record.
JUDGE_SOFT_SECONDS = _int("REVIEW_V3_JUDGE_SOFT_SECONDS", 18 * 60)

# Progress-driven budget. A session runs CHUNK_TURNS turns at a time and is
# extended only while its ledger keeps closing items.
CHUNK_TURNS = _int("REVIEW_V3_CHUNK_TURNS", 12)
MAX_CHUNKS_INVESTIGATE = _int("REVIEW_V3_MAX_CHUNKS", 16)
# Investigation chunks by shard size. A one-item shard took the same three
# chunks (39 turns) as a twelve-item one and wrote its ledger only in the
# last chunk; the session then ends with a wrap-up chunk (WRAPUP_TURNS) that
# records everything verified, so a small budget loses no result. The
# page-by-page visual pass keeps the full budget: its one item covers every
# figure page.
INVESTIGATE_CHUNKS_SMALL = _int("REVIEW_V3_INVESTIGATE_CHUNKS_SMALL", 2)   # <= SMALL_SHARD_ITEMS items
INVESTIGATE_CHUNKS_MEDIUM = _int("REVIEW_V3_INVESTIGATE_CHUNKS_MEDIUM", 3)  # <= MEDIUM_SHARD_ITEMS items
SMALL_SHARD_ITEMS = _int("REVIEW_V3_SMALL_SHARD_ITEMS", 2)
MEDIUM_SHARD_ITEMS = _int("REVIEW_V3_MEDIUM_SHARD_ITEMS", 6)
FULL_BUDGET_SHARD_KINDS = ("image_scan",)


def chunk_budget(items: int, cap: int, kind: str | None = None) -> int:
    """Chunk budget for a session that checks ``items`` ledger entries, under
    the phase cap ``cap``. The same pattern held for challenge, arbitration
    and follow-up sessions on 2026-11-06: one or two items took three chunks
    and the ledger was written in the last one."""
    if kind in FULL_BUDGET_SHARD_KINDS:
        return cap
    if items <= SMALL_SHARD_ITEMS:
        return min(INVESTIGATE_CHUNKS_SMALL, cap)
    if items <= MEDIUM_SHARD_ITEMS:
        return min(INVESTIGATE_CHUNKS_MEDIUM, cap)
    return cap


def complete_chunks(items: int, kind: str | None) -> int:
    """Chunk budget for a follow-up session (see COMPLETE_CHUNKS_IMAGE_SCAN)."""
    if kind in FULL_BUDGET_SHARD_KINDS:
        return min(COMPLETE_CHUNKS_IMAGE_SCAN, MAX_CHUNKS_COMPLETE)
    return chunk_budget(items, MAX_CHUNKS_COMPLETE, kind)


def write_only_after(chunks: int, kind: str | None) -> int | None:
    """Chunks a session may spend reading before its last regular chunk is
    restricted to ledger writes. On 2026-11-08 every follow-up session wrote
    nothing in its reading chunks (two hit the 5-minute cap) and closed every
    item within three minutes once told to write; the same day two data
    arbitrations spent three chunks (14 min) writing nothing and handed the
    same items to a follow-up. The page-by-page visual pass is exempt because
    it writes through pv3 visual update while rendering pages."""
    if kind in FULL_BUDGET_SHARD_KINDS or chunks < 2:
        return None
    return chunks - 1


def complete_write_only_after(chunks: int, kind: str | None) -> int | None:
    """Write-only threshold for a follow-up session (see write_only_after)."""
    return write_only_after(chunks, kind)


def investigate_chunks(shard: dict) -> int:
    """Chunk budget for one investigation shard (see above)."""
    return chunk_budget(len(shard.get("items") or []), MAX_CHUNKS_INVESTIGATE, shard.get("kind"))
MAX_CHUNKS_CHALLENGE = _int("REVIEW_V3_MAX_CHUNKS_CHALLENGE", 10)
MAX_CHUNKS_ARBITRATE = _int("REVIEW_V3_MAX_CHUNKS_ARBITRATE", 6)
MAX_CHUNKS_CALIBRATE = _int("REVIEW_V3_MAX_CHUNKS_CALIBRATE", 4)
MAX_CHUNKS_EVIDENCE = _int("REVIEW_V3_MAX_CHUNKS_EVIDENCE", 3)
# Screenshot sessions running at once for one paper (one session per issue).
CAPTURE_PARALLEL = _int("REVIEW_V3_CAPTURE_PARALLEL", 2)
NO_PROGRESS_CHUNKS = _int("REVIEW_V3_NO_PROGRESS_CHUNKS", 2)
WRITER_TURNS = _int("REVIEW_V3_WRITER_TURNS", 3)
WRITER_REPAIRS = _int("REVIEW_V3_WRITER_REPAIRS", 3)
TRANSPORT_RETRIES = _int("REVIEW_V3_TRANSPORT_RETRIES", 3)
RETRY_BACKOFF_SECONDS = _int("REVIEW_V3_RETRY_BACKOFF_SECONDS", 10)
# One CLI chunk may not exceed this wall time (a hung tool or stalled stream).
CHUNK_TIMEOUT_SECONDS = _int("REVIEW_V3_CHUNK_TIMEOUT_SECONDS", 900)
# After its turn limit the CLI waits for background jobs before exiting; the
# chunk is over at that point, so the process is ended this many seconds later.
EXIT_WAIT_SECONDS = _int("REVIEW_V3_EXIT_WAIT_SECONDS", 20)
WATCH_POLL_SECONDS = float(os.environ.get("REVIEW_V3_WATCH_POLL_SECONDS", "3"))
# One calibrator grades all issues of a paper. With a share below 1 it starts
# once that share of the shards has settled and takes later shards' issues as
# they come. Off by default (2026-11-05 rerun of three papers: 4 min faster on
# average but uneven, calibration cost four times as high).
CALIBRATE_START_FRACTION = float(os.environ.get("REVIEW_V3_CALIBRATE_START_FRACTION", "1.0"))

# Phase time boxes, measured from the start of the review (seconds). When a
# phase's box closes, each running session gets one short wrap-up chunk.
# 2026-11-06, six API papers: sessions ran until the box closed whatever the
# shard held, results were written in the last chunk, and the phase ended at
# the box edge (19/28/38 min). With tool calls no longer stalling (7b43225)
# the same checks fit in smaller boxes; the earlier 14/22/30 values are
# reachable again through the environment.
INVESTIGATE_SOFT_SECONDS = _int("REVIEW_V3_INVESTIGATE_SOFT_SECONDS", 10 * 60)
CHALLENGE_SOFT_SECONDS = _int("REVIEW_V3_CHALLENGE_SOFT_SECONDS", 16 * 60)
ARBITRATE_SOFT_SECONDS = _int("REVIEW_V3_ARBITRATE_SOFT_SECONDS", 24 * 60)
# Follow-up for must-check findings still open after arbitration: small
# batches in parallel, each with its own time box, at most this many rounds.
COMPLETE_BATCH = _int("REVIEW_V3_COMPLETE_BATCH", 6)
COMPLETE_ROUNDS = _int("REVIEW_V3_COMPLETE_ROUNDS", 2)
# Follow-up sessions per round for one paper. Beyond this the remaining items
# are reported as not checked: one paper must not take every session slot.
COMPLETE_MAX_BATCHES = _int("REVIEW_V3_COMPLETE_MAX_BATCHES", 8)
# Entries per list given to the writer; the rest are summarised in one line.
FACTS_LIST_LIMIT = _int("REVIEW_V3_FACTS_LIST_LIMIT", 60)
COMPLETE_SECONDS = _int("REVIEW_V3_COMPLETE_SECONDS", 10 * 60)
MAX_CHUNKS_COMPLETE = _int("REVIEW_V3_MAX_CHUNKS_COMPLETE", 8)
CALIBRATE_SOFT_SECONDS = _int("REVIEW_V3_CALIBRATE_SOFT_SECONDS", 34 * 60)
EVIDENCE_SOFT_SECONDS = _int("REVIEW_V3_EVIDENCE_SOFT_SECONDS", 38 * 60)
# A late phase still gets this much time from its own start when earlier
# phases ran over (the whole-paper deadline remains the hard limit).
PHASE_FLOOR_SECONDS = {"arbitrate": _int("REVIEW_V3_ARBITRATE_FLOOR_SECONDS", 6 * 60),
                       "calibrate": _int("REVIEW_V3_CALIBRATE_FLOOR_SECONDS", 10 * 60),
                       "evidence": _int("REVIEW_V3_EVIDENCE_FLOOR_SECONDS", 4 * 60)}

# When the rules change, finished reviews can be re-graded from their saved
# ledgers (calibration + writing only) instead of being investigated again.
REGRADE = os.environ.get("REVIEW_V3_REGRADE", "0") == "1"
SOFT_GRACE_SECONDS = _int("REVIEW_V3_SOFT_GRACE_SECONDS", 180)
WRAPUP_TURNS = _int("REVIEW_V3_WRAPUP_TURNS", 8)
# A tool call that waits longer than this (median per chunk) is logged with
# where the wait happened: before the PreToolUse hook ran or after it.
TOOL_WAIT_WARN_SECONDS = _int("REVIEW_V3_TOOL_WAIT_WARN_SECONDS", 5)
WRAPUP_SECONDS = _int("REVIEW_V3_WRAPUP_SECONDS", 300)

# Sharding.
DATA_SHARD_SIZE = _int("REVIEW_V3_DATA_SHARD_SIZE", 12)
FIGURE_SHARD_SIZE = _int("REVIEW_V3_FIGURE_SHARD_SIZE", 2)
MAX_SHARDS_PER_DIMENSION = _int("REVIEW_V3_MAX_SHARDS_PER_DIMENSION", 8)

# Concurrency of model sessions within one process (all papers together).
# 48 was an unmeasured first-version default: on 2026-11-06 six concurrent
# API papers hit it and 82 of 213 sessions queued (median 3 min, up to 5),
# while the model endpoint showed no throttling. The host (192 cores, 2 TB,
# ~200 MB per CLI session) is not the limit; the watchdog mails any 503/429
# the endpoint returns, so the cap is set where it does not queue.
MAX_SESSIONS = _int("REVIEW_V3_MAX_SESSIONS", 300)

# Whole-paper wall clock (seconds). 0 disables.
PAPER_TIMEOUT_SECONDS = _int("REVIEW_PAPER_TIMEOUT_SECONDS", 90 * 60)
# Investigation-type phases stop this long before the paper deadline so that
# grading, screenshots and writing always run and a report is published.
FINISH_RESERVE_SECONDS = _int("REVIEW_V3_FINISH_RESERVE_SECONDS", 15 * 60)
GRADING_RESERVE_SECONDS = _int("REVIEW_V3_GRADING_RESERVE_SECONDS", 6 * 60)
# Calibration ends this long before the paper deadline so that the judge
# (median 2 min, see 2026-11-06 runs) gets its turn; three API papers lost
# their judge because calibration ran to the deadline.
JUDGE_RESERVE_SECONDS = _int("REVIEW_V3_JUDGE_RESERVE_SECONDS", 5 * 60)
# Issues per calibration session. One session grading 25-48 issues spent
# its first chunk (8 min) reading and wrote nothing; batches of this size
# run in parallel, each grading its own issues (same dimension together).
CALIBRATE_BATCH = _int("REVIEW_V3_CALIBRATE_BATCH", 12)
# A calibrator's reading chunk ends after this long; the next chunk only
# classifies and writes. 2026-11-07: one 12-issue batch sat 9 min in its first
# chunk and was cut by the judge reserve with nothing written.
CALIBRATE_CHUNK_SECONDS = _int("REVIEW_V3_CALIBRATE_CHUNK_SECONDS", 240)
# A judge chunk may run this long before the next chunk only records
# (81444ed4, 2026-11-07: 13 minutes and 30 tool calls for two issues, nothing written).
JUDGE_CHUNK_SECONDS = _int("REVIEW_V3_JUDGE_CHUNK_SECONDS", 480)
# Follow-up (completion) sessions for the page-by-page visual pass: in a
# follow-up the pass restarts from page one and ran 6-9 chunks per round
# (20 min per paper) while writing only in its wrap-up chunk.
COMPLETE_CHUNKS_IMAGE_SCAN = _int("REVIEW_V3_COMPLETE_CHUNKS_IMAGE_SCAN", 3)
# One arbitration chunk may run this long: without a cap, arbitration chunks
# ran to the phase box (5-9 min each, 2026-11-08) and wrote nothing.
ARBITRATE_CHUNK_SECONDS = _int("REVIEW_V3_ARBITRATE_CHUNK_SECONDS", 240)
# One follow-up chunk may run this long: a follow-up session sat 16 minutes
# in two chunks without writing (2026-11-07, two-item consistency batch);
# with the last regular chunk write-only, 4 minutes of reading per chunk
# covered the 2026-11-08 follow-ups.
COMPLETE_CHUNK_SECONDS = _int("REVIEW_V3_COMPLETE_CHUNK_SECONDS", 240)
# The page-by-page visual pass gets one follow-up round only: a second
# round restarts from page one and took 4-9 more chunks per paper.
COMPLETE_VISUAL_ROUNDS = _int("REVIEW_V3_COMPLETE_VISUAL_ROUNDS", 1)

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
