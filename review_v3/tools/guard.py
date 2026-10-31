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


WRITE_CALLS = re.compile(
    r"savefig|\.save\(|imwrite|imsave|write_text|write_bytes|to_csv|to_excel|np\.save|"
    r"open\([^)]*['\"][wax]b?\+?['\"]")
RELATIVE_WRITE = re.compile(
    r"(?:savefig|\.save|imwrite|imsave|to_csv|to_excel|np\.save|write_text|write_bytes)\(\s*f?['\"](?!/)"
    r"|open\(\s*f?['\"](?!/)[^'\"]*['\"]\s*,\s*['\"][wax]"
    r"|(?:^|[\s;&|])(?:cp|mv)\s[^;&|\n]*\s\.(?:/|\s|$)")
COPY_COMMANDS = {"cp", "mv", "ln", "install", "rsync"}       # the last argument is written
TOUCH_COMMANDS = {"rm", "touch", "mkdir", "tee", "rmdir"}  # every argument is written


def _writes_input(command: str) -> bool:
    """True when a Bash command would create or change files in the paper's
    input directory (redirection into it, a file command targeting it, or a
    write call while working inside it). The input must stay as delivered:
    its contents identify the review run."""
    import shlex
    input_dir = (os.environ.get("PV3_INPUT_DIR") or "").rstrip("/")
    if not input_dir:
        return False
    inside = lambda token: token == input_dir or token.startswith(input_dir + "/")
    if re.search(r">>?\s*['\"]?" + re.escape(input_dir), command):
        return True
    for segment in re.split(r"[;&|\n]+", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        args = [t for t in tokens[1:] if not t.startswith("-")]
        if name in COPY_COMMANDS and args and inside(args[-1]):
            return True
        if name in TOUCH_COMMANDS and any(inside(a) for a in args):
            return True
    for line in command.splitlines():
        if input_dir in line and WRITE_CALLS.search(line.replace(input_dir, "")) and not re.search(
                r"fitz\.open|read_|load|imread|Image\.open", line.split(input_dir)[0][-40:]):
            return True
    # After `cd <input>`, relative writes land in the input directory.
    for chunk in re.split(r"(?:^|[\s;&|])(?=cd\s)", command):
        if re.match(r"cd\s+['\"]?" + re.escape(input_dir), chunk) and RELATIVE_WRITE.search(chunk):
            return True
    return False


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
        if _writes_input(str(payload.get("command") or "")):
            return ("不要在论文原始材料目录中写入或修改文件（原始材料必须保持原样）。"
                    "截图请用 pv3 render/view 生成，脚本与中间文件请写到 /tmp 或资料包目录。")
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
