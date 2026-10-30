"""Paper directory preprocessor.

The 4050-matched corpus is heterogeneous. Before `analyze_paper` reads a paper
dir we inspect and optionally normalize the on-disk layout in five steps.
The public API defaults to dry-run because production screening/review jobs
must never delete or rewrite user-uploaded source material implicitly.

  (0) **Remove OS/editor junk** such as `.DS_Store`, `._*`, `__MACOSX`,
      `Thumbs.db`, `desktop.ini`, and Office lock files. This narrow cleanup is
      safe for uploaded source trees because these files are metadata noise, not
      paper evidence.

  (a) **Decompress** any `.zip` / `.rar` archive found anywhere in the tree;
      extract in place to the archive's parent dir and delete the archive only
      when explicitly called with ``dry_run=False``.
      Retries 3 times (1s/2s backoff) on transient I/O errors.

  (b) **Unnest** the redundant `<paper_id>/<paper_id>/` outer wrapper. Move the
      nested dir's contents up one level so the existing internal structure
      (`extended_data/`, `source_data/`, `article.html`, `doi.txt`, ...) is
      preserved at `paper_dir/`. The internal subdirs are NOT flattened.

  (c) **Replace 0-byte placeholders** for files matching `DATA_FILE_EXTENSIONS`
      by copying a same-basename, larger-than-zero file from anywhere in the
      tree (prefer the largest).

  (d) **Remove byte-identical duplicate files** within the paper directory,
      keeping the best-named/shallowest copy. This avoids repeatedly reading
      identical PDFs, supplements, or source data.

  (e) **Sync `doi.txt`** from the folder name if missing and the name parses
      to a valid `10.NNNN/...` DOI. Never overwrites a non-empty `doi.txt`.

The preprocessor is best-effort. If any step fails, it is logged and the rest
of the pipeline continues (caller wraps it in try/except).
"""

from __future__ import annotations

import logging
import hashlib
import os
import re
import shutil
import sys
import time
from pathlib import Path
from collections.abc import Callable

# Make the repo root importable so modules.* and api.* resolve when this file
# is imported as `utils.paper_preprocessor` from inside the package AND when
# it's imported by the standalone CLI (`python preprocess_4050.py`).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from modules.data_checker import DATA_FILE_EXTENSIONS, is_metadata_data_file  # noqa: E402
from modules.doi_utils import dir_name_to_doi  # noqa: E402

# Lazy import for zip_handler — it pulls in `api.config` which can fail outside
# a FastAPI process. We resolve it on first use.
_zip_handler_module = None


def _get_zip_handler():
    global _zip_handler_module
    if _zip_handler_module is None:
        from api.services import zip_handler  # noqa: F401
        _zip_handler_module = zip_handler
    return _zip_handler_module


log = logging.getLogger(__name__)

_ARCHIVE_EXTS = {".zip", ".rar"}
_UNNEST_TMP_SUFFIX = ".__unnest_tmp"
_VALIDATE_DOI_RE = re.compile(r"^10\.\d{4,}/")
_MAX_EXTRACT_ATTEMPTS = 3
_PROTECTED_INPUT_ROOTS = ((_REPO_ROOT / "data" / "input").resolve(),)
_JUNK_FILE_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
_JUNK_DIR_NAMES = {"__macosx"}


def _is_protected_input_path(path: Path) -> bool:
    """Return True when path is inside a user-uploaded source tree."""
    for root in _PROTECTED_INPUT_ROOTS:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _is_junk(p: Path) -> bool:
    n = p.name
    low = n.lower()
    if low.startswith("._") or low in _JUNK_FILE_NAMES:
        return True
    if n.startswith("~$"):
        return True
    if any(part.lower() in _JUNK_DIR_NAMES for part in p.parts):
        return True
    return False


# ---------------------------------------------------------------------------
# (0) Remove OS/editor junk
# ---------------------------------------------------------------------------

