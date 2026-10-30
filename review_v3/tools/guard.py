"""PreToolUse guard for review sessions.

A review must be an independent check of this paper's materials. Sessions may
read the paper's input directory, their own work directory, temporary files
and the shared forensic scripts. Reading prior review reports, other papers'
outputs, the audit memory files or the project's source code is refused:
those would let a session copy an earlier verdict instead of verifying.
Relevant lessons are available through ``pv3 mem`` (filtered by DOI).
"""
from __future__ import annotations

import json
import os
import re
import sys

BASE = os.environ.get("PAPER_INTEGRITY_ROOT", "/home/sjtu/paper-integrity-checker").rstrip("/")
FORBIDDEN = [
    re.compile(r"/\.claude/projects/"),           # session transcripts and audit memory
    re.compile(r"(?<![\w-])memory/"),
    re.compile(r"review_v2|review_reports|chinese_reports|english_reports|review_artifacts"),
    re.compile(r"review_\d|\breview_[^ ]*\.pdf"),
    re.compile(r"\.audit_|\.work_|audit_round|yujing_[a-z_]*round|review_v3_ab/[^ ]*/result\.json"),
    re.compile(re.escape(BASE) + r"/(?:api|modules|core|utils|scripts|review_v3|skills|tests|new-first-checker)(?:/|\b)"),
    re.compile(re.escape(BASE) + r"/data/(?:output|tasks|review_v3_batches)/"),
    re.compile(r"(?:^|[\s'\"=])(?:\.\./)+"),       # walking out of the work directory
]
ALLOWED_PREFIXES = [p for p in (os.environ.get("PV3_WORKDIR"), os.environ.get("PV3_INPUT_DIR"), "/tmp/",
                              *os.environ.get("PV3_EXTRA_DIRS", "").split(os.pathsep)) if p]
SENSITIVE = re.compile(r"review_artifacts|review_v2|review_reports|/memory/|\.claude/projects/|review_[^/]*\.pdf$")
SKILL_TOOL = re.compile(r"\.claude/skills/paper-batch-review/scripts/(?:forensic_checks|scan_columns)\.py")


def _strip_allowed(text: str) -> str:
    for prefix in ALLOWED_PREFIXES:
        text = text.replace(prefix.rstrip("/"), "<allowed>")
    return SKILL_TOOL.sub("<tool>", text)


def check(tool: str, payload: dict) -> str | None:
    if tool == "Read":
        target = str(payload.get("file_path") or "")
        if SENSITIVE.search(target):
            return f"不允许读取历史复核结果或 memory 文件：{target}"
        if any(target.startswith(p.rstrip("/") + "/") or target == p.rstrip("/") for p in ALLOWED_PREFIXES):
            return None
        return f"只能读取本论文材料与资料包目录：{target} 不在允许范围内"
    if tool == "Bash":
        command = _strip_allowed(str(payload.get("command") or ""))
        for pattern in FORBIDDEN:
            if pattern.search(command):
                return ("该命令访问了历史复核结果、其他论文输出、memory 或系统代码，不允许。"
                        "复核必须独立核查本论文材料；既往经验请用 pv3 mem 检索。")
        if re.search(r"(?:grep|rg|find|ls|du)\s+[^|;&]*\s(?:-r\S*\s+)?(?:/home|/opt|/root|/data|/)(?:\s|$)", command):
            return "不要在系统目录中大范围搜索；本论文材料见 pv3 files 与 pv3 info。"
    return None


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    reason = check(event.get("tool_name", ""), event.get("tool_input") or {})
    if reason:
        print(reason, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
