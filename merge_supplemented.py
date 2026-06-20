#!/usr/bin/env python3
"""Merge 4050-matched-supplemented/ into 4050-matched/ and purge crawler pollution.

Rules:
  - supplemented subdir 'NNN_<doi>' -> same DOI 4050-matched subdir.
    Prefer an exact same-name match when present, otherwise resolve by normalized DOI.
  - Pollution files NOT carried over, and also deleted from 4050-matched itself:
    manifest.json, resources.csv, figures.csv, AppleDouble ._*, .DS_Store.
  - Non-pollution file in both: keep the LARGER one.
  - Top-level crawler logs (_log_*.jsonl, _summary.csv, _leftover.csv) ignored.
"""
import os, shutil, json
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from modules.doi_utils import dir_name_to_doi, normalize_doi

S = BASE / "data" / "input" / "4050-matched-supplemented"
B = BASE / "data" / "input" / "4050-matched"
LOG = BASE / "data" / "output" / "merge_supplemented_log.json"

POLLUTION_NAMES = {"manifest.json", "resources.csv", "figures.csv", ".ds_store"}


def is_pollution(fname):
    n = fname.lower()
    return n in POLLUTION_NAMES or n.startswith("._")


def doi_key(path_or_name):
    return normalize_doi(dir_name_to_doi(path_or_name)).lower()


def build_target_index(target_root):
    index = defaultdict(list)
    for child in target_root.iterdir():
        if not child.is_dir():
            continue
        key = doi_key(child)
        if key:
            index[key].append(child)
    return index


def resolve_target(source_dir, target_root, target_index):
    exact = target_root / source_dir.name
    key = doi_key(source_dir)
    if exact.is_dir():
        return exact, key, None
    if not key:
        return None, key, "bad_source_doi"

    matches = target_index.get(key, [])
    if len(matches) == 1:
        return matches[0], key, None
    if not matches:
        return None, key, "no_target"
    return None, key, "ambiguous_target"


def main():
    copied = bigger = kept = skipped = 0
    errors = []
    target_index = build_target_index(B)
    for d in sorted(os.listdir(S)):
        sp = S / d
        if not sp.is_dir():
            continue
        tgt, key, status = resolve_target(sp, B, target_index)
        if status:
            errors.append({"dir": d, "doi": key, "status": status})
            continue
        for r, dirs, fs in os.walk(sp):
            rel = os.path.relpath(r, sp)
            for f in fs:
                if is_pollution(f):
                    skipped += 1
                    continue
                src_f = Path(r) / f
                out_dir = tgt if rel == "." else tgt / rel
                dst_f = out_dir / f
                try:
                    if not dst_f.exists():
                        out_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_f, dst_f)
                        copied += 1
                    elif src_f.stat().st_size > dst_f.stat().st_size:
                        shutil.copy2(src_f, dst_f)
                        bigger += 1
                    else:
                        kept += 1
                except Exception as e:
                    errors.append({"dir": d, "file": f, "error": str(e)[:60]})

    # Purge pollution from the merged 4050-matched (including pre-existing).
    purged = 0
    for r, dirs, fs in os.walk(B):
        if "__macosx" in r.lower():
            continue
        for f in fs:
            if is_pollution(f):
                try:
                    os.remove(os.path.join(r, f))
                    purged += 1
                except Exception as e:
                    errors.append({"file": os.path.join(r, f), "error": str(e)[:60]})
    # remove __MACOSX dirs too
    for r, dirs, fs in os.walk(B, topdown=False):
        for dd in dirs:
            if dd == "__MACOSX":
                shutil.rmtree(os.path.join(r, dd), ignore_errors=True)

    summary = {"copied_new": copied, "overwritten_bigger": bigger,
               "kept_existing_larger": kept, "pollution_skipped_in_merge": skipped,
               "pollution_purged_from_4050": purged, "errors": errors[:50],
               "error_count": len(errors)}
    LOG.write_text(json.dumps(summary,
                                  ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 56)
    print("合并完成:")
    print(f"  新增文件: {copied}")
    print(f"  用较大版本覆盖: {bigger}")
    print(f"  保留原较大文件: {kept}")
    print(f"  合并时跳过的污染: {skipped}")
    print(f"  从4050-matched清除的污染(含原有): {purged}")
    print(f"  错误: {len(errors)}")
    print(f"  日志: {LOG}")


if __name__ == "__main__":
    main()
