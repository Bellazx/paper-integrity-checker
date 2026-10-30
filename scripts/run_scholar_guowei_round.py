#!/usr/bin/env python3
"""Incremental three-stage rereview round for scholar 郭威.

Only rows already present in yujing_scholar and having a matching initial
report are fed. The feeder can be run repeatedly while initial screening is
still in progress; newly completed rows are appended to the same round.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pymssql

BASE = Path("/home/sjtu/paper-integrity-checker")
SKILL = Path("/home/library/.claude/skills/paper-batch-review/scripts/yujing_scholar_caideng_huyuhong_tangyehui_round_1")
ROUND_NAME = "yujing_scholar_guowei_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
CONFIG_DIR = ROUND_DIR / "config"
INPUT_ROOT = BASE / "data" / "input" / "scholar" / "郭威"
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
CHINESE_REPORT_DIR = BASE / "data" / "output" / "yujing_scholar" / "chinese_reports"
START_BATCH = 1001000
ROUND_NUM = 414
APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_guowei_round.py"
EVIDENCE_ROOT = BASE / "data" / "output" / "review_v2"
DB = dict(server="10.119.5.44", user="yujing", password="fengxian_YJ514",
          database="lunwenyujing", charset="utf8", timeout=30)


def log(msg: str) -> None:
    path = ROUND_DIR / "monitors" / "feeder.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    path.open("a", encoding="utf-8").write(line + "\n")


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def slug(doi: str) -> str:
    return doi.replace("/", "_")


def report_doi(path: Path) -> str:
    data = load_json(path)
    if not isinstance(data, dict):
        return ""
    return str((data.get("paper") or {}).get("doi") or "").strip().lower()


def material_index() -> dict[str, tuple[Path, Path]]:
    out = {}
    for d in sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir()):
        report = INIT_ROOT / d.name / "report.json"
        if not report.is_file():
            continue
        doi = report_doi(report)
        if doi:
            if doi in out:
                raise RuntimeError(f"DOI重复: {doi}")
            out[doi] = (d, report)
    return out


def db_rows() -> list[tuple[int, str, str | None]]:
    conn = pymssql.connect(**DB)
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, doi, report_url FROM yujing_scholar
               WHERE scholar=%s AND (status IS NULL OR status=0) ORDER BY id""",
            ("郭威",),
        )
        return [(int(a), str(b or "").strip(), c) for a, b, c in cur.fetchall()]
    finally:
        conn.close()


def build_evidence(input_dir: Path, report: Path, doi: str) -> Path:
    out = EVIDENCE_ROOT / f"{slug(doi)}_evidence.json"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    env = dict(**__import__("os").environ)
    env.update({"PAPER_INTEGRITY_ROOT": str(BASE), "PAPER_INTEGRITY_INPUT": str(BASE / "data/input"),
                "PAPER_INTEGRITY_OUTPUT": str(BASE / "data/output")})
    subprocess.run([
        str(BASE / "venv/bin/python"),
        "/home/library/.claude/skills/paper-batch-review/scripts/review_evidence.py",
        "--input-dir", str(input_dir), "--report-json", str(report), "--out", str(out),
    ], cwd=BASE, env=env, check=True)
    return out


def existing_ids() -> set[int]:
    ids = set()
    for p in (ROUND_DIR / "batches").glob("batch_*.json"):
        data = load_json(p)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            ids.add(int(data[0]["id"]))
    return ids


