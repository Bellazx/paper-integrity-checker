"""Bounded reads of TXT/TSV source tables; prose is never treated as a table."""
from __future__ import annotations

import csv
import codecs
from pathlib import Path

import pandas as pd

TEXT_TABLE_SUFFIXES = {".txt", ".tsv"}
MAX_SCREEN_ROWS = 10000
MAX_SCREEN_COLUMNS = 200


def table_format(path: Path) -> tuple[str, str, list[str]]:
    with path.open("rb") as stream:
        sample = stream.read(65536)
    encoding = "utf-16" if sample.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = codecs.getincrementaldecoder(encoding)().decode(sample, final=False)
    except UnicodeDecodeError:
        # A sample may end halfway through a character. The full reader still
        # uses strict decoding and will surface invalid data.
        encoding = "gb18030" if b"\x00" not in sample else encoding
        text = sample.decode(encoding, errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or "\x00" in text:
        raise ValueError("未识别为可读文本表格")
    delimiter = max(("\t", ",", ";"), key=lines[0].count)
    if not lines[0].count(delimiter):
        raise ValueError("未识别到表格分隔符（可能是说明文字）")
    # Read the whole header: proteomics exports can have thousands of columns.
    with path.open(encoding=encoding, newline="") as stream:
        headers = next(csv.reader(stream, delimiter=delimiter))
    if len(headers) < 2 or len(set(headers)) != len(headers):
        raise ValueError("列名缺失或重复，无法可靠映射数据列")
    return encoding, delimiter, headers


def read_screen_table(path: Path) -> tuple[pd.DataFrame, list[str]]:
    encoding, delimiter, headers = table_format(path)
    frame = pd.read_csv(
        path, encoding=encoding, sep=delimiter,
        usecols=list(range(min(len(headers), MAX_SCREEN_COLUMNS))),
        nrows=MAX_SCREEN_ROWS + 1,
    )
    warnings = []
    if len(headers) > MAX_SCREEN_COLUMNS or len(frame) > MAX_SCREEN_ROWS:
        warnings.append(
            f"{path.name}: 通用数列筛查仅覆盖前{MAX_SCREEN_ROWS}行、"
            f"前{MAX_SCREEN_COLUMNS}列（原表{len(headers)}列）；不能视为全表核验"
        )
    return frame.iloc[:MAX_SCREEN_ROWS], warnings


def protein_count(path: Path) -> dict | None:
    """Stream ALL rows of a MaxQuant protein-group table, not the screen sample."""
    encoding, delimiter, headers = table_format(path)
    flags = [x for x in ("Reverse", "Potential contaminant", "Only identified by site") if x in headers]
    if "Protein IDs" not in headers or "Intensity" not in headers or len(flags) != 3:
        return None
    count = total = 0
    for frame in pd.read_csv(path, sep=delimiter, encoding=encoding, dtype=str,
                             usecols=["Protein IDs", "Intensity", *flags], chunksize=4096):
        total += len(frame)
        valid = pd.to_numeric(frame["Intensity"], errors="coerce").gt(0)
        for flag in flags:
            valid &= frame[flag].fillna("").str.strip().ne("+")
        count += int(valid.sum())
    return {"count": count, "total_rows": total, "filter": "Intensity>0；排除Reverse、Potential contaminant、Only identified by site为+的行",
            "source": str(path)}