def clean_junk_files(
    paper_dir: str | Path,
    *,
    dry_run: bool = False,
    log_fn: Callable[[str], None] | None = None,
) -> dict:
    """Remove only explicitly whitelisted OS/editor noise files.

    This intentionally does not delete archives, PDFs, spreadsheets, source data,
    or byte-identical duplicate evidence files.
    """
    if log_fn is None:
        log_fn = log.info

    paper_dir = Path(paper_dir).resolve()
    actions = {"junk_files_removed": 0, "junk_dirs_removed": 0}
    touched: list[str] = []
    warnings: list[str] = []
    if not paper_dir.is_dir():
        return {
            "paper_dir": str(paper_dir),
            "actions": actions,
            "touched": [],
            "warnings": [f"not a directory: {paper_dir}"],
            "error": "not a directory",
        }

    for path in sorted(paper_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            rel = path.relative_to(paper_dir)
        except ValueError:
            rel = path
        low_name = path.name.lower()

        try:
            if path.is_file() and (
                low_name.startswith("._")
                or low_name in _JUNK_FILE_NAMES
                or path.name.startswith("~$")
            ):
                touched.append(str(path))
                actions["junk_files_removed"] += 1
                if dry_run:
                    log_fn(f"clean-junk: [dry-run] would remove file {rel}")
                else:
                    path.unlink()
                    log_fn(f"clean-junk: removed file {rel}")
            elif path.is_dir() and low_name in _JUNK_DIR_NAMES:
                touched.append(str(path))
                actions["junk_dirs_removed"] += 1
                if dry_run:
                    log_fn(f"clean-junk: [dry-run] would remove dir {rel}/")
                else:
                    shutil.rmtree(path)
                    log_fn(f"clean-junk: removed dir {rel}/")
        except Exception as e:
            warnings.append(f"failed to remove junk {rel}: {e}")
            log_fn(f"clean-junk: failed to remove {rel}: {e}")

    return {
        "paper_dir": str(paper_dir),
        "actions": actions,
        "touched": sorted(set(touched)),
        "warnings": warnings,
        "error": None,
    }


# ---------------------------------------------------------------------------
# (a) Decompress
# ---------------------------------------------------------------------------

def _decompress(paper_dir: Path, *, dry_run: bool, log_fn: Callable[[str], None]) -> tuple[int, int, list[str]]:
    """Extract every archive in the paper_dir tree in place.

    Returns (ok_count, fail_count, touched_paths).
    """
    ok = 0
    fail = 0
    touched: list[str] = []

    archives = [
        p for p in paper_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in _ARCHIVE_EXTS
        and not _is_junk(p)
    ]

    for archive in archives:
        rel = archive.relative_to(paper_dir)
        zh = _get_zip_handler()

        # Step 1: deterministic validation (no retry — failure is definitive).
        try:
            zh.validate_archive(archive)
        except Exception as e:
            fail += 1
            log_fn(f"preprocess: validate failed for {rel}: {e}")
            continue

        # Step 2: extract with retry. _open_archive + extractall are the
        # operations that can hit transient I/O contention (e.g. another
        # process still writing the archive).
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_EXTRACT_ATTEMPTS + 1):
            try:
                if dry_run:
                    touched.append(str(archive))
                    log_fn(f"preprocess: [dry-run] would extract {rel}")
                    break  # counted as ok below
                with zh._open_archive(archive) as af:
                    af.extractall(archive.parent)
                archive.unlink()
                touched.append(str(archive))
                log_fn(f"preprocess: extracted {rel} (attempt {attempt})")
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                if attempt < _MAX_EXTRACT_ATTEMPTS:
                    backoff = 2 ** (attempt - 1)  # 1s, 2s
                    log_fn(
                        f"preprocess: extract attempt {attempt}/{_MAX_EXTRACT_ATTEMPTS} "
                        f"failed for {rel} ({e}); retrying in {backoff}s"
                    )
                    time.sleep(backoff)
                else:
                    log_fn(
                        f"preprocess: ERROR failed to extract {rel} after "
                        f"{_MAX_EXTRACT_ATTEMPTS} attempts: {last_exc}"
                    )
        else:
            # for-else: loop didn't break — every attempt failed
            fail += 1
            continue

        if last_exc is None:
            ok += 1
        elif dry_run:
            ok += 1  # dry-run never actually tries extractall
        else:
            fail += 1

    return ok, fail, touched


