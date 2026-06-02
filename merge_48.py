#!/usr/bin/env python3
"""Merge the user-uploaded '48' folder into 4050-matched.

Subdir names already carry the 'NNN_<doi__>' prefix matching 4050-matched exactly, so
each source subdir maps to the same-named target dir (verified 46/46). Skips macOS junk
(.DS_Store, AppleDouble ._* files, __MACOSX dirs). Same-size existing files are skipped,
others overwritten. Log -> data/output/merge_48_log.json
"""
import os, json, shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "data" / "input" / "48"
DST = BASE / "data" / "input" / "4050-matched"
LOG = BASE / "data" / "output" / "merge_48_log.json"


def is_junk(name: str) -> bool:
    return name == ".DS_Store" or name.startswith("._")


def main():
    dst_names = set(os.listdir(DST))
    results = []
    copied = overwritten = skipped_same = junk = unmatched = 0

    for d in sorted(os.listdir(SRC)):
        sp = SRC / d
        if not sp.is_dir():
            continue
        if d not in dst_names:
            unmatched += 1
            results.append({"src": d, "status": "unmatched"})
            continue
        tgt = DST / d
        n_copy = n_over = n_skip = n_junk = 0
        for r, dirs, fs in os.walk(sp):
            if "__MACOSX" in r:
                n_junk += len(fs); continue
            dirs[:] = [x for x in dirs if x != "__MACOSX"]
            rel = os.path.relpath(r, sp)
            out_dir = tgt if rel == "." else tgt / rel
            for f in fs:
                if is_junk(f):
                    n_junk += 1; continue
                out_dir.mkdir(parents=True, exist_ok=True)
                src_f = Path(r) / f
                dst_f = out_dir / f
                if dst_f.exists() and dst_f.stat().st_size == src_f.stat().st_size:
                    n_skip += 1; continue
                existed = dst_f.exists()
                shutil.copy2(src_f, dst_f)
                if existed: n_over += 1
                else: n_copy += 1
        copied += n_copy; overwritten += n_over; skipped_same += n_skip; junk += n_junk
        results.append({"src": d, "copied": n_copy, "overwritten": n_over,
                        "skipped_same": n_skip, "junk_skipped": n_junk})

    LOG.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 60)
    print(f"源子目录: {sum(1 for d in os.listdir(SRC) if (SRC/d).is_dir())} | 未匹配: {unmatched}")
    print(f"新增文件: {copied} | 覆盖: {overwritten} | 跳过(相同): {skipped_same} | 跳过macOS垃圾: {junk}")
    print(f"日志: {LOG}")


if __name__ == "__main__":
    main()
