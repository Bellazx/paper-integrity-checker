#!/usr/bin/env python3
"""Merge user-uploaded supplements from data/input/补充未保障/<doi__>/ into the matching
data/input/4050-matched/<seq>_<doi__>/ dirs.

Mapping: source dir name is the DOI with '__' (e.g. 10.1002__cac2.12073); target is the
4050-matched dir whose name after the 'NNN_' sequence prefix equals that DOI. Verified
1247/1247 map unambiguously. Copies recursively (preserves nested subdirs like 数据集).

Collision policy: if a same-path file already exists with identical size, skip it; otherwise
overwrite (the uploaded file is the intended supplement). Everything logged to
data/output/补充未保障_merge.json.
"""
import os, json, shutil, hashlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
SRC = BASE / "data" / "input" / "补充未保障"
DST = BASE / "data" / "input" / "4050-matched"
LOG = BASE / "data" / "output" / "supp_merge_log.json"


def main():
    dst_by_doi = {}
    for d in os.listdir(DST):
        if (DST / d).is_dir() and "_" in d:
            dst_by_doi[d.split("_", 1)[1]] = d

    results = []
    copied = overwritten = skipped_same = 0
    unmatched = []
    src_dirs = sorted([d for d in os.listdir(SRC) if (SRC / d).is_dir()])

    for s in src_dirs:
        tgt_name = dst_by_doi.get(s)
        if not tgt_name:
            unmatched.append(s)
            results.append({"src": s, "status": "unmatched"})
            continue
        src_dir = SRC / s
        tgt_dir = DST / tgt_name
        n_copy = n_over = n_skip = 0
        for r, _, fs in os.walk(src_dir):
            rel = os.path.relpath(r, src_dir)
            out_dir = tgt_dir if rel == "." else tgt_dir / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            for f in fs:
                src_f = Path(r) / f
                dst_f = out_dir / f
                if dst_f.exists() and dst_f.stat().st_size == src_f.stat().st_size:
                    n_skip += 1; skipped_same += 1
                    continue
                existed = dst_f.exists()
                shutil.copy2(src_f, dst_f)
                if existed: n_over += 1; overwritten += 1
                else: n_copy += 1; copied += 1
        results.append({"src": s, "target": tgt_name,
                        "copied": n_copy, "overwritten": n_over, "skipped_same": n_skip})

    LOG.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 60)
    print(f"源目录: {len(src_dirs)} | 映射成功: {len(src_dirs)-len(unmatched)} | 未匹配: {len(unmatched)}")
    print(f"新增文件: {copied} | 覆盖(内容不同): {overwritten} | 跳过(完全相同): {skipped_same}")
    print(f"日志: {LOG}")
    if unmatched:
        print("未匹配:", unmatched[:10])


if __name__ == "__main__":
    main()
