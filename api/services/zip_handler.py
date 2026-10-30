from __future__ import annotations

import logging
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import rarfile

from api.config import (
    MAX_UPLOAD_SIZE_BYTES, MAX_EXTRACTED_SIZE_BYTES, MAX_ARCHIVE_FILES, RUNTIME_TMP_DIR,
    input_task_root, output_task_root,
)
from api.models import PaperInfo
from api.services.runtime_tools import require_rar_tool
from modules.doi_utils import dir_name_to_doi, normalize_doi

log = logging.getLogger(__name__)


def _open_archive(archive_path: Path):
    ext = archive_path.suffix.lower()
    if ext == ".rar":
        require_rar_tool()
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
        entries = af.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ValueError(f'Archive exceeds {MAX_ARCHIVE_FILES} entries')
        total_size = sum(item.file_size for item in entries)
        if total_size > MAX_EXTRACTED_SIZE_BYTES:
            raise ValueError('Archive exceeds the 8 GiB expanded-size limit')
        if total_size > shutil.disk_usage(RUNTIME_TMP_DIR).free // 2:
            raise ValueError('Insufficient free space for extraction and input copy')
        for item in entries:
            name = item.filename
            normalized = name.replace('\\', '/')
            if ('..' in PurePosixPath(normalized).parts or normalized.startswith('/')
                    or PureWindowsPath(normalized).drive):
                raise ValueError(f"Archive contains unsafe path: {name}")
            if isinstance(item, zipfile.ZipInfo):
                mode = stat.S_IFMT(item.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise ValueError(f'Archive contains a link or special file: {name}')
            elif item.is_symlink() or getattr(item, 'file_redir', None):
                raise ValueError(f'Archive contains a link: {name}')


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
                return normalize_doi(m.group(1))
    except Exception:
        pass
    return ""


def _read_doi_from_file(paper_dir: Path) -> str:
    doi_file = paper_dir / "doi.txt"
    if doi_file.exists():
        return normalize_doi(doi_file.read_text(encoding="utf-8"))
    return ""


def _read_doi_from_dir_name(paper_dir: Path) -> str:
    """Infer a DOI from the conventional single-paper folder name.

    Single-paper uploads commonly arrive as ``10.7554_eLife.97621`` (or one
    of the other DOI-slug forms supported by :func:`dir_name_to_doi`).  The
    old extraction path only looked at ``doi.txt`` and PDF metadata, so a
    perfectly valid folder name could silently become a no-DOI task.  Keep
    this fallback conservative: only accept names that contain a DOI prefix.
    """
    if not paper_dir.name.startswith("10."):
        return ""
    try:
        return normalize_doi(dir_name_to_doi(paper_dir))
    except Exception:
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
    doi = normalize_doi(doi)
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
    with tempfile.TemporaryDirectory(prefix=f'paper_extract_{task_id}_', dir=RUNTIME_TMP_DIR) as directory:
        return _extract_single(archive_path, task_id, Path(directory))


def _extract_single(archive_path: Path, task_id: str, extract_dir: Path) -> list[PaperInfo]:

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
    if not doi:
        doi = _read_doi_from_dir_name(source_dir)

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

    paper = PaperInfo(
        doi_slug=doi_slug,
        doi=doi,
        input_dir=str(target_dir),
        output_dir=str(task_output_root / doi_slug),
    )
    return [paper]


def extract_batch(archive_path: Path, task_id: str) -> list[PaperInfo]:
    validate_archive(archive_path)
    with tempfile.TemporaryDirectory(prefix=f'paper_extract_{task_id}_', dir=RUNTIME_TMP_DIR) as directory:
        return _extract_batch(archive_path, task_id, Path(directory))


def _extract_batch(archive_path: Path, task_id: str, extract_dir: Path) -> list[PaperInfo]:

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
        if not doi:
            doi = _read_doi_from_dir_name(subdir)

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

    if not papers:
        raise ValueError("Archive does not contain any paper directories with PDFs")

    return papers
