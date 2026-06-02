#!/usr/bin/env python3
"""Distribute supplementary data from zip extract to paper directories."""
import json
import re
import shutil
from pathlib import Path

SRC_BASE = Path("/tmp/zhangwanbin_extract/zhangwanbin_papers")
TARGET_BASE = Path("/opt/paper-integrity-checker/data/input/zhangwanbin")
KB_BASE = Path("/opt/knowledge-base/data/paper-integrity/input/zhangwanbin")

SRC_DATA_DIR = SRC_BASE / "source_data"
SRC_PDF_DIR = SRC_BASE / "pdfs"


def _build_dir_index():
    index = {}
    for d in TARGET_BASE.iterdir():
        if d.is_dir():
            index[d.name.lower()] = d
    for d in KB_BASE.iterdir():
        if d.is_dir():
            key = d.name.lower()
            if key not in index:
                index[key] = d
    return index


def _find_target_dir(doi: str, dir_index: dict) -> Path | None:
    candidates = [
        doi.replace("/", "_"),
        doi.replace("/", "__"),
    ]
    for c in candidates:
        if c.lower() in dir_index:
            return dir_index[c.lower()]
    return None


def _load_source_map():
    p = SRC_BASE / "source_data_all_publishers.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return []


def _load_all_papers():
    p = SRC_BASE / "all_papers.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return []


def _normalize_title(t: str) -> str:
    return re.sub(r'[^a-z0-9]', '', t.lower())


def distribute_source_data():
    source_map = _load_source_map()
    all_papers = _load_all_papers()
    dir_index = _build_dir_index()

    doi_title_map = {}
    for item in source_map:
        doi = item.get("doi", "")
        title = item.get("title", "")
        if doi and title:
            doi_title_map[_normalize_title(title[:50])] = doi

    for p in all_papers:
        doi = p.get("doi", "")
        title = p.get("title", "")
        if doi and title:
            doi_title_map[_normalize_title(title[:50])] = doi

    if not SRC_DATA_DIR.exists():
        print("No source_data directory")
        return 0

    copied = 0
    skipped = 0
    not_matched = []

    for sf in sorted(SRC_DATA_DIR.iterdir()):
        if sf.name.startswith("."):
            continue

        fname = sf.name
        parts = fname.split("_", 1)
        if len(parts) < 2:
            not_matched.append(fname)
            continue

        year_str = parts[0]
        rest = parts[1]
        title_part = rest.rsplit("_ESI", 1)[0].rsplit("_MOESM", 1)[0]
        title_norm = _normalize_title(title_part[:50])

        matched_doi = None
        for key, doi in doi_title_map.items():
            if title_norm[:30] in key or key[:30] in title_norm:
                matched_doi = doi
                break

        if not matched_doi:
            not_matched.append(fname)
            continue

        target_dir = _find_target_dir(matched_doi, dir_index)
        if not target_dir:
            not_matched.append(f"{fname} (DOI={matched_doi}, no dir)")
            continue

        supp_dir = target_dir / "supplementary"
        supp_dir.mkdir(exist_ok=True)
        dest = supp_dir / sf.name
        if not dest.exists():
            shutil.copy2(str(sf), str(dest))
            copied += 1
            print(f"  COPIED: {sf.name} -> {target_dir.name}/supplementary/")
        else:
            skipped += 1

    print(f"\nSource data: copied={copied}, skipped(exists)={skipped}, unmatched={len(not_matched)}")
    if not_matched:
        print("  Unmatched files:")
        for f in not_matched:
            print(f"    {f}")
    return copied


def distribute_pdfs():
    all_papers = _load_all_papers()
    dir_index = _build_dir_index()

    title_doi_map = {}
    for p in all_papers:
        doi = p.get("doi", "")
        title = p.get("title", "")
        year = p.get("year", "")
        if doi and title:
            title_doi_map[_normalize_title(title[:60])] = {"doi": doi, "year": year}

    if not SRC_PDF_DIR.exists():
        print("No pdfs directory")
        return 0

    copied = 0
    skipped_exists = 0
    not_matched = []

    for pf in sorted(SRC_PDF_DIR.iterdir()):
        if not pf.name.endswith(".pdf") or pf.name.startswith("."):
            continue

        fname = pf.name
        parts = fname.split("_", 1)
        if len(parts) < 2:
            not_matched.append(fname)
            continue

        title_part = parts[1].rsplit(".pdf", 1)[0]
        title_norm = _normalize_title(title_part[:60])

        matched_doi = None
        for key, info in title_doi_map.items():
            if len(title_norm) >= 20 and (title_norm[:25] in key or key[:25] in title_norm):
                matched_doi = info["doi"]
                break

        if not matched_doi:
            not_matched.append(fname)
            continue

        target_dir = _find_target_dir(matched_doi, dir_index)
        if not target_dir:
            not_matched.append(f"{fname} (DOI={matched_doi}, no dir)")
            continue

        existing_pdfs = list(target_dir.glob("*.pdf"))
        if existing_pdfs:
            skipped_exists += 1
            continue

        dest = target_dir / pf.name
        if not dest.exists():
            shutil.copy2(str(pf), str(dest))
            copied += 1
            print(f"  COPIED PDF: {pf.name} -> {target_dir.name}/")

    print(f"\nPDFs: copied={copied}, skipped(already has pdf)={skipped_exists}, unmatched={len(not_matched)}")
    if not_matched and len(not_matched) <= 20:
        print("  Unmatched PDFs:")
        for f in not_matched[:20]:
            print(f"    {f}")
    elif not_matched:
        print(f"  ({len(not_matched)} unmatched PDFs)")
    return copied


def main():
    print("=" * 60)
    print("Distributing supplementary data to paper directories")
    print("=" * 60)

    print("\n--- Source Data Files ---")
    src_count = distribute_source_data()

    print("\n--- PDF Files ---")
    pdf_count = distribute_pdfs()

    print(f"\n{'='*60}")
    print(f"Done. Source data copied: {src_count}, PDFs copied: {pdf_count}")


if __name__ == "__main__":
    main()
