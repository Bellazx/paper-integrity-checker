#!/usr/bin/env python3
"""Run the 6-paper high-risk re-review for 9月-八刊 - 通讯."""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
TEMPLATE_DIR = Path("/home/library/.claude/skills/paper-batch-review/scripts/yujing_4050_202607_round_1")
ROUND_NAME = "yujing_4050_sep30_comm_highrisk_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
CONFIG_DIR = ROUND_DIR / "config"
INPUT_ROOT = BASE / "data" / "input" / "9月-八刊 - 通讯"
INIT_ROOT = BASE / "data" / "output" / "yujing_4050_202607" / "init"
CHINESE_REPORT_DIR = BASE / "data" / "output" / "yujing_4050_202607" / "chinese_reports"
START_BATCH = 1110000
ROUND_NUM = 413
EXPECTED = 6
APPLY_SCRIPT = BASE / "scripts" / "apply_4050_sep30_comm_highrisk_round.py"
HIGH_DOIS = {
    "10.1016/j.chom.2026.08.009",
    "10.1016/j.isci.2026.117527",
    "10.1016/j.xcrp.2026.103559",
    "10.1016/j.xplc.2026.102090",
    "10.1016/j.xplc.2026.102118",
    "10.1073/pnas.2535342123",
}

DB = dict(server="10.119.5.44", user="yujing", password="fengxian_YJ514",
          database="lunwenyujing", charset="utf8", timeout=30)


def _slug(doi: str) -> str:
    return doi.replace("/", "_")


def _load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def material_index() -> dict[str, tuple[Path, Path]]:
    result: dict[str, tuple[Path, Path]] = {}
    for input_dir in sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir()):
        if input_dir.name.lower() not in {_slug(x).lower() for x in HIGH_DOIS}:
            continue
        report = INIT_ROOT / input_dir.name / "report.json"
        if not report.is_file():
            raise RuntimeError(f"缺少初审报告: {report}")
        data = _load_report(report)
        doi = str((data.get("paper") or {}).get("doi") or "").strip().lower()
        if doi not in HIGH_DOIS:
            raise RuntimeError(f"初审报告 DOI 不在高风险清单: {report} -> {doi}")
        if doi in result:
            raise RuntimeError(f"输入目录 DOI 重复: {doi}")
        result[doi] = (input_dir, report)
    missing = sorted(HIGH_DOIS - set(result))
    if missing:
        raise RuntimeError("输入目录/初审报告缺少 DOI: " + ", ".join(missing))
    return result


def fetch_rows() -> list[tuple[int, str, str | None]]:
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(HIGH_DOIS))
        cur.execute(
            f"SELECT id, doi, report_url FROM yujing_4050_202607 "
            f"WHERE doi IN ({placeholders}) AND risk_level=N'高风险' ORDER BY id",
            tuple(sorted(HIGH_DOIS)),
        )
        rows = [(int(a), str(b or "").strip(), c) for a, b, c in cur.fetchall()]
    finally:
        conn.close()
    got = {doi.lower() for _, doi, _ in rows}
    missing = sorted(HIGH_DOIS - got)
    if missing:
        raise RuntimeError("数据库缺少高风险 DOI: " + ", ".join(missing))
    if len(rows) != EXPECTED:
        raise RuntimeError(f"高风险记录应为 {EXPECTED}，实际为 {len(rows)}")
    return rows


def feed() -> int:
    batch_dir = ROUND_DIR / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    if list(batch_dir.glob("batch_*.json")):
        raise RuntimeError(f"批次目录已有文件，拒绝重复投递: {batch_dir}")
    index = material_index()
    rows = fetch_rows()
    papers = []
    for offset, (paper_id, doi, report_url) in enumerate(rows):
        input_dir, report = index[doi.lower()]
        first_dir = report.parent
        batch_num = START_BATCH + offset
        item = {
            "id": paper_id,
            "doi": doi,
            "slug": _slug(doi),
            "source_path": "input/9月-八刊 - 通讯/",
            "initial_report_url": report_url,
            "input_dir": str(input_dir),
            "report_json": str(report),
            "first_check_dir": str(first_dir),
            "first_check_images": str(first_dir / "images") if (first_dir / "images").exists() else None,
            "first_check_data_tables": str(first_dir / "data_tables") if (first_dir / "data_tables").exists() else None,
            "first_check_chinese_reports_dir": str(CHINESE_REPORT_DIR),
            "evidence_path": str(BASE / "data" / "output" / "review_v2" / f"{_slug(doi)}_evidence.json"),
        }
        (batch_dir / f"batch_{batch_num:04d}.json").write_text(
            json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        papers.append(item)
    (ROUND_DIR / "papers.json").write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"fed {len(papers)} high-risk papers: {', '.join(p['doi'] for p in papers)}", flush=True)
    return 0


def prepare_prompts() -> dict[str, Path]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    replacements = {
        "yujing_4050_202607_round_1": ROUND_NAME,
        "4050_202607_round1": "4050_sep30_comm_highrisk_round1",
        "/home/sjtu/paper-integrity-checker/data/yujing_4050_202607_round_1": str(ROUND_DIR),
        "data/input/20260716": "data/input/9月-八刊 - 通讯",
        "本批次材料位置": "本批次材料位置",
    }
    names = {"r1": "audit_ai_prompt.txt", "r2": "audit_ai_prompt_reviewer2.txt", "judge": "audit_ai_prompt_judge.txt"}
    result = {}
    for stage, filename in names.items():
        content = (TEMPLATE_DIR / filename).read_text(encoding="utf-8")
        for old, new in replacements.items():
            content = content.replace(old, new)
        content = content.replace("本批 80 篇", f"本批 {EXPECTED} 篇")
        target = CONFIG_DIR / filename
        target.write_text(content, encoding="utf-8")
        result[stage] = target
    return result


def load_template(stage: str):
    filenames = {
        "r1": "audit_ai_daemon_4050_202607_round1.py",
        "r2": "audit_ai_daemon_reviewer2_4050_202607_round1.py",
        "judge": "audit_ai_daemon_judge_4050_202607_round1.py",
    }
    path = TEMPLATE_DIR / filenames[stage]
    spec = importlib.util.spec_from_file_location(f"sep30_{stage}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载模板: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(module, stage: str, prompt: Path) -> None:
    batch_root = ROUND_DIR / "batches"
    module.ROUND_DIR = str(batch_root)
    module.PATH_PAPER = str(BASE)
    module.PROMPT_FILE = str(prompt)
    module.ROUND_NUM = ROUND_NUM
    module.STAGE = stage
    module.LOG = str(ROUND_DIR / "monitors" / f"daemon_{stage}.log")
    module.LOG_DIR = str(ROUND_DIR / "monitors" / f"agent_logs_{stage}")
    module.TRACKER = str(ROUND_DIR / "monitors" / f"tracker_{stage}.json")
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
    print(f"launched r1/r2/judge target={target}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("feed", "launch", "r1", "r2", "judge"))
    parser.add_argument("--target", type=int, default=6)
    args = parser.parse_args()
    if args.action == "feed":
        return feed()
    if args.action == "launch":
        return launch(args.target)
    return run_stage(args.action, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
