"""Side-by-side view of delivered findings and v3 findings for mismatched papers.

    venv/bin/python scripts/review_v3_mismatch_report.py data/review_v3_ab/<run>.log [--out report.md]

For each paper whose v3 verdict differs from the delivered one, prints the
delivered report's confirmed issues (from the published PDF) next to the v3
ledger issues with their severity, so each disagreement can be attributed:
standard difference (v3 found it, graded minor), v3 miss, or v3 false positive.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.review_v3_eval import rows_from_log  # noqa: E402


def delivered_pdf(url: str) -> Path | None:
    name = Path(url or "").name
    if not name:
        return None
    for path in (ROOT / "data" / "output").rglob(name):
        return path
    return None


def delivered_issues(pdf: Path) -> str:
    text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True).stdout
    body = text[text.find("三、复核详情"):text.find("四、综合评估")]
    keep = [line.strip() for line in body.splitlines()
            if line.strip() and not re.search(r"证据截图|对照本项所述", line)]
    return "\n".join(keep)[:3500]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--db-rows", default="/tmp/scholar_1021.json")
    parser.add_argument("--out")
    args = parser.parse_args()
    urls = {row[1]: row[8] for row in json.loads(Path(args.db_rows).read_text())}
    out = []
    for row in rows_from_log(Path(args.log)):
        if not row["error"] and row["got"] == row["expected"]:
            continue
        out.append(f"## {row['doi']}  delivered={row['expected']}  v3={row['got'] if not row['error'] else 'error'}")
        if row["error"]:
            out.append(f"error: {row['error'][:300]}")
        pdf = delivered_pdf(urls.get(row["doi"], ""))
        out.append("### delivered report\n" + (delivered_issues(pdf) if pdf else "(report PDF not found)"))
        finals_path = Path(row.get("workdir") or "", "finals.json")
        if finals_path.is_file():
            finals = json.loads(finals_path.read_text())["finals"]
            lines = []
            for item_id, record in finals.items():
                if record.get("status") == "issue":
                    lines.append(f"- [{record.get('severity')}] {item_id[:40]} | {str(record.get('location', ''))[:120]}"
                                 f" | {str(record.get('finding', ''))[:300]}")
            out.append("### v3 issues\n" + ("\n".join(lines) or "(none)"))
        out.append("")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