# ---------------------------------------------------------------------------
# (b) Unnest
# ---------------------------------------------------------------------------

def _unnest(paper_dir: Path, *, dry_run: bool, log_fn: Callable[[str], None]) -> tuple[int, int, list[str]]:
    """Collapse `<paper_dir>/<paper_dir.name>/` into `<paper_dir>/`.

    Internal subdirs (extended_data/, source_data/, ...) are preserved exactly.
    Files are merged with the "larger non-zero wins" rule so a 0-byte placeholder
    never overwrites a real file at the parent.

    Returns (removed_count, kept_count, touched_paths).
    """
    nested = paper_dir / paper_dir.name
    if not nested.is_dir() or nested == paper_dir:
        return 0, 0, []

    # First pass: walk the nested tree. For each file, decide whether the
    # nested has strictly larger real data than the parent. If yes, abort.
    abort = False
    unique_paths: list[Path] = []
    for f in nested.rglob("*"):
        if not f.is_file() or _is_junk(f):
            continue
        rel = f.relative_to(nested)
        target = paper_dir / rel
        if not target.exists():
            continue
        try:
            f_size = f.stat().st_size
            t_size = target.stat().st_size
        except OSError:
            continue
        if f_size > t_size and f_size > 0:
            abort = True
            unique_paths.append(f)

    if abort:
        names = ", ".join(str(p.relative_to(paper_dir)) for p in unique_paths[:5])
        log_fn(
            f"preprocess: kept nested dir {nested.relative_to(paper_dir)}/"
            f"{nested.name}/ — has unique non-zero data: {names}"
            + (" ..." if len(unique_paths) > 5 else "")
        )
        return 0, 1, []

    # Safety gate passed. Do an entry-by-entry merge of the nested tree into
    # paper_dir. For each file: skip if target exists and is same-or-larger;
    # else shutil.copy2 over the target. For each subdir: recurse, then
    # remove the now-empty subdir. This avoids the "shutil.move moves dir
    # INTO target dir" trap.
    touched: list[str] = []

    def _merge(src_dir: Path, dst_dir: Path) -> None:
        """Merge src_dir into dst_dir recursively with the larger-wins rule."""
        if not src_dir.is_dir():
            return
        dst_dir.mkdir(parents=True, exist_ok=True)
        for entry in list(src_dir.iterdir()):
            if _is_junk(entry):
                continue
            dest = dst_dir / entry.name
            if entry.is_dir():
                if dest.exists() and not dest.is_dir():
                    # dest is a file but src is a dir — shouldn't happen
                    # in practice; skip with warning.
                    log_fn(
                        f"preprocess: unnest skipping {entry.name} "
                        f"(target is a file, not a dir)"
                    )
                    continue
                _merge(entry, dest)
                # After recursive merge, the source subdir is now empty
                # (or contains only junk); remove it.
                try:
                    if not any(p for p in entry.iterdir() if not _is_junk(p)):
                        shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    pass
                continue

            if entry.is_file():
                if dest.exists():
                    try:
                        s_size = entry.stat().st_size
                        d_size = dest.stat().st_size
                    except OSError:
                        continue
                    if s_size <= d_size:
                        # dest already has same-or-bigger data; drop the
                        # source (delete the redundant 0-byte/smaller copy).
                        if not dry_run:
                            try:
                                entry.unlink()
                            except OSError as e:
                                log_fn(
                                    f"preprocess: failed to unlink redundant "
                                    f"{entry.relative_to(paper_dir)}: {e}"
                                )
                        continue
                    # dest is smaller; this should have tripped the safety
                    # gate above. Defensive: overwrite anyway.
                try:
                    if dry_run:
                        touched.append(str(dest))
                        log_fn(
                            f"preprocess: [dry-run] would merge "
                            f"{entry.relative_to(paper_dir)} -> {dest.relative_to(paper_dir)}"
                        )
                    else:
                        shutil.copy2(entry, dest)
                        try:
                            entry.unlink()
                        except OSError:
                            pass
                        touched.append(str(dest))
                        log_fn(
                            f"preprocess: merged {entry.relative_to(paper_dir)} "
                            f"-> {dest.relative_to(paper_dir)}"
                        )
                except Exception as e:
                    log_fn(
                        f"preprocess: failed to merge {entry}: {e}"
                    )

    if dry_run:
        touched.append(str(nested))
        log_fn(f"preprocess: [dry-run] would unnest {nested.relative_to(paper_dir)}/{nested.name}/")
    else:
        try:
            _merge(nested, paper_dir)
            # After recursive merge, nested should be empty or contain only
            # 0-byte placeholders (the "larger wins" rule skipped them — dest
            # already had real data). Treat those as removable too.
            try:
                has_real = any(
                    p for p in nested.rglob("*")
                    if not _is_junk(p) and p.is_file() and p.stat().st_size > 0
                )
            except OSError:
                has_real = True
            if not has_real:
                shutil.rmtree(nested, ignore_errors=True)
                log_fn(
                    f"preprocess: removed redundant nested dir {nested.relative_to(paper_dir)}/{nested.name}/"
                )
        except Exception as e:
            log_fn(f"preprocess: unnest failed: {e}")
            return 0, 1, touched

    log_fn(
        f"preprocess: unnested {nested.relative_to(paper_dir)}/{nested.name}/ "
        f"({len(touched)} entries merged)"
    )
    return 1, 0, touched


