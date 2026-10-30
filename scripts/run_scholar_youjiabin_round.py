#!/usr/bin/env python3
"""Feed and run the isolated three-stage review round for 尤佳彬."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pymssql


BASE = Path("/home/sjtu/paper-integrity-checker")
SKILL_ROOT = Path("/home/library/.claude/skills/paper-batch-review")
TEMPLATE_DIR = SKILL_ROOT / "scripts" / "yujing_scholar_caideng_huyuhong_tangyehui_round_1"
ROUND_NAME = "yujing_scholar_youjiabin_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
CONFIG_DIR = ROUND_DIR / "config"
SCHOLAR = "尤佳彬"
PROMPT_TAG = "scholar_youjiabin_round1"
INPUT_ROOT = BASE / "data" / "input" / "scholar" / SCHOLAR
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
CHINESE_REPORT_DIR = BASE / "data" / "output" / "yujing_scholar" / "chinese_reports"
START_BATCH = 901000
ROUND_NUM = 409
EXPECTED = 12
APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_youjiabin_round.py"

DB = dict(
    server="10.119.5.44", user="yujing", password="fengxian_YJ514",
    database="lunwenyujing", charset="utf8", timeout=30,
)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def log(message: str) -> None:
    path = ROUND_DIR / "monitors" / "feeder.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    print(line, flush=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def report_doi(path: Path) -> str:
    data = load_json(path)
    if not isinstance(data, dict):
        return ""
    return str((data.get("paper") or {}).get("doi") or "").strip().lower()


def material_index() -> dict[str, tuple[Path, Path]]:
    reports = {
        child.name.lower(): child / "report.json"
        for child in INIT_ROOT.iterdir()
        if child.is_dir() and (child / "report.json").is_file()
    }
    result = {}
    for input_dir in INPUT_ROOT.iterdir() if INPUT_ROOT.is_dir() else ():
        if not input_dir.is_dir():
            continue
        report = reports.get(input_dir.name.lower())
        if report:
            doi = report_doi(report)
            if doi:
                result[doi] = (input_dir, report)
    return result


def fetch_rows() -> list[tuple[int, str, str | None]]:
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, doi, report_url FROM yujing_scholar
               WHERE scholar=%s AND (status IS NULL OR status=0) ORDER BY id""",
            (SCHOLAR,),
        )
        return [(int(r[0]), str(r[1] or "").strip(), r[2]) for r in cur.fetchall()]
    finally:
        conn.close()


