from __future__ import annotations

import logging
import shutil
import zipfile
from pathlib import Path

import rarfile

from api.config import (
    MAX_UPLOAD_SIZE_BYTES, RUNTIME_TMP_DIR,
    input_task_root, output_task_root,
)
from api.models import PaperInfo

log = logging.getLogger(__name__)


def _open_archive(archive_path: Path):
    ext = archive_path.suffix.lower()
    if ext == ".rar":
        return rarfile.RarFile(str(archive_path))
    return zipfile.ZipFile(archive_path)


def validate_archive(archive_path: Path) -> None:
    if archive_path.stat().st_size > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError(f"Archive exceeds maximum size ({archive_path.stat().st_size / 1024 / 1024:.0f}MB)")
    ext = archive_path.suffix.lower()
    if ext == ".rar":
        if not rarfile.is_rarfile(str(archive_path)):
            raise ValueError("Uploaded file is not a valid RAR archive")
    else:
        if not zipfile.is_zipfile(archive_path):
            raise ValueError("Uploaded file is not a valid ZIP archive")
    with _open_archive(archive_path) as af:
        for name in af.namelist():
            if ".." in name or name.startswith("/"):
                raise ValueError(f"Archive contains unsafe path: {name}")


def _read_doi_from_pdf(pdf_path: Path) -> str:
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
        metadata = doc.metadata or {}
        doc.close()
        doi = metadata.get("subject", "") or metadata.get("keywords", "")
        if "10." in doi:
            import re
            m = re.search(r'(10\.\d{4,}/\S+)', doi)
            if m:
                return m.group(1).rstrip(".")
    except Exception:
        pass
    return ""


def _read_doi_from_file(paper_dir: Path) -> str:
    doi_file = paper_dir / "doi.txt"
    if doi_file.exists():
        return doi_file.read_text(encoding="utf-8").strip()
    return ""


def _unique_dir(root: Path, desired_name: str, used: set[str] | None = None) -> Path:
    used = used if used is not None else set()
    base = desired_name.strip() or "paper"
    candidate = base
    index = 2
    while candidate in used or (root / candidate).exists():
        candidate = f"{base}__{index}"
        index += 1
    used.add(candidate)
    return root / candidate


def apply_doi_override(paper: PaperInfo, doi: str) -> PaperInfo:
    """Make a user-supplied DOI authoritative across paths and filenames.

    main.py names the output folder after the input folder basename, while review report
    lookup later searches by DOI slug. Rename the extracted input directory to the DOI
    slug and write doi.txt so every downstream step agrees on the same identifier.
    """
    doi = (doi or "").strip()
    if not doi:
        return paper

    doi_slug = doi.replace("/", "_")
    src = Path(paper.input_dir)
    target = src.parent / doi_slug
    if src.exists() and src.resolve() != target.resolve():
        if target.exists():
            raise ValueError(f"DOI override target already exists: {target}")
        shutil.move(str(src), str(target))
        paper.input_dir = str(target)
    elif not src.exists():
        target.mkdir(parents=True, exist_ok=True)
        paper.input_dir = str(target)

    Path(paper.input_dir, "doi.txt").write_text(doi, encoding="utf-8")
    paper.doi = doi
    paper.doi_slug = doi_slug
    paper.output_dir = str(Path(paper.output_dir).parent / doi_slug)
    return paper


def extract_single(archive_path: Path, task_id: str) -> list[PaperInfo]:
    validate_archive(archive_path)
    extract_dir = RUNTIME_TMP_DIR / f"paper_extract_{task_id}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with _open_archive(archive_path) as af:
        af.extractall(extract_dir)

    pdfs = list(extract_dir.rglob("*.pdf")) + list(extract_dir.rglob("*.PDF"))
    if not pdfs:
        raise ValueError("Archive does not contain any PDF files")

    top_items = [p for p in extract_dir.iterdir() if not p.name.startswith("__MACOSX")]
    if len(top_items) == 1 and top_items[0].is_dir():
        source_dir = top_items[0]
    else:
        source_dir = extract_dir

    doi = _read_doi_from_file(source_dir)
    if not doi:
        pdf_file = pdfs[0]
        doi = _read_doi_from_pdf(pdf_file)

    if doi:
        doi_slug = doi.replace("/", "_")
    else:
        doi_slug = f"task_{task_id}"

    task_input_root = input_task_root(task_id)
    task_output_root = output_task_root(task_id)
    target_dir = task_input_root / doi_slug
    target_dir.mkdir(parents=True, exist_ok=True)

    for item in source_dir.iterdir():
        if item.name.startswith("__MACOSX"):
            continue
        dest = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    shutil.rmtree(extract_dir, ignore_errors=True)

    paper = PaperInfo(
        doi_slug=doi_slug,
        doi=doi,
        input_dir=str(target_dir),
        output_dir=str(task_output_root / doi_slug),
    )
    return [paper]


def extract_batch(archive_path: Path, task_id: str) -> list[PaperInfo]:
    validate_archive(archive_path)
    extract_dir = RUNTIME_TMP_DIR / f"paper_extract_{task_id}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with _open_archive(archive_path) as af:
        af.extractall(extract_dir)

    top_items = [p for p in extract_dir.iterdir() if p.is_dir() and not p.name.startswith("__MACOSX")]
    if len(top_items) == 1 and all(
        d.is_dir() for d in top_items[0].iterdir() if not d.name.startswith("__MACOSX")
    ):
        root = top_items[0]
    else:
        root = extract_dir

    papers = []
    task_input_root = input_task_root(task_id)
    task_output_root = output_task_root(task_id)
    used_names: set[str] = set()

    for subdir in sorted(root.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("__MACOSX"):
            continue
        sub_pdfs = list(subdir.rglob("*.pdf")) + list(subdir.rglob("*.PDF"))
        if not sub_pdfs:
            log.warning("Skipping directory without PDF: %s", subdir.name)
            continue

        doi = _read_doi_from_file(subdir)
        if not doi:
            doi = _read_doi_from_pdf(sub_pdfs[0])

        doi_slug = doi.replace("/", "_") if doi else subdir.name

        target_dir = _unique_dir(task_input_root, doi_slug, used_names)
        doi_slug = target_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)

        for item in subdir.iterdir():
            if item.name.startswith("__MACOSX"):
                continue
            dest = target_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        papers.append(PaperInfo(
            doi_slug=doi_slug,
            doi=doi,
            input_dir=str(target_dir),
            output_dir=str(task_output_root / doi_slug),
            fold_name=subdir.name,
        ))

    shutil.rmtree(extract_dir, ignore_errors=True)

    if not papers:
        raise ValueError("Archive does not contain any paper directories with PDFs")

    return papers