# ---------------------------------------------------------------------------
# (c) Replace 0-byte placeholders
# ---------------------------------------------------------------------------

def _replace_placeholders(paper_dir: Path, *, dry_run: bool, log_fn: Callable[[str], None]) -> tuple[int, int, list[str]]:
    """Find 0-byte data files; replace with a same-basename, non-zero, largest file from anywhere in the tree.

    Returns (replaced_count, remaining_count, touched_paths).
    """
    # First pass: build a basename -> largest real-version mapping.
    real_by_basename: dict[str, Path] = {}
    for p in paper_dir.rglob("*"):
        if not p.is_file() or _is_junk(p):
            continue
        if p.suffix.lower() not in DATA_FILE_EXTENSIONS:
            continue
        if is_metadata_data_file(p):
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz == 0:
            continue
        b = p.name
        cur = real_by_basename.get(b)
        if cur is None or p.stat().st_size > cur.stat().st_size:
            real_by_basename[b] = p

    # Second pass: find 0-byte placeholders and replace.
    replaced = 0
    remaining = 0
    touched: list[str] = []
    for p in paper_dir.rglob("*"):
        if not p.is_file() or _is_junk(p):
            continue
        if p.suffix.lower() not in DATA_FILE_EXTENSIONS:
            continue
        if is_metadata_data_file(p):
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sz > 0:
            continue

        real = real_by_basename.get(p.name)
        if real is None:
            remaining += 1
            log_fn(
                f"preprocess: 0-byte {p.relative_to(paper_dir)} "
                f"has no non-zero counterpart in tree"
            )
            continue

        try:
            if not dry_run:
                shutil.copy2(real, p)
            touched.append(str(p))
            replaced += 1
            log_fn(
                f"preprocess: replaced 0-byte {p.relative_to(paper_dir)} "
                f"with {real.relative_to(paper_dir)}"
            )
        except Exception as e:
            remaining += 1
            log_fn(f"preprocess: failed to replace {p}: {e}")

    return replaced, remaining, touched


# ---------------------------------------------------------------------------
# (d) Remove byte-identical duplicate files
# ---------------------------------------------------------------------------