def feed() -> int:
    batch_dir = ROUND_DIR / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    index = material_index()
    rows = fetch_rows()
    if len(rows) != EXPECTED:
        raise RuntimeError(f"{SCHOLAR}待复审记录应为 {EXPECTED}，实际为 {len(rows)}")
    if list(batch_dir.glob("batch_*.json")):
        raise RuntimeError(f"批次目录已有文件，拒绝重复投递: {batch_dir}")

    papers = []
    missing = []
    for offset, (paper_id, doi, report_url) in enumerate(rows):
        found = index.get(doi.lower())
        if not found:
            missing.append(f"id={paper_id} doi={doi}")
            continue
        input_dir, report = found
        batch_num = START_BATCH + offset
        first_dir = report.parent
        item = {
            "id": paper_id,
            "doi": doi,
            "slug": doi.replace("/", "_"),
            "scholar": SCHOLAR,
            "source_path": f"input/scholar/{SCHOLAR}/",
            "initial_report_url": report_url,
            "input_dir": str(input_dir),
            "report_json": str(report),
            "first_check_dir": str(first_dir),
            "first_check_images": str(first_dir / "images") if (first_dir / "images").exists() else None,
            "first_check_data_tables": str(first_dir / "data_tables") if (first_dir / "data_tables").exists() else None,
            "first_check_chinese_reports_dir": str(CHINESE_REPORT_DIR),
            "evidence_path": str(ROUND_DIR / "evidence" / f"batch_{batch_num:04d}_evidence.json"),
        }
        (batch_dir / f"batch_{batch_num:04d}.json").write_text(
            json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        papers.append(item)
    if missing:
        raise RuntimeError("初审材料未能匹配:\n" + "\n".join(missing))
    (ROUND_DIR / "papers.json").write_text(
        json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"db_pending={len(rows)} added={len(papers)} total={len(papers)} missing=0")
    return 0


def prepare_prompts() -> dict[str, Path]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    replacements = {
        "yujing_scholar_caideng_huyuhong_tangyehui_round_1": ROUND_NAME,
        "scholar_three_round1": PROMPT_TAG,
        "蔡登、胡玉鸿、唐业辉": SCHOLAR,
        "caideng`、`huyuhong` 或 `tangyehui": SCHOLAR,
        "本批 27 篇": f"本批 {EXPECTED} 篇",
    }
    names = {
        "r1": "audit_ai_prompt.txt",
        "r2": "audit_ai_prompt_reviewer2.txt",
        "judge": "audit_ai_prompt_judge.txt",
    }
    result = {}
    for stage, filename in names.items():
        content = (TEMPLATE_DIR / filename).read_text(encoding="utf-8")
        for old, new in replacements.items():
            content = content.replace(old, new)
        target = CONFIG_DIR / filename
        target.write_text(content, encoding="utf-8")
        result[stage] = target
    return result


def load_template(stage: str):
    filenames = {
        "r1": "audit_ai_daemon_scholar_three_round1.py",
        "r2": "audit_ai_daemon_reviewer2_scholar_three_round1.py",
        "judge": "audit_ai_daemon_judge_scholar_three_round1.py",
    }
    path = TEMPLATE_DIR / filenames[stage]
    spec = importlib.util.spec_from_file_location(f"youjiabin_{stage}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载模板: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(module, stage: str, prompt: Path) -> None:
    module.ROUND_NUM = ROUND_NUM
    module.PATH_PAPER = str(BASE)
    module.ROUND_DIR = str(ROUND_DIR / "batches")
    module.PROMPT_FILE = str(prompt)
    suffix = "" if stage == "r1" else f"_{stage}"
    module.LOG = str(ROUND_DIR / "monitors" / f"daemon{suffix}.log")
    module.TRACKER = str(ROUND_DIR / "monitors" / f"tracker{suffix}.json")
    module.LOG_DIR = str(ROUND_DIR / "monitors" / f"agent_logs{suffix}")
    Path(module.LOG_DIR).mkdir(parents=True, exist_ok=True)
    if stage == "r1":
        module.RES_DIR = str(ROUND_DIR / "results")
        Path(module.RES_DIR).mkdir(parents=True, exist_ok=True)
    elif stage == "r2":
        module.RES1_DIR = str(ROUND_DIR / "results")
        module.RES2_DIR = str(ROUND_DIR / "results_reviewer2")
        Path(module.RES2_DIR).mkdir(parents=True, exist_ok=True)
    else:
        module.RES1_DIR = str(ROUND_DIR / "results")
        module.RES2_DIR = str(ROUND_DIR / "results_reviewer2")
        module.RESJ_DIR = str(ROUND_DIR / "results_judge")
        module.APPLY_SCRIPT = str(APPLY_SCRIPT)
        module.APPLY_STAMP = str(ROUND_DIR / "monitors" / "apply_judge.stamp")
        Path(module.RESJ_DIR).mkdir(parents=True, exist_ok=True)


def run_stage(stage: str, target: int) -> int:
    prompts = prepare_prompts()
    module = load_template(stage)
    configure(module, stage, prompts[stage])
    module.main(target)
    return 0


def launch(target: int) -> int:
    prepare_prompts()
    monitor_dir = ROUND_DIR / "monitors"
    monitor_dir.mkdir(parents=True, exist_ok=True)
    for stage in ("r1", "r2", "judge"):
        command = [str(BASE / "venv/bin/python"), str(Path(__file__).resolve()), stage, "--target", str(target)]
        handle = (monitor_dir / f"launcher_{stage}.log").open("a", encoding="utf-8")
        subprocess.Popen(command, cwd=BASE, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    log(f"launched r1/r2/judge target={target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("feed", "launch", "r1", "r2", "judge"))
    parser.add_argument("--target", type=int, default=12)
    args = parser.parse_args()
    if args.action == "feed":
        return feed()
    if args.action == "launch":
        return launch(args.target)
    return run_stage(args.action, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
