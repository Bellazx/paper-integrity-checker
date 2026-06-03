from __future__ import annotations

import json
import logging
import secrets
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import rarfile

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from api.config import (
    FORBIDDEN_EXTENSIONS, MAX_BATCH_PAPERS,
    MAX_UPLOAD_SIZE_BYTES, MAX_UPLOAD_SIZE_MB, UPLOADS_DIR,
    upload_dir_for_file_id,
)
from api.models import FileInfo, UploadResponse

log = logging.getLogger(__name__)

router = APIRouter()

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_ARCHIVE_EXTS = {".zip", ".rar"}


def _error(status: int, message: str, errors: list[str]) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"success": False, "message": message, "errors": errors},
    )


@router.post("", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    username: str = Form("anonymous"),
):
    # 1. Extension check
    if not file.filename:
        return _error(400, "未提供文件", ["未提供文件名"])
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_ARCHIVE_EXTS:
        return _error(400, "文件格式不支持", [
            f"仅支持 ZIP 和 RAR 格式压缩包，当前文件扩展名为 {ext}",
            f"允许的格式: {', '.join(sorted(ALLOWED_ARCHIVE_EXTS))}",
        ])

    # 2. Generate fileId
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_username = "".join(c for c in username if c.isalnum() or c in "_-")[:20] or "anonymous"
    file_id = f"{timestamp}_{safe_username}_{secrets.token_hex(4)}"

    upload_dir = upload_dir_for_file_id(file_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    archive_path = upload_dir / f"upload{ext}"

    try:
        # 3. Save with size check
        size = 0
        with open(archive_path, "wb") as f:
            while chunk := await file.read(8192):
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE_BYTES:
                    shutil.rmtree(upload_dir, ignore_errors=True)
                    return _error(413, "文件过大", [
                        f"文件大小已超过 {MAX_UPLOAD_SIZE_MB}MB 限制",
                        f"当前已读取 {size / 1024 / 1024:.1f}MB",
                    ])
                f.write(chunk)

        # 4. Validate archive integrity
        if ext == ".zip" and not zipfile.is_zipfile(archive_path):
            shutil.rmtree(upload_dir, ignore_errors=True)
            return _error(400, "压缩包损坏", [
                "ZIP 压缩包已损坏或格式无效",
                "请确认文件未在传输过程中损坏，或重新压缩后上传",
            ])
        if ext == ".rar" and not rarfile.is_rarfile(str(archive_path)):
            shutil.rmtree(upload_dir, ignore_errors=True)
            return _error(400, "压缩包损坏", [
                "RAR 压缩包已损坏或格式无效",
                "请确认文件未在传输过程中损坏，或重新压缩后上传",
            ])

        # 5. Deep validation — collect ALL issues
        result = _validate_archive_contents(archive_path, ext)
        if isinstance(result, list):
            shutil.rmtree(upload_dir, ignore_errors=True)
            return _error(400, f"压缩包内容校验失败，共 {len(result)} 个问题", result)

        file_info = result

        # 6. Write metadata
        meta = {
            "file_id": file_id,
            "filename": file.filename,
            "size_bytes": size,
            "username": safe_username,
            "uploaded_at": datetime.now().isoformat(),
            "archive_type": ext,
            "file_info": file_info.model_dump(),
        }
        (upload_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        return UploadResponse(
            file_id=file_id,
            message=f"上传成功，识别到 {file_info.paper_count} 篇论文",
            file_info=file_info,
        )

    except Exception as e:
        shutil.rmtree(upload_dir, ignore_errors=True)
        log.error("Upload validation failed: %s", e)
        return _error(400, "上传校验失败", [str(e)])


def _validate_archive_contents(archive_path: Path, ext: str) -> FileInfo | list[str]:
    """Return FileInfo on success, or a list of error strings on failure."""
    names = []
    if ext == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
    elif ext == ".rar":
        with rarfile.RarFile(str(archive_path)) as rf:
            names = rf.namelist()

    errors: list[str] = []

    # Path safety
    unsafe_paths = [n for n in names if ".." in n or n.startswith("/")]
    for p in unsafe_paths:
        errors.append(f"压缩包内含不安全路径: {p}")

    # Forbidden file types
    forbidden_found = []
    for name in names:
        if name.endswith("/"):
            continue
        file_ext = Path(name).suffix.lower()
        if file_ext in FORBIDDEN_EXTENSIONS:
            forbidden_found.append(f"{name} ({file_ext})")
    if forbidden_found:
        errors.append(
            f"压缩包内含 {len(forbidden_found)} 个不允许的文件类型: "
            + "; ".join(forbidden_found)
        )
        errors.append(
            "不允许的类型: " + ", ".join(sorted(FORBIDDEN_EXTENSIONS))
        )

    # Must contain PDF
    pdf_files = [n for n in names if n.lower().endswith(".pdf") and "__MACOSX" not in n]
    if not pdf_files:
        errors.append("压缩包内未找到任何 PDF 文件，每篇论文必须包含至少一个 PDF")

    # If critical errors found, return early
    if errors:
        return errors

    # Determine structure (single vs batch)
    top_entries = set()
    for name in names:
        if "__MACOSX" in name:
            continue
        parts = name.split("/")
        if parts[0]:
            top_entries.add(parts[0])

    effective_root = ""
    if len(top_entries) == 1:
        sole_entry = list(top_entries)[0]
        children = [n for n in names if n.startswith(sole_entry + "/") and "__MACOSX" not in n]
        if children:
            effective_root = sole_entry + "/"

    papers_found = []

    if effective_root:
        sub_entries = set()
        for name in names:
            if not name.startswith(effective_root) or "__MACOSX" in name:
                continue
            relative = name[len(effective_root):]
            if not relative:
                continue
            sub_entries.add(relative.split("/")[0])

        sub_dirs_with_pdf = []
        root_pdfs = []
        for entry in sub_entries:
            prefix = effective_root + entry + "/"
            entry_pdfs = [n for n in names if n.startswith(prefix) and n.lower().endswith(".pdf")]
            if entry_pdfs:
                sub_dirs_with_pdf.append(entry)
            if (effective_root + entry).lower().endswith(".pdf"):
                root_pdfs.append(entry)

        if sub_dirs_with_pdf and not root_pdfs:
            mode = "batch"
            for d in sub_dirs_with_pdf:
                papers_found.append({"dir_name": d, "has_pdf": True})
        else:
            mode = "single"
            papers_found.append({"dir_name": effective_root.rstrip("/"), "has_pdf": True})
    else:
        root_pdfs = [n for n in pdf_files if "/" not in n]
        sub_dirs_with_pdf = []
        for entry in top_entries:
            prefix = entry + "/"
            entry_pdfs = [n for n in names if n.startswith(prefix) and n.lower().endswith(".pdf") and "__MACOSX" not in n]
            if entry_pdfs:
                sub_dirs_with_pdf.append(entry)

        if root_pdfs or (len(sub_dirs_with_pdf) == 0 and pdf_files):
            mode = "single"
            papers_found.append({"dir_name": "(root)", "has_pdf": True})
        elif sub_dirs_with_pdf:
            mode = "batch"
            for d in sub_dirs_with_pdf:
                papers_found.append({"dir_name": d, "has_pdf": True})
        else:
            mode = "single"
            papers_found.append({"dir_name": "(root)", "has_pdf": True})

    if mode == "batch" and len(papers_found) > MAX_BATCH_PAPERS:
        return [
            f"批量上传最多 {MAX_BATCH_PAPERS} 篇论文，当前包含 {len(papers_found)} 篇",
            "请拆分后分批上传",
        ]

    size_mb = round(archive_path.stat().st_size / 1024 / 1024, 1)

    return FileInfo(
        filename=archive_path.name,
        size_mb=size_mb,
        paper_count=len(papers_found),
        mode=mode,
        papers=papers_found,
    )