def _file_digest(path: Path, chunk_size: int = 1024 * 1024) -> str | None:
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _duplicate_keep_score(path: Path, paper_dir: Path) -> tuple[int, int, int, str]:
    """Lower score wins. Prefer canonical/shallow names over copied artifacts."""
    rel = path.relative_to(paper_dir)
    name = path.name.lower()
    rel_s = rel.as_posix().lower()

    bad_terms = (
        "peer_review", "peer-review", "reviewer", "response", "cover_letter",
        "copyright", "license", "metadata", "resources.csv", "figures.csv",
    )
    good_terms = (
        "manuscript", "article", "main", "paper", "source", "data",
        "supplement", "supplementary", "extended",
    )

    semantic = 10
    if any(term in name for term in good_terms):
        semantic -= 4
    if any(term in rel_s for term in bad_terms):
        semantic += 8
    if name == "doi.txt":
        semantic -= 2

    return (semantic, len(rel.parts), len(name), rel_s)


def _deduplicate_identical_files(
    paper_dir: Path,
    *,
    dry_run: bool,
    log_fn: Callable[[str], None],
) -> tuple[int, int, list[str]]:
    """Delete byte-identical duplicate files inside one paper directory.

    Returns (removed_count, duplicate_group_count, touched_paths).
    """
    by_size: dict[int, list[Path]] = {}
    for p in paper_dir.rglob("*"):
        if not p.is_file() or _is_junk(p):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size <= 0:
            continue
        by_size.setdefault(size, []).append(p)

    by_digest: dict[tuple[int, str], list[Path]] = {}
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        for p in paths:
            digest = _file_digest(p)
            if not digest:
                continue
            by_digest.setdefault((size, digest), []).append(p)

    removed = 0
    groups = 0
    touched: list[str] = []
    for (_size, _digest), paths in by_digest.items():
        unique_paths = sorted(set(paths), key=lambda p: p.as_posix())
        if len(unique_paths) < 2:
            continue
        keeper = sorted(unique_paths, key=lambda p: _duplicate_keep_score(p, paper_dir))[0]
        duplicates = [p for p in unique_paths if p != keeper]
        groups += 1
        for dup in duplicates:
            rel_dup = dup.relative_to(paper_dir)
            rel_keep = keeper.relative_to(paper_dir)
            try:
                if dry_run:
                    touched.append(str(dup))
                    removed += 1
                    log_fn(f"preprocess: [dry-run] would remove duplicate {rel_dup} (same as {rel_keep})")
                else:
                    dup.unlink()
                    touched.append(str(dup))
                    removed += 1
                    log_fn(f"preprocess: removed duplicate {rel_dup} (same as {rel_keep})")
            except Exception as e:
                log_fn(f"preprocess: failed to remove duplicate {rel_dup}: {e}")

    return removed, groups, touched


# ---------------------------------------------------------------------------
# (e) Sync doi.txt
# ---------------------------------------------------------------------------

