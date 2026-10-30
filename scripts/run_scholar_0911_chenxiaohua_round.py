#!/usr/bin/env python3
"""Isolated three-stage review round for the 2026-09-11 scholar download."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pymssql


BASE = Path("/home/sjtu/paper-integrity-checker")
SOURCE = BASE / "scripts" / "run_scholar_youjiabin_round.py"
INPUT_ROOT = BASE / "data" / "input" / "scholar" / "0911批量下载-陈小华等625个文件"
INIT_ROOT = BASE / "data" / "output" / "yujing_scholar" / "init"
ROUND_NAME = "yujing_scholar_0911_chenxiaohua_round_1"
ROUND_DIR = BASE / "data" / ROUND_NAME
AUTHORS = ("唐功利", "孟浩巍", "张海波", "陈小华")
EXPECTED = 33

DB = dict(
    server="10.119.5.44", user="yujing", password="fengxian_YJ514",
    database="lunwenyujing", charset="utf8", timeout=30,
)


def load_runner():
    spec = importlib.util.spec_from_file_location("scholar_0911_shared", SOURCE)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载批次编排模块: {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(Path(__file__).resolve())
    module.ROUND_NAME = ROUND_NAME
    module.ROUND_DIR = ROUND_DIR
    module.CONFIG_DIR = ROUND_DIR / "config"
    module.SCHOLAR = "、".join(AUTHORS)
    module.PROMPT_TAG = "scholar_0911_chenxiaohua_round1"
    module.INPUT_ROOT = INPUT_ROOT
    module.START_BATCH = 1301000
    module.ROUND_NUM = 413
    module.EXPECTED = EXPECTED
    module.APPLY_SCRIPT = BASE / "scripts" / "apply_scholar_0911_chenxiaohua_round.py"
    module.DB = DB

    def material_index():
        result = {}
        for author_dir in sorted(p for p in INPUT_ROOT.iterdir() if p.is_dir()):
            for input_dir in sorted(p for p in author_dir.iterdir() if p.is_dir()):
                report = INIT_ROOT / input_dir.name / "report.json"
                if not report.is_file():
                    continue
                doi = module.report_doi(report)
                if doi in result:
                    raise RuntimeError(f"输入目录 DOI 重复: {doi}")
                if doi:
                    result[doi] = (author_dir.name, input_dir, report)
        return result

    def feed():
        batch_dir = ROUND_DIR / "batches"
        batch_dir.mkdir(parents=True, exist_ok=True)
        if list(batch_dir.glob("batch_*.json")):
            raise RuntimeError(f"批次目录已有文件，拒绝重复投递: {batch_dir}")
        index = material_index()
        if len(index) != EXPECTED:
            raise RuntimeError(f"论文材料应为 {EXPECTED} 篇，实际为 {len(index)} 篇")
        conn = pymssql.connect(**DB)
        try:
            cur = conn.cursor()
            rows = []
            for doi, (author, input_dir, report) in index.items():
                cur.execute(
                    "SELECT id, doi, report_url FROM yujing_scholar WHERE doi=%s AND scholar=%s",
                    (doi, author),
                )
                found = cur.fetchall()
                if len(found) != 1:
                    raise RuntimeError(f"数据库匹配异常: scholar={author} doi={doi} rows={len(found)}")
                paper_id, db_doi, report_url = found[0]
                rows.append((int(paper_id), str(db_doi).strip(), report_url, author, input_dir, report))
        finally:
            conn.close()
        rows.sort(key=lambda row: row[0])
        papers = []
        for offset, (paper_id, doi, report_url, author, input_dir, report) in enumerate(rows):
            batch_num = module.START_BATCH + offset
            first_dir = report.parent
            item = {
                "id": paper_id,
                "doi": doi,
                "slug": doi.replace("/", "_"),
                "scholar": author,
                "source_path": str(input_dir.relative_to(BASE / "data")),
                "initial_report_url": report_url,
                "input_dir": str(input_dir.resolve()),
                "report_json": str(report.resolve()),
                "first_check_dir": str(first_dir.resolve()),
                "first_check_images": str(first_dir / "images") if (first_dir / "images").exists() else None,
                "first_check_data_tables": str(first_dir / "data_tables") if (first_dir / "data_tables").exists() else None,
                "first_check_chinese_reports_dir": str(module.CHINESE_REPORT_DIR),
                "evidence_path": str(ROUND_DIR / "evidence" / f"batch_{batch_num:04d}_evidence.json"),
            }
            (batch_dir / f"batch_{batch_num:04d}.json").write_text(
                json.dumps([item], ensure_ascii=False, indent=2), encoding="utf-8"
            )
            papers.append(item)
        (ROUND_DIR / "papers.json").write_text(
            json.dumps(papers, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        module.log(f"db_matched={len(rows)} added={len(papers)} total={len(papers)} missing=0")
        return 0

    def prepare_prompts():
        module.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        names = {
            "r1": "audit_ai_prompt.txt",
            "r2": "audit_ai_prompt_reviewer2.txt",
            "judge": "audit_ai_prompt_judge.txt",
        }
        result = {}
        for stage, filename in names.items():
            content = (module.TEMPLATE_DIR / filename).read_text(encoding="utf-8")
            content = content.replace("yujing_scholar_caideng_huyuhong_tangyehui_round_1", ROUND_NAME)
            content = content.replace("scholar_three_round1", module.PROMPT_TAG)
            content = content.replace("蔡登、胡玉鸿、唐业辉", "唐功利、孟浩巍、张海波、陈小华")
            content = content.replace("caideng`、`huyuhong` 或 `tangyehui", "0911批量下载-陈小华等625个文件/<学者姓名>")
            content = content.replace("本批 27 篇", f"本批 {EXPECTED} 篇")
            content = content.replace("data/input/scholar/caideng", "data/input/scholar/0911批量下载-陈小华等625个文件/唐功利")
            target = module.CONFIG_DIR / filename
            target.write_text(content, encoding="utf-8")
            result[stage] = target
        return result

    module.material_index = material_index
    module.feed = feed
    module.prepare_prompts = prepare_prompts
    return module


if __name__ == "__main__":
    raise SystemExit(load_runner().main())
