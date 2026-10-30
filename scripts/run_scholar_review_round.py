#!/usr/bin/env python3
"""Feed and run a three-stage scholar review round."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
SKILL_ROOT = Path("/home/library/.claude/skills/paper-batch-review")
TEMPLATE_DIR = SKILL_ROOT / "scripts" / "yujing_scholar_caideng_huyuhong_tangyehui_round_1"
ROUND_NAME = "yujing_scholar_chenzehua_sunke_wuyongji_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
SCRIPT_CONFIG_DIR = ROUND_DIR / "config"
INPUT_ROOT = BASE / "data" / "input" / "scholar"
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
CHINESE_REPORT_DIR = BASE / "data" / "output" / "yujing_scholar" / "chinese_reports"
SCHOLARS = ("陈泽华", "孙科", "吴永基")
START_BATCH = 801000
ROUND_NUM = 408

DB = dict(
    server="10.119.5.44",
    user="yujing",
    password="fengxian_YJ514",
    database="lunwenyujing",
    charset="utf8",
    timeout=30,
)


def log(message: str) -> None:
    path = ROUND_DIR / "monitors" / "feeder.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    print(line, flush=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def report_doi(path: Path) -> str:
    data = load_json(path)
    if not isinstance(data, dict):
        return ""
    return str((data.get("paper") or {}).get("doi") or "").strip().lower()


def build_material_index() -> dict[tuple[str, str], tuple[Path, Path]]:
    """Index material by the DOI recorded inside each completed initial report."""
    report_by_slug = {
        child.name.lower(): child / "report.json"
        for child in INIT_ROOT.iterdir()
        if child.is_dir() and (child / "report.json").is_file()
    }
    index: dict[tuple[str, str], tuple[Path, Path]] = {}
    for scholar in SCHOLARS:
        root = INPUT_ROOT / scholar
        for input_dir in root.iterdir() if root.is_dir() else ():
            if not input_dir.is_dir():
                continue
            report = report_by_slug.get(input_dir.name.lower())
            if not report:
                continue
            doi = report_doi(report)
            if doi:
                index[(scholar, doi)] = (input_dir, report)
    return index


def fetch_rows() -> list[tuple[int, str, str, str | None]]:
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(SCHOLARS))
        cur.execute(
            f"""SELECT id, doi, scholar, report_url
                FROM yujing_scholar
                WHERE scholar IN ({placeholders})
                  AND (status IS NULL OR status = 0)
                ORDER BY id""",
            SCHOLARS,
        )
        return [(int(row[0]), str(row[1] or "").strip(), str(row[2]), row[3]) for row in cur.fetchall()]
    finally:
        conn.close()


def evidence_path(batch_num: int) -> Path:
    return ROUND_DIR / "evidence" / f"batch_{batch_num:04d}_evidence.json"


def feed() -> int:
    batch_dir = ROUND_DIR / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    material_index = build_material_index()
    rows = fetch_rows()
    seen_ids: set[int] = set()
    for path in batch_dir.glob("batch_*.json"):
        data = load_json(path)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            seen_ids.add(int(data[0]["id"]))

    missing: list[str] = []
    new_items: list[dict] = []
    for paper_id, doi, scholar, report_url in rows:
        if paper_id in seen_ids:
            continue
        material = material_index.get((scholar, doi.lower()))
        if not material:
            missing.append(f"id={paper_id} scholar={scholar} doi={doi}")
            continue
        input_dir, report = material
        first_dir = report.parent
        item = {
            "id": paper_id,
            "doi": doi,
            "slug": doi.replace("/", "_"),
            "scholar": scholar,
            "source_path": f"input/scholar/{scholar}/",
            "initial_report_url": report_url,
            "input_dir": str(input_dir),
            "report_json": str(report),
            "first_check_dir": str(first_dir),
            "first_check_images": str(first_dir / "images") if (first_dir / "images").exists() else None,
            "first_check_data_tables": str(first_dir / "data_tables") if (first_dir / "data_tables").exists() else None,
            "first_check_chinese_reports_dir": str(CHINESE_REPORT_DIR),
        }
        new_items.append(item)

    if missing:
        raise RuntimeError("初审材料未能匹配:\n" + "\n".join(missing))

    existing_nums = [int(path.stem.split("_")[1]) for path in batch_dir.glob("batch_*.json")]
    batch_num = max(existing_nums, default=START_BATCH - 1) + 1
    for item in new_items:
        item["evidence_path"] = str(evidence_path(batch_num))
        path = batch_dir / f"batch_{batch_num:04d}.json"
        path.write_text(json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8")
        batch_num += 1

    papers = []
    for path in sorted(batch_dir.glob("batch_*.json")):
        data = load_json(path)
        if isinstance(data, list) and data:
            papers.append(data[0])
    (ROUND_DIR / "papers.json").write_text(
        json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"db_pending={len(rows)} added={len(new_items)} total={len(papers)} missing=0")
    if len(papers) != 30:
        raise RuntimeError(f"批次数量应为 30，实际为 {len(papers)}")
    return 0


def prepare_prompts() -> dict[str, Path]:
    SCRIPT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    old_round = "yujing_scholar_caideng_huyuhong_tangyehui_round_1"
    replacements = {
        old_round: ROUND_NAME,
        "scholar_three_round1": "scholar_chen_sun_wu_round1",
        "蔡登、胡玉鸿、唐业辉": "陈泽华、孙科、吴永基",
        "caideng`、`huyuhong` 或 `tangyehui": "陈泽华`、`孙科` 或 `吴永基",
        "本批 27 篇": "本批 30 篇",
    }
    names = {
        "r1": "audit_ai_prompt.txt",
        "r2": "audit_ai_prompt_reviewer2.txt",
        "judge": "audit_ai_prompt_judge.txt",
    }
    result = {}
    for stage, filename in names.items():
        text = (TEMPLATE_DIR / filename).read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        target = SCRIPT_CONFIG_DIR / filename
        target.write_text(text, encoding="utf-8")
        result[stage] = target
    return result


def load_template_module(stage: str):
    modules = {
        "r1": "audit_ai_daemon_scholar_three_round1.py",
        "r2": "audit_ai_daemon_reviewer2_scholar_three_round1.py",
        "judge": "audit_ai_daemon_judge_scholar_three_round1.py",
    }
    path = TEMPLATE_DIR / modules[stage]
    spec = importlib.util.spec_from_file_location(f"scholar_round_{stage}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_stage(module, stage: str, prompt_path: Path) -> None:
    module.ROUND_NUM = ROUND_NUM
    module.PATH_PAPER = str(BASE)
    module.ROUND_DIR = str(ROUND_DIR / "batches")
    module.PROMPT_FILE = str(prompt_path)
    module.LOG = str(ROUND_DIR / "monitors" / ("daemon.log" if stage == "r1" else f"daemon_{stage}.log"))
    module.TRACKER = str(ROUND_DIR / "monitors" / ("tracker.json" if stage == "r1" else f"tracker_{stage}.json"))
    module.LOG_DIR = str(ROUND_DIR / "monitors" / ("agent_logs" if stage == "r1" else f"agent_logs_{stage}"))
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
        module.APPLY_SCRIPT = str(BASE / "scripts" / "apply_scholar_review_round.py")
        module.APPLY_STAMP = str(ROUND_DIR / "monitors" / "apply_judge.stamp")
        Path(module.RESJ_DIR).mkdir(parents=True, exist_ok=True)


def run_stage(stage: str, target: int) -> int:
    prompts = prepare_prompts()
    module = load_template_module(stage)
    configure_stage(module, stage, prompts[stage])
    module.main(target)
    return 0


def launch(target: int) -> int:
    prepare_prompts()
    log_dir = ROUND_DIR / "monitors"
    log_dir.mkdir(parents=True, exist_ok=True)
    for stage in ("r1", "r2", "judge"):
        command = [str(BASE / "venv" / "bin" / "python"), str(Path(__file__).resolve()), stage, "--target", str(target)]
        handle = (log_dir / f"launcher_{stage}.log").open("a", encoding="utf-8")
        subprocess.Popen(
            command,
            cwd=BASE,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    log(f"launched r1/r2/judge target={target}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("feed", "launch", "r1", "r2", "judge"))
    parser.add_argument("--target", type=int, default=20)
    args = parser.parse_args()
    if args.action == "feed":
        return feed()
    if args.action == "launch":
        return launch(args.target)
    return run_stage(args.action, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