def _sync_doi(paper_dir: Path, *, dry_run: bool, log_fn: Callable[[str], None]) -> tuple[int, list[str]]:
    """If doi.txt is missing and folder name parses to a valid DOI, write it.

    Returns (written_count, touched_paths).
    """
    doi_file = paper_dir / "doi.txt"
    if doi_file.exists():
        try:
            if doi_file.stat().st_size > 0:
                return 0, []  # user/upload-supplied — never overwrite
            # empty file: leave alone
            log_fn(f"preprocess: empty {doi_file.name} left alone (manual fix needed)")
            return 0, []
        except OSError:
            return 0, []

    name = paper_dir.name
    candidate = dir_name_to_doi(name)
    if not _VALIDATE_DOI_RE.match(candidate):
        return 0, []

    try:
        if not dry_run:
            doi_file.write_text(candidate, encoding="utf-8")
        log_fn(f"preprocess: wrote {doi_file.name} = {candidate}")
        return 1, [str(doi_file)]
    except Exception as e:
        log_fn(f"preprocess: failed to write {doi_file}: {e}")
        return 0, []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def preprocess_paper_dir(
    paper_dir: str | Path,
    *,
    dry_run: bool = True,
    log_fn: Callable[[str], None] | None = None,
) -> dict:
    """Run preprocessing steps in order. Best-effort, dry-run by default.

    Args:
        paper_dir: absolute or relative path to a paper directory.
        dry_run: if True, only report what would happen; do not modify the FS.
            Defaults to True so automated screening/review cannot mutate input
            trees unless a caller explicitly opts in.
        log_fn: callable that receives a human-readable status line. Defaults
            to module-level logger.info.

    Returns:
        A stats dict. See module docstring for shape.
    """
    if log_fn is None:
        log_fn = log.info

    paper_dir = Path(paper_dir).resolve()
    if not paper_dir.is_dir():
        return {
            "paper_dir": str(paper_dir),
            "actions": {
                "decompressed": 0, "decompressed_failed": 0,
                "unnested_removed": 0, "unnested_kept": 0,
                "placeholders_replaced": 0, "placeholders_remaining": 0,
                "duplicates_removed": 0, "duplicate_groups": 0,
                "doi_txt_written": 0,
            },
            "touched": [],
            "warnings": [f"not a directory: {paper_dir}"],
            "had_data_files": False,
            "error": "not a directory",
        }

    if not dry_run and _is_protected_input_path(paper_dir):
        raise PermissionError(
            f"refusing destructive preprocessing inside protected input tree: {paper_dir}. "
            "Copy the paper directory to a staging/work directory before applying changes."
        )

    actions = {
        "decompressed": 0, "decompressed_failed": 0,
        "unnested_removed": 0, "unnested_kept": 0,
        "placeholders_replaced": 0, "placeholders_remaining": 0,
        "duplicates_removed": 0, "duplicate_groups": 0,
        "doi_txt_written": 0,
    }
    touched: list[str] = []
    warnings: list[str] = []

    # (a) Decompress first.
    try:
        ok, fail, t = _decompress(paper_dir, dry_run=dry_run, log_fn=log_fn)
        actions["decompressed"] = ok
        actions["decompressed_failed"] = fail
        touched.extend(t)
    except Exception as e:
        warnings.append(f"decompress error: {e}")
        log_fn(f"preprocess: decompress error: {e}")

    # (b) Unnest second.
    try:
        removed, kept, t = _unnest(paper_dir, dry_run=dry_run, log_fn=log_fn)
        actions["unnested_removed"] = removed
        actions["unnested_kept"] = kept
        touched.extend(t)
    except Exception as e:
        warnings.append(f"unnest error: {e}")
        log_fn(f"preprocess: unnest error: {e}")

    # (c) Replace 0-byte placeholders third.
    try:
        replaced, remaining, t = _replace_placeholders(paper_dir, dry_run=dry_run, log_fn=log_fn)
        actions["placeholders_replaced"] = replaced
        actions["placeholders_remaining"] = remaining
        touched.extend(t)
    except Exception as e:
        warnings.append(f"placeholder error: {e}")
        log_fn(f"preprocess: placeholder error: {e}")

    # (d) Remove byte-identical duplicate files fourth.
    try:
        removed, groups, t = _deduplicate_identical_files(paper_dir, dry_run=dry_run, log_fn=log_fn)
        actions["duplicates_removed"] = removed
        actions["duplicate_groups"] = groups
        touched.extend(t)
    except Exception as e:
        warnings.append(f"duplicate removal error: {e}")
        log_fn(f"preprocess: duplicate removal error: {e}")

    # (e) Sync doi.txt last.
    try:
        written, t = _sync_doi(paper_dir, dry_run=dry_run, log_fn=log_fn)
        actions["doi_txt_written"] = written
        touched.extend(t)
    except Exception as e:
        warnings.append(f"doi sync error: {e}")
        log_fn(f"preprocess: doi sync error: {e}")

    # did the tree ever have any data files? (used by callers to know if
    # preprocessor can possibly help — s41551 has none, returns False.)
    had_data = False
    try:
        for p in paper_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() in DATA_FILE_EXTENSIONS and p.stat().st_size > 0:
                had_data = True
                break
    except OSError:
        pass

    if not had_data:
        # The preprocessor can't help a paper whose tree genuinely has no data
        # files. Log a warning so callers know a separate re-fetch is needed.
        # Don't gate on other actions: even if we unnest an empty wrapper or
        # wrote doi.txt, the user still needs the actual data files.
        warnings.append("no data files anywhere in tree (preprocessor cannot help; re-fetch needed)")

    return {
        "paper_dir": str(paper_dir),
        "actions": actions,
        "touched": sorted(set(touched)),
        "warnings": warnings,
        "had_data_files": had_data,
        "error": None,
    }