def feed() -> int:
    batch_dir = ROUND_DIR / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    index = material_index()
    rows = db_rows()
    seen = existing_ids()
    nums = [int(p.stem.split("_")[1]) for p in batch_dir.glob("batch_*.json")]
    next_num = max(nums, default=START_BATCH - 1) + 1
    added = []
    for paper_id, doi, report_url in rows:
        if paper_id in seen:
            continue
        material = index.get(doi.lower())
        if not material:
            log(f"skip pending initial: id={paper_id} doi={doi}")
            continue
        input_dir, report = material
        evidence = build_evidence(input_dir, report, doi)
        first = report.parent
        item = {
            "id": paper_id, "doi": doi, "slug": slug(doi), "scholar": "郭威",
            "source_path": "input/scholar/郭威/", "initial_report_url": report_url,
            "input_dir": str(input_dir), "report_json": str(report),
            "first_check_dir": str(first),
            "first_check_images": str(first / "images") if (first / "images").exists() else None,
            "first_check_data_tables": str(first / "data_tables") if (first / "data_tables").exists() else None,
            "first_check_chinese_reports_dir": str(CHINESE_REPORT_DIR),
            "evidence_path": str(evidence),
        }
        path = batch_dir / f"batch_{next_num:04d}.json"
        path.write_text(json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8")
        added.append(doi)
        next_num += 1
    papers = []
    for p in sorted(batch_dir.glob("batch_*.json")):
        data = load_json(p)
        if isinstance(data, list) and data:
            papers.append(data[0])
    (ROUND_DIR / "papers.json").write_text(json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"db_pending={len(rows)} added={len(added)} total={len(papers)} added_doi={','.join(added) or '-'}")
    return 0


def watch(interval: int) -> int:
    while True:
        try:
            feed()
        except Exception as exc:
            log(f"ERROR: {type(exc).__name__}: {exc}")
        time.sleep(interval)


def prepare_prompts() -> dict[str, Path]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    replacements = {
        "yujing_scholar_caideng_huyuhong_tangyehui_round_1": ROUND_NAME,
        "scholar_three_round1": "scholar_guowei_round1",
        "蔡登、胡玉鸿、唐业辉": "郭威",
        "caideng`、`huyuhong` 或 `tangyehui": "guowei",
        "本批 27 篇": "本批增量论文",
        "data/input/scholar/caideng`、`huyuhong` 或 `tangyehui": "data/input/scholar/郭威",
        "data/input/scholar/caideng、huyuhong 或 tangyehui": "data/input/scholar/郭威",
        "/home/sjtu/paper-integrity-checker/data/yujing_scholar_caideng_huyuhong_tangyehui_round_1": str(ROUND_DIR),
    }
    result = {}
    for stage, name in {"r1": "audit_ai_prompt.txt", "r2": "audit_ai_prompt_reviewer2.txt", "judge": "audit_ai_prompt_judge.txt"}.items():
        text = (SKILL / name).read_text(encoding="utf-8")
        for old, new in replacements.items():
            text = text.replace(old, new)
        target = CONFIG_DIR / name
        target.write_text(text, encoding="utf-8")
        result[stage] = target
    return result


def load_template(stage: str):
    names = {"r1": "audit_ai_daemon_scholar_three_round1.py", "r2": "audit_ai_daemon_reviewer2_scholar_three_round1.py", "judge": "audit_ai_daemon_judge_scholar_three_round1.py"}
    path = SKILL / names[stage]
    spec = importlib.util.spec_from_file_location(f"guowei_{stage}", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(module, stage: str, prompt: Path) -> None:
    module.ROUND_NUM = ROUND_NUM
    module.PATH_PAPER = str(BASE)
    module.ROUND_DIR = str(ROUND_DIR / "batches")
    module.PROMPT_FILE = str(prompt)
    module.LOG = str(ROUND_DIR / "monitors" / f"daemon_{stage}.log")
    module.TRACKER = str(ROUND_DIR / "monitors" / f"tracker_{stage}.json")
    module.LOG_DIR = str(ROUND_DIR / "monitors" / f"agent_logs_{stage}")
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
    md = ROUND_DIR / "monitors"
    md.mkdir(parents=True, exist_ok=True)
    for stage in ("r1", "r2", "judge"):
        cmd = [str(BASE / "venv/bin/python"), str(Path(__file__).resolve()), stage, "--target", str(target)]
        handle = (md / f"launcher_{stage}.log").open("a", encoding="utf-8")
        subprocess.Popen(cmd, cwd=BASE, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    log(f"launched stages target={target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("feed", "watch", "launch", "r1", "r2", "judge"))
    ap.add_argument("--target", type=int, default=8)
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    if args.action == "feed": return feed()
    if args.action == "watch": return watch(args.interval)
    if args.action == "launch": return launch(args.target)
    return run_stage(args.action, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
