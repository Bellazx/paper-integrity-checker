"""Lossless, per-review cache of fully read XLSX sheets (never conclusions)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile


def _signature(path):
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def _encode(value):
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {"$cell_type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {"$cell_type": "timedelta", "value": [value.days, value.seconds, value.microseconds]}
    raise TypeError(f"Unsupported workbook cell type: {type(value).__name__}")


def _decode(value):
    kind = value.get("$cell_type")
    if kind == "timedelta":
        return dt.timedelta(*value["value"])
    if kind in {"datetime", "date", "time"}:
        return getattr(dt, kind).fromisoformat(value["value"])
    return value


def read_sheet(path: Path, sheet: str | None, cache_dir: Path | None = None):
    """Return sheet names and a closable row iterator, preserving native types.

    Short previews do not scan the remainder just to fill a cache. A cache is
    published atomically only after EOF and a second source-identity check.
    Concurrent readers may build the same cache; neither sees partial rows.
    """
    signature = _signature(path)
    cache = None
    if cache_dir is not None and sheet is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(json.dumps(["xlsx-data-only-v1", signature, sheet]).encode()).hexdigest()
        cache = cache_dir / f"{key}.jsonl"
        if cache.is_file():
            with cache.open(encoding="utf-8") as handle:
                names = json.loads(handle.readline())["sheets"]

            def cached_rows():
                try:
                    with cache.open(encoding="utf-8") as handle:
                        next(handle)
                        for line in handle:
                            yield tuple(json.loads(line, object_hook=_decode))
                except (ValueError, KeyError):
                    cache.unlink(missing_ok=True)
                    raise  # never mix cached and fresh rows in one comparison
            return names, cached_rows()

    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    names = book.sheetnames
    if sheet is None:
        book.close()
        return names, None
    if sheet not in names:
        book.close()
        raise ValueError(f"无 sheet {sheet!r}；可用：{names}")

    def source_rows():
        temporary = None
        handle = None
        try:
            if cache is not None:
                handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache_dir,
                                                     prefix=".sheet-", delete=False)
                temporary = Path(handle.name)
                handle.write(json.dumps({"sheets": names}) + "\n")
            for row in book[sheet].iter_rows(values_only=True):
                if handle is not None:
                    handle.write(json.dumps(row, ensure_ascii=False, default=_encode) + "\n")
                    if handle.tell() > 128 * 1024 * 1024:
                        handle.close()
                        handle = None
                        temporary.unlink(missing_ok=True)
                yield row
            if handle is not None:
                handle.close()
                handle = None
                if _signature(path) == signature:
                    os.replace(temporary, cache)
        finally:
            book.close()
            if handle is not None:
                handle.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return names, source_rows()