# Review subagents (and the LLM-driven review pipeline) drop PDF-to-PNG render
# artifacts at the project root as `.tmp_*/.render_*/.batch_*_render` directories,
# `.tmp_*.png`/`.render_*.png`, and manual-review-style loose PNGs such as
# `page_*.png`, `audit_*.png`, `fig*.png`, and `ann_*.png`. These are
# intermediate products that are not part of the deliverable — the final images
# live under `data/output/.../<doi>/images/` and the source PDFs stay in
# `data/input/<date>/<doi>/`. In practice subagents never clean them up, and
# the project root accumulates hundreds of them per overnight run.
#
# The function below is the project's own sweeper. It is intentionally narrow
# (only the named patterns at depth 1) and only removes items older than
# `min_age_seconds` so a review that is *currently* writing its render dir
# is never disturbed. Callers wire it into the end of each review entry point
# (see `batch_rereview.py`, `watch_4050_reviews.py`).

_REVIEW_ARTIFACT_PATTERNS = (".tmp_*", ".render_*", ".batch_*_render")
_REVIEW_LOOSE_PNG_PATTERNS = (
    ".tmp_*.png",
    ".render_*.png",
    "ann_*.png",
    "audit*.png",
    "aud_*.png",
    "cm_*.png",
    "docx_*.png",
    "fig*.png",
    "gr2_*.png",
    "p[0-9]_*.png",
    "page*.png",
    "pair_*.png",
    "paper_p*.png",
    "peer_p*.png",
    "r2_*.png",
    "round8_*.png",
    "splice_*.png",
    "supp_p*.png",
    "table*.png",
)


def cleanup_review_artifacts(
    base_dir: str | os.PathLike | None = None,
    min_age_seconds: int = 3600,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Remove stale review-render artifacts from the project root.

    Scans the top level of `base_dir` (defaults to the project root) for
    known review artifact directories plus narrowly named loose PNG render
    files. Anything older than `min_age_seconds` is removed (directories are
    rmtree'd, files are unlinked). Items newer than the cutoff are left alone
    so a review that is still writing its render dir does not race with the
    sweeper.

    Args:
        base_dir: Project root to scan. Defaults to this file's repo root.
        min_age_seconds: Skip artifacts modified within this many seconds.
            Default 1h — long enough that no in-flight review is at risk.
        dry_run: When True, do not delete; just count what would be removed.

    Returns:
        (dirs_removed, files_removed) tuple. Safe to call repeatedly.
    """
    base = Path(base_dir) if base_dir else _REPO_ROOT
    cutoff = time.time() - max(0, int(min_age_seconds))
    dirs_removed = 0
    files_removed = 0

    seen: set[Path] = set()
    for pattern in _REVIEW_ARTIFACT_PATTERNS + _REVIEW_LOOSE_PNG_PATTERNS:
        for entry in base.glob(pattern):
            if entry in seen:
                continue
            seen.add(entry)
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            try:
                if entry.is_dir() and not entry.is_symlink():
                    if not dry_run:
                        shutil.rmtree(entry, ignore_errors=True)
                    dirs_removed += 1
                elif entry.is_file():
                    if not dry_run:
                        entry.unlink(missing_ok=True)
                    files_removed += 1
            except OSError as e:
                log.warning("cleanup_review_artifacts: failed to remove %s: %s", entry, e)

    return dirs_removed, files_removed


if __name__ == "__main__":
    # Allow ad-hoc usage: python utils/paper_preprocessor.py <paper_dir> [--apply]
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("paper_dir")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = preprocess_paper_dir(args.paper_dir, dry_run=not args.apply)
    import json as _json
    print(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
