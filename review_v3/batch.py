"""Generic batch review with engine v3 (replaces per-round r1/r2/judge daemons).

    # review and publish (PDF + DB status=2 with read-back) every pending high-risk paper
    venv/bin/python -m review_v3.batch run --round quanliang_v3 --table yujing_quanliang \
        --report-root data/output/yujing_quanliang --apply

    # scholar table, scoped by scholar; keep polling for new rows every 10 min
    venv/bin/python -m review_v3.batch run --round scholar_xx --table yujing_scholar --scholar 某某 \
        --input-root data/input/scholar/某某 --report-root data/output/yujing_scholar/某某 --apply --watch 600

    # explicit manifest [{doi, input_dir, report_json}], dry run (render PDFs, no DB)
    venv/bin/python -m review_v3.batch run --round test --manifest papers.json

    venv/bin/python -m review_v3.batch status --round quanliang_v3

The round directory keeps per-paper state, so rerunning resumes where it
stopped; the engine's own checkpoints resume interrupted papers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from review_v3 import config, engine  # noqa: E402

log = logging.getLogger("review_v3.batch")
TABLE_RE = re.compile(r"^yujing_[a-z0-9_]{1,50}$")
DEFAULT_WHERE = "risk_level=N'高风险' AND (status IS NULL OR status=0)"
ROUNDS_DIR = config.BASE_DIR / "data" / "review_v3_batches"


def _norm(doi: str) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(doi or "").strip(), flags=re.I).lower()


class RoundState:
    def __init__(self, name: str):
        self.dir = ROUNDS_DIR / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"papers": {}}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def paper(self, doi: str) -> dict:
        return self.data["papers"].setdefault(_norm(doi), {"doi": doi, "status": "pending", "attempts": 0})


# ------------------------------------------------------------------ discovery

def material_index(report_root: Path, input_root: Path | None) -> dict[str, dict]:
    """DOI -> {input_dir, report_json} from report.json files under report_root."""
    index = {}
    for report in report_root.rglob("report.json"):
        if len(report.relative_to(report_root).parts) > 4:
            continue
        try:
            paper = (json.loads(report.read_text(encoding="utf-8")).get("paper") or {})
        except (OSError, ValueError):
            continue
        doi = _norm(paper.get("doi") or "")
        if not doi:
            continue
        input_dir = None
        if input_root is not None and (input_root / report.parent.name).is_dir():
            input_dir = input_root / report.parent.name
        elif paper.get("filepath") and Path(paper["filepath"]).is_file():
            input_dir = Path(paper["filepath"]).parent
        if input_dir is None:
            continue
        current = index.get(doi)
        if current is None or report.stat().st_mtime > Path(current["report_json"]).stat().st_mtime:
            index[doi] = {"input_dir": str(input_dir), "report_json": str(report)}
    return index


def fetch_rows(table: str, where: str, scholar: str) -> list[dict]:
    import pymssql
    from api.config import DB_CONFIG
    if not TABLE_RE.match(table):
        raise ValueError(f"非法表名 {table}")
    sql = f"SELECT id, doi FROM {table} WHERE ({where})"
    params: tuple = ()
    if scholar:
        sql += " AND scholar=%s"
        params = (scholar,)
    conn = pymssql.connect(**DB_CONFIG)
    try:
        cursor = conn.cursor()
        cursor.execute(sql + " ORDER BY id", params)
        return [{"id": int(r[0]), "doi": str(r[1] or "").strip()} for r in cursor.fetchall() if r[1]]
    finally:
        conn.close()


# ------------------------------------------------------------------ publish

async def publish(result: dict, round_state: RoundState, args) -> str:
    """Render the review PDF; with --apply also write and verify the DB row."""
    from api.config import REVIEW_DIR, REPORT_TIMEOUT_SECONDS, PYTHON, REVIEW_REPORT_SCRIPT
    from api.services.report import _kill_process_group, _review_report_slug
    namespace = re.sub(r"[^A-Za-z0-9_\-]", "_", args.namespace or args.table or args.round)
    output = REVIEW_DIR / namespace
    output.mkdir(parents=True, exist_ok=True)
    slug = _review_report_slug(result)
    results_path = round_state.dir / "publish" / f"{slug}.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps([result], ensure_ascii=False, indent=1), encoding="utf-8")
    command = [str(PYTHON), str(REVIEW_REPORT_SCRIPT), "--results", str(results_path), "--output", str(output),
               "--table", args.table or "yujing_quanliang", "--namespace", namespace]
    if args.scholar:
        command += ["--scholar", args.scholar]
    if not args.apply:
        command.append("--no-db")
    proc = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(config.BASE_DIR),
        env={**os.environ, "PAPER_INTEGRITY_ROOT": str(config.BASE_DIR), "REVIEW_COVERAGE_GAP_POLICY": "strict"},
        start_new_session=True)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), REPORT_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.CancelledError):
        await _kill_process_group(proc)
        raise
    pdf = output / f"review_{slug}.pdf"
    if proc.returncode != 0 or not pdf.is_file():
        raise RuntimeError(f"报告生成失败 rc={proc.returncode}: {stderr.decode(errors='replace')[-600:]}")
    return str(pdf)


# ------------------------------------------------------------------ run

async def process(paper: dict, material: dict, round_state: RoundState, args, gate: asyncio.Semaphore) -> None:
    async with gate:
        record = round_state.paper(paper["doi"])
        record.update(status="reviewing", attempts=record["attempts"] + 1, started_at=time.time(), id=paper.get("id"))
        round_state.save()
        work = round_state.dir / "work" / re.sub(r"[^A-Za-z0-9._-]", "_", paper["doi"])[:80]
        try:
            result = await engine.run_review(paper["doi"], material["report_json"], material["input_dir"],
                                             str(work), resume=True)
        except Exception as exc:
            log.exception("DOI=%s review crashed", paper["doi"])
            result = {"trigger": "review_error", "_review_error": f"{type(exc).__name__}: {exc}"}
        record["finished_at"] = time.time()
        record["wall_seconds"] = round(record["finished_at"] - record["started_at"], 1)
        if result.get("trigger") == "review_error":
            record.update(status="failed", error=str(result.get("_review_error"))[:500])
            round_state.save()
            return
        record.update(status="reviewed", result=result.get("result"), workdir=result.get("_review_workdir"))
        (round_state.dir / "results").mkdir(exist_ok=True)
        (round_state.dir / "results" / f"{work.name}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        round_state.save()
        if args.no_publish:
            return
        try:
            record["pdf"] = await publish(result, round_state, args)
            record["status"] = "published" if args.apply else "rendered"
        except Exception as exc:
            record.update(status="publish_failed", error=str(exc)[:500])
        round_state.save()


def _pending(round_state: RoundState, papers: list[dict], max_attempts: int) -> list[dict]:
    out = []
    for paper in papers:
        record = round_state.data["papers"].get(_norm(paper["doi"]))
        if record is None or record["status"] in ("pending", "reviewing") or (
                record["status"] in ("failed", "publish_failed") and record["attempts"] < max_attempts):
            out.append(paper)
    return out


async def run(args) -> None:
    round_state = RoundState(args.round)
    gate = asyncio.Semaphore(args.concurrency)
    while True:
        if args.manifest:
            entries = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
            papers = [{"doi": e["doi"]} for e in entries]
            materials = {_norm(e["doi"]): {"input_dir": e["input_dir"], "report_json": e["report_json"]} for e in entries}
        else:
            papers = await asyncio.to_thread(fetch_rows, args.table, args.where, args.scholar)
            materials = await asyncio.to_thread(material_index, Path(args.report_root),
                                                Path(args.input_root) if args.input_root else None)
        todo = []
        for paper in _pending(round_state, papers, args.max_attempts):
            material = materials.get(_norm(paper["doi"]))
            if material is None:
                record = round_state.paper(paper["doi"])
                record.update(status="missing_material")
                continue
            todo.append((paper, material))
        round_state.save()
        log.info("round=%s rows=%d to_review=%d", args.round, len(papers), len(todo))
        if todo:
            await asyncio.gather(*(process(p, m, round_state, args, gate) for p, m in todo))
        if not args.watch:
            break
        await asyncio.sleep(args.watch)
    print(json.dumps(summary(round_state), ensure_ascii=False, indent=1))


def summary(round_state: RoundState) -> dict:
    counts: dict[str, int] = {}
    results: dict[str, int] = {}
    walls = []
    for record in round_state.data["papers"].values():
        counts[record["status"]] = counts.get(record["status"], 0) + 1
        if record.get("result"):
            results[record["result"]] = results.get(record["result"], 0) + 1
        if record.get("wall_seconds"):
            walls.append(record["wall_seconds"])
    walls.sort()
    return {"round": round_state.dir.name, "status": counts, "results": results,
            "median_wall_seconds": walls[len(walls) // 2] if walls else None}


async def _with_signals(coro):
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    except asyncio.CancelledError:
        await asyncio.sleep(1)  # let killed sessions' pipes close inside the loop
        log.warning("cancelled; model sessions terminated, progress saved")


def main() -> None:
    parser = argparse.ArgumentParser(prog="review_v3.batch")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--round", required=True, help="轮次名称，决定状态目录 data/review_v3_batches/<round>")
    p.add_argument("--table", help="yujing_* 表名")
    p.add_argument("--where", default=DEFAULT_WHERE)
    p.add_argument("--scholar", default="")
    p.add_argument("--report-root", help="初审 report.json 所在根目录")
    p.add_argument("--input-root", help="论文材料根目录（与 report 目录同名的子目录）")
    p.add_argument("--manifest", help="JSON 清单 [{doi,input_dir,report_json}]，替代数据库查询")
    p.add_argument("--namespace", help="报告目录命名空间，默认同表名")
    p.add_argument("--concurrency", type=int, default=6, help="同时复核的论文数")
    p.add_argument("--max-attempts", type=int, default=2)
    p.add_argument("--apply", action="store_true", help="写数据库（status=2）；默认只生成 PDF")
    p.add_argument("--no-publish", action="store_true", help="只复核，不生成 PDF")
    p.add_argument("--watch", type=int, default=0, help="每隔 N 秒重新查询新论文；0 表示处理完即退出")
    s = sub.add_parser("status")
    s.add_argument("--round", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "status":
        print(json.dumps(summary(RoundState(args.round)), ensure_ascii=False, indent=1))
        return
    if not args.manifest and not (args.table and args.report_root):
        parser.error("需要 --manifest，或 --table 与 --report-root")
    if args.apply and not args.table:
        parser.error("--apply 需要 --table")
    asyncio.run(_with_signals(run(args)))


if __name__ == "__main__":
    main()
