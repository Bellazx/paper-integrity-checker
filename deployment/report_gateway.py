#!/usr/bin/env python3
"""Read-only HTTP gateway for migrated and newly generated paper reports."""

from __future__ import annotations

import argparse
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


PROJECT_ROOT = Path("/home/sjtu/paper-integrity-checker")
ROOTS = (
    (PROJECT_ROOT / "data/migrated_99_reports/output").resolve(),
    (PROJECT_ROOT / "data/output").resolve(),
)


def _safe_relative(raw: str) -> Path | None:
    """Decode an URL suffix and reject absolute paths and traversal."""
    try:
        decoded = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    candidate = Path(decoded)
    if not decoded or candidate.is_absolute() or "\x00" in decoded:
        return None
    if any(part in {"", ".", ".."} for part in candidate.parts):
        return None
    return candidate


def _relative_candidates(url_path: str) -> list[Path]:
    """Translate the public report URL using the legacy Nginx layout."""
    if url_path.startswith("/chinese_reports/"):
        suffix = _safe_relative(url_path.removeprefix("/chinese_reports/"))
        if suffix is None:
            return []
        parts = suffix.parts
        if parts[0] == "detection_reports":
            return [Path("chinese_reports") / suffix]
        if len(parts) == 1:
            return [Path("chinese_reports") / suffix]
        return [Path(parts[0]) / "chinese_reports" / Path(*parts[1:])]

    if url_path.startswith("/english_reports/"):
        suffix = _safe_relative(url_path.removeprefix("/english_reports/"))
        if suffix is None:
            return []
        parts = suffix.parts
        if parts[0] == "detection_reports":
            return [Path("english_reports") / suffix]
        if len(parts) == 1:
            return [Path("english_reports") / suffix]
        return [Path(parts[0]) / "english_reports" / Path(*parts[1:])]

    if url_path.startswith("/review_reports/"):
        suffix = _safe_relative(url_path.removeprefix("/review_reports/"))
        if suffix is None:
            return []
        parts = suffix.parts
        if parts[0] == "detection_reports" and len(parts) > 1:
            # The first path is what 99 currently serves. The second supports
            # reports written by an older detection worker layout.
            old_layout = (
                Path("detection_reports") / parts[1] / "review_v2" / Path(*parts[2:])
                if len(parts) > 2
                else None
            )
            paths = [Path("review_v2") / suffix]
            if old_layout is not None:
                paths.append(old_layout)
            return paths
        if len(parts) == 1 or parts[0].startswith("review_"):
            return [Path("review_v2") / suffix]
        return [Path(parts[0]) / "review_v2" / Path(*parts[1:])]
    return []


def resolve_report(url_path: str) -> Path | None:
    for relative in _relative_candidates(url_path):
        for root in ROOTS:  # Migrated 99 copy takes precedence over local output.
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
    return None


class ReportHandler(BaseHTTPRequestHandler):
    server_version = "PaperReportGateway/1.0"

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_GET(self) -> None:
        self._serve(send_body=True)

    def _serve(self, *, send_body: bool) -> None:
        path = resolve_report(urlsplit(self.path).path)
        if path is None:
            self.send_error(404, "Report not found")
            return

        stat = path.stat()
        start, end = 0, stat.st_size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match:
                self.send_error(416, "Invalid byte range")
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), end) if last else end
            elif last:
                length = int(last)
                start = max(0, stat.st_size - length)
            if start > end or start >= stat.st_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{stat.st_size}")
                self.end_headers()
                return
            status = 206

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{stat.st_size}")
        self.end_headers()
        if not send_body:
            return
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), ReportHandler).serve_forever()


if __name__ == "__main__":
    main()
